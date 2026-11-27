import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx

from messy_weather_sports_bot import pipeline, publication_store, scheduler, scheduler_store
from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.collection import forecast_db_transaction
from messy_weather_sports_bot.nfl import NFL, main
from messy_weather_sports_bot.pipeline import EXIT_NOTHING_POSTED, EXIT_OK, EXIT_PARTIAL
from messy_weather_sports_bot.poster.console import ConsolePoster
from messy_weather_sports_bot.stadiums import stadium_for_team
from messy_weather_sports_bot.state import DayState, publication_lock, state_file_path
from tests.support.espn import espn_event
from tests.support.nws import forecast_response, mock_hourly_forecast

NOW = dt.datetime(2026, 11, 21, 14, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("CFB_HEALTHCHECK_URL", raising=False)
    scheduler_store.initialize(NOW)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = SimpleNamespace(now=NOW)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz)

    frozen = SimpleNamespace(**(vars(dt) | {"datetime": FrozenDateTime}))
    for module in (pipeline, publication_store, scheduler):
        monkeypatch.setattr(module, "dt", frozen)
    monkeypatch.setattr(pipeline, "todays_game_day", lambda sport: sport.game_day_for(clock.now))
    return clock


def advance_when_write_lock_acquired(monkeypatch, clock, acquired_at):
    @contextmanager
    def transaction():
        with forecast_db_transaction() as db:
            clock.now = acquired_at
            yield db

    monkeypatch.setattr(publication_store, "forecast_db_transaction", transaction)


def schedules():
    with forecast_db_transaction() as db:
        return {row.sport: row for row in db.exec_driver_sql("SELECT * FROM publication_schedules")}


def executions():
    with forecast_db_transaction() as db:
        return list(db.exec_driver_sql("SELECT * FROM publication_executions ORDER BY id"))


def request(now=NOW, key=None):
    return publication_store.request_publish(NFL, now, NFL.game_day_for(now), key or uuid4().hex)


def test_defaults_resume_and_weekday_edit():
    assert {(row.enabled, row.run_time, row.weekdays) for row in schedules().values()} == {
        (0, "10:00", 127)
    }
    publication_store.configure(
        NFL, NOW, run_time="08:30", weekdays=(0, 3, 6), enabled=True, expected_revision=0
    )
    scheduler_store.initialize(NOW)
    configured = schedules()
    assert dt.datetime.fromisoformat(configured["nfl"].next_due) == dt.datetime(
        2026, 11, 22, 13, 30, tzinfo=dt.UTC
    )
    assert configured["cfb"].next_due is None
    with pytest.raises(publication_store.ScheduleConflict):
        publication_store.configure(
            NFL, NOW, run_time="10:00", weekdays=(0,), enabled=False, expected_revision=0
        )
    assert schedules()["nfl"] == configured["nfl"]


@pytest.mark.parametrize(
    ("start", "run_time", "expected"),
    [
        (dt.datetime(2026, 3, 7, 18, tzinfo=dt.UTC), "02:30", (3, 8, 7, 30)),
        (dt.datetime(2026, 10, 31, 18, tzinfo=dt.UTC), "01:30", (11, 1, 5, 30)),
    ],
)
def test_schedule_uses_first_local_occurrence_across_dst(start, run_time, expected):
    publication_store.configure(
        NFL, start, run_time=run_time, weekdays=(6,), enabled=True, expected_revision=0
    )
    month, day, hour, minute = expected
    due = dt.datetime(2026, month, day, hour, minute, tzinfo=dt.UTC)
    execution = publication_store.claim(NFL, due)
    assert execution is not None
    assert execution.game_day == dt.date(2026, month, day)
    publication_store.finish(execution, due, "complete")
    assert publication_store.claim(NFL, due + dt.timedelta(hours=1)) is None


def test_missed_slot_only_catches_up_current_game_day():
    publication_store.configure(
        NFL, NOW, run_time="10:00", weekdays=(5,), enabled=True, expected_revision=0
    )
    sunday = NOW + dt.timedelta(days=1)
    assert publication_store.claim(NFL, sunday) is None
    assert dt.datetime.fromisoformat(schedules()["nfl"].next_due) == NOW + dt.timedelta(
        days=7, hours=1
    )


