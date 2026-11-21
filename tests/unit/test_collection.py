import datetime as dt
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config

from messy_weather_sports_bot import collection
from messy_weather_sports_bot.cfb import CFB, cfb_catalog
from messy_weather_sports_bot.cfb import main as cfb_main
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.nfl import main as nfl_main
from messy_weather_sports_bot.pipeline import EXIT_NOTHING_POSTED, EXIT_OK, EXIT_PARTIAL
from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.stadiums import stadium_for_team
from messy_weather_sports_bot.weather import nws_point
from tests.support.espn import cfb_event, espn_event
from tests.support.nws import (
    clear_period_response,
    forecast_response,
    mock_hourly_forecast,
    rainy_period_response,
)

DAY = dt.date(2026, 1, 18)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: DAY)
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("CFB_HEALTHCHECK_URL", raising=False)


@pytest.fixture
def poster(monkeypatch: pytest.MonkeyPatch) -> Mock:
    fake = Mock(spec=SocialMediaPoster)
    fake.post_thread.return_value = [PostRef(id="post", root_id="post")]
    monkeypatch.setattr("messy_weather_sports_bot.pipeline.build_posters", lambda *args: [fake])
    return fake


@respx.mock
def test_reruns_keep_clear_and_messy_snapshots_even_after_posting(
    tmp_path: Path, poster: Mock
) -> None:
    def slate(kickoff: str) -> dict:
        return {
            "events": [
                dict(espn_event("GB", "CHI", kickoff=kickoff), id="401"),
                espn_event("BUF", "NE"),
                espn_event("MIN", "DET"),
            ]
        }

    scoreboard = respx.get(NFL.scoreboard_url)
    scoreboard.respond(json=slate("2026-01-18T18:00Z"))
    gb = stadium_for_team("GB")
    buf = stadium_for_team("BUF")
    rain = forecast_response("Rain", 50, "5 mph", 90)
    mock_hourly_forecast(gb.latitude, gb.longitude, response=rain)
    mock_hourly_forecast(buf.latitude, buf.longitude, response=clear_period_response())

    assert nfl_main([]) == EXIT_OK
    scoreboard.respond(json=slate("2026-01-18T19:00Z"))
    periods = rain.json()["properties"]["periods"]
    periods[0].update(endTime="2026-01-18T15:00:00-05:00")
    periods.append(
        {
            **periods[0],
            "startTime": "2026-01-18T15:00:00-05:00",
            "endTime": "2026-01-18T18:30:00-05:00",
            "shortForecast": "Heavy Rain",
        }
    )
    mock_hourly_forecast(
        gb.latitude,
        gb.longitude,
        response=httpx.Response(200, json={"properties": {"periods": periods}}),
    )

    assert nfl_main([]) == EXIT_OK
    poster.post_thread.assert_called_once()
    with closing(sqlite3.connect(tmp_path / "forecast-history.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM games").fetchone() == (3,)
        assert db.execute("SELECT outcome FROM collection_runs ORDER BY id").fetchall() == [
            ("complete",),
            ("complete",),
        ]
        assert db.execute(
            """
            SELECT kickoff, is_messy, condition, score, stadium_name, latitude
            FROM game_observations WHERE home_team = 'GB' ORDER BY id
            """
        ).fetchall() == [
            ("2026-01-18T18:00:00+00:00", 1, "rain", 111.0, gb.name, gb.latitude),
            ("2026-01-18T19:00:00+00:00", 1, "rain", 133.5, gb.name, gb.latitude),
        ]
        assert db.execute(
            """
            SELECT o.is_messy, o.condition, p.short_forecast, p.selected, p.starts_at
            FROM game_observations AS o JOIN forecast_periods AS p ON o.id = p.observation_id
            ORDER BY o.id, p.period_index
            """
        ).fetchall() == [
            (1, "rain", "Rain", 1, "2026-01-18T13:00:00-05:00"),
            (0, "clear", "Sunny", 1, "2026-01-18T13:00:00-05:00"),
            (1, "rain", "Rain", 0, "2026-01-18T13:00:00-05:00"),
            (1, "rain", "Heavy Rain", 1, "2026-01-18T15:00:00-05:00"),
            (0, "clear", "Sunny", 1, "2026-01-18T13:00:00-05:00"),
        ]
        times = db.execute(
            "SELECT observed_at FROM game_observations WHERE home_team = 'GB' ORDER BY id"
        ).fetchall()
        assert dt.datetime.fromisoformat(times[0][0]) <= dt.datetime.fromisoformat(times[1][0])
        assert db.execute(
            "SELECT outcome, is_messy FROM game_observations WHERE home_team = 'MIN'"
        ).fetchall() == [("skipped", None), ("skipped", None)]


@respx.mock
def test_shared_default_db_records_cfb_partial_collection_and_nfl_schedule_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poster: Mock
) -> None:
    monkeypatch.delenv(collection.DB_PATH_ENV_VAR)
    events = [
        cfb_event("Ohio State", "Minnesota", home_rank=1),
        cfb_event("Pitt", "Boston College", venue_id="3752", away_rank=16),
        cfb_event("Unranked", "Another unranked team"),
    ]
    respx.get(CFB.scoreboard_url).respond(json={"events": events})
    ohio = cfb_catalog().for_venue("3861", "")
    pitt = cfb_catalog().for_venue("3752", "")
    assert ohio is not None and pitt is not None
    mock_hourly_forecast(ohio.latitude, ohio.longitude, response=rainy_period_response())
    mock_hourly_forecast(pitt.latitude, pitt.longitude, response=httpx.Response(404))
    point = nws_point(ohio.latitude, ohio.longitude)
    respx.get(f"https://api.weather.gov/alerts/active?point={point}").respond(500)
    respx.get(NFL.scoreboard_url).respond(500)

    assert cfb_main([]) == EXIT_PARTIAL
    assert nfl_main([]) == EXIT_NOTHING_POSTED
    poster.post_thread.assert_called_once()
    with closing(sqlite3.connect(tmp_path / "state" / "forecast-history.sqlite3")) as db:
        assert db.execute(
            "SELECT sport, outcome, games_found, error_type FROM collection_runs ORDER BY id"
        ).fetchall() == [
            ("cfb", "partial", 3, None),
            ("nfl", "schedule_unavailable", None, "HTTPStatusError"),
        ]
        assert db.execute(
            """
            SELECT home_team, home_rank, away_rank, outcome, alerts_error_type, is_messy
            FROM game_observations ORDER BY id
            """
        ).fetchall() == [
            ("Ohio State", 1, None, "evaluated", "HTTPStatusError", 1),
            ("Pitt", None, 16, "forecast_unavailable", None, None),
        ]


