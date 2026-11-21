"""Collect upcoming games and forecasts without running the publication pipeline."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from messy_weather_sports_bot.alerts import get_active_alerts
from messy_weather_sports_bot.collection import GameObservation, RunOutcome
from messy_weather_sports_bot.espn import EspnScoreboard, VenueTableError
from messy_weather_sports_bot.evaluation import evaluate_games, rankings_missing
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.weather import get_forecast

logger = logging.getLogger(__name__)
FORECAST_WINDOW = dt.timedelta(days=7)


@dataclass(frozen=True)
class CollectedDay:
    game_day: dt.date
    started_at: dt.datetime
    outcome: RunOutcome
    games_found: int | None = None
    observations: tuple[GameObservation, ...] = ()
    rankings_unavailable: bool = False
    error_type: str | None = None


def collect_window(sport: Sport, now: dt.datetime) -> Iterator[CollectedDay]:
    end = now + FORECAST_WINDOW
    game_day = sport.game_day_for(now)
    last_day = sport.game_day_for(end - dt.timedelta(microseconds=1))
    with httpx.Client(timeout=10.0) as client:
        scoreboard = EspnScoreboard(sport, client)
        while game_day <= last_day:
            yield _collect_day(sport, scoreboard, game_day, now, end)
            game_day += dt.timedelta(days=1)


def _collect_day(
    sport: Sport,
    scoreboard: EspnScoreboard,
    game_day: dt.date,
    start: dt.datetime,
    end: dt.datetime,
) -> CollectedDay:
    started_at = dt.datetime.now(dt.UTC)
    try:
        slate = scoreboard.fetch(game_day)
    except VenueTableError as exc:
        logger.warning(
            "%s %s venue table unavailable (%s)", sport.slug, game_day, type(exc).__name__
        )
        return CollectedDay(
            game_day, started_at, "venue_table_unavailable", error_type=type(exc).__name__
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("%s %s schedule unavailable (%s)", sport.slug, game_day, type(exc).__name__)
        return CollectedDay(
            game_day, started_at, "schedule_unavailable", error_type=type(exc).__name__
        )
    missing_rankings = rankings_missing(sport, slate)
    upcoming = [game for game in slate if start <= game.kickoff < end]
    evaluation = evaluate_games(
        sport, upcoming, get_forecast, get_active_alerts, collection_only=True
    )
    partial = (
        missing_rankings
        or evaluation.games_missing > 0
        or any(
            obs.alerts_error_type is not None
            or obs.outcome == "venue_unavailable"
            or obs.detail == "partial_forecast_coverage"
            for obs in evaluation.observations
        )
    )
    return CollectedDay(
        game_day,
        started_at,
        "partial" if partial else "complete",
        len(upcoming),
        tuple(evaluation.observations),
        missing_rankings,
    )
