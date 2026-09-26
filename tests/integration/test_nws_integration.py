"""Hits the real (keyless) National Weather Service API to verify our parsing still works."""

import datetime as dt

import pytest

from messy_weather_nfl_bot.alerts import get_active_alerts
from messy_weather_nfl_bot.stadiums import stadium_for_team
from messy_weather_nfl_bot.weather import get_forecast


@pytest.mark.integration
def test_fetches_forecast_for_a_known_outdoor_stadium() -> None:
    lambeau = stadium_for_team("GB")
    kickoff = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=4)

    reports = get_forecast(lambeau.latitude, lambeau.longitude, kickoff)

    assert reports
    for report in reports:
        assert report.short_forecast
        if report.temperature_f is not None:
            assert -50 <= report.temperature_f <= 130
        assert report.wind_speed_mph >= 0
        if report.precipitation_probability is not None:
            assert 0 <= report.precipitation_probability <= 100


@pytest.mark.integration
def test_fetches_active_alerts_for_a_known_outdoor_stadium() -> None:
    # Whatever's active right now (often nothing) - this just verifies the request and
    # response parsing work against the real endpoint, not that an alert exists.
    lambeau = stadium_for_team("GB")
    kickoff = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=4)

    alerts = get_active_alerts(lambeau.latitude, lambeau.longitude, kickoff)

    for alert in alerts:
        assert alert.event
