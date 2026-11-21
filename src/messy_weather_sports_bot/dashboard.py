"""Serve the private forecast dashboard behind a LAN HTTPS proxy."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from flask import Flask, Response, abort, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy.exc import SQLAlchemyError
from waitress import serve

from messy_weather_sports_bot import dashboard_read, scheduler_store
from messy_weather_sports_bot.collection import forecast_db_path
from messy_weather_sports_bot.sports import SPORTS

logger = logging.getLogger(__name__)

SECRET_ENV = "MESSY_WEATHER_DASHBOARD_SECRET"
HOST_ENV = "MESSY_WEATHER_DASHBOARD_HOST"
DEFAULT_SOCKET = Path("/run/messy-weather-dashboard/dashboard.sock")
MAX_REQUEST_BODY_SIZE = 8 * 1024
MAX_HOURS = scheduler_store.MAX_INTERVAL_SECONDS // 3600
SPORT_NAMES = {"nfl": "NFL", "cfb": "Top 25 CFB"}
PRECIP_LABELS = {
    "rain": "Rain",
    "snow": "Snow",
    "ice": "Ice or wintry mix",
    "thunderstorms": "Thunderstorms",
}
WEATHER_LABELS = {
    "messy": "Messy",
    "pleasant": "Pleasant",
    "covered": "Covered venue",
    "venue_unavailable": "Venue unavailable",
    "forecast_unavailable": "Forecast unavailable",
    "forecast_horizon": "Beyond forecast horizon",
    "pending": "Awaiting collection",
}
NOTICE_LABELS = {
    "rankings_unavailable": "Rankings could not be verified",
    "schedule_unavailable": "Schedule could not be verified",
    "venue_table_unavailable": "Venue data could not be loaded",
    "pending": "Waiting for the first collection",
}
TABLE_LABELS = {
    "games": "Games",
    "game_observations": "Game observations",
    "forecast_periods": "Hourly forecast periods",
    "collection_runs": "Collection days",
    "collection_schedules": "Collection schedules",
    "collection_executions": "Collection executions",
    "collection_refresh_requests": "Refresh requests",
}
RELATED_COLUMNS = {
    "game_id": "games",
    "run_id": "collection_runs",
    "observation_id": "game_observations",
    "execution_id": "collection_executions",
}


def _sport(slug: str) -> str:
    if slug not in SPORTS:
        abort(404)
    return slug


def _timestamp(value: str | None, sport: str | None = None) -> str:
    if not value:
        return "—"
    try:
        timestamp = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        return value
    timezone = SPORTS[sport].game_day_timezone if sport else dt.UTC
    return timestamp.astimezone(timezone).strftime("%a, %b %d · %I:%M %p %Z")


def _interval_hours(seconds: int) -> str:
    return f"{seconds / 3600:g}"


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _raw_row_url(table: str, row) -> str:
    if table == "collection_schedules":
        return url_for("raw_row", table=table, key=row["sport"])
    if table == "forecast_periods":
        return url_for("raw_row", table=table, key=row["observation_id"], index=row["period_index"])
    return url_for("raw_row", table=table, key=row["id"])


def create_app(secret_key: str, *, trusted_host: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=MAX_REQUEST_BODY_SIZE,
        TRUSTED_HOSTS=[trusted_host] if trusted_host else None,
    )
    CSRFProtect(app)
    app.jinja_env.filters.update(timestamp=_timestamp, interval_hours=_interval_hours, age=_age)
    # Jinja's globals accept arbitrary template values.
    cast(dict[str, object], app.jinja_env.globals).update(
        sport_names=SPORT_NAMES,
        precip_labels=PRECIP_LABELS,
        weather_labels=WEATHER_LABELS,
        notice_labels=NOTICE_LABELS,
        table_labels=TABLE_LABELS,
        related_columns=RELATED_COLUMNS,
        raw_row_url=_raw_row_url,
    )

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(_error: CSRFError) -> tuple[str, int]:
        return render_template("error.html", message="Reload the page and try again."), 400

    @app.errorhandler(sqlite3.Error)
    @app.errorhandler(SQLAlchemyError)
    def database_error(error: Exception) -> tuple[str, int]:
        logger.warning("Dashboard storage unavailable (%s)", type(error).__name__)
        return render_template(
            "error.html",
            message="Forecast storage is unavailable. Check the collector and try again.",
        ), 503

    @app.get("/")
    def home() -> ResponseReturnValue:
        return redirect(url_for("games", sport="nfl"))

    @app.get("/games/<sport>")
    def games(sport: str) -> str:
        _sport(sport)
        sort = request.args.get("sort", "score")
        precipitation = request.args.get("precipitation") or None
        day_input = request.args.get("day", "")
        if sort not in {"score", "kickoff"} or (
            precipitation is not None and precipitation not in PRECIP_LABELS
        ):
            abort(400)
        try:
            day = dt.date.fromisoformat(day_input) if day_input else None
        except ValueError:
            abort(400)
        messy = request.args.get("messy") == "1"
        with dashboard_read.read_database(forecast_db_path()) as db:
            result = dashboard_read.browse_games(
                db,
                sport,
                dt.datetime.now(dt.UTC),
                day=day,
                precipitation=precipitation,
                messy_only=messy,
                sort=sort,
            )
        return render_template(
            "games.html",
            sport=sport,
            day=day,
            sort=sort,
            precipitation=precipitation,
            messy=messy,
            **result,
        )

    @app.get("/game/<int:game_id>")
    def game(game_id: int) -> str:
        observation = request.args.get("observation", type=int)
        if "observation" in request.args and observation is None:
            abort(400)
        with dashboard_read.read_database(forecast_db_path()) as db:
            detail = dashboard_read.game_detail(
                db, game_id, observation, now=dt.datetime.now(dt.UTC)
            )
        if detail is None:
            abort(404)
        return render_template("game.html", sport=detail["game"]["sport"], **detail)

    def task_page(
        *, message: str | None = None, status: int = 200, editing: str | None = None
    ) -> tuple[str, int]:
        with dashboard_read.read_database(forecast_db_path()) as db:
            schedules = dashboard_read.task_status(db)
            executions = dashboard_read.recent_executions(db)
        return render_template(
            "tasks.html",
            schedules=schedules,
            executions=executions,
            message=message,
            max_hours=MAX_HOURS,
            editing=editing,
        ), status

    @app.get("/admin/tasks")
    def tasks() -> tuple[str, int]:
        return task_page()

    @app.get("/admin/tasks/status")
    def task_status() -> str:
        with dashboard_read.read_database(forecast_db_path()) as db:
            schedules = dashboard_read.task_status(db)
            executions = dashboard_read.recent_executions(db)
        return render_template("task_status.html", schedules=schedules, executions=executions)

    @app.post("/admin/tasks/<sport>/schedule")
    def edit_schedule(sport: str) -> ResponseReturnValue:
        _sport(sport)
        hours = request.form.get("hours", type=int)
        revision = request.form.get("revision", type=int)
        enabled = request.form.get("enabled")
        if hours is None or not 1 <= hours <= MAX_HOURS or revision is None or revision < 0:
            return task_page(
                message=f"Enter an interval from 1 to {MAX_HOURS} whole hours.",
                status=400,
                editing=sport,
            )
        if enabled not in {"0", "1"}:
            return task_page(message="Select Enabled or Paused.", status=400, editing=sport)
        try:
            scheduler_store.configure(
                sport,
                dt.datetime.now(dt.UTC),
                interval_seconds=hours * 3600,
                enabled=enabled == "1",
                expected_revision=revision,
            )
        except scheduler_store.ScheduleConflict:
            return task_page(
                message="This schedule changed while you were editing. Review it and try again.",
                status=409,
                editing=sport,
            )
        return redirect(url_for("tasks") + f"#{sport}-schedule", code=303)

    @app.post("/admin/tasks/<sport>/refresh")
    def refresh(sport: str) -> ResponseReturnValue:
        _sport(sport)
        scheduler_store.request_refresh(sport, dt.datetime.now(dt.UTC))
        return redirect(url_for("tasks") + "#run-status", code=303)

    @app.get("/admin/data")
    def data() -> str:
        return render_template("data.html")

    @app.get("/admin/data/<table>")
    def raw_table(table: str) -> str:
        if table not in dashboard_read.RAW_TABLES:
            abort(404)
        page = request.args.get("page", 1, type=int)
        sort = request.args.get("sort") or None
        with dashboard_read.read_database(forecast_db_path()) as db:
            try:
                result = dashboard_read.browse_table(
                    db,
                    table,
                    page=page,
                    sort=sort,
                    descending=request.args.get("direction") == "desc",
                )
            except ValueError:
                abort(400)
        return render_template("raw_table.html", **result)

    @app.get("/admin/data/<table>/<key>")
    def raw_row(table: str, key: str) -> str:
        if table not in dashboard_read.RAW_TABLES:
            abort(404)
        index = request.args.get("index", type=int)
        with dashboard_read.read_database(forecast_db_path()) as db:
            row = dashboard_read.read_row(db, table, key, index)
        if row is None:
            abort(404)
        return render_template("raw_row.html", table=table, row=row)

    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    if not args.socket.is_absolute():
        parser.error("--socket must be an absolute path")
    secret = os.environ.get(SECRET_ENV, "")
    if len(secret) < 32:
        parser.error(f"Set {SECRET_ENV} to a random value of at least 32 characters")
    host = os.environ.get(HOST_ENV)
    if not host:
        parser.error(f"Set {HOST_ENV} to the HTTPS proxy's hostname or IP address")
    app = create_app(secret, trusted_host=host)
    # Only the proxy group may connect. Waitress calls Unix socket peers "localhost".
    serve(
        app,
        unix_socket=str(args.socket),
        unix_socket_perms="660",
        trusted_proxy="localhost",
        trusted_proxy_headers={"x-forwarded-proto"},
        max_request_body_size=MAX_REQUEST_BODY_SIZE,
    )
    return 0