def test_manual_request_deduplicates_and_requires_new_confirmation_to_retry():
    key = uuid4().hex
    coalesced_key = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, NOW, key)
        second = pool.submit(request, NOW, coalesced_key)
        assert first.result() == second.result()
    run = publication_store.claim(NFL, NOW)
    assert run is not None
    assert request() == run.id
    publication_store.finish(run, NOW, "partial")
    assert request(key=key) == run.id
    assert request(key=coalesced_key) == run.id
    assert publication_store.claim(NFL, NOW) is None
    retry_key = uuid4().hex
    retry_id = request(key=retry_key)
    retry = publication_store.claim(NFL, NOW)
    assert retry is not None
    assert retry.id == retry_id
    publication_store.finish(retry, NOW, "failed")
    assert request(key=retry_key) == retry_id
    assert publication_store.claim(NFL, NOW) is None


@pytest.mark.parametrize(
    ("requested", "delay"),
    [
        (NOW, dt.timedelta(minutes=16)),
        (dt.datetime(2026, 11, 22, 4, 59, tzinfo=dt.UTC), dt.timedelta(minutes=2)),
    ],
)
def test_stale_requests_expire_without_becoming_a_run(requested, delay):
    request(requested)
    assert publication_store.claim(NFL, requested + delay) is None
    assert executions()[0].outcome == "expired"


def test_manual_and_due_slot_share_one_successful_game_day():
    publication_store.configure(
        NFL, NOW, run_time="10:00", weekdays=(5,), enabled=True, expected_revision=0
    )
    request()
    run = publication_store.claim(NFL, NOW)
    assert run is not None
    publication_store.finish(run, NOW, "complete")
    due = NOW + dt.timedelta(hours=1)
    assert request(due) == run.id
    heartbeat = publication_store.claim(NFL, due)
    assert heartbeat is not None and heartbeat.previous_outcome == "complete"
    publication_store.finish(heartbeat, due, "skipped")
    assert publication_store.claim(NFL, due) is None
    assert dt.datetime.fromisoformat(schedules()["nfl"].next_due) > due


@respx.mock
def test_worker_uses_sport_healthcheck_and_defers_to_cron_lock(monkeypatch):
    now = NOW
    key = uuid4().hex
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/nfl")
    monkeypatch.setenv("CFB_HEALTHCHECK_URL", "https://hc-ping.com/cfb")
    for sport in (NFL, CFB):
        scheduler_store.configure(sport.slug, now, enabled=False)
    request(now, key)
    publication_store.configure(
        CFB,
        now - dt.timedelta(days=2),
        run_time=now.astimezone(CFB.game_day_timezone).strftime("%H:%M"),
        weekdays=tuple(range(7)),
        enabled=True,
        expected_revision=0,
    )
    respx.get(CFB.scoreboard_url).respond(json={"events": []})
    start = respx.get("https://hc-ping.com/cfb/start").respond(200)
    end = respx.post("https://hc-ping.com/cfb/0").respond(200)

    with publication_lock(NFL) as acquired:
        assert acquired
        assert main([]) == EXIT_PARTIAL
        assert scheduler.run_once() == "complete"
        assert scheduler.run_once() is None

    assert start.called and end.called
    assert executions()[1].trigger == "scheduled"
    assert [(row.sport, row.outcome) for row in executions()] == [
        ("nfl", "queued"),
        ("cfb", "complete"),
    ]
    respx.get(NFL.scoreboard_url).respond(json={"events": []})
    respx.get("https://hc-ping.com/nfl/start").respond(200)
    respx.post("https://hc-ping.com/nfl/0").respond(200)
    assert scheduler.run_once() == "complete"
    scheduler_store.initialize(now)
    assert scheduler.run_once() is None
    assert main([]) == EXIT_OK


@respx.mock
def test_scheduled_healthcheck_after_manual_completion_does_not_rerun_pipeline(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="messy_weather_sports_bot")
    now = NOW
    for sport in (NFL, CFB):
        scheduler_store.configure(sport.slug, now, enabled=False)
    request(now)
    run = publication_store.claim(NFL, now)
    assert run is not None
    publication_store.finish(run, now, "complete")
    publication_store.configure(
        NFL,
        now - dt.timedelta(days=2),
        run_time=now.astimezone(NFL.game_day_timezone).strftime("%H:%M"),
        weekdays=tuple(range(7)),
        enabled=True,
        expected_revision=0,
    )
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/nfl")
    start = respx.get("https://hc-ping.com/nfl/start").respond(200)
    end = respx.post("https://hc-ping.com/nfl/0").respond(200)

    assert scheduler.run_once() == "complete"
    assert start.called and end.called
    assert b"already completed" in end.calls.last.request.content
    assert executions()[-1].outcome == "skipped"
    assert scheduler.run_once() is None


