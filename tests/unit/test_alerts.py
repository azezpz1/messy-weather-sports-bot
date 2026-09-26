import datetime as dt

import httpx
import pytest
import respx

from messy_weather_nfl_bot.alerts import (
    AlertSeverity,
    WeatherAlert,
    classify_severity,
    get_active_alerts,
    most_severe,
)

LAT, LON = 44.5013, -88.0622
ALERTS_URL = f"https://api.weather.gov/alerts/active?point={LAT},{LON}"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test suite fast - backoff timing is covered by tests/unit/test_retry.py.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


def _feature(event: str, onset: str | None, ends: str | None) -> dict:
    return {"properties": {"event": event, "onset": onset, "ends": ends}}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("Winter Storm Warning", AlertSeverity.WARNING),
        ("High Wind Warning", AlertSeverity.WARNING),
        ("Severe Thunderstorm Watch", AlertSeverity.WATCH),
        ("Heat Advisory", AlertSeverity.ADVISORY),
        ("Special Weather Statement", AlertSeverity.OTHER),
    ],
)
def test_classify_severity(event: str, expected: AlertSeverity) -> None:
    assert classify_severity(event) == expected


def test_most_severe_picks_warning_over_watch_and_advisory() -> None:
    alerts = [
        WeatherAlert("Heat Advisory", AlertSeverity.ADVISORY, None, None),
        WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None),
        WeatherAlert("Severe Thunderstorm Watch", AlertSeverity.WATCH, None, None),
    ]
    result = most_severe(alerts)
    assert result is not None
    assert result.event == "Winter Storm Warning"


def test_most_severe_of_no_alerts_is_none() -> None:
    assert most_severe([]) is None


@respx.mock
def test_get_active_alerts_keeps_an_alert_overlapping_kickoff() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    _feature(
                        "Winter Storm Warning",
                        "2026-01-18T17:00:00+00:00",
                        "2026-01-18T23:00:00+00:00",
                    )
                ]
            },
        )
    )

    alerts = get_active_alerts(LAT, LON, kickoff, game_duration=dt.timedelta(hours=3, minutes=30))

    assert len(alerts) == 1
    assert alerts[0].event == "Winter Storm Warning"
    assert alerts[0].severity == AlertSeverity.WARNING


@respx.mock
def test_get_active_alerts_drops_an_alert_that_expires_before_kickoff() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    _feature(
                        "Winter Storm Warning",
                        "2026-01-18T10:00:00+00:00",
                        "2026-01-18T17:00:00+00:00",
                    )
                ]
            },
        )
    )

    alerts = get_active_alerts(LAT, LON, kickoff, game_duration=dt.timedelta(hours=3, minutes=30))

    assert alerts == []


@respx.mock
def test_get_active_alerts_drops_an_alert_that_starts_after_the_game_ends() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    _feature(
                        "Winter Storm Warning",
                        "2026-01-18T22:30:00+00:00",
                        "2026-01-19T04:00:00+00:00",
                    )
                ]
            },
        )
    )

    alerts = get_active_alerts(LAT, LON, kickoff, game_duration=dt.timedelta(hours=3, minutes=30))

    assert alerts == []


@respx.mock
def test_get_active_alerts_treats_missing_onset_and_ends_as_covering_the_game() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json={"features": [_feature("Heat Advisory", None, None)]})
    )

    alerts = get_active_alerts(LAT, LON, kickoff, game_duration=dt.timedelta(hours=3, minutes=30))

    assert len(alerts) == 1
    assert alerts[0].event == "Heat Advisory"


@respx.mock
def test_get_active_alerts_no_active_alerts_returns_empty_list() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json={"features": []}))

    assert get_active_alerts(LAT, LON, kickoff) == []


@respx.mock
def test_get_active_alerts_raises_after_persistent_5xx() -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(500))
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)

    with pytest.raises(httpx.HTTPStatusError):
        get_active_alerts(LAT, LON, kickoff)


@respx.mock
def test_get_active_alerts_raises_a_clean_error_on_a_malformed_response() -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json={}))
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)

    with pytest.raises(ValueError, match="features"):
        get_active_alerts(LAT, LON, kickoff)


@respx.mock
def test_get_active_alerts_ignores_a_feature_missing_an_event() -> None:
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC)
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(
            200, json={"features": [{"properties": {"onset": None, "ends": None}}]}
        )
    )

    assert get_active_alerts(LAT, LON, kickoff) == []
