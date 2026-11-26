"""Read saved forecasts and collection state for the LAN dashboard."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections import defaultdict
from typing import Any

from messy_weather_sports_bot.collection import read_database as read_database
from messy_weather_sports_bot.collector import FORECAST_WINDOW
from messy_weather_sports_bot.scheduler_store import DEFAULT_INTERVAL_SECONDS
from messy_weather_sports_bot.sports import SPORTS

PAGE_SIZE = 50
_SQLITE_MAX_INT = 2**63 - 1
RAW_TABLES = {
    "games": ("id", "sport", "source_key", "espn_event_id"),
    "game_observations": (
        "id",
        "run_id",
        "game_id",
        "observed_at",
        "home_team",
        "away_team",
        "kickoff",
        "espn_venue_id",
        "espn_venue_name",
        "stadium_name",
        "stadium_venue_id",
        "latitude",
        "longitude",
        "is_covered",
        "neutral_site",
        "home_rank",
        "away_rank",
        "outcome",
        "detail",
        "alerts_error_type",
        "is_messy",
        "condition",
        "score",
        "alert_event",
        "alert_severity",
        "alert_onset",
        "alert_ends",
    ),
    "forecast_periods": (
        "observation_id",
        "period_index",
        "starts_at",
        "ends_at",
        "short_forecast",
        "temperature_f",
        "wind_speed_mph",
        "precipitation_probability",
        "relative_humidity",
        "feels_like_f",
        "selected",
    ),
    "collection_runs": (
        "id",
        "sport",
        "game_day",
        "started_at",
        "collected_at",
        "outcome",
        "games_found",
        "rankings_unavailable",
        "error_type",
        "execution_id",
        "publication_game_count",
    ),
    "collection_schedules": ("sport", "interval_seconds", "enabled", "next_due", "revision"),
    "collection_executions": (
        "id",
        "sport",
        "trigger",
        "scheduled_for",
        "started_at",
        "finished_at",
        "outcome",
        "failure_summary",
    ),
    "collection_refresh_requests": ("id", "sport", "requested_at", "execution_id"),
}
_RAW_TABLE_IDENTIFIERS = {table: f'"{table}"' for table in RAW_TABLES}
PRECIPITATION = ("rain", "snow", "ice", "thunderstorms")
_ICE = re.compile(
    r"\b(?:freezing[ -]+(?:rain(?: showers?)?|drizzle|showers?)|sleet|"
    r"ice(?: pellets?)?|wintery mix|wintry mix)\b"
)
_SNOW = re.compile(r"\b(?:snow(?:fall| showers?)?|blizzards?|flurr(?:y|ies))\b")
_SNOW_SHOWERS = re.compile(r"\bsnow showers?\b")
_RAIN = re.compile(r"\b(?:rain(?:fall)?|showers?|drizzle)\b")
_THUNDERSTORMS = re.compile(r"\b(?:thunderstorms?|t-storms?)\b")

# Views combine stored SQLite values with derived display values and child rows.
Row = dict[str, Any]


def _rows(db: sqlite3.Connection, sql: str, parameters: tuple = ()) -> list[Row]:
    return [dict(row) for row in db.execute(sql, parameters)]


def _periods(db: sqlite3.Connection, observation_ids: list[int]) -> dict[int, list[Row]]:
    periods: dict[int, list[Row]] = defaultdict(list)
    if observation_ids:
        parameters = ", ".join("?" for _ in observation_ids)
        for row in _rows(
            db,
            f"""
            SELECT * FROM forecast_periods WHERE observation_id IN ({parameters})
            ORDER BY observation_id, period_index
            """,
            tuple(observation_ids),
        ):
            periods[row["observation_id"]].append(row)
    return periods


def _precipitation(periods: list[Row]) -> tuple[str, ...]:
    tags = set()
    for period in periods:
        forecast = period["short_forecast"].lower()
        if _ICE.search(forecast):
            tags.add("ice")
        if _SNOW.search(forecast):
            tags.add("snow")
        if _THUNDERSTORMS.search(forecast):
            tags.add("thunderstorms")
        if _RAIN.search(_SNOW_SHOWERS.sub("", _ICE.sub("", forecast))):
            tags.add("rain")
    return tuple(tag for tag in PRECIPITATION if tag in tags)


def _weather_status(observation: Row) -> str:
    if observation["is_covered"]:
        return "covered"
    if observation["outcome"] in {"skipped", "venue_unavailable"}:
        return "venue_unavailable"
    if observation["outcome"] == "forecast_unavailable":
        if observation["detail"] == "forecast_horizon":
            return "forecast_horizon"
        return "forecast_unavailable"
    if observation["is_messy"] is None:
        return "pending"
    return "messy" if observation["is_messy"] else "pleasant"


def _forecast_summary(
    observation: Row, periods: list[Row], now: dt.datetime | None, interval: int
) -> Row:
    age = None
    if periods and now is not None:
        age = max(0, (now - dt.datetime.fromisoformat(observation["observed_at"])).total_seconds())
    return {
        **observation,
        "selected_forecast": next((period for period in periods if period["selected"]), None),
        "precipitation": _precipitation(periods),
        "weather_status": _weather_status(observation),
        "partial_coverage": observation["detail"] == "partial_forecast_coverage",
        "stale": age is not None and age > 2 * interval,
        "forecast_age_seconds": int(age) if age is not None else None,
    }


def _collection_interval(db: sqlite3.Connection, sport: str) -> int:
    interval = db.execute(
        "SELECT interval_seconds FROM collection_schedules WHERE sport = ?", (sport,)
    ).fetchone()
    return interval[0] if interval else DEFAULT_INTERVAL_SECONDS


def browse_games(
    db: sqlite3.Connection,
    sport: str,
    now: dt.datetime,
    *,
    day: dt.date | None = None,
    precipitation: str | None = None,
    messy_only: bool = False,
    sort: str = "score",
) -> Row:
    """Return the current verified independent slate and any date-level failures."""
    if sort not in {"score", "kickoff"} or (
        precipitation is not None and precipitation not in PRECIPITATION
    ):
        raise ValueError("Unknown game sort or precipitation filter")

    config = SPORTS[sport]
    end = now + FORECAST_WINDOW
    first_day = config.game_day_for(now)
    last_day = config.game_day_for(end - dt.timedelta(microseconds=1))
    days = [
        first_day + dt.timedelta(days=offset) for offset in range((last_day - first_day).days + 1)
    ]
    slates = _rows(
        db,
        """
        WITH runs AS (
            SELECT id, game_day, outcome, rankings_unavailable
            FROM collection_runs
            WHERE sport = ? AND game_day BETWEEN ? AND ? AND execution_id IS NOT NULL
        )
        SELECT latest.game_day, latest.outcome, latest.rankings_unavailable, verified.run_id
        FROM runs AS latest
        JOIN (SELECT MAX(id) AS id FROM runs GROUP BY game_day) AS attempt ON attempt.id = latest.id
        LEFT JOIN (
            SELECT game_day, MAX(id) AS run_id FROM runs
            WHERE rankings_unavailable = 0 AND outcome IN ('complete', 'partial')
            GROUP BY game_day
        ) AS verified ON verified.game_day = latest.game_day
        ORDER BY latest.game_day
        """,
        (sport, first_day.isoformat(), last_day.isoformat()),
    )
    visible_slates = [
        slate for slate in slates if day is None or slate["game_day"] == day.isoformat()
    ]
    notices = [
        {
            "game_day": slate["game_day"],
            "outcome": "rankings_unavailable"
            if slate["rankings_unavailable"]
            else slate["outcome"],
            "retained": slate["run_id"] is not None,
        }
        for slate in visible_slates
        if slate["rankings_unavailable"] or slate["outcome"] not in {"complete", "partial"}
    ]
    if not visible_slates and (day is None or day in days):
        notices.append(
            {"game_day": day.isoformat() if day else None, "outcome": "pending", "retained": False}
        )

    observations = _current_observations(db, visible_slates)
    interval_seconds = _collection_interval(db, sport)
    upcoming = [
        observation
        for observation in observations
        if now <= dt.datetime.fromisoformat(observation["kickoff"]) < end
    ]
    periods = _periods(db, [observation["id"] for observation in upcoming])
    games = [
        _forecast_summary(observation, periods.get(observation["id"], []), now, interval_seconds)
        for observation in upcoming
    ]
    filtered = [
        game
        for game in games
        if (precipitation is None or precipitation in game["precipitation"])
        and (not messy_only or game["is_messy"])
    ]
    filtered.sort(key=lambda game: (dt.datetime.fromisoformat(game["kickoff"]), game["game_id"]))
    if sort == "score":
        filtered.sort(key=lambda game: (game["score"] is None, -(game["score"] or 0)))
    recorded_days = {slate["game_day"] for slate in slates}
    pending_days = (
        [date for date in days if date.isoformat() not in recorded_days]
        if slates and day is None
        else []
    )
    return {"games": filtered, "notices": notices, "days": days, "pending_days": pending_days}


def _current_observations(db: sqlite3.Connection, slates: list[Row]) -> list[Row]:
    run_ids = tuple(slate["run_id"] for slate in slates if slate["run_id"] is not None)
    if not run_ids:
        return []
    parameters = ", ".join("?" for _ in run_ids)
    # A newer verified appearance supersedes the older one across dates, including
    # when the older day could not be refreshed or the new day subsequently cleared.
    return _rows(
        db,
        f"""
        SELECT o.*, r.sport, r.game_day
        FROM game_observations AS o JOIN collection_runs AS r ON r.id = o.run_id
        WHERE o.run_id IN ({parameters}) AND NOT EXISTS (
            SELECT 1
            FROM game_observations AS newer JOIN collection_runs AS newer_run
                ON newer_run.id = newer.run_id
            WHERE newer.game_id = o.game_id
                AND (newer.run_id > o.run_id OR (newer.run_id = o.run_id AND newer.id > o.id))
                AND newer_run.execution_id IS NOT NULL
                AND newer_run.rankings_unavailable = 0
                AND newer_run.outcome IN ('complete', 'partial')
        )
        """,
        run_ids,
    )


def game_detail(
    db: sqlite3.Connection,
    game_id: int,
    observation_id: int | None = None,
    *,
    now: dt.datetime | None = None,
) -> Row | None:
    if not 0 < game_id <= _SQLITE_MAX_INT:
        return None
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        return None
    history = _rows(
        db,
        """
        SELECT o.*, r.sport, r.game_day, r.execution_id
        FROM game_observations AS o JOIN collection_runs AS r ON r.id = o.run_id
        WHERE o.game_id = ?
        ORDER BY o.observed_at DESC, o.id DESC
        """,
        (game_id,),
    )
    if observation_id is None:
        selected = history[0] if history else None
    else:
        selected = next((item for item in history if item["id"] == observation_id), None)
    if selected is None:
        return None

    periods = _periods(db, [selected["id"]]).get(selected["id"], [])
    return {
        "game": dict(game),
        "observation": _forecast_summary(
            selected, periods, now, _collection_interval(db, game["sport"])
        ),
        "periods": periods,
        "history": [{**item, "weather_status": _weather_status(item)} for item in history],
    }


def _execution_days(db: sqlite3.Connection, ids: list[int]) -> dict[int, list[Row]]:
    days: dict[int, list[Row]] = defaultdict(list)
    if ids:
        parameters = ", ".join("?" for _ in ids)
        for day in _rows(
            db,
            f"""
            SELECT * FROM collection_runs WHERE execution_id IN ({parameters})
            ORDER BY game_day, id
            """,
            tuple(ids),
        ):
            days[day["execution_id"]].append(day)
    return days


def task_status(db: sqlite3.Connection) -> list[Row]:
    schedules = _rows(
        db,
        """
        SELECT s.*, (
            SELECT MAX(e.finished_at) FROM collection_executions AS e
            WHERE e.sport = s.sport AND e.outcome = 'complete'
        ) AS last_success
        FROM collection_schedules AS s ORDER BY s.sport
        """,
    )
    refreshes = _rows(
        db,
        """
        SELECT q.*, COALESCE(e.outcome, 'queued') AS status,
               e.started_at, e.finished_at, e.failure_summary
        FROM collection_refresh_requests AS q
        LEFT JOIN collection_executions AS e ON e.id = q.execution_id
        JOIN (
            SELECT sport, MAX(id) AS id FROM collection_refresh_requests GROUP BY sport
        ) AS latest ON latest.id = q.id
        """,
    )
    days = _execution_days(db, [row["execution_id"] for row in refreshes if row["execution_id"]])
    by_sport = {
        row["sport"]: {**row, "days": days.get(row["execution_id"], [])} for row in refreshes
    }
    return [{**schedule, "refresh": by_sport.get(schedule["sport"])} for schedule in schedules]


def recent_executions(db: sqlite3.Connection, limit: int = 25) -> list[Row]:
    executions = _rows(
        db,
        "SELECT * FROM collection_executions ORDER BY id DESC LIMIT ?",
        (max(0, min(limit, 100)),),
    )
    days = _execution_days(db, [execution["id"] for execution in executions])
    return [{**execution, "days": days.get(execution["id"], [])} for execution in executions]


def _raw_table(table: str) -> tuple[str, tuple[str, ...]]:
    if table not in RAW_TABLES:
        raise ValueError("Unknown data table")
    return _RAW_TABLE_IDENTIFIERS[table], RAW_TABLES[table]


def browse_table(
    db: sqlite3.Connection,
    table: str,
    page: int = 1,
    sort: str | None = None,
    descending: bool = False,
) -> Row:
    table_identifier, columns = _raw_table(table)
    sort = columns[0] if sort is None else sort
    if sort not in columns:
        raise ValueError("Unknown data sort column")
    sort = columns[columns.index(sort)]
    direction = "DESC" if descending else "ASC"
    keys = columns[:2] if table == "forecast_periods" else columns[:1]
    order = ", ".join([f'"{sort}" {direction}', *(f'"{key}"' for key in keys if key != sort)])
    total = db.execute(f"SELECT COUNT(*) FROM {table_identifier}").fetchone()[0]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    rows = _rows(
        db,
        f"SELECT * FROM {table_identifier} ORDER BY {order} LIMIT ? OFFSET ?",
        (PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "page": page,
        "pages": pages,
        "total": total,
        "sort": sort,
        "descending": descending,
    }


def read_row(db: sqlite3.Connection, table: str, key: str, index: int | None = None) -> Row | None:
    table_identifier, columns = _raw_table(table)
    if table == "collection_schedules":
        value: str | int = key
    else:
        if not key.isascii() or not key.isdecimal() or len(key) > 19:
            return None
        value = int(key)
        if not 0 < value <= _SQLITE_MAX_INT:
            return None
    if table == "forecast_periods":
        if index is None or not 0 <= index <= _SQLITE_MAX_INT:
            return None
        row = db.execute(
            "SELECT * FROM forecast_periods WHERE observation_id = ? AND period_index = ?",
            (value, index),
        ).fetchone()
    elif index is None:
        row = db.execute(
            f'SELECT * FROM {table_identifier} WHERE "{columns[0]}" = ?', (value,)
        ).fetchone()
    else:
        return None
    return dict(row) if row is not None else None