@respx.mock
@pytest.mark.parametrize("cron_first", [True, False])
def test_cron_and_worker_share_the_existing_post_state(monkeypatch, cron_first):
    now = NOW
    day = NFL.game_day_for(now)
    poster = ConsolePoster()

    def build_posters(platforms, dry_run, env_prefix):
        assert platforms == ["bluesky"]
        assert not dry_run
        assert env_prefix == NFL.env_prefix
        return [poster]

    monkeypatch.setattr("messy_weather_sports_bot.pipeline.build_posters", build_posters)
    respx.get(NFL.scoreboard_url).respond(
        json={
            "events": [
                espn_event("GB", "CHI", kickoff=f"{day}T13:00:00-05:00"),
            ]
        }
    )
    stadium = stadium_for_team("GB")
    mock_hourly_forecast(
        stadium.latitude,
        stadium.longitude,
        response=forecast_response("Heavy Snow", 27, "15 mph", 90, day=day.isoformat()),
    )
    publication_store.configure(
        NFL,
        now - dt.timedelta(days=2),
        run_time=now.astimezone(NFL.game_day_timezone).strftime("%H:%M"),
        weekdays=tuple(range(7)),
        enabled=True,
        expected_revision=0,
    )

    if cron_first:
        assert main([]) == EXIT_OK
    assert scheduler.run_once() == "complete"
    if not cron_first:
        assert main([]) == EXIT_OK

    assert len(poster.posted) == 1
    assert "CHI @ GB" in poster.posted[0]
    assert DayState.load(state_file_path(day)).for_platform("ConsolePoster").completed
    assert executions()[0].trigger == "scheduled"


def test_restart_does_not_replay_a_claimed_slot_but_can_retry_manually():
    publication_store.configure(
        NFL, NOW, run_time="10:00", weekdays=(5,), enabled=True, expected_revision=0
    )
    due = NOW + dt.timedelta(hours=1)
    run = publication_store.claim(NFL, due)
    assert run is not None
    restarted = due + dt.timedelta(minutes=2)
    scheduler_store.initialize(restarted)
    scheduler_store.recover(restarted)
    assert publication_store.claim(NFL, restarted) is None
    assert executions()[0].outcome == "interrupted"
    assert request(restarted) != run.id
    retry = publication_store.claim(NFL, restarted)
    assert retry is not None
    assert retry.game_day == run.game_day


def test_new_game_day_request_survives_previous_day_interruption():
    request()
    old_run = publication_store.claim(NFL, NOW)
    assert old_run is not None
    tomorrow = NOW + dt.timedelta(days=1)
    queued_id = request(tomorrow)
    assert queued_id != old_run.id

    scheduler_store.recover(tomorrow)
    new_run = publication_store.claim(NFL, tomorrow)
    assert new_run is not None
    assert (new_run.id, new_run.game_day) == (queued_id, NFL.game_day_for(tomorrow))


@respx.mock
def test_due_slot_requires_fresh_confirmation_after_interrupted_manual_run(monkeypatch):
    now = NOW
    request(now)
    abandoned = publication_store.claim(NFL, now)
    assert abandoned is not None
    publication_store.configure(
        NFL,
        now - dt.timedelta(days=2),
        run_time=now.astimezone(NFL.game_day_timezone).strftime("%H:%M"),
        weekdays=tuple(range(7)),
        enabled=True,
        expected_revision=0,
    )
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/nfl")
    respx.get("https://hc-ping.com/nfl/start").respond(200)
    end = respx.post("https://hc-ping.com/nfl/1").respond(200)

    assert scheduler.run_once() == "failed"
    assert end.called
    assert [row.outcome for row in executions()] == ["interrupted", "failed"]
    assert "Previous publication interrupted" in executions()[-1].failure_summary
    assert request(now) != abandoned.id


