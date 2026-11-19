"""Best-effort SQLite history for the bots' normal schedule and weather collection."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from messy_weather_sports_bot import state
from messy_weather_sports_bot.messiness import GameWeather
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.weather import WeatherReport

logger = logging.getLogger(__name__)

DB_PATH_ENV_VAR = "MESSY_WEATHER_DB_PATH"
RunOutcome = Literal["complete", "partial", "schedule_unavailable", "venue_table_unavailable"]
GameOutcome = Literal["skipped", "venue_unavailable", "forecast_unavailable", "evaluated"]

# Each migration is a tuple of statements run in the same transaction as its version
# change. Append migrations here so databases from earlier releases keep their history.
_MIGRATIONS = (
    (
        """
        CREATE TABLE collection_runs (
            id INTEGER PRIMARY KEY,
            sport TEXT NOT NULL,
            game_day TEXT NOT NULL,
            started_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            games_found INTEGER,
            rankings_unavailable INTEGER NOT NULL,
            error_type TEXT
        )
        """,
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY,
            sport TEXT NOT NULL,
            source_key TEXT NOT NULL,
            espn_event_id TEXT,
            UNIQUE (sport, source_key)
        )
        """,
        """
        CREATE TABLE game_observations (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES collection_runs(id),
            game_id INTEGER NOT NULL REFERENCES games(id),
            observed_at TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff TEXT NOT NULL,
            espn_venue_id TEXT,
            espn_venue_name TEXT,
            stadium_name TEXT,
            stadium_venue_id TEXT,
            latitude REAL,
            longitude REAL,
            is_covered INTEGER,
            neutral_site INTEGER NOT NULL,
            home_rank INTEGER,
            away_rank INTEGER,
            outcome TEXT NOT NULL,
            detail TEXT,
            alerts_error_type TEXT,
            is_messy INTEGER,
            condition TEXT,
            score REAL,
            alert_event TEXT,
            alert_severity TEXT,
            alert_onset TEXT,
            alert_ends TEXT
        )
        """,
        "CREATE INDEX game_observations_by_game ON game_observations(game_id, observed_at)",
        "CREATE INDEX game_observations_by_run ON game_observations(run_id)",
        """
        CREATE TABLE forecast_periods (
            observation_id INTEGER NOT NULL REFERENCES game_observations(id),
            period_index INTEGER NOT NULL,
            starts_at TEXT,
            ends_at TEXT,
            short_forecast TEXT NOT NULL,
            temperature_f INTEGER,
            wind_speed_mph REAL NOT NULL,
            precipitation_probability INTEGER,
            relative_humidity INTEGER,
            feels_like_f REAL,
            selected INTEGER NOT NULL,
            PRIMARY KEY (observation_id, period_index)
        )
        """,
    ),
)


@dataclass(frozen=True)
class GameObservation:
    game: Game
    outcome: GameOutcome
    detail: str | None = None
    forecasts: Sequence[WeatherReport] = ()
    result: GameWeather | None = None
    alerts_error_type: str | None = None
    observed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


def _migrate(db: sqlite3.Connection) -> None:
    (version,) = db.execute("PRAGMA user_version").fetchone()
    if version > len(_MIGRATIONS):
        raise sqlite3.DatabaseError("Forecast history schema is newer than this bot supports")
    for next_version, statements in enumerate(_MIGRATIONS[version:], start=version + 1):
        for statement in statements:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {next_version}")


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
        override = os.environ.get(DB_PATH_ENV_VAR)
        path = (
            Path(override) if override else state.default_state_dir() / "forecast-history.sqlite3"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0.5)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            # Lock before reading the schema version so overlapping cron runs can't
            # both try to initialize or upgrade the same database.
            db.execute("BEGIN IMMEDIATE")
            _migrate(db)
            run = db.execute(
                """
                INSERT INTO collection_runs (
                    sport, game_day, started_at, collected_at, outcome, games_found,
                    rankings_unavailable, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            for observation in observations:
                _insert_observation(db, run.lastrowid, sport, game_day, observation)
    except Exception as exc:  # isolate storage and serialization failures from publishing
        error = getattr(exc, "sqlite_errorname", type(exc).__name__)
        logger.warning("Could not save forecast history (%s).", error)


def _timestamp(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat() if value is not None else None


def _insert_observation(
    db: sqlite3.Connection,
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
    db.execute(
        """
        INSERT INTO games (sport, source_key, espn_event_id) VALUES (?, ?, ?)
        ON CONFLICT (sport, source_key) DO NOTHING
        """,
        (sport, key, game.espn_event_id),
    )
    snapshot = db.execute(
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
    db.executemany(
        """
        INSERT INTO forecast_periods (
            observation_id, period_index, starts_at, ends_at, short_forecast,
            temperature_f, wind_speed_mph, precipitation_probability, relative_humidity,
            feels_like_f, selected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
        ),
    )
