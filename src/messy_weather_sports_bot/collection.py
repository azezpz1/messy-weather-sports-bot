"""Best-effort SQLite history for the bots' normal schedule and weather collection."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection, create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from messy_weather_sports_bot import state
from messy_weather_sports_bot.messiness import GameWeather
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.weather import WeatherReport

logger = logging.getLogger(__name__)

DB_PATH_ENV_VAR = "MESSY_WEATHER_DB_PATH"
_MIGRATION_LOCATION = "messy_weather_sports_bot:migrations"
RunOutcome = Literal["complete", "partial", "schedule_unavailable", "venue_table_unavailable"]
GameOutcome = Literal["skipped", "venue_unavailable", "forecast_unavailable", "evaluated"]


@dataclass(frozen=True)
class GameObservation:
    game: Game
    outcome: GameOutcome
    detail: str | None = None
    forecasts: Sequence[WeatherReport] = ()
    result: GameWeather | None = None
    alerts_error_type: str | None = None
    observed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


def forecast_db_path() -> Path:
    override = os.environ.get(DB_PATH_ENV_VAR)
    return Path(override) if override else state.default_state_dir() / "forecast-history.sqlite3"


@contextmanager
def forecast_db_transaction() -> Generator[Connection]:
    """Open and lock the database before inspecting or changing the schema."""
    path = forecast_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        connect_args={"timeout": 0.5},
        poolclass=NullPool,
    )
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys = ON")
        # Take the write lock before Alembic reads the revision so concurrent
        # runs can't both try to initialize or upgrade the same database.
        db.exec_driver_sql("BEGIN IMMEDIATE")
        yield db


def migrate_forecast_db(db: Connection) -> None:
    config = Config(attributes={"connection": db})
    config.set_main_option("script_location", _MIGRATION_LOCATION.replace("%", "%%"))
    command.upgrade(config, "head")


def record_collection(
    sport: str,
    game_day: dt.date,
    started_at: dt.datetime,
    outcome: RunOutcome,
    games_found: int | None,
    observations: Sequence[GameObservation] = (),
    *,
    rankings_unavailable: bool = False,
    error_type: str | None = None,
) -> None:
    """Append one collection run atomically, without affecting its publishing outcome."""
    try:
        with forecast_db_transaction() as db:
            migrate_forecast_db(db)
            insert_collection(
                db,
                sport,
                game_day,
                started_at,
                outcome,
                games_found,
                observations,
                rankings_unavailable=rankings_unavailable,
                error_type=error_type,
            )
    except Exception as exc:  # isolate storage and serialization failures from publishing
        cause = exc.orig if isinstance(exc, DBAPIError) else exc
        error = getattr(cause, "sqlite_errorname", type(cause).__name__)
        logger.warning("Could not save forecast history (%s).", error)


def insert_collection(
    db: Connection,
    sport: str,
    game_day: dt.date,
    started_at: dt.datetime,
    outcome: RunOutcome,
    games_found: int | None,
    observations: Sequence[GameObservation] = (),
    *,
    rankings_unavailable: bool = False,
    error_type: str | None = None,
    execution_id: int | None = None,
) -> None:
    """Write one day's result inside the caller's transaction."""
    run = db.exec_driver_sql(
        """
        INSERT INTO collection_runs (
            sport, game_day, started_at, collected_at, outcome, games_found,
            rankings_unavailable, error_type, execution_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sport,
            game_day.isoformat(),
            _timestamp(started_at),
            _timestamp(dt.datetime.now(dt.UTC)),
            outcome,
            games_found,
            rankings_unavailable,
            error_type,
            execution_id,
        ),
    )
    for observation in observations:
        _insert_observation(db, run.lastrowid, sport, game_day, observation)


def _timestamp(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat() if value is not None else None


def _insert_observation(
    db: Connection,
    run_id: int | None,
    sport: str,
    game_day: dt.date,
    observation: GameObservation,
) -> None:
    game = observation.game
    stadium = game.stadium
    result = observation.result
    alert = result.alert if result is not None else None
    # A stable ESPN ID survives changes to the kickoff, teams or venue. An ID-less
    # event can only be matched to the same matchup within the same game day.
    key = (
        f"espn:{game.espn_event_id}"
        if game.espn_event_id
        else "matchup:" + json.dumps([game_day.isoformat(), game.away_team, game.home_team])
    )
    db.exec_driver_sql(
        """
        INSERT INTO games (sport, source_key, espn_event_id) VALUES (?, ?, ?)
        ON CONFLICT (sport, source_key) DO NOTHING
        """,
        (sport, key, game.espn_event_id),
    )
    snapshot = db.exec_driver_sql(
        """
        INSERT INTO game_observations (
            run_id, game_id, observed_at, home_team, away_team, kickoff,
            espn_venue_id, espn_venue_name, stadium_name, stadium_venue_id,
            latitude, longitude, is_covered, neutral_site, home_rank, away_rank,
            outcome, detail, alerts_error_type, is_messy, condition, score,
            alert_event, alert_severity, alert_onset, alert_ends
        ) VALUES (?, (SELECT id FROM games WHERE sport = ? AND source_key = ?),
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            sport,
            key,
            _timestamp(observation.observed_at),
            game.home_team,
            game.away_team,
            _timestamp(game.kickoff),
            game.venue_id or None,
            game.venue_name or None,
            stadium.name if stadium else None,
            stadium.venue_id if stadium else None,
            stadium.latitude if stadium else None,
            stadium.longitude if stadium else None,
            stadium.is_covered if stadium else None,
            game.neutral_site,
            game.home_rank,
            game.away_rank,
            observation.outcome,
            observation.detail,
            observation.alerts_error_type,
            result.is_messy if result else None,
            result.condition.value if result else None,
            result.score if result else None,
            alert.event if alert else None,
            alert.severity.name.lower() if alert else None,
            _timestamp(alert.onset) if alert else None,
            _timestamp(alert.ends) if alert else None,
        ),
    )
    if not observation.forecasts:
        return
    db.exec_driver_sql(
        """
        INSERT INTO forecast_periods (
            observation_id, period_index, starts_at, ends_at, short_forecast,
            temperature_f, wind_speed_mph, precipitation_probability, relative_humidity,
            feels_like_f, selected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot.lastrowid,
                index,
                weather.starts_at,
                weather.ends_at,
                weather.short_forecast,
                weather.temperature_f,
                weather.wind_speed_mph,
                weather.precipitation_probability,
                weather.relative_humidity,
                weather.feels_like_f,
                result is not None and weather is result.weather,
            )
            for index, weather in enumerate(observation.forecasts)
        ],
    )
