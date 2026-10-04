"""Locks the exact NFL `--dry-run` output (posts, exit code, and run log) for fixed
slates, so refactors that are meant to leave the NFL bot's behavior untouched can
prove it: these tests must keep passing, unchanged, across them. An *intentional*
change to scoring, thresholds, venue data or post wording will legitimately change
the expected values here; a refactor must not.

Three slates: the happy path (one post, an alert, a clear game left out), every skip
category plus a game whose forecast is unavailable (a partial run), and a slate long
enough to need a multi-post thread. The happy path is also driven through `main()`, so
argument parsing, logging setup and the version line are locked too."""

import datetime as dt
import importlib.metadata
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot.nfl import NFL, main
from messy_weather_sports_bot.pipeline import EXIT_OK, EXIT_PARTIAL, GameDayPipeline
from messy_weather_sports_bot.stadiums import stadium_for_team
from tests.support.espn import espn_event
from tests.support.nws import forecast_response, mock_hourly_forecast

LOGGER_NAME = "messy_weather_sports_bot"
TARGET_DATE = dt.date(2026, 1, 18)
INSTALLED_VERSION = importlib.metadata.version("messy-weather-sports-bot")


def run(platform_names: list[str], dry_run: bool) -> int:
    return GameDayPipeline(NFL).run(platform_names, dry_run)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: TARGET_DATE
    )


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)


def _mock_stadium(home: str, forecast: httpx.Response, alerts: list[dict] | None = None) -> None:
    stadium = stadium_for_team(home)
    mock_hourly_forecast(stadium.latitude, stadium.longitude, response=forecast, alerts=alerts)


def _mock_slate(events: list[dict]) -> None:
    respx.get(NFL.scoreboard_url).mock(return_value=httpx.Response(200, json={"events": events}))


WINTER_STORM_WARNING = {
    "properties": {
        "event": "Winter Storm Warning",
        "onset": "2026-01-18T17:00:00+00:00",
        "ends": "2026-01-18T23:00:00+00:00",
    }
}


def mock_happy_path() -> None:
    _mock_slate([espn_event("GB", "CHI"), espn_event("BUF", "NE"), espn_event("PIT", "CLE")])
    _mock_stadium("GB", forecast_response("Rain", 40, "5 mph", 90), alerts=[WINTER_STORM_WARNING])
    _mock_stadium("BUF", forecast_response("Heavy Snow", 28, "18 mph", 90))
    _mock_stadium("PIT", forecast_response("Sunny", 55, "4 mph", 0))


def mock_every_skip_category() -> None:
    _mock_slate(
        [
            espn_event("MIN", "DET"),  # covered stadium
            espn_event(
                "JAX", "PHI", "Tottenham Hotspur Stadium", country="England"
            ),  # international
            espn_event("CHI", "GB", "Brand New Soldier Field"),  # a US venue not on file: drift
            espn_event("BUF", "NE"),  # outdoor, but the forecast doesn't cover the game
            espn_event("PIT", "CLE"),  # messy
            espn_event("DEN", "KC"),  # messy
        ]
    )
    _mock_stadium("BUF", forecast_response("Rain", 40, "5 mph", 90, day="2026-01-17"))
    _mock_stadium("PIT", forecast_response("Thunderstorms", 62, "12 mph", 70))
    _mock_stadium("DEN", forecast_response("Snow", 25, "14 mph", 70))


def mock_long_thread() -> None:
    _mock_slate(
        [
            espn_event("GB", "MIN"),
            espn_event("BUF", "NE"),
            espn_event("PIT", "BAL"),
            espn_event("CHI", "DET"),
            espn_event("CLE", "CIN"),
            espn_event("DEN", "LV"),
        ]
    )
    _mock_stadium("GB", forecast_response("Snow", 22, "16 mph", 80))
    _mock_stadium("BUF", forecast_response("Heavy Snow", 28, "18 mph", 90))
    _mock_stadium("PIT", forecast_response("Rain", 41, "9 mph", 80))
    _mock_stadium("CHI", forecast_response("Thunderstorms", 58, "14 mph", 60))
    _mock_stadium("CLE", forecast_response("Freezing Rain", 30, "8 mph", 60))
    _mock_stadium("DEN", forecast_response("Blowing Snow", 18, "24 mph", 50))


HAPPY_PATH_STDOUT = (
    "--- post ---\n"
    "🌩️ Messy NFL games to watch — Sun Jan 18 ⚠️ Alerts in effect\n"
    "🏈 NE @ BUF (1:00pm ET): ❄️ Heavy Snow, 28°F (feels 15°F), 18mph wind\n"
    "🏈 CHI @ GB (1:00pm ET): 🌧️ Rain, 40°F, 5mph wind ⚠️ Winter Storm Warning\n"
    "\n"
)
HAPPY_PATH_LOG = [
    "CHI @ GB 1:00pm ET — included: score 149.5 (Rain, 40°F, 5mph) across 1 hourly period",
    "NE @ BUF 1:00pm ET — included: score 254.1 (Heavy Snow, 28°F, 18mph) across 1 hourly period",
    "CLE @ PIT 1:00pm ET — skipped: not messy: score 6.0 (Sunny, 55°F, 4mph) "
    "across 1 hourly period",
    "Run summary: 3 game(s) found, 3 outdoor, 3 evaluated, 2 messy, "
    "platforms: ConsolePoster, thread: console-1",
]

