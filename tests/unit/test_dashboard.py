import datetime as dt
import re

import pytest

from messy_weather_sports_bot import scheduler_store
from messy_weather_sports_bot.collection import GameObservation
from messy_weather_sports_bot.collector import CollectedDay
from messy_weather_sports_bot.dashboard import create_app
from messy_weather_sports_bot.messiness import evaluate_game
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.venues import StadiumInfo
from messy_weather_sports_bot.weather import WeatherReport


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    path = tmp_path / "history.sqlite3"
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(path))
    scheduler_store.initialize(dt.datetime.now(dt.UTC))
    return create_app("test-secret").test_client()


def csrf_token(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def test_sport_pages_preserve_valid_filters(dashboard):
    assert dashboard.get("/").headers["Location"].endswith("/games/nfl")
    response = dashboard.get("/games/cfb?precipitation=snow&sort=kickoff&messy=1")
    assert response.status_code == 200
    assert b"Top 25 CFB" in response.data
    assert b'<option value="snow" selected>' in response.data
    assert b'<option value="kickoff" selected>' in response.data
    assert b'name="messy" value="1" checked' in response.data
    assert dashboard.get("/games/other").status_code == 404
    assert dashboard.get("/games/cfb?day=bad").status_code == 400


def test_refresh_requires_csrf_and_deduplicates(dashboard):
    url = "/admin/tasks/nfl/refresh"
    assert dashboard.post(url).status_code == 400
    token = csrf_token(dashboard.get("/admin/tasks"))
    for _ in range(2):
        assert dashboard.post(url, data={"csrf_token": token}).status_code == 303
    assert b"Queued" in dashboard.get("/admin/tasks/status").data
    response = dashboard.get("/admin/data/collection_refresh_requests")
    assert b"1 row" in response.data


def test_schedule_validation_and_stale_edit(dashboard):
    url = "/admin/tasks/nfl/schedule"
    token = csrf_token(dashboard.get("/admin/tasks"))
    form = {"csrf_token": token, "revision": 0, "hours": 0, "enabled": "1"}
    assert dashboard.post(url, data=form).status_code == 400
    form["hours"] = 4
    assert dashboard.post(url, data=form).status_code == 303
    form["hours"] = 8
    assert dashboard.post(url, data=form).status_code == 409
    row = next(row for row in scheduler_store.status() if row["sport"] == "nfl")
    assert row["interval_seconds"] == 4 * 3600


def test_raw_browser_only_accepts_known_tables_and_columns(dashboard):
    assert dashboard.get("/admin/data/collection_schedules/nfl").status_code == 200
    assert dashboard.get("/admin/data/collection_schedules?sort=sport").status_code == 200
    assert dashboard.get("/admin/data/collection_schedules?sort=missing").status_code == 400
    assert dashboard.get("/admin/data/sqlite_master").status_code == 404
    assert dashboard.get("/admin/data/collection_schedules/nba").status_code == 404


def test_game_history_labels_and_escapes_saved_weather(dashboard):
    now = dt.datetime.now(dt.UTC)
    kickoff = now + dt.timedelta(hours=12)
    match = Game("Home", "Away", kickoff, StadiumInfo("Field", 44.5, -88.0, False))
    reports = [
        WeatherReport("Clear", 62, 5, 0),
        WeatherReport("Snow <em>outlook</em>", 25, 12, 80),
    ]
    for hours, report in zip((2, 1), reports, strict=True):
        captured = now - dt.timedelta(hours=hours)
        execution = scheduler_store.claim_manual("nfl", captured)
        observation = GameObservation(
            match,
            "evaluated",
            forecasts=(report,),
            result=evaluate_game(match, [report], []),
            observed_at=captured,
        )
        scheduler_store.save_day(
            execution,
            CollectedDay(NFL.game_day_for(kickoff), captured, "complete", 1, (observation,)),
        )
        scheduler_store.finish(execution, captured, "complete")

    response = dashboard.get("/games/nfl?precipitation=snow")
    assert b'href="/game/1?observation=2"' in response.data
    response = dashboard.get("/game/1?observation=2")
    assert response.status_code == 200
    assert b"Snow &lt;em&gt;outlook&lt;/em&gt;" in response.data
    assert b"Pleasant \xc2\xb7 Score " in response.data
    assert dashboard.get("/admin/data/forecast_periods/2?index=0").status_code == 200


def test_refresh_status_can_be_read_after_a_new_browser_session(dashboard):
    token = csrf_token(dashboard.get("/admin/tasks"))
    assert dashboard.post("/admin/tasks/nfl/refresh", data={"csrf_token": token}).status_code == 303
    restart = create_app("test-secret").test_client()
    queued = restart.get("/admin/tasks/status").data
    assert b"Queued" in queued
    assert b'hx-trigger="every 5s"' in queued
    now = dt.datetime.now(dt.UTC)
    execution = scheduler_store.claim_requested(now)
    assert execution is not None
    scheduler_store.save_day(execution, CollectedDay(now.date(), now, "complete", 0))
    assert b"Running</strong> \xc2\xb7 1 date processed" in restart.get("/admin/tasks/status").data
    scheduler_store.finish(execution, now, "complete")
    terminal = restart.get("/admin/tasks/status").data
    assert b"Complete" in terminal
    assert b"hx-trigger=" not in terminal


def test_https_forms_require_same_origin_and_trusted_host(dashboard):
    app = create_app("test-secret", trusted_host="dashboard.test")
    client = app.test_client()
    assert client.get("/games/nfl", base_url="https://untrusted.test").status_code == 400
    origin = "https://dashboard.test"
    token = csrf_token(client.get("/admin/tasks", base_url=origin))
    url = "/admin/tasks/nfl/refresh"
    data = {"csrf_token": token}
    assert (
        client.post(
            url, base_url=origin, data=data, headers={"Referer": "https://other.test/"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            url, base_url=origin, data=data, headers={"Referer": f"{origin}/admin/tasks"}
        ).status_code
        == 303
    )
