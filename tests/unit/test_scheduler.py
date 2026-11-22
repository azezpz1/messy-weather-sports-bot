import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest

from messy_weather_sports_bot import scheduler, scheduler_store
from messy_weather_sports_bot.collection import forecast_db_transaction

NOW = dt.datetime(2026, 11, 21, 18, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))


def test_overdue_run_coalesces_and_interval_edit_wins():
    scheduler_store.initialize(NOW)
    overdue = NOW + dt.timedelta(hours=25)
    run = scheduler_store.claim_due(overdue)
    assert run is not None
    assert run.slot == NOW
    scheduler_store.configure(run.sport, overdue, interval_seconds=4 * 3600)
    scheduler_store.finish(run, overdue + dt.timedelta(hours=8), "complete")
    row = next(row for row in scheduler_store.status() if row["sport"] == run.sport)
    assert dt.datetime.fromisoformat(row["next_due"]) == overdue + dt.timedelta(hours=4)


def test_interrupted_execution_keeps_committed_day():
    scheduler_store.initialize(NOW)
    run = scheduler_store.claim_manual("nfl", NOW)
    scheduler_store.save_day(run, scheduler_store.CollectedDay(NOW.date(), NOW, "complete", 0))
    scheduler_store.recover(NOW + dt.timedelta(hours=1))
    with forecast_db_transaction() as db:
        assert (
            db.exec_driver_sql(
                "SELECT outcome FROM collection_executions WHERE id = ?", (run.id,)
            ).scalar_one()
            == "interrupted"
        )
        assert db.exec_driver_sql("SELECT execution_id FROM collection_runs").scalar_one() == run.id


def test_worker_and_manual_run_cannot_overlap(monkeypatch):
    scheduler_store.initialize(dt.datetime.now(dt.UTC))

    def collect(sport, started):
        assert scheduler.run_once("nfl") is None
        yield scheduler_store.CollectedDay(started.date(), started, "complete", 0)

    monkeypatch.setattr(scheduler, "collect_window", collect)
    assert scheduler.run_once() == "complete"
    assert sum(row["outcome"] == "complete" for row in scheduler_store.status()) == 1


def test_resume_makes_paused_schedule_due_immediately():
    scheduler_store.initialize(NOW)
    scheduler_store.configure("nfl", NOW, enabled=False)
    scheduler_store.configure("cfb", NOW, enabled=False)
    assert scheduler_store.claim_due(NOW) is None

    resumed_at = NOW + dt.timedelta(days=2)
    scheduler_store.initialize(resumed_at)
    assert scheduler_store.claim_due(resumed_at) is None
    scheduler_store.configure("nfl", resumed_at, enabled=True)
    run = scheduler_store.claim_due(resumed_at)
    assert run is not None
    assert run.sport == "nfl"
    assert run.slot == resumed_at


def test_failed_date_is_visible_in_execution_status(monkeypatch):
    scheduler_store.initialize(NOW)

    def collect(sport, started):
        date = started.date()
        yield scheduler_store.CollectedDay(date, started, "complete", 0)
        yield scheduler_store.CollectedDay(
            date + dt.timedelta(days=1), started, "schedule_unavailable"
        )

    monkeypatch.setattr(scheduler, "collect_window", collect)
    assert scheduler.run_once("nfl") == "partial"
    row = next(row for row in scheduler_store.status() if row["sport"] == "nfl")
    assert row["outcome"] == "partial"
    assert row["failure_summary"] == "days saved: 1 complete, 0 partial, 1 unavailable"
    assert row["last_success"] is None


def test_worker_runs_both_due_sports_once_and_waits_after_restart(monkeypatch):
    started = dt.datetime.now(dt.UTC)
    scheduler_store.initialize(started - dt.timedelta(days=2))
    seen = []

    def collect(sport, now):
        seen.append(sport.slug)
        yield scheduler_store.CollectedDay(now.date(), now, "complete", 0)

    def stop_at_idle(seconds):
        assert seconds == scheduler.POLL_SECONDS
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "collect_window", collect)
    monkeypatch.setattr(scheduler.time, "sleep", stop_at_idle)
    with pytest.raises(KeyboardInterrupt):
        scheduler.run_worker()

    assert sorted(seen) == ["cfb", "nfl"]
    for row in scheduler_store.status():
        assert row["trigger"] == "scheduled"
        assert row["last_success"] == row["finished_at"]
        assert dt.datetime.fromisoformat(row["next_due"]) > started

    scheduler_store.initialize(dt.datetime.now(dt.UTC))
    with pytest.raises(KeyboardInterrupt):
        scheduler.run_worker()
    assert len(seen) == 2


def test_run_skips_recurrences_that_elapsed_while_running():
    scheduler_store.initialize(NOW)
    run = scheduler_store.claim_due(NOW)
    assert run is not None
    finished = NOW + dt.timedelta(hours=13)
    scheduler_store.finish(run, finished, "complete")
    row = next(row for row in scheduler_store.status() if row["sport"] == run.sport)
    assert dt.datetime.fromisoformat(row["next_due"]) == NOW + dt.timedelta(hours=18)