SKIPS_STDOUT = (
    "--- post ---\n"
    "🌩️ Messy NFL games to watch — Sun Jan 18\n"
    "🏈 KC @ DEN (1:00pm ET): ❄️ Snow, 25°F (feels 13°F), 14mph wind\n"
    "🏈 CLE @ PIT (1:00pm ET): ⛈️ Thunderstorms, 62°F, 12mph wind\n"
    "\n"
)
SKIPS_LOG = [
    "DET @ MIN — skipped: covered stadium (U.S. Bank Stadium)",
    'PHI @ JAX — skipped: international venue "Tottenham Hotspur Stadium"',
    "GB @ CHI — skipped: unrecognized venue \"Brand New Soldier Field\" (espn venue id '')",
    "NE @ BUF — skipped: forecast unavailable (no forecast period covers the game window "
    "2026-01-18T18:00:00+00:00 to 2026-01-18T21:30:00+00:00 "
    "(forecast not available yet, or the game is over))",
    "CLE @ PIT 1:00pm ET — included: score 116.0 (Thunderstorms, 62°F, 12mph) "
    "across 1 hourly period",
    "KC @ DEN 1:00pm ET — included: score 188.0 (Snow, 25°F, 14mph) across 1 hourly period",
    "Run summary: 6 game(s) found, 3 outdoor, 2 evaluated, 2 messy, "
    "platforms: ConsolePoster, thread: console-1",
]

LONG_THREAD_STDOUT = (
    "--- post ---\n"
    "🌩️ Messy NFL games to watch — Sun Jan 18\n"
    "🏈 NE @ BUF (1:00pm ET): ❄️ Heavy Snow, 28°F (feels 15°F), 18mph wind\n"
    "🏈 MIN @ GB (1:00pm ET): ❄️ Snow, 22°F (feels 8°F), 16mph wind\n"
    "🏈 LV @ DEN (1:00pm ET): ❄️ Blowing Snow, 18°F (feels 0°F), 24mph wind\n"
    "\n"
    "--- reply to console-1 ---\n"
    "🏈 CIN @ CLE (1:00pm ET): 🧊 Freezing Rain, 30°F (feels 22°F), 8mph wind\n"
    "🏈 BAL @ PIT (1:00pm ET): 🌧️ Rain, 41°F (feels 35°F), 9mph wind\n"
    "🏈 DET @ CHI (1:00pm ET): ⛈️ Thunderstorms, 58°F, 14mph wind\n"
    "\n"
)
LONG_THREAD_LOG = [
    "MIN @ GB 1:00pm ET — included: score 215.6 (Snow, 22°F, 16mph) across 1 hourly period",
    "NE @ BUF 1:00pm ET — included: score 254.1 (Heavy Snow, 28°F, 18mph) across 1 hourly period",
    "BAL @ PIT 1:00pm ET — included: score 110.2 (Rain, 41°F, 9mph) across 1 hourly period",
    "DET @ CHI 1:00pm ET — included: score 105.0 (Thunderstorms, 58°F, 14mph) "
    "across 1 hourly period",
    "CIN @ CLE 1:00pm ET — included: score 155.6 (Freezing Rain, 30°F, 8mph) "
    "across 1 hourly period",
    "LV @ DEN 1:00pm ET — included: score 188.2 (Blowing Snow, 18°F, 24mph) across 1 hourly period",
    "Run summary: 6 game(s) found, 6 outdoor, 6 evaluated, 6 messy, "
    "platforms: ConsolePoster, thread: console-1",
]


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


@respx.mock
def test_happy_path_dry_run_is_locked(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    mock_happy_path()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == HAPPY_PATH_STDOUT
    assert _messages(caplog) == HAPPY_PATH_LOG
    # A dry run must never read or write post state (see state.py).
    assert not (tmp_path / "state").exists()


@respx.mock
def test_happy_path_through_main_is_locked(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mock_happy_path()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    exit_code = main(["--dry-run"])

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == HAPPY_PATH_STDOUT
    version_line = f"messy-weather-sports-bot {INSTALLED_VERSION} starting"
    assert _messages(caplog) == [version_line, *HAPPY_PATH_LOG]


@respx.mock
def test_every_skip_category_and_a_partial_run_are_locked(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mock_every_skip_category()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_PARTIAL
    assert capsys.readouterr().out == SKIPS_STDOUT
    assert _messages(caplog) == SKIPS_LOG


@respx.mock
def test_a_multi_post_thread_is_locked(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mock_long_thread()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == LONG_THREAD_STDOUT
    assert _messages(caplog) == LONG_THREAD_LOG
