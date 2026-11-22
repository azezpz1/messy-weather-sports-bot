import datetime as dt
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from messy_weather_sports_bot import dashboard_read, scheduler_store
from messy_weather_sports_bot.alerts import AlertSeverity, WeatherAlert
from messy_weather_sports_bot.collection import (
    GameObservation,
    RunOutcome,
    forecast_db_transaction,
    record_collection,
)
from messy_weather_sports_bot.collector import CollectedDay
from messy_weather_sports_bot.messiness import evaluate_game
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.venues import StadiumInfo
from messy_weather_sports_bot.weather import WeatherReport

NOW = dt.datetime(2026, 11, 21, 15, tzinfo=dt.UTC)
DAY = NOW.date()
STADIUM = StadiumInfo("Open stadium", 44.5, -88, False)


@pytest.fixture
def database(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "history #1.sqlite3"
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(path))
    scheduler_store.initialize(NOW)
    return path


def game(event: str, *, kickoff: dt.datetime = NOW + dt.timedelta(hours=4)) -> Game:
    return Game(event, "Away", kickoff, STADIUM, espn_event_id=event, home_rank=10)


def observation(
    match: Game,
    *forecasts: WeatherReport,
    observed_at: dt.datetime = NOW - dt.timedelta(hours=1),
    alert: WeatherAlert | None = None,
    **kwargs,
) -> GameObservation:
    return GameObservation(
        match,
        "evaluated",
        result=evaluate_game(match, list(forecasts), [alert] if alert else []),
        forecasts=forecasts,
        observed_at=observed_at,
        **kwargs,
    )


def save(
    day: dt.date,
    *observations: GameObservation,
    sport: str = "nfl",
    outcome: RunOutcome = "complete",
    rankings_unavailable: bool = False,
) -> None:
    execution = scheduler_store.claim_manual(sport, NOW)
    scheduler_store.save_day(
        execution,
        CollectedDay(
            day,
            NOW,
            outcome,
            len(observations),
            observations,
            rankings_unavailable=rankings_unavailable,
        ),
    )
    scheduler_store.finish(execution, NOW, "complete")


def test_authoritative_empty_slate_clears_day(database):
    save(DAY, observation(game("old"), WeatherReport("Snow", 27, 5, 80)))
    save(DAY)

    with dashboard_read.read_database(database) as db:
        result = dashboard_read.browse_games(db, "nfl", NOW)

    assert result["games"] == []
    assert result["notices"] == []


@pytest.mark.parametrize(
    ("outcome", "rankings_unavailable", "notice"),
    [
        ("schedule_unavailable", False, "schedule_unavailable"),
        ("venue_table_unavailable", False, "venue_table_unavailable"),
        ("partial", True, "rankings_unavailable"),
    ],
)
def test_unverified_slate_keeps_existing_snapshot_and_age(
    database, outcome, rankings_unavailable, notice
):
    save(
        DAY,
        observation(
            game("kept"),
            WeatherReport("Snow", 27, 5, 80),
            observed_at=NOW - dt.timedelta(hours=13),
        ),
        sport="cfb",
    )
    save(
        DAY,
        observation(game("unverified"), WeatherReport("Sunny", 65, 0, 0)),
        sport="cfb",
        outcome=outcome,
        rankings_unavailable=rankings_unavailable,
    )

    with dashboard_read.read_database(database) as db:
        result = dashboard_read.browse_games(db, "cfb", NOW)
        assert dashboard_read.browse_games(db, "nfl", NOW)["games"] == []

    assert [
        (row["home_team"], row["forecast_age_seconds"], row["stale"]) for row in result["games"]
    ] == [("kept", 13 * 3600, True)]
    assert result["notices"] == [{"game_day": DAY.isoformat(), "outcome": notice, "retained": True}]


def test_latest_attempt_can_remove_a_score_and_explain_missing_weather(database):
    failed = game("failed")
    save(DAY, observation(failed, WeatherReport("Snow", 27, 5, 80)))
    save(
        DAY,
        GameObservation(failed, "forecast_unavailable", "ReadTimeout", observed_at=NOW),
        GameObservation(game("horizon"), "forecast_unavailable", "forecast_horizon"),
        GameObservation(game("venue"), "venue_unavailable"),
        GameObservation(
            replace(game("covered"), stadium=replace(STADIUM, is_covered=True)), "skipped"
        ),
        outcome="partial",
    )

    with dashboard_read.read_database(database) as db:
        result = dashboard_read.browse_games(db, "nfl", NOW)
        rows = {item["home_team"]: item for item in result["games"]}
        detail = dashboard_read.game_detail(db, rows["failed"]["game_id"])

    assert result["notices"] == []
    assert [(rows[name]["weather_status"], rows[name]["score"]) for name in rows] == [
        ("forecast_unavailable", None),
        ("forecast_horizon", None),
        ("venue_unavailable", None),
        ("covered", None),
    ]
    assert rows["failed"]["forecast_age_seconds"] is None
    assert detail is not None
    assert detail["observation"]["score"] is None
    assert len(detail["history"]) == 2
    assert detail["history"][1]["condition"] == "snow"


