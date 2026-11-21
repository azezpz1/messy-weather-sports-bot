"""Run independent collection jobs on a single host."""

from __future__ import annotations

import datetime as dt
import fcntl
import logging
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager

from messy_weather_sports_bot import scheduler_store
from messy_weather_sports_bot.collection import forecast_db_path
from messy_weather_sports_bot.collector import collect_window
from messy_weather_sports_bot.scheduler_store import Execution, ExecutionOutcome
from messy_weather_sports_bot.sports import SPORTS

logger = logging.getLogger(__name__)
POLL_SECONDS = 15


@contextmanager
def collector_lock() -> Generator[bool]:
    """Hold a process lock for network work without holding a SQLite transaction."""
    path = forecast_db_path().resolve()
    lock_path = path.with_name(f"{path.name}.collector.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            # Closing the descriptor releases the lock, including on process death.
            yield True


def run_execution(execution: Execution, started_at: dt.datetime) -> ExecutionOutcome:
    days: Counter[str] = Counter()
    error = None
    try:
        for result in collect_window(SPORTS[execution.sport], started_at):
            scheduler_store.save_day(execution, result)
            days[result.outcome] += 1
    except Exception as exc:
        # Keep previous days available and record a safe error type for status.
        error = type(exc).__name__
    good = days["complete"]
    partial = days["partial"]
    failures = days.total() - good - partial
    outcome: ExecutionOutcome
    if not error and not failures and not partial:
        outcome = "complete"
    elif good or partial:
        outcome = "partial"
    else:
        outcome = "failed"
    summary = None
    if outcome != "complete":
        summary = f"days saved: {good} complete, {partial} partial, {failures} unavailable"
        if error:
            summary += f"; stopped early ({error})"
    scheduler_store.finish(execution, dt.datetime.now(dt.UTC), outcome, summary)
    logger.log(
        logging.INFO if outcome == "complete" else logging.WARNING,
        "%s collection %d: %s (%d day records saved)",
        execution.sport,
        execution.id,
        outcome,
        days.total(),
    )
    return outcome


def run_once(sport: str | None = None) -> ExecutionOutcome | None:
    """Run a manual refresh or the next due task if the collector is idle."""
    with collector_lock() as acquired:
        if not acquired:
            return None
        now = dt.datetime.now(dt.UTC)
        scheduler_store.recover(now)
        execution = (
            scheduler_store.claim_due(now)
            if sport is None
            else scheduler_store.claim_manual(sport, now)
        )
        if execution is None:
            return None
        return run_execution(execution, now)


def run_worker() -> None:
    while True:
        if run_once() is None:
            time.sleep(POLL_SECONDS)
