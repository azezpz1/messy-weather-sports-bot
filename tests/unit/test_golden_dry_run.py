"""Locks the exact NFL `--dry-run` output (posts, exit code, and run log) for a fixed
slate, so refactors that are meant to leave the NFL bot's behavior untouched can
prove it: this test must keep passing, unchanged, across them."""

import datetime as dt
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot.main import EXIT_OK, run
from messy_weather_sports_bot.schedule import SCOREBOARD_URL
from messy_weather_sports_bot.weather import nws_point

LOGGER_NAME = "messy_weather_sports_bot"
TARGET_DATE = dt.date(2026, 1, 18)

# (home, away, venue name, home stadium lat/lon)
GB = ("GB", "CHI", "Lambeau Field", 44.5013, -88.0622)
BUF = ("BUF", "NE", "Highmark Stadium", 42.77306, -78.79222)
PIT = ("PIT", "CLE", "Acrisure Stadium", 40.4468, -80.0158)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("messy_weather_sports_bot.main.todays_local_date", lambda: TARGET_DATE)


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))


def _event(home: str, away: str, venue_name: str) -> dict:
    kickoff = "2026-01-18T18:00Z"
    return {
        "date": kickoff,
        "competitions": [
            {
                "date": kickoff,
                "venue": {"fullName": venue_name, "indoor": False, "address": {"country": "USA"}},
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": home}},
                    {"homeAway": "away", "team": {"abbreviation": away}},
                ],
            }
        ],
    }


def _forecast(short_forecast: str, temperature: int, wind: str, precip: int) -> httpx.Response:
    period = {
        "startTime": "2026-01-18T13:00:00-05:00",
        "endTime": "2026-01-18T18:30:00-05:00",
        "isDaytime": True,
        "shortForecast": short_forecast,
        "temperature": temperature,
        "windSpeed": wind,
        "probabilityOfPrecipitation": {"value": precip},
    }
    return httpx.Response(200, json={"properties": {"periods": [period]}})


def _mock_stadium(game: tuple, forecast: httpx.Response, alerts: list[dict] | None = None) -> None:
    _, _, _, lat, lon = game
    point = nws_point(lat, lon)
    hourly_url = f"https://api.weather.gov/gridpoints/MOCK-{lat}-{lon}/forecast/hourly"
    respx.get(f"https://api.weather.gov/points/{point}").mock(
        return_value=httpx.Response(200, json={"properties": {"forecastHourly": hourly_url}})
    )
    respx.get(hourly_url).mock(return_value=forecast)
    respx.get(f"https://api.weather.gov/alerts/active?point={point}").mock(
        return_value=httpx.Response(200, json={"features": alerts or []})
    )


WINTER_STORM_WARNING = {
    "properties": {
        "event": "Winter Storm Warning",
        "onset": "2026-01-18T17:00:00+00:00",
        "ends": "2026-01-18T23:00:00+00:00",
    }
}

EXPECTED_STDOUT = (
    "--- post ---\n"
    "🌩️ Messy NFL games to watch — Sun Jan 18 ⚠️ Alerts in effect\n"
    "🏈 NE @ BUF (1:00pm ET): ❄️ Heavy Snow, 28°F (feels 15°F), 18mph wind\n"
    "🏈 CHI @ GB (1:00pm ET): 🌧️ Rain, 40°F, 5mph wind ⚠️ Winter Storm Warning\n"
    "\n"
)
EXPECTED_LOG_MESSAGES = [
    "CHI @ GB 1:00pm ET — included: score 149.5 (Rain, 40°F, 5mph) across 1 hourly period",
    "NE @ BUF 1:00pm ET — included: score 254.1 (Heavy Snow, 28°F, 18mph) across 1 hourly period",
    "CLE @ PIT 1:00pm ET — skipped: not messy: score 6.0 (Sunny, 55°F, 4mph) "
    "across 1 hourly period",
    "Run summary: 3 game(s) found, 3 outdoor, 3 evaluated, 2 messy, "
    "platforms: ConsolePoster, thread: console-1",
]


@respx.mock
def test_nfl_dry_run_output_is_locked(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(SCOREBOARD_URL).mock(
        return_value=httpx.Response(
            200, json={"events": [_event(*GB[:3]), _event(*BUF[:3]), _event(*PIT[:3])]}
        )
    )
    _mock_stadium(GB, _forecast("Rain", 40, "5 mph", 90), alerts=[WINTER_STORM_WARNING])
    _mock_stadium(BUF, _forecast("Heavy Snow", 28, "18 mph", 90))
    _mock_stadium(PIT, _forecast("Sunny", 55, "4 mph", 0))
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == EXPECTED_STDOUT
    assert [record.getMessage() for record in caplog.records] == EXPECTED_LOG_MESSAGES
