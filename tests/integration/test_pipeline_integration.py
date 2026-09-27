"""End-to-end run against real ESPN + NWS APIs, using ConsolePoster so nothing is ever
actually posted (Bluesky is never exercised in CI)."""

import logging

import pytest

from messy_weather_nfl_bot.main import run


@pytest.mark.integration
def test_full_pipeline_runs_against_real_apis_without_posting_anywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regardless of whether today happens to have outdoor NFL games, the pipeline should
    # run end-to-end against the real schedule/weather APIs and exit cleanly. The
    # per-game include/skip lines are INFO, below pytest's default capture level -
    # without them, a failure here doesn't say which game or API response caused it.
    caplog.set_level(logging.INFO, logger="messy_weather_nfl_bot")
    exit_code = run(platform_names=[], dry_run=True)
    assert exit_code == 0, f"run() exited {exit_code}; its log:\n{caplog.text}"