def test_reschedule_keeps_one_game_and_original_publishing_history(database):
    match = game("moving")
    tomorrow = DAY + dt.timedelta(days=1)
    moved = replace(
        match, kickoff=match.kickoff + dt.timedelta(days=1), stadium=replace(STADIUM, name="New")
    )
    rain = WeatherReport("Rain", 55, 5, 70)
    save(DAY, observation(match, rain, observed_at=NOW - dt.timedelta(hours=3)))
    save(tomorrow, observation(moved, rain))
    save(DAY, outcome="schedule_unavailable")
    record_collection("nfl", DAY, NOW, "complete", 1, [observation(match, rain, observed_at=NOW)])

    with dashboard_read.read_database(database) as db:
        rows = dashboard_read.browse_games(db, "nfl", NOW)["games"]
        assert dashboard_read.browse_games(db, "nfl", NOW, day=DAY)["games"] == []
        detail = dashboard_read.game_detail(db, rows[0]["game_id"], rows[0]["id"])
        assert detail is not None
        original = dashboard_read.game_detail(db, rows[0]["game_id"], detail["history"][-1]["id"])

    assert len(rows) == 1
    assert rows[0]["stadium_name"] == "New"
    assert rows[0]["kickoff"] == moved.kickoff.isoformat()
    assert detail["observation"]["stadium_name"] == "New"
    assert detail["periods"][0]["short_forecast"] == "Rain"
    assert len(detail["history"]) == 3
    assert original is not None
    assert original["observation"]["stadium_name"] == "Open stadium"


def test_precipitation_uses_all_period_text_and_score_uses_saved_evaluation(database):
    wind = WeatherReport("Sunny", 60, 30, 0)
    light_rain = WeatherReport("Slight Chance Rain Showers", 60, 4, 20)
    light_ice = WeatherReport("Slight Chance Freezing Rain Showers", 60, 2, 20)
    mixed = WeatherReport("Snow Showers and Thunderstorms with Wintry Mix", 60, 0, 20)
    alert = WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, NOW, None)
    save(
        DAY,
        GameObservation(game("unscored", kickoff=NOW), "forecast_unavailable", "ReadTimeout"),
        observation(game("pleasant", kickoff=NOW + dt.timedelta(hours=1)), light_rain),
        observation(game("wind"), wind, light_rain),
        observation(
            game("alert"),
            wind,
            light_ice,
            alert=alert,
            detail="partial_forecast_coverage",
            alerts_error_type="ReadTimeout",
        ),
        observation(game("mixed"), mixed),
        outcome="partial",
    )

    with dashboard_read.read_database(database) as db:

        def rows(**kwargs):
            return dashboard_read.browse_games(db, "nfl", NOW, **kwargs)["games"]

        assert [row["home_team"] for row in rows()] == [
            "alert",
            "wind",
            "pleasant",
            "mixed",
            "unscored",
        ]
        assert [row["home_team"] for row in rows(sort="kickoff")] == [
            "unscored",
            "pleasant",
            "wind",
            "alert",
            "mixed",
        ]
        assert [row["home_team"] for row in rows(precipitation="rain")] == ["wind", "pleasant"]
        assert [row["home_team"] for row in rows(precipitation="rain", messy_only=True)] == ["wind"]
        assert [row["home_team"] for row in rows(precipitation="ice")] == ["alert", "mixed"]
        (thunderstorm,) = rows(precipitation="thunderstorms")

    assert set(thunderstorm["precipitation"]) == {"ice", "snow", "thunderstorms"}
    assert thunderstorm["weather_status"] == "pleasant"
    with dashboard_read.read_database(database) as db:
        alerted = dashboard_read.browse_games(db, "nfl", NOW)["games"][0]
    assert alerted["condition"] == "wind"
    assert alerted["score"] == 90
    assert alerted["alert_event"] == "Winter Storm Warning"
    assert alerted["selected_forecast"]["short_forecast"] == "Sunny"
    assert alerted["partial_coverage"]
    assert alerted["alerts_error_type"] == "ReadTimeout"


def test_read_connection_uses_existing_database_and_coexists_with_writer(database, tmp_path):
    absent = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError), dashboard_read.read_database(absent):
        pass
    assert not absent.exists()

    with forecast_db_transaction() as writer:
        writer.exec_driver_sql("UPDATE collection_schedules SET enabled = 0")
        with dashboard_read.read_database(database) as reader:
            assert reader.execute("SELECT enabled FROM collection_schedules").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("DELETE FROM collection_schedules")


