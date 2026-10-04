"""Hits the real (keyless) ESPN scoreboard API to verify our parsing still matches reality."""

import datetime as dt

import pytest

from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.nfl import NFL

# Super Bowl LVIII (Chiefs vs. 49ers), a fixed historical date guaranteed to have exactly
# one NFL game, played at the (covered) Allegiant Stadium.
KNOWN_GAME_DATE = dt.date(2024, 2, 11)


@pytest.mark.integration
def test_fetches_and_parses_a_known_historical_slate() -> None:
    games = EspnScoreboard(NFL).fetch(KNOWN_GAME_DATE)

    assert len(games) == 1
    game = games[0]
    assert {game.home_team, game.away_team} == {"KC", "SF"}
    # Allegiant Stadium is a fixed-roof stadium - should resolve as covered.
    assert game.stadium is not None
    assert game.stadium.is_covered is True


@pytest.mark.integration
def test_date_with_no_nfl_games_returns_empty_list() -> None:
    # The NFL doesn't play in July.
    games = EspnScoreboard(NFL).fetch(dt.date(2025, 7, 15))
    assert games == []
