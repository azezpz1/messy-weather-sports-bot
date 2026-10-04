"""Mocks for the NWS forecast and alerts endpoints, shared by the unit tests. Call these
inside a `@respx.mock` test."""

import httpx
import respx

from messy_weather_sports_bot.weather import nws_point


def forecast_response(
    short_forecast: str,
    temperature: int,
    wind: str,
    precipitation_probability: int,
    *,
    day: str = "2026-01-18",
) -> httpx.Response:
    """An hourly-forecast response with one period covering a 1:00pm ET kickoff on `day`."""
    period = {
        "startTime": f"{day}T13:00:00-05:00",
        "endTime": f"{day}T18:30:00-05:00",
        "isDaytime": True,
        "shortForecast": short_forecast,
        "temperature": temperature,
        "windSpeed": wind,
        "probabilityOfPrecipitation": {"value": precipitation_probability},
    }
    return httpx.Response(200, json={"properties": {"periods": [period]}})


def clear_period_response() -> httpx.Response:
    return forecast_response("Sunny", 40, "5 mph", 0)


def rainy_period_response() -> httpx.Response:
    return forecast_response("Rain", 40, "5 mph", 90)


def mock_hourly_forecast(
    lat: float, lon: float, *, response: httpx.Response, alerts: list[dict] | None = None
) -> None:
    """Mock the point lookup, the hourly forecast (`response`) and the active alerts
    (none unless `alerts` is given) for a stadium. Mocks the point as the bot actually
    requests it (rounded to what NWS accepts), not the raw stadium coordinates - see
    test_weather's over-precise point regression test."""
    point = nws_point(lat, lon)
    hourly_url = f"https://api.weather.gov/gridpoints/MOCK-{lat}-{lon}/forecast/hourly"
    respx.get(f"https://api.weather.gov/points/{point}").mock(
        return_value=httpx.Response(200, json={"properties": {"forecastHourly": hourly_url}})
    )
    respx.get(hourly_url).mock(return_value=response)
    respx.get(f"https://api.weather.gov/alerts/active?point={point}").mock(
        return_value=httpx.Response(200, json={"features": alerts or []})
    )