def test_schedule_edit_rejects_stale_revision_without_changing_schedule():
    scheduler_store.initialize(NOW)
    scheduler_store.configure("nfl", NOW, interval_seconds=4 * 3600, expected_revision=0)

    with pytest.raises(scheduler_store.ScheduleConflict):
        scheduler_store.configure("nfl", NOW, enabled=False, expected_revision=0)

    row = next(row for row in scheduler_store.status() if row["sport"] == "nfl")
    assert row["enabled"] == 1
    assert row["interval_seconds"] == 4 * 3600
    assert dt.datetime.fromisoformat(row["next_due"]) == NOW + dt.timedelta(hours=4)


def test_pause_with_unchanged_interval_preserves_next_due():
    scheduler_store.initialize(NOW)
    scheduler_store.configure(
        "nfl", NOW, interval_seconds=6 * 3600, enabled=False, expected_revision=0
    )

    row = next(row for row in scheduler_store.status() if row["sport"] == "nfl")
    assert row["enabled"] == 0
    assert dt.datetime.fromisoformat(row["next_due"]) == NOW


def test_refresh_requests_deduplicate_until_execution_finishes():
    scheduler_store.initialize(NOW)
    with ThreadPoolExecutor(max_workers=2) as clients:
        first = clients.submit(scheduler_store.request_refresh, "nfl", NOW)
        second = clients.submit(scheduler_store.request_refresh, "nfl", NOW)
        request_id = first.result()
        assert second.result() == request_id

    execution = scheduler_store.claim_requested(NOW)
    assert execution is not None
    assert execution.sport == "nfl"
    assert execution.slot is None
    assert scheduler_store.request_refresh("nfl", NOW) == request_id
    assert scheduler_store.claim_requested(NOW) is None

    scheduler_store.finish(execution, NOW, "complete")
    assert scheduler_store.request_refresh("nfl", NOW) != request_id
    with pytest.raises(ValueError, match="sport"):
        scheduler_store.request_refresh("other", NOW)


def test_worker_refreshes_paused_schedule_under_lock_without_moving_due_time(monkeypatch):
    scheduler_store.initialize(NOW)
    scheduler_store.configure("nfl", NOW, enabled=False)
    scheduler_store.configure("cfb", NOW, enabled=False)
    request_id = scheduler_store.request_refresh("nfl", NOW)

    def collect(sport, started):
        yield scheduler_store.CollectedDay(started.date(), started, "complete", 0)

    monkeypatch.setattr(scheduler, "collect_window", collect)
    with scheduler.collector_lock() as acquired:
        assert acquired
        assert scheduler.run_once() is None
        with forecast_db_transaction() as db:
            request = db.exec_driver_sql("SELECT execution_id FROM collection_refresh_requests")
            assert request.scalar_one() is None

    assert scheduler.run_once() == "complete"
    assert scheduler.run_once() is None
    row = next(row for row in scheduler_store.status() if row["sport"] == "nfl")
    assert row["enabled"] == 0
    assert row["trigger"] == "manual"
    assert dt.datetime.fromisoformat(row["next_due"]) == NOW
    with forecast_db_transaction() as db:
        request = db.exec_driver_sql(
            "SELECT execution_id FROM collection_refresh_requests WHERE id = ?", (request_id,)
        )
        assert request.scalar_one() == row["execution_id"]


def test_worker_orders_queued_and_scheduled_work_by_eligibility(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    scheduler_store.initialize(now - dt.timedelta(minutes=1))
    scheduler_store.configure("cfb", now, enabled=False)
    scheduler_store.request_refresh("cfb", now - dt.timedelta(minutes=2))
    scheduler_store.request_refresh("nfl", now)
    seen = []

    def collect(sport, started):
        seen.append(sport.slug)
        yield scheduler_store.CollectedDay(started.date(), started, "complete", 0)

    monkeypatch.setattr(scheduler, "collect_window", collect)
    for _ in range(3):
        assert scheduler.run_once() == "complete"
    assert scheduler.run_once() is None
    assert seen == ["cfb", "nfl", "nfl"]
    with forecast_db_transaction() as db:
        assert db.exec_driver_sql(
            "SELECT trigger FROM collection_executions ORDER BY id"
        ).scalars().all() == ["manual", "scheduled", "manual"]


def test_worker_restart_keeps_queued_and_partially_saved_refreshes(monkeypatch):
    scheduler_store.initialize(NOW)
    scheduler_store.configure("nfl", NOW, enabled=False)
    scheduler_store.configure("cfb", NOW, enabled=False)
    interrupted_id = scheduler_store.request_refresh("nfl", NOW)
    waiting_id = scheduler_store.request_refresh("cfb", NOW + dt.timedelta(seconds=1))

    def collect(sport, started):
        yield scheduler_store.CollectedDay(started.date(), started, "complete", 0)
        if sport.slug == "nfl":
            raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "collect_window", collect)
    with pytest.raises(KeyboardInterrupt):
        scheduler.run_once()

    scheduler_store.initialize(dt.datetime.now(dt.UTC))
    assert scheduler.run_once() == "complete"
    assert scheduler.run_once() is None
    assert scheduler_store.request_refresh("nfl", dt.datetime.now(dt.UTC)) != interrupted_id
    with forecast_db_transaction() as db:
        assert db.exec_driver_sql(
            """
            SELECT q.id, e.outcome, r.outcome
            FROM collection_refresh_requests AS q
            JOIN collection_executions AS e ON q.execution_id = e.id
            JOIN collection_runs AS r ON r.execution_id = e.id
            ORDER BY q.id
            """
        ).all() == [
            (interrupted_id, "interrupted", "complete"),
            (waiting_id, "complete", "complete"),
        ]
