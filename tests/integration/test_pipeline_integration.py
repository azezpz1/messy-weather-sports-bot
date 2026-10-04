"""End-to-end run against real ESPN + NWS APIs, using ConsolePoster so nothing is ever
actually posted (Bluesky is never exercised in CI)."""

import logging

import pytest

from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.pipeline import EXIT_OK, EXIT_PARTIAL, GameDayPipeline


@pytest.mark.integration
def test_full_pipeline_runs_against_real_apis_without_posting_anywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regardless of whether today happens to have outdoor NFL games, the pipeline should
    # run end-to-end against the real schedule/weather APIs and exit cleanly. EXIT_PARTIAL
    # is also fine: when this runs late on a game day, NWS has dropped the forecast
    # periods for games already played, so those are legitimately "forecast unavailable"
    # (a real failure would be EXIT_NOTHING_POSTED or an exception). The
    # per-game include/skip lines are INFO, below pytest's default capture level -
    # without them, a failure here doesn't say which game or API response caused it.
    caplog.set_level(logging.INFO, logger="messy_weather_sports_bot")
    exit_code = GameDayPipeline(NFL).run(platform_names=[], dry_run=True)
    assert exit_code in (EXIT_OK, EXIT_PARTIAL), (
        f"run() exited {exit_code}; its log:\n{caplog.text}"
    )
