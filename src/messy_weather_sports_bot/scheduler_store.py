"""Persistent collection schedules and execution history."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, RowMapping

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
) -> None:
    if interval_seconds is not None and not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError("interval must be between one second and 365 days")
    with forecast_db_transaction() as db:
        current = db.exec_driver_sql(
            "SELECT interval_seconds, enabled, next_due FROM collection_schedules WHERE sport = ?",
            (sport,),
        ).one()
        seconds = current.interval_seconds if interval_seconds is None else interval_seconds
        is_enabled = current.enabled if enabled is None else int(enabled)
        if (seconds, is_enabled) == (current.interval_seconds, current.enabled):
            return
        next_due = current.next_due
        if enabled and not current.enabled:
            next_due = _stamp(now)
        elif interval_seconds is not None:
            next_due = _stamp(now + dt.timedelta(seconds=seconds))
        db.exec_driver_sql(
            """
            UPDATE collection_schedules
            SET interval_seconds = ?, enabled = ?, next_due = ?, revision = revision + 1
            WHERE sport = ?
            """,
            (seconds, is_enabled, next_due, sport),
        )


def recover(now: dt.datetime) -> None:
    """Mark abandoned executions after taking the exclusive collector lock."""
    with forecast_db_transaction() as db:
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


def claim_due(now: dt.datetime) -> Execution | None:
    with forecast_db_transaction() as db:
        row = db.exec_driver_sql(
            """
            SELECT sport, next_due, interval_seconds, revision
            FROM collection_schedules
            WHERE enabled = 1 AND next_due <= ?
            ORDER BY next_due, sport LIMIT 1
            """,
            (_stamp(now),),
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
