"""Persistent collection schedules, refresh requests, and execution history."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, RowMapping

from messy_weather_sports_bot import publication_store
from messy_weather_sports_bot.collection import (
    forecast_db_transaction,
    insert_collection,
    migrate_forecast_db,
)
from messy_weather_sports_bot.collector import CollectedDay
from messy_weather_sports_bot.sports import SPORTS

DEFAULT_INTERVAL_SECONDS = 6 * 3600
MAX_INTERVAL_SECONDS = 365 * 24 * 3600
ExecutionOutcome = Literal["complete", "partial", "failed"]


class ScheduleConflict(ValueError):
    """The schedule changed since the caller last read it."""


@dataclass(frozen=True)
class Execution:
    id: int
    sport: str
    slot: dt.datetime | None
    interval_seconds: int = 0
    schedule_revision: int = 0


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _next_slot(slot: dt.datetime, seconds: int, now: dt.datetime) -> dt.datetime:
    interval = dt.timedelta(seconds=seconds)
    return slot + interval * ((now - slot) // interval + 1)


def initialize(now: dt.datetime) -> None:
    with forecast_db_transaction() as db:
        migrate_forecast_db(db)
        publication_store.initialize(db)
        db.exec_driver_sql(
            """
            INSERT INTO collection_schedules (sport, interval_seconds, enabled, next_due)
            VALUES (?, ?, 1, ?) ON CONFLICT (sport) DO NOTHING
            """,
            [(sport, DEFAULT_INTERVAL_SECONDS, _stamp(now)) for sport in SPORTS],
        )


def configure(
    sport: str,
    now: dt.datetime,
    *,
    interval_seconds: int | None = None,
    enabled: bool | None = None,
    expected_revision: int | None = None,
) -> None:
    if interval_seconds is not None and not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError("interval must be between one second and 365 days")
    with forecast_db_transaction() as db:
        current = db.exec_driver_sql(
            """
            SELECT interval_seconds, enabled, next_due, revision
            FROM collection_schedules WHERE sport = ?
            """,
            (sport,),
        ).one()
        if expected_revision is not None and expected_revision != current.revision:
            raise ScheduleConflict("Schedule changed. Reload it before saving.")
        seconds = current.interval_seconds if interval_seconds is None else interval_seconds
        is_enabled = current.enabled if enabled is None else int(enabled)
        if (seconds, is_enabled) == (current.interval_seconds, current.enabled):
            return
        next_due = current.next_due
        if enabled and not current.enabled:
            next_due = _stamp(now)
        elif seconds != current.interval_seconds:
            next_due = _stamp(now + dt.timedelta(seconds=seconds))
        db.exec_driver_sql(
            """
            UPDATE collection_schedules
            SET interval_seconds = ?, enabled = ?, next_due = ?, revision = revision + 1
            WHERE sport = ?
            """,
            (seconds, is_enabled, next_due, sport),
        )


def request_refresh(sport: str, now: dt.datetime) -> int:
    """Queue a refresh or return the existing pending or running request ID."""
    if sport not in SPORTS:
        raise ValueError("unsupported sport")
    with forecast_db_transaction() as db:
        latest = db.exec_driver_sql(
            """
            SELECT q.id, q.execution_id, e.outcome
            FROM collection_refresh_requests AS q
            LEFT JOIN collection_executions AS e ON e.id = q.execution_id
            WHERE q.sport = ? ORDER BY q.id DESC LIMIT 1
            """,
            (sport,),
        ).one_or_none()
        if latest is not None and (latest.execution_id is None or latest.outcome == "running"):
            return latest.id
        return db.exec_driver_sql(
            """
            INSERT INTO collection_refresh_requests (sport, requested_at)
            VALUES (?, ?) RETURNING id
            """,
            (sport, _stamp(now)),
        ).scalar_one()


def recover(now: dt.datetime) -> None:
    """Mark abandoned executions after taking the exclusive collector lock."""
    with forecast_db_transaction() as db:
        publication_store.recover(db, now)
        db.exec_driver_sql(
            """
            UPDATE collection_executions
            SET outcome = 'interrupted', finished_at = ?, failure_summary = 'process exited'
            WHERE outcome = 'running'
            """,
            (_stamp(now),),
        )


def _insert_execution(
    db: Connection, sport: str, now: dt.datetime, slot: dt.datetime | None
) -> int:
    return db.exec_driver_sql(
        """
        INSERT INTO collection_executions (sport, trigger, scheduled_for, started_at, outcome)
        VALUES (?, ?, ?, ?, 'running') RETURNING id
        """,
        (
            sport,
            "manual" if slot is None else "scheduled",
            _stamp(slot) if slot else None,
            _stamp(now),
        ),
    ).scalar_one()


def claim_manual(sport: str, now: dt.datetime) -> Execution:
    with forecast_db_transaction() as db:
        return Execution(_insert_execution(db, sport, now, None), sport, None)


def claim_requested(now: dt.datetime) -> Execution | None:
    """Start the oldest pending request while the caller holds the collector lock."""
    with forecast_db_transaction() as db:
        request = db.exec_driver_sql(
            """
            SELECT id, sport FROM collection_refresh_requests WHERE execution_id IS NULL
            ORDER BY requested_at, id LIMIT 1
            """
        ).one_or_none()
        if request is None:
            return None
        execution_id = _insert_execution(db, request.sport, now, None)
        db.exec_driver_sql(
            "UPDATE collection_refresh_requests SET execution_id = ? WHERE id = ?",
            (execution_id, request.id),
        )
        return Execution(execution_id, request.sport, None)


def claim_due(now: dt.datetime, *, defer_to_requests: bool = False) -> Execution | None:
    """Start the next due schedule, optionally yielding to older queued refreshes."""
    with forecast_db_transaction() as db:
        row = db.exec_driver_sql(
            """
            SELECT s.sport, s.next_due, s.interval_seconds, s.revision
            FROM collection_schedules AS s
            WHERE s.enabled = 1 AND s.next_due <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM collection_refresh_requests
                  WHERE ? AND execution_id IS NULL AND requested_at <= s.next_due
              )
            ORDER BY s.next_due, s.sport LIMIT 1
            """,
            (_stamp(now), defer_to_requests),
        ).one_or_none()
        if row is None:
            return None
        sport, next_due, seconds, revision = row
        slot = dt.datetime.fromisoformat(next_due)
        execution = Execution(
            _insert_execution(db, sport, now, slot), sport, slot, seconds, revision
        )
        db.exec_driver_sql(
            "UPDATE collection_schedules SET next_due = ? WHERE sport = ?",
            (_stamp(_next_slot(slot, seconds, now)), sport),
        )
        return execution


def save_day(execution: Execution, result: CollectedDay) -> None:
    with forecast_db_transaction() as db:
        insert_collection(
            db,
            execution.sport,
            result.game_day,
            result.started_at,
            result.outcome,
            result.games_found,
            result.observations,
            rankings_unavailable=result.rankings_unavailable,
            error_type=result.error_type,
            execution_id=execution.id,
            publication_game_count=result.publication_game_count,
        )


def finish(
    execution: Execution,
    now: dt.datetime,
    outcome: ExecutionOutcome,
    failure_summary: str | None = None,
) -> None:
    with forecast_db_transaction() as db:
        db.exec_driver_sql(
            """
            UPDATE collection_executions SET finished_at = ?, outcome = ?, failure_summary = ?
            WHERE id = ?
            """,
            (_stamp(now), outcome, failure_summary, execution.id),
        )
        if execution.slot is not None:
            db.exec_driver_sql(
                """
                UPDATE collection_schedules SET next_due = MAX(next_due, ?)
                WHERE sport = ? AND revision = ?
                """,
                (
                    _stamp(_next_slot(execution.slot, execution.interval_seconds, now)),
                    execution.sport,
                    execution.schedule_revision,
                ),
            )


def status() -> list[RowMapping]:
    with forecast_db_transaction() as db:
        return list(
            db.exec_driver_sql(
                """
                SELECT s.sport, s.interval_seconds, s.enabled, s.next_due,
                       e.id AS execution_id, e.trigger, e.scheduled_for,
                       e.started_at, e.finished_at, e.outcome, e.failure_summary,
                       (SELECT MAX(finished_at) FROM collection_executions
                        WHERE sport = s.sport AND outcome = 'complete') AS last_success
                FROM collection_schedules AS s
                LEFT JOIN collection_executions AS e ON e.id = (
                    SELECT MAX(id) FROM collection_executions WHERE sport = s.sport
                )
                ORDER BY s.sport
                """
            ).mappings()
        )