def test_data_explorer_pages_and_limits_identifiers(database):
    save(
        DAY,
        *(observation(game(str(index)), WeatherReport("Sunny", 50, 0, 0)) for index in range(55)),
    )

    with dashboard_read.read_database(database) as db:
        first = dashboard_read.browse_table(db, "games", sort="id", descending=True)
        last = dashboard_read.browse_table(db, "games", page=10**12, sort="id", descending=True)
        stored = dashboard_read.read_row(db, "forecast_periods", "1", 0)
        assert dashboard_read.read_row(db, "forecast_periods", "1", 5) is None
        assert dashboard_read.read_row(db, "games", "9999999999999999999999999999") is None
        with pytest.raises(ValueError):
            dashboard_read.browse_table(db, "sqlite_schema")
        with pytest.raises(ValueError):
            dashboard_read.browse_table(db, "games", sort="id; SELECT NULL")

    assert first["columns"] == ("id", "sport", "source_key", "espn_event_id")
    assert (first["rows"][0]["id"], first["total"], first["pages"]) == (55, 55, 2)
    assert (last["page"], last["rows"][-1]["id"]) == (2, 1)
    assert stored is not None
    assert stored["temperature_f"] == 50
    assert stored["starts_at"] is None


def test_task_status_reports_queued_running_and_interrupted_progress(database):
    from messy_weather_sports_bot.scheduler_store import claim_requested, request_refresh

    save(DAY)
    scheduler_store.configure("nfl", NOW, enabled=False)
    request_id = request_refresh("nfl", NOW)
    with dashboard_read.read_database(database) as db:
        tasks = {row["sport"]: row for row in dashboard_read.task_status(db)}
        assert tasks["cfb"]["refresh"] is None
        assert tasks["nfl"]["enabled"] == 0
        assert tasks["nfl"]["last_success"] == NOW.isoformat(timespec="microseconds")
        assert tasks["nfl"]["refresh"]["id"] == request_id
        assert tasks["nfl"]["refresh"]["status"] == "queued"
        assert tasks["nfl"]["refresh"]["days"] == []

    execution = claim_requested(NOW)
    assert execution is not None
    scheduler_store.save_day(execution, CollectedDay(DAY, NOW, "complete", 0))
    with dashboard_read.read_database(database) as db:
        nfl = next(item for item in dashboard_read.task_status(db) if item["sport"] == "nfl")
        assert nfl["refresh"]["status"] == "running"
        assert [item["game_day"] for item in nfl["refresh"]["days"]] == [DAY.isoformat()]

    scheduler_store.recover(NOW + dt.timedelta(minutes=5))
    with dashboard_read.read_database(database) as db:
        nfl = next(item for item in dashboard_read.task_status(db) if item["sport"] == "nfl")
        (recent,) = dashboard_read.recent_executions(db, limit=1)
        assert nfl["refresh"]["status"] == "interrupted"
        assert recent["id"] == nfl["refresh"]["execution_id"]
        assert recent["days"] == nfl["refresh"]["days"]


def test_pending_notice_and_raw_game_lookup_respect_sqlite_integer_bounds(database):
    with dashboard_read.read_database(database) as db:
        result = dashboard_read.browse_games(db, "nfl", NOW, day=DAY)
        assert result["games"] == []
        assert result["notices"] == [
            {"game_day": DAY.isoformat(), "outcome": "pending", "retained": False}
        ]
        assert dashboard_read.game_detail(db, 2**80) is None


def test_staleness_uses_schedule_interval_and_upcoming_window_is_half_open(database):
    interval = dt.timedelta(hours=3)
    scheduler_store.configure("nfl", NOW, interval_seconds=int(interval.total_seconds()))
    start = NOW - 2 * interval
    horizon = NOW + dt.timedelta(days=7)
    sunny = WeatherReport("Sunny", 60, 0, 0)
    save(
        DAY,
        observation(game("past", kickoff=NOW - dt.timedelta(microseconds=1)), sunny),
        observation(game("start", kickoff=NOW), sunny, observed_at=start),
        observation(
            game("older", kickoff=NOW),
            sunny,
            observed_at=start - dt.timedelta(microseconds=1),
        ),
    )
    save(
        horizon.date(),
        observation(game("inside", kickoff=horizon - dt.timedelta(microseconds=1)), sunny),
        observation(game("end", kickoff=horizon), sunny),
    )

    with dashboard_read.read_database(database) as db:
        rows = dashboard_read.browse_games(db, "nfl", NOW)["games"]

    assert [(row["home_team"], row["stale"]) for row in rows] == [
        ("start", False),
        ("older", True),
        ("inside", False),
    ]


def test_upcoming_uncollected_dates_are_reported_when_a_slate_already_exists(database):
    save(DAY)
    with dashboard_read.read_database(database) as db:
        result = dashboard_read.browse_games(db, "nfl", NOW)
    assert result["pending_days"][0] == DAY + dt.timedelta(days=1)
    assert DAY not in result["pending_days"]


def test_game_detail_uses_the_sport_interval_to_report_staleness(database):
    scheduler_store.configure("nfl", NOW, interval_seconds=3600)
    save(
        DAY,
        observation(
            game("stale"),
            WeatherReport("Rain", 52, 15, 70),
            observed_at=NOW - dt.timedelta(hours=3),
        ),
    )
    with dashboard_read.read_database(database) as db:
        detail = dashboard_read.game_detail(db, 1, now=NOW)
    assert detail is not None
    assert detail["observation"]["stale"] is True
    assert detail["observation"]["forecast_age_seconds"] == 3 * 3600
