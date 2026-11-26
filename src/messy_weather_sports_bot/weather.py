"""Fetch game-day forecasts from the (free, keyless) US National Weather Service API."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from messy_weather_sports_bot.retry import request_with_retry

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
    precipitation_probability: float | None
    """Percent chance of precipitation (0-100), or None if NWS didn't report one."""
    relative_humidity: int | None = None
    """Percent relative humidity (0-100), or None if NWS didn't report one."""

    starts_at: str | None = None
    ends_at: str | None = None
    """The timestamp bounds of this NWS period, including the provider's UTC offset."""

    @property
    def feels_like_f(self) -> float | None:
        """How cold or hot it actually feels: the NWS wind chill when it's cold and
        windy, the NWS heat index when it's hot, otherwise the air temperature. None if
        the temperature is missing."""
        if self.temperature_f is None:
            return None
        if (
            self.temperature_f <= WIND_CHILL_MAX_F
            and self.wind_speed_mph >= WIND_CHILL_MIN_WIND_MPH
        ):
            return wind_chill_f(self.temperature_f, self.wind_speed_mph)
        if self.temperature_f >= HEAT_INDEX_MIN_F and self.relative_humidity is not None:
            return heat_index_f(self.temperature_f, self.relative_humidity)
        return float(self.temperature_f)


# The NWS wind chill formula is only defined at or below 50°F with at least 3 mph of
# wind, and the heat index is only meaningful from about 80°F up.
WIND_CHILL_MAX_F = 50
WIND_CHILL_MIN_WIND_MPH = 3.0
HEAT_INDEX_MIN_F = 80


def wind_chill_f(temperature_f: float, wind_speed_mph: float) -> float:
    """The NWS (2001) wind chill formula. Only valid for temperatures at or below 50°F
    and winds of at least 3 mph - see WeatherReport.feels_like_f."""
    wind_factor = wind_speed_mph**0.16
    return (
        35.74 + 0.6215 * temperature_f - 35.75 * wind_factor + 0.4275 * temperature_f * wind_factor
    )


def heat_index_f(temperature_f: float, relative_humidity: float) -> float:
    """The NWS heat index: Steadman's simple formula, switching to the Rothfusz
    regression (with its low- and high-humidity adjustments) once the simple formula
    averaged with the air temperature reaches 80°F, as described at https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml."""
    t, rh = temperature_f, relative_humidity
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if (simple + t) / 2 < 80:
        return simple
    index = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 0.00683783 * t * t
        - 0.05481717 * rh * rh
        + 0.00122874 * t * t * rh
        + 0.00085282 * t * rh * rh
        - 0.00000199 * t * t * rh * rh
    )
    if rh < 13 and 80 <= t <= 112:
        index -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and 80 <= t <= 87:
        index += ((rh - 85) / 10) * ((87 - t) / 5)
    return index


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


class ForecastHorizonError(ValueError):
    """Valid forecast periods exist but they end before kickoff."""


def _periods_in_window(periods: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Return every forecast period overlapping [start, end)."""
    if not periods:
        raise ValueError("NWS returned no forecast periods")
    covering = []
    latest_end = None
    for period in periods:
        period_start = dt.datetime.fromisoformat(period["startTime"])
        period_end = dt.datetime.fromisoformat(period["endTime"])
        if period_start.utcoffset() is None or period_end.utcoffset() is None:
            raise ValueError("NWS forecast period timestamps must have UTC offsets")
        if period_end <= period_start:
            raise ValueError("NWS forecast period ends before or at its start")
        latest_end = period_end if latest_end is None else max(latest_end, period_end)
        if period_start < end and period_end > start:
            covering.append(period)
    if not covering:
        error = (
            ForecastHorizonError if latest_end is not None and latest_end <= start else ValueError
        )
        raise error(
            f"no forecast period covers the game window {start.isoformat()} to "
            f"{end.isoformat()} (forecast not available yet, or the game is over)"
        )
    return covering


def covers_game(
    reports: Sequence[WeatherReport], kickoff: dt.datetime, duration: dt.timedelta
) -> bool:
    """Check the union of the hourly periods for gaps across the game."""
    end = kickoff + duration
    intervals = []
    for report in reports:
        if report.starts_at is None or report.ends_at is None:
            return False
        start = dt.datetime.fromisoformat(report.starts_at)
        finish = dt.datetime.fromisoformat(report.ends_at)
        if (
            start.utcoffset() is None
            or finish.utcoffset() is None
            or not start < finish
            or start >= end
            or finish <= kickoff
        ):
            return False
        intervals.append((start, finish))

    covered_until = kickoff
    for start, finish in sorted(intervals):
        if start > covered_until:
            return False
        covered_until = max(covered_until, finish)
        if covered_until >= end:
            return True
    return False


def _forecast_hourly_url(points_payload: object) -> str:
    if not isinstance(points_payload, dict):
        kind = type(points_payload).__name__
        raise ValueError(f"Unexpected NWS points response shape: expected object, got {kind}")
    properties = points_payload.get("properties")
    if not isinstance(properties, dict) or "forecastHourly" not in properties:
        raise ValueError(
            "Unexpected NWS points response shape: missing 'properties.forecastHourly'"
        )
    url = properties["forecastHourly"]
    if not isinstance(url, str) or not url:
        raise ValueError("Unexpected NWS points response shape: forecastHourly must be a URL")
    return url


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


def _period_quantity(period: dict, field: str) -> object:
    quantity = period.get(field)
    if quantity is None:
        return None
    if not isinstance(quantity, dict):
        raise ValueError(f"Unexpected NWS forecast response shape: {field} must be an object")
    return quantity.get("value")


def _period_to_report(period: dict) -> WeatherReport:
    # Missing values are expected. Keep temperatures unknown and default missing wind
    # speeds to calm. Reject malformed fields before they reach scoring or storage.
    wind = period.get("windSpeed")
    if wind is not None and not isinstance(wind, str):
        raise ValueError("Unexpected NWS forecast response shape: windSpeed must be a string")
    summary = period.get("shortForecast", "")
    if not isinstance(summary, str):
        raise ValueError("Unexpected NWS forecast response shape: shortForecast must be a string")
    precip = _period_quantity(period, "probabilityOfPrecipitation")
    if precip is not None and (
        not isinstance(precip, int | float) or isinstance(precip, bool) or not 0 <= precip <= 100
    ):
        raise ValueError(
            "Unexpected NWS forecast response shape: invalid precipitation probability"
        )
    humidity = _period_quantity(period, "relativeHumidity")
    raw_temperature = period.get("temperature")
    try:
        temperature = int(raw_temperature) if raw_temperature is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Unexpected NWS forecast response shape: invalid temperature") from exc
    return WeatherReport(
        starts_at=period["startTime"],
        ends_at=period["endTime"],
        short_forecast=summary,
        temperature_f=temperature,
        wind_speed_mph=_parse_wind_speed_mph(wind or ""),
        precipitation_probability=precip,
        relative_humidity=(
            round(humidity)
            if isinstance(humidity, int | float)
            and not isinstance(humidity, bool)
            and 0 <= humidity <= 100
            else None
        ),
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

        try:
            forecast_response = request_with_retry(lambda: _get(forecast_url))
        except httpx.InvalidURL as exc:
            raise ValueError("NWS returned an invalid hourly forecast URL") from exc
        periods = _forecast_periods(forecast_response.json())
    finally:
        if owns_client:
            http_client.close()

    game_periods = _periods_in_window(periods, kickoff, kickoff + game_duration)
    return [_period_to_report(period) for period in game_periods]