@respx.mock
@pytest.mark.parametrize("symlink_loop", [False, True])
def test_unavailable_publication_lock_reports_failure_without_blocking_collection(
    monkeypatch, tmp_path, symlink_loop
):
    now = NOW
    state = tmp_path / "state"
    if symlink_loop:
        state.symlink_to(state.name)
    else:
        state.write_text("fixture is a file, so the directory cannot be created")
    request(now)
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/nfl")
    respx.get("https://hc-ping.com/nfl/start").respond(200)
    end = respx.post("https://hc-ping.com/nfl/1").respond(200)

    assert main([]) == EXIT_NOTHING_POSTED
    assert scheduler.run_once() == "failed"
    assert end.call_count == 2
    assert executions()[0].outcome == "failed"
    assert "lock unavailable" in executions()[0].failure_summary

    monkeypatch.setattr(scheduler, "collect_window", lambda *_: iter(()))
    assert scheduler.run_once() == "complete"
    assert len(executions()) == 1


def test_pending_manual_request_serves_the_due_slot():
    publication_store.configure(
        NFL, NOW, run_time="10:00", weekdays=(5,), enabled=True, expected_revision=0
    )
    due = NOW + dt.timedelta(hours=1)
    request_id = request(due)
    run = publication_store.claim(NFL, due)
    assert run is not None and run.id == request_id
    assert dt.datetime.fromisoformat(executions()[0].scheduled_for) == due
    publication_store.finish(run, due, "complete")
    assert publication_store.claim(NFL, due) is None
    assert len(executions()) == 1


@respx.mock
def test_game_day_rollover_after_claim_reports_expired_publication(monkeypatch):
    now = NOW
    request(now)
    next_day = NFL.game_day_for(now) + dt.timedelta(days=1)
    monkeypatch.setattr("messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: next_day)
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/nfl")
    respx.get("https://hc-ping.com/nfl/start").respond(200)
    end = respx.post("https://hc-ping.com/nfl/1").respond(200)

    assert scheduler.run_once() == "failed"
    assert end.called
    assert executions()[0].failure_summary == "Bot exited with code 1"


def test_schedule_edit_selects_a_slot_after_acquiring_the_write_lock(monkeypatch, clock):
    slot = NOW + dt.timedelta(hours=1)
    clock.now = slot - dt.timedelta(milliseconds=100)
    advance_when_write_lock_acquired(monkeypatch, clock, slot + dt.timedelta(milliseconds=100))

    publication_store.configure(
        NFL, run_time="10:00", weekdays=tuple(range(7)), enabled=True, expected_revision=0
    )

    assert dt.datetime.fromisoformat(schedules()["nfl"].next_due) == slot + dt.timedelta(days=1)


@respx.mock
def test_request_that_expires_while_waiting_for_the_write_lock_is_not_published(monkeypatch, clock):
    for sport in (NFL, CFB):
        scheduler_store.configure(sport.slug, NOW, enabled=False)
    request()
    expiry = NOW + dt.timedelta(minutes=publication_store.REQUEST_TTL_MINUTES)
    clock.now = expiry - dt.timedelta(milliseconds=100)
    advance_when_write_lock_acquired(monkeypatch, clock, expiry + dt.timedelta(milliseconds=100))

    assert scheduler.run_once() is None
    assert executions()[0].outcome == "expired"


@respx.mock
def test_worker_rechecks_the_game_day_after_logging_in_to_the_poster(monkeypatch, clock):
    day = NFL.game_day_for(NOW)
    request()
    poster = ConsolePoster()

    def build_posters(*args):
        clock.now = dt.datetime.combine(
            day + dt.timedelta(days=1), dt.time(), NFL.game_day_timezone
        ).astimezone(dt.UTC)
        return [poster]

    monkeypatch.setattr(pipeline, "build_posters", build_posters)
    respx.get(NFL.scoreboard_url).respond(
        json={"events": [espn_event("GB", "CHI", kickoff=f"{day}T13:00:00-05:00")]}
    )
    stadium = stadium_for_team("GB")
    mock_hourly_forecast(
        stadium.latitude,
        stadium.longitude,
        response=forecast_response("Heavy Snow", 27, "15 mph", 90, day=day.isoformat()),
    )

    assert scheduler.run_once() == "failed"
    assert not poster.posted
    assert executions()[0].outcome == "failed"
