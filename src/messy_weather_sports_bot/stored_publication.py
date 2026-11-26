"""Build a game-day evaluation from the latest independent collection run."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections import defaultdict

from messy_weather_sports_bot.alerts import AlertSeverity, WeatherAlert
from messy_weather_sports_bot.collection import forecast_db_path, read_database
from messy_weather_sports_bot.evaluation import Evaluation
from messy_weather_sports_bot.messiness import evaluate_game
from messy_weather_sports_bot.schedule import Game, skip_reason
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.venues import StadiumInfo
from messy_weather_sports_bot.weather import WeatherReport, covers_game

logger = logging.getLogger(__name__)

# Two default collection intervals. Slower schedules still use this publication limit.
MAX_SNAPSHOT_AGE = dt.timedelta(hours=12)


class _UnusableSlate(ValueError):
    """A stored slate cannot be used, with a fixed reason suitable for logging."""


def load_stored_evaluation(
    sport: Sport, game_day: dt.date, now: dt.datetime
) -> tuple[int, Evaluation] | None:
    """Return a complete stored evaluation or fall back to an independent live fetch."""
    path = forecast_db_path()
    try:
        if not path.exists():
            logger.debug("No forecast history yet; fetching live.")
            return None
        with read_database(path) as db:
            stored = _read_slate(db, sport, game_day, now)
    except _UnusableSlate as exc:
        logger.info("Stored %s slate for %s: %s; fetching live.", sport.name, game_day, exc)
        return None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
        logger.warning("Could not read stored forecasts (%s); fetching live.", type(exc).__name__)
        return None

    logger.info("Using stored %s forecasts for %s.", sport.name, game_day)
    return stored


def _read_slate(
    db: sqlite3.Connection, sport: Sport, game_day: dt.date, now: dt.datetime
) -> tuple[int, Evaluation]:
    # The collector writes one day atomically. A newer failure or truncated slate must
    # trigger a refresh so old kickoff times, venues or rankings cannot reappear.
    run = db.execute(
        """
        SELECT id, started_at, outcome, games_found, rankings_unavailable, publication_game_count
        FROM collection_runs
        WHERE sport = ? AND game_day = ? AND execution_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (sport.slug, game_day.isoformat()),
    ).fetchone()
    if run is None:
        raise _UnusableSlate("no independent collection")
    expected_count = run["publication_game_count"]
    if (
        run["outcome"] != "complete"
        or run["rankings_unavailable"]
        or expected_count is None
        or not 0 <= expected_count <= run["games_found"]
    ):
        raise _UnusableSlate("latest collection did not verify a complete slate")

    started_at = _timestamp(run["started_at"])
    if not now - MAX_SNAPSHOT_AGE <= started_at <= now:
        raise _UnusableSlate("collection is outside the 12-hour freshness window")

    observations = db.execute(
        """
        SELECT o.*, g.espn_event_id, EXISTS (
            SELECT 1
            FROM game_observations AS newer JOIN collection_runs AS newer_run
                ON newer_run.id = newer.run_id
            WHERE newer.game_id = o.game_id AND newer.run_id > o.run_id
              AND newer_run.execution_id IS NOT NULL AND newer_run.rankings_unavailable = 0
              AND newer_run.outcome IN ('complete', 'partial')
        ) AS superseded
        FROM game_observations AS o JOIN games AS g ON g.id = o.game_id
        WHERE o.run_id = ? AND g.sport = ?
        ORDER BY o.id
        """,
        (run["id"], sport.slug),
    ).fetchall()
    if (
        len(observations) != expected_count
        or len({row["game_id"] for row in observations}) != expected_count
        or any(row["superseded"] for row in observations)
    ):
        raise _UnusableSlate("game observations are missing, duplicated or superseded")

    periods: dict[int, list[WeatherReport]] = defaultdict(list)
    for row in db.execute(
        """
        SELECT p.*
        FROM forecast_periods AS p
        JOIN game_observations AS o ON o.id = p.observation_id
        WHERE o.run_id = ?
        ORDER BY p.observation_id, p.period_index
        """,
        (run["id"],),
    ):
        periods[row["observation_id"]].append(_weather_report(row))

    evaluation = Evaluation()
    for row in observations:
        game = _game(row)
        if sport.game_day_for(game.kickoff) != game_day or (
            sport.game_filter is not None and sport.game_filter(game) is not None
        ):
            raise _UnusableSlate("game does not belong to the publication slate")
        if row["outcome"] == "skipped" and skip_reason(game) is not None:
            continue

        if (
            row["outcome"] != "evaluated"
            or row["alerts_error_type"] is not None
            or skip_reason(game) is not None
            or game.stadium is None
        ):
            raise _UnusableSlate("venue, forecast or alert data is incomplete")
        if game.kickoff < now or not started_at <= _timestamp(row["observed_at"]) <= now:
            raise _UnusableSlate("game already started or its observation timestamp is invalid")

        forecasts = periods[row["id"]]
        if row["detail"] or not covers_game(forecasts, game.kickoff, sport.game_duration):
            raise _UnusableSlate("hourly forecast does not cover the whole game")
        evaluation.evaluated.append(evaluate_game(game, forecasts, _alerts(row)))

    evaluation.outdoor_count = len(evaluation.evaluated)
    return run["games_found"], evaluation


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Stored timestamp must have a UTC offset")
    return parsed


def _game(row: sqlite3.Row) -> Game:
    stadium = None
    if row["stadium_name"] is not None:
        stadium = StadiumInfo(
            name=row["stadium_name"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            is_covered=bool(row["is_covered"]),
            venue_id=row["stadium_venue_id"],
        )
    return Game(
        home_team=row["home_team"],
        away_team=row["away_team"],
        kickoff=_timestamp(row["kickoff"]),
        stadium=stadium,
        venue_name=row["espn_venue_name"] or "",
        venue_id=row["espn_venue_id"] or "",
        unresolved_reason=row["detail"] if stadium is None else None,
        home_rank=row["home_rank"],
        away_rank=row["away_rank"],
        neutral_site=bool(row["neutral_site"]),
        espn_event_id=row["espn_event_id"],
        is_international=row["outcome"] == "skipped" and stadium is None,
    )


def _weather_report(row: sqlite3.Row) -> WeatherReport:
    return WeatherReport(
        short_forecast=row["short_forecast"],
        temperature_f=row["temperature_f"],
        wind_speed_mph=row["wind_speed_mph"],
        precipitation_probability=row["precipitation_probability"],
        relative_humidity=row["relative_humidity"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
    )


def _alerts(row: sqlite3.Row) -> list[WeatherAlert]:
    if row["alert_event"] is None:
        return []
    severity = row["alert_severity"]
    if severity is None:
        raise _UnusableSlate("alert severity is missing")
    return [
        WeatherAlert(
            event=row["alert_event"],
            severity=AlertSeverity[severity.upper()],
            onset=_timestamp(row["alert_onset"]) if row["alert_onset"] is not None else None,
            ends=_timestamp(row["alert_ends"]) if row["alert_ends"] is not None else None,
        )
    ]
