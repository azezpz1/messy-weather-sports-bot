"""End-to-end run against real ESPN + NWS APIs, using ConsolePoster so nothing is ever
actually posted (Bluesky is never exercised in CI)."""

import datetime as dt
import logging

import pytest

from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.pipeline import EXIT_OK, EXIT_PARTIAL, GameDayPipeline
from messy_weather_sports_bot.schedule import outdoor_games, todays_game_day


@pytest.mark.integration
def test_full_pipeline_runs_against_real_apis_without_posting_anywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # NWS can drop kickoff-hour forecasts after a game starts. Prefer an upcoming
    # outdoor slate within the forecast horizon. If there isn't one, exercise the
    # schedule path while the separate NWS tests cover weather and alerts.
    tomorrow = todays_game_day(NFL) + dt.timedelta(days=1)
    game_day = tomorrow
    scoreboard = EspnScoreboard(NFL, verify_slate=True)
    for offset in range(4):
        candidate = tomorrow + dt.timedelta(days=offset)
        if outdoor_games(scoreboard.fetch(candidate)):
            game_day = candidate
            break

    caplog.set_level(logging.INFO, logger="messy_weather_sports_bot")
    exit_code = GameDayPipeline(NFL).run(platform_names=[], dry_run=True, date=game_day, live=True)
    assert exit_code in (EXIT_OK, EXIT_PARTIAL), (
        f"Dry run for {game_day} exited {exit_code}; log:\n{caplog.text}"
    )
