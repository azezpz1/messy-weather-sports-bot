"""Run forecast collection and publishing jobs on a single host."""

from __future__ import annotations

import datetime as dt
import fcntl
import logging
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager

from messy_weather_sports_bot import publication_store, scheduler_store
from messy_weather_sports_bot.cli import report_publication_status, run_scheduled_publication
from messy_weather_sports_bot.collection import forecast_db_path
from messy_weather_sports_bot.collector import collect_window
from messy_weather_sports_bot.pipeline import EXIT_NOTHING_POSTED, EXIT_OK, EXIT_PARTIAL
from messy_weather_sports_bot.scheduler_store import Execution, ExecutionOutcome
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.sports import SPORTS
from messy_weather_sports_bot.state import PublicationLockError, publication_lock

logger = logging.getLogger(__name__)
POLL_SECONDS = 15
_PUBLICATION_OUTCOMES: dict[int, ExecutionOutcome] = {EXIT_OK: "complete", EXIT_PARTIAL: "partial"}


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


def _finish_publication(
    sport: Sport,
    execution: publication_store.Execution,
    outcome: publication_store.Outcome,
    summary: str | None = None,
) -> None:
    publication_store.finish(execution, dt.datetime.now(dt.UTC), outcome, summary)
    logger.log(
        logging.INFO if outcome in {"complete", "skipped"} else logging.WARNING,
        "%s publication %d: %s%s",
        sport.slug,
        execution.id,
        outcome,
        f" ({summary})" if summary else "",
    )


def _run_publication(sport: Sport, execution: publication_store.Execution) -> ExecutionOutcome:
    previous = execution.previous_outcome
    if previous == "complete":
        report_publication_status(
            sport, f"{sport.name} publication already completed for {execution.game_day}.", EXIT_OK
        )
        _finish_publication(sport, execution, "skipped")
        return "complete"
    if previous is not None:
        summary = f"Previous publication {previous}. Verify the result before confirming a retry."
        outcome = "partial" if previous == "partial" else "failed"
        code = EXIT_PARTIAL if previous == "partial" else EXIT_NOTHING_POSTED
        report_publication_status(sport, summary, code)
        _finish_publication(sport, execution, outcome, summary)
        return outcome
    try:
        code = run_scheduled_publication(sport, execution.game_day)
    except (Exception, SystemExit) as exc:
        outcome = "failed"
        summary = f"Publication stopped ({type(exc).__name__})"
    else:
        outcome = _PUBLICATION_OUTCOMES.get(code, "failed")
        summary = None if outcome == "complete" else f"Bot exited with code {code}"
    _finish_publication(sport, execution, outcome, summary)
    return outcome


def _publish_ready(now: dt.datetime) -> ExecutionOutcome | None:
    for slug in publication_store.ready_sports(now):
        sport = SPORTS[slug]
        try:
            with publication_lock(sport) as acquired:
                if acquired and (
                    execution := publication_store.claim(sport, dt.datetime.now(dt.UTC))
                ):
                    return _run_publication(sport, execution)
        except PublicationLockError as exc:
            # Consume this attempt and notify monitoring without touching the platform.
            if execution := publication_store.claim(sport, dt.datetime.now(dt.UTC)):
                summary = str(exc)
                _finish_publication(sport, execution, "failed", summary)
                report_publication_status(sport, summary, EXIT_NOTHING_POSTED)
                return "failed"
    return None


def run_once(sport: str | None = None) -> ExecutionOutcome | None:
    """Run an explicit collection refresh or the next eligible worker task."""
    with collector_lock() as acquired:
        if not acquired:
            return None
        now = dt.datetime.now(dt.UTC)
        scheduler_store.recover(now)
        if sport is None:
            outcome = _publish_ready(now)
            if outcome is not None:
                return outcome
            execution = scheduler_store.claim_due(
                now, defer_to_requests=True
            ) or scheduler_store.claim_requested(now)
        else:
            execution = scheduler_store.claim_manual(sport, now)
        if execution is None:
            return None
        return run_execution(execution, now)


def run_worker() -> None:
    while True:
        if run_once() is None:
            time.sleep(POLL_SECONDS)
