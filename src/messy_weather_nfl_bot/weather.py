"""Fetch game-day forecasts from the (free, keyless) US National Weather Service API."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import httpx

from messy_weather_nfl_bot.retry import request_with_retry

POINTS_URL = "https://api.weather.gov/points/{point}"

# NWS accepts at most 4 decimal places in a lat,lon point (~11m, far finer than its
# 2.5km forecast grid) and 301-redirects anything more precise to the rounded point.
# httpx doesn't follow redirects by default, so an over-precise point fails outright.
NWS_POINT_DECIMALS = 4

# NWS asks API consumers to identify themselves in the User-Agent.
USER_AGENT = "messy-weather-sports-bot (https://github.com/azezpz1/messy-weather-sports-bot)"

# Roughly how long an NFL broadcast runs, kickoff to final whistle. Used to build the
# window we check for messy weather, since conditions can turn ugly well after kickoff.
GAME_DURATION = dt.timedelta(hours=3, minutes=30)

_WIND_SPEED_RE = re.compile(r"(\d+)(?:\s*to\s*(\d+))?\s*mph", re.IGNORECASE)


@dataclass(frozen=True)
class WeatherReport:
    short_forecast: str
    temperature_f: int | None
    """None if NWS didn't report a temperature for this period - distinct from a measured
    0°F, which would otherwise be scored as extreme cold."""
    wind_speed_mph: float
    precipitation_probability: int | None
    """Percent chance of precipitation (0-100), or None if NWS didn't report one."""


def nws_point(latitude: float, longitude: float) -> str:
    """Format a location as an NWS `lat,lon` point, rounded to the precision NWS
    accepts. round() rather than a fixed-width format, so an already-short coordinate
    like 44.5 isn't padded to 44.5000 - which NWS would redirect too."""
    return f"{round(latitude, NWS_POINT_DECIMALS)},{round(longitude, NWS_POINT_DECIMALS)}"


def _parse_wind_speed_mph(wind_speed: str) -> float:
    """Parse an NWS windSpeed string (e.g. "10 mph" or "10 to 20 mph") into mph.

    Uses the upper bound of a range, since the worse case is what matters for messiness.
    """
    match = _WIND_SPEED_RE.search(wind_speed)
    if not match:
        return 0.0
    low, high = match.groups()
    return float(high or low)


def _periods_in_window(periods: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Return every forecast period overlapping [start, end), falling back to the soonest
    daytime period (then the first period) if none overlap at all."""
    if not periods:
        raise ValueError("NWS returned no forecast periods")
    covering = [
        period
        for period in periods
        if dt.datetime.fromisoformat(period["startTime"]) < end
        and dt.datetime.fromisoformat(period["endTime"]) > start
    ]
    if covering:
        return covering
    for period in periods:
        if period.get("isDaytime"):
            return [period]
    return [periods[0]]


def _forecast_hourly_url(points_payload: object) -> str:
    if not isinstance(points_payload, dict):
        kind = type(points_payload).__name__
        raise ValueError(f"Unexpected NWS points response shape: expected object, got {kind}")
    properties = points_payload.get("properties")
    if not isinstance(properties, dict) or "forecastHourly" not in properties:
        raise ValueError(
            "Unexpected NWS points response shape: missing 'properties.forecastHourly'"
        )
    return properties["forecastHourly"]


def _forecast_periods(forecast_payload: object) -> list[dict]:
    if not isinstance(forecast_payload, dict):
        kind = type(forecast_payload).__name__
        raise ValueError(f"Unexpected NWS forecast response shape: expected object, got {kind}")
    properties = forecast_payload.get("properties")
    if not isinstance(properties, dict):
        kind = type(properties).__name__
        raise ValueError(
            f"Unexpected NWS forecast response shape: expected 'properties' object, got {kind}"
        )
    periods = properties.get("periods")
    if not isinstance(periods, list):
        kind = type(periods).__name__
        raise ValueError(
            f"Unexpected NWS forecast response shape: expected 'periods' list, got {kind}"
        )
    for period in periods:
        if (
            not isinstance(period, dict)
            or not isinstance(period.get("startTime"), str)
            or not isinstance(period.get("endTime"), str)
        ):
            raise ValueError(
                "Unexpected NWS forecast response shape: a period is missing a "
                "string 'startTime'/'endTime'"
            )
    return periods


def _period_to_report(period: dict) -> WeatherReport:
    # NWS can report a null temperature/windSpeed for a period with missing data. A null
    # temperature stays None rather than becoming a fabricated 0°F extreme-cold reading;
    # windSpeed falls back to calm, which doesn't skew scoring the same way.
    precip = period.get("probabilityOfPrecipitation", {}) or {}
    temperature = period.get("temperature")
    return WeatherReport(
        short_forecast=period.get("shortForecast", ""),
        temperature_f=int(temperature) if temperature is not None else None,
        wind_speed_mph=_parse_wind_speed_mph(period.get("windSpeed") or ""),
        precipitation_probability=precip.get("value"),
    )


def get_forecast(
    latitude: float,
    longitude: float,
    kickoff: dt.datetime,
    client: httpx.Client | None = None,
    *,
    game_duration: dt.timedelta = GAME_DURATION,
) -> list[WeatherReport]:
    """Fetch hourly forecasts for every period spanning the game, from kickoff through the
    estimated final whistle - so messiness can be judged over the whole game, not just the
    conditions at the moment it starts."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})

    def _get(url: str) -> httpx.Response:
        response = http_client.get(url)
        response.raise_for_status()
        return response

    try:
        points_response = request_with_retry(
            lambda: _get(POINTS_URL.format(point=nws_point(latitude, longitude)))
        )
        forecast_url = _forecast_hourly_url(points_response.json())

        forecast_response = request_with_retry(lambda: _get(forecast_url))
        periods = _forecast_periods(forecast_response.json())
    finally:
        if owns_client:
            http_client.close()

    game_periods = _periods_in_window(periods, kickoff, kickoff + game_duration)
    return [_period_to_report(period) for period in game_periods]
