"""Fetch active NWS weather alerts (warnings, watches, advisories) for game locations."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import IntEnum

import httpx

from messy_weather_nfl_bot.retry import request_with_retry
from messy_weather_nfl_bot.weather import GAME_DURATION, USER_AGENT, nws_point

ALERTS_URL = "https://api.weather.gov/alerts/active?point={point}"


class AlertSeverity(IntEnum):
    """How urgently NWS wants people to act, ranked by the alert's headline (Warning >
    Watch > Advisory) - not NWS's own Extreme/Severe/Moderate/Minor `severity` field,
    since a Winter Storm WARNING is more actionable than a Severe Thunderstorm WATCH
    even if NWS scores the underlying event as less "severe"."""

    OTHER = 0
    ADVISORY = 1
    WATCH = 2
    WARNING = 3


_SEVERITY_BY_SUFFIX: tuple[tuple[str, AlertSeverity], ...] = (
    ("warning", AlertSeverity.WARNING),
    ("watch", AlertSeverity.WATCH),
    ("advisory", AlertSeverity.ADVISORY),
)

# Mirrors messiness.py's per-condition score bonuses - the more urgent the alert, the
# further it should push a game up the "messiest games" ranking.
SEVERITY_SCORE_BONUS: dict[AlertSeverity, float] = {
    AlertSeverity.OTHER: 0.0,
    AlertSeverity.ADVISORY: 10.0,
    AlertSeverity.WATCH: 20.0,
    AlertSeverity.WARNING: 35.0,
}


@dataclass(frozen=True)
class WeatherAlert:
    event: str
    """The NWS alert headline, e.g. "Winter Storm Warning"."""
    severity: AlertSeverity
    onset: dt.datetime | None
    ends: dt.datetime | None


def classify_severity(event: str) -> AlertSeverity:
    lowered = event.lower()
    for suffix, severity in _SEVERITY_BY_SUFFIX:
        if lowered.endswith(suffix):
            return severity
    return AlertSeverity.OTHER


def most_severe(alerts: list[WeatherAlert]) -> WeatherAlert | None:
    """The alert NWS considers most urgent, or None if there aren't any active."""
    if not alerts:
        return None
    return max(alerts, key=lambda alert: alert.severity)


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # A naive timestamp would raise TypeError once compared against the
        # timezone-aware kickoff in _overlaps_window - treat it the same as an
        # unparseable one, so _feature_to_alert skips just this feature.
        raise ValueError(f"NWS alert timestamp has no timezone: {value!r}")
    return parsed


def _overlaps_window(alert: WeatherAlert, start: dt.datetime, end: dt.datetime) -> bool:
    """NWS omits `onset`/`ends` for some ongoing or indefinite alerts - treat a missing
    bound as covering the whole game window rather than dropping the alert outright."""
    onset = alert.onset or start
    ends = alert.ends or end
    return onset < end and ends > start


def _feature_to_alert(feature: object) -> WeatherAlert | None:
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return None
    event = properties.get("event")
    if not isinstance(event, str) or not event:
        return None
    try:
        onset = _parse_time(properties.get("onset"))
        ends = _parse_time(properties.get("ends") or properties.get("expires"))
    except ValueError:
        # A malformed timestamp must drop this one feature, not silently become a
        # missing bound - _overlaps_window treats None as covering the whole game.
        return None
    return WeatherAlert(event=event, severity=classify_severity(event), onset=onset, ends=ends)


def _alert_features(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        kind = type(payload).__name__
        raise ValueError(f"Unexpected NWS alerts response shape: expected object, got {kind}")
    features = payload.get("features")
    if not isinstance(features, list):
        kind = type(features).__name__
        raise ValueError(
            f"Unexpected NWS alerts response shape: expected 'features' list, got {kind}"
        )
    return features


def get_active_alerts(
    latitude: float,
    longitude: float,
    kickoff: dt.datetime,
    client: httpx.Client | None = None,
    *,
    game_duration: dt.timedelta = GAME_DURATION,
) -> list[WeatherAlert]:
    """Fetch active NWS alerts for a stadium's location and keep only the ones whose
    onset-ends window overlaps the game, from kickoff through the estimated final
    whistle - an alert that expires before kickoff, or hasn't started by the final
    whistle, doesn't apply."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})

    def _get() -> httpx.Response:
        response = http_client.get(ALERTS_URL.format(point=nws_point(latitude, longitude)))
        response.raise_for_status()
        return response

    try:
        response = request_with_retry(_get)
        features = _alert_features(response.json())
    finally:
        if owns_client:
            http_client.close()

    alerts = [alert for feature in features if (alert := _feature_to_alert(feature)) is not None]
    end = kickoff + game_duration
    return [alert for alert in alerts if _overlaps_window(alert, kickoff, end)]
