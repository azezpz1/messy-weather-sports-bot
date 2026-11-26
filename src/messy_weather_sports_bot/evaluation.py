"""Shared forecast evaluation for collection and publishing."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from messy_weather_sports_bot.alerts import WeatherAlert
from messy_weather_sports_bot.collection import GameObservation
from messy_weather_sports_bot.formatting import format_kickoff
from messy_weather_sports_bot.messiness import GameWeather, evaluate_game
from messy_weather_sports_bot.schedule import Game, skip_reason
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.weather import (
    USER_AGENT,
    ForecastHorizonError,
    WeatherReport,
    covers_game,
)

logger = logging.getLogger(__name__)


def _weather_detail(weather: WeatherReport) -> str:
    parts = [weather.short_forecast]
    if weather.temperature_f is not None:
        parts.append(f"{weather.temperature_f}°F")
    if weather.wind_speed_mph > 0:
        parts.append(f"{weather.wind_speed_mph:g}mph")
    return ", ".join(parts)


@dataclass
class Evaluation:
    evaluated: list[GameWeather] = field(default_factory=list)
    observations: list[GameObservation] = field(default_factory=list)
    outdoor_count: int = 0
    games_missing: int = 0
    """Outdoor games whose forecast couldn't be fetched - one of them might have been
    the messy one, so a run that skipped any isn't a clean one."""

    @property
    def incomplete(self) -> bool:
        return self.games_missing > 0 or any(
            observation.alerts_error_type is not None
            or observation.outcome == "venue_unavailable"
            or observation.detail == "partial_forecast_coverage"
            for observation in self.observations
        )

    @property
    def messy(self) -> list[GameWeather]:
        return [gw for gw in self.evaluated if gw.is_messy]


def rankings_missing(sport: Sport, games: list[Game]) -> bool:
    """Report whether a verified slate has a missing or unreadable team ranking."""
    if not sport.parse_rankings:
        return False
    unreadable = sum(game.rankings_unreadable for game in games)
    if unreadable:
        logger.warning(
            "There is a ranking this bot can't read for %d of the %d games on the ESPN slate; "
            "a ranking field may be missing or malformed.",
            unreadable,
            len(games),
        )
        return True
    return False


def evaluate_games(
    sport: Sport,
    games: list[Game],
    fetch_forecast: Callable[..., list[WeatherReport]],
    fetch_alerts: Callable[..., list[WeatherAlert]],
    *,
    collection_only: bool = False,
) -> Evaluation:
    """Look up the weather for every game that applies it to, and score it."""
    evaluation = Evaluation()
    # One client for the whole slate, so the NWS connection is reused across games.
    with httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT}) as nws_client:
        for game in games:
            matchup = f"{game.away_team} @ {game.home_team}"
            if sport.game_filter is not None:
                # Before any venue or weather work: a game the sport doesn't cover costs
                # nothing, and is only worth a line when someone asks for the detail.
                filtered = sport.game_filter(game)
                if filtered is not None:
                    logger.debug("%s — skipped: %s", matchup, filtered)
                    continue
            reason = skip_reason(game)
            if reason is not None:
                logger.info("%s — skipped: %s", matchup, reason)
                outcome = (
                    "venue_unavailable"
                    if game.stadium is None and not game.is_international
                    else "skipped"
                )
                evaluation.observations.append(GameObservation(game, outcome, reason))
                continue

            evaluation.outdoor_count += 1
            assert game.stadium is not None  # guaranteed by skip_reason() returning None
            alerts: list[WeatherAlert] = []
            try:
                forecasts = fetch_forecast(
                    game.stadium.latitude,
                    game.stadium.longitude,
                    game.kickoff,
                    nws_client,
                    game_duration=sport.game_duration,
                )
            except (httpx.HTTPError, ValueError) as exc:
                if collection_only and isinstance(exc, ForecastHorizonError):
                    detail = "forecast_horizon"
                else:
                    evaluation.games_missing += 1
                    detail = (
                        "ValueError"
                        if isinstance(exc, ForecastHorizonError)
                        else type(exc).__name__
                    )
                evaluation.observations.append(
                    GameObservation(game, "forecast_unavailable", detail)
                )
                logger.info(
                    "%s — skipped: forecast unavailable (%s)",
                    matchup,
                    detail if collection_only else exc,
                )
                continue

            forecast_observed_at = dt.datetime.now(dt.UTC)
            alerts_error_type = None
            try:
                alerts = fetch_alerts(
                    game.stadium.latitude,
                    game.stadium.longitude,
                    game.kickoff,
                    nws_client,
                    game_duration=sport.game_duration,
                )
            except (httpx.HTTPError, ValueError) as exc:
                # An alert lookup failing shouldn't drop the game - the line just shows
                # no alert and scoring falls back to the forecast alone.
                alerts_error_type = type(exc).__name__
                logger.info(
                    "%s — no alert data (%s)",
                    matchup,
                    alerts_error_type if collection_only else exc,
                )

            gw = evaluate_game(game, forecasts, alerts)
            full_coverage = covers_game(forecasts, game.kickoff, sport.game_duration)
            evaluation.observations.append(
                GameObservation(
                    game,
                    "evaluated",
                    detail=None if full_coverage else "partial_forecast_coverage",
                    forecasts=forecasts,
                    result=gw,
                    alerts_error_type=alerts_error_type,
                    observed_at=forecast_observed_at,
                )
            )
            if not collection_only and not full_coverage:
                evaluation.games_missing += 1
                logger.info("%s — skipped: incomplete forecast coverage", matchup)
                continue

            evaluation.evaluated.append(gw)
            period_word = "period" if len(forecasts) == 1 else "periods"
            logger.info(
                "%s %s — %s: score %.1f (%s) across %d hourly %s",
                matchup,
                format_kickoff(game.kickoff, sport),
                "included" if gw.is_messy else "skipped: not messy",
                gw.score,
                _weather_detail(gw.weather),
                len(forecasts),
                period_word,
            )
    return evaluation
