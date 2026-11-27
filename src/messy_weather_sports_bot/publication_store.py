"""Persist local-time publishing schedules and date-bound publication requests."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection

from messy_weather_sports_bot.collection import (
    forecast_db_path,
    forecast_db_transaction,
    read_database,
)
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.sports import SPORTS

DEFAULT_RUN_TIME = "10:00"  # 9 AM Central for the current Eastern game-day sports.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
EVERY_DAY = (1 << len(WEEKDAYS)) - 1
REQUEST_TTL_MINUTES = 15
Outcome = Literal["complete", "partial", "failed", "skipped"]


class ScheduleConflict(ValueError):
    """The schedule changed since the form was loaded."""


@dataclass(frozen=True)
class Execution:
    id: int
    game_day: dt.date
    previous_outcome: Literal["complete", "partial", "failed", "interrupted"] | None = None


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _slot_on(sport: Sport, day: dt.date, run_time: str) -> dt.datetime:
    # fold=0 chooses the first occurrence in the fall and shifts a missing spring time
    # forward by the DST gap. Compare UTC instants to keep the repeated hour unambiguous.
    return dt.datetime.combine(
        day, dt.time.fromisoformat(run_time), sport.game_day_timezone
    ).astimezone(dt.UTC)


def _next_slot(sport: Sport, now: dt.datetime, run_time: str, weekdays: int) -> dt.datetime:
    day = sport.game_day_for(now)
    for offset in range(8):
        candidate = day + dt.timedelta(days=offset)
        if weekdays & (1 << candidate.weekday()):
            slot = _slot_on(sport, candidate, run_time)
            if slot > now:
                return slot
    raise ValueError("Select at least one weekday.")


def initialize(db: Connection) -> None:
    db.exec_driver_sql(
        """
        INSERT INTO publication_schedules (sport, run_time, weekdays, enabled)
        VALUES (?, ?, ?, 0) ON CONFLICT (sport) DO NOTHING
        """,
        [(sport, DEFAULT_RUN_TIME, EVERY_DAY) for sport in SPORTS],
    )


def configure(
    sport: Sport,
    now: dt.datetime,
    *,
    run_time: str,
    weekdays: Sequence[int],
    enabled: bool,
    expected_revision: int,
) -> None:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", run_time):
        raise ValueError("Enter a time between 00:00 and 23:59.")
    if not weekdays or any(day not in range(len(WEEKDAYS)) for day in weekdays):
        raise ValueError("Select at least one valid weekday.")
    mask = sum(1 << day for day in set(weekdays))
    with forecast_db_transaction() as db:
        current = db.exec_driver_sql(
            "SELECT run_time, weekdays, enabled, revision "
            "FROM publication_schedules WHERE sport = ?",
            (sport.slug,),
        ).one()
        if expected_revision != current.revision:
            raise ScheduleConflict(
                "This schedule changed while you were editing. Review it and try again."
            )
        if (run_time, mask, enabled) == (current.run_time, current.weekdays, current.enabled):
            return
        next_due = _stamp(_next_slot(sport, now, run_time, mask)) if enabled else None
        db.exec_driver_sql(
            """
            UPDATE publication_schedules
            SET run_time = ?, weekdays = ?, enabled = ?, next_due = ?, revision = revision + 1
            WHERE sport = ?
            """,
            (run_time, mask, enabled, next_due, sport.slug),
        )


def _expire_requests(db: Connection, sport: Sport, now: dt.datetime, game_day: str) -> None:
    db.exec_driver_sql(
        """
        UPDATE publication_executions SET outcome = 'expired', finished_at = ?
        WHERE sport = ? AND outcome = 'queued' AND (game_day != ? OR requested_at <= ?)
        """,
        (
            _stamp(now),
            sport.slug,
            game_day,
            _stamp(now - dt.timedelta(minutes=REQUEST_TTL_MINUTES)),
        ),
    )


def request_publish(sport: Sport, now: dt.datetime, game_day: dt.date, request_key: str) -> int:
    if game_day != sport.game_day_for(now) or not re.fullmatch(r"[a-f0-9]{32}", request_key):
        raise ValueError("Reload the page to confirm publishing for the current game day.")
    day = game_day.isoformat()
    with forecast_db_transaction() as db:
        _expire_requests(db, sport, now, day)
        confirmed = db.exec_driver_sql(
            "SELECT execution_id FROM publication_confirmations "
            "WHERE sport = ? AND request_key = ?",
            (sport.slug, request_key),
        ).scalar_one_or_none()
        if confirmed is not None:
            return confirmed
        execution_id = db.exec_driver_sql(
            """
            SELECT id FROM publication_executions WHERE sport = ? AND game_day = ?
              AND outcome IN ('queued', 'running', 'complete')
            ORDER BY id DESC LIMIT 1
            """,
            (sport.slug, day),
        ).scalar_one_or_none()
        if execution_id is None:
            execution_id = db.exec_driver_sql(
                """
                INSERT INTO publication_executions
                    (sport, game_day, trigger, requested_at, outcome)
                VALUES (?, ?, 'manual', ?, 'queued') RETURNING id
                """,
                (sport.slug, day, _stamp(now)),
            ).scalar_one()
        db.exec_driver_sql(
            """
            INSERT INTO publication_confirmations (sport, request_key, execution_id)
            VALUES (?, ?, ?)
            """,
            (sport.slug, request_key, execution_id),
        )
        return execution_id


def ready_sports(now: dt.datetime) -> list[str]:
    with read_database(forecast_db_path()) as db:
        return [
            row["sport"]
            for row in db.execute(
                """
                SELECT s.sport FROM publication_schedules AS s
                WHERE s.next_due <= ? OR EXISTS (
                    SELECT 1 FROM publication_executions
                    WHERE sport = s.sport AND outcome = 'queued'
                )
                ORDER BY s.next_due IS NULL, s.next_due, s.sport
                """,
                (_stamp(now),),
            )
        ]


def _claim_slot(db: Connection, sport: Sport, now: dt.datetime, today: dt.date) -> str | None:
    schedule = db.exec_driver_sql(
        """
        SELECT run_time, weekdays FROM publication_schedules
        WHERE sport = ? AND next_due <= ?
        """,
        (sport.slug, _stamp(now)),
    ).one_or_none()
    if schedule is None:
        return None
    next_due = _next_slot(sport, now, schedule.run_time, schedule.weekdays)
    db.exec_driver_sql(
        "UPDATE publication_schedules SET next_due = ? WHERE sport = ?",
        (_stamp(next_due), sport.slug),
    )
    if not schedule.weekdays & (1 << today.weekday()):
        return None
    slot = _slot_on(sport, today, schedule.run_time)
    if slot > now:
        return None
    already_claimed = db.exec_driver_sql(
        """
        SELECT 1 FROM publication_executions WHERE sport = ? AND game_day = ?
          AND scheduled_for IS NOT NULL LIMIT 1
        """,
        (sport.slug, today.isoformat()),
    ).scalar_one_or_none()
    return None if already_claimed else _stamp(slot)


def claim(sport: Sport, now: dt.datetime) -> Execution | None:
    """Claim one run under the scheduler lock. Publishing also needs the sport lock."""
    today = sport.game_day_for(now)
    day = today.isoformat()
    started = _stamp(now)
    with forecast_db_transaction() as db:
        _expire_requests(db, sport, now, day)
        slot = _claim_slot(db, sport, now, today)
        queued = db.exec_driver_sql(
            """
            UPDATE publication_executions
            SET started_at = ?, scheduled_for = ?, outcome = 'running'
            WHERE sport = ? AND outcome = 'queued' RETURNING id
            """,
            (started, slot, sport.slug),
        ).scalar_one_or_none()
        if queued is not None:
            return Execution(queued, today)
        if slot is None:
            return None
        previous_outcome = db.exec_driver_sql(
            """
            SELECT outcome FROM publication_executions WHERE sport = ? AND game_day = ?
              AND outcome IN ('complete', 'partial', 'failed', 'interrupted')
            ORDER BY id DESC LIMIT 1
            """,
            (sport.slug, day),
        ).scalar_one_or_none()
        execution_id = db.exec_driver_sql(
            """
            INSERT INTO publication_executions
                (sport, game_day, trigger, scheduled_for, started_at, outcome)
            VALUES (?, ?, 'scheduled', ?, ?, 'running') RETURNING id
            """,
            (sport.slug, day, slot, started),
        ).scalar_one()
        return Execution(execution_id, today, previous_outcome)


def finish(
    execution: Execution, now: dt.datetime, outcome: Outcome, summary: str | None = None
) -> None:
    with forecast_db_transaction() as db:
        db.exec_driver_sql(
            """
            UPDATE publication_executions SET finished_at = ?, outcome = ?, failure_summary = ?
            WHERE id = ?
            """,
            (_stamp(now), outcome, summary, execution.id),
        )


def recover(db: Connection, now: dt.datetime) -> None:
    db.exec_driver_sql(
        """
        UPDATE publication_executions
        SET outcome = 'interrupted', finished_at = ?, failure_summary = 'process exited'
        WHERE outcome = 'running'
        """,
        (_stamp(now),),
    )
