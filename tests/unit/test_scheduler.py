import datetime as dt

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