@respx.mock
def test_dry_runs_and_date_previews_do_not_create_history(tmp_path: Path) -> None:
    respx.get(NFL.scoreboard_url).respond(json={"events": []})
    assert nfl_main(["--dry-run"]) == EXIT_OK
    assert nfl_main(["--date", "2026-01-19"]) == EXIT_OK
    assert not (tmp_path / "forecast-history.sqlite3").exists()
    assert not (tmp_path / "state").exists()


@respx.mock
def test_unusable_database_does_not_prevent_posting(
    tmp_path: Path, poster: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "forecast-history.sqlite3").write_text("not a database")
    respx.get(NFL.scoreboard_url).respond(json={"events": [espn_event("GB", "CHI")]})
    stadium = stadium_for_team("GB")
    mock_hourly_forecast(stadium.latitude, stadium.longitude, response=rainy_period_response())

    assert nfl_main([]) == EXIT_OK
    poster.post_thread.assert_called_once()
    assert "Could not save forecast history" in caplog.text
    assert (tmp_path / "forecast-history.sqlite3").read_text() == "not a database"
    assert (tmp_path / "state" / f"{DAY}.json").is_file()


def test_schema_upgrade_keeps_history_and_rolls_back_a_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = dt.datetime.now(dt.UTC)
    path = tmp_path / "forecast-history.sqlite3"
    collection.record_collection("nfl", DAY, started, "complete", 0)

    migrations = tmp_path / "migrations"
    shutil.copytree(Path(collection.__file__).with_name("migrations"), migrations)
    monkeypatch.setattr(collection, "_MIGRATION_LOCATION", str(migrations))
    revision = migrations / "versions" / "0003_source.py"
    script = """from alembic import op

revision = "0003_source"
down_revision = "0002_collection_scheduler"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE collection_runs ADD COLUMN source TEXT DEFAULT 'espn'")
"""
    revision.write_text(script + '    op.execute("invalid SQL")\n')
    collection.record_collection("cfb", DAY, started, "complete", 0)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_collection_scheduler",
        )
        assert db.execute("SELECT sport FROM collection_runs").fetchall() == [("nfl",)]
        assert "source" not in {
            column[1] for column in db.execute("PRAGMA table_info(collection_runs)")
        }

    revision.write_text(script)
    collection.record_collection("cfb", DAY, started, "complete", 0)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003_source",)
        assert db.execute("SELECT sport, source FROM collection_runs ORDER BY id").fetchall() == [
            ("nfl", "espn"),
            ("cfb", "espn"),
        ]


@pytest.mark.parametrize("legacy", [False, True])
def test_scheduler_migration_preserves_previous_forecast_history(
    tmp_path: Path, legacy: bool
) -> None:
    started = dt.datetime.now(dt.UTC)
    path = tmp_path / "forecast-history.sqlite3"
    game = Game("GB", "CHI", started, stadium_for_team("GB"), espn_event_id="123")
    observation = collection.GameObservation(
        game, "evaluated", forecasts=(collection.WeatherReport("Rain", 42, 5, 90),)
    )
    collection.record_collection("nfl", DAY, started, "complete", 1, (observation,))
    with collection.forecast_db_transaction() as db:
        config = Config(attributes={"connection": db})
        config.set_main_option("script_location", collection._MIGRATION_LOCATION)
        command.downgrade(config, "0001_forecast_history")
        if legacy:
            db.exec_driver_sql("DROP TABLE alembic_version")
            db.exec_driver_sql("PRAGMA user_version = 1")

    collection.record_collection("cfb", DAY, started, "complete", 0)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_collection_scheduler",
        )
        assert db.execute(
            "SELECT sport, execution_id FROM collection_runs ORDER BY id"
        ).fetchall() == [
            ("nfl", None),
            ("cfb", None),
        ]
        assert db.execute("SELECT espn_event_id FROM games").fetchall() == [("123",)]
        assert db.execute("SELECT home_team, away_team FROM game_observations").fetchall() == [
            ("GB", "CHI")
        ]
        assert db.execute("SELECT short_forecast FROM forecast_periods").fetchall() == [("Rain",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
