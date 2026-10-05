"""Hits the real (keyless) ESPN college football scoreboard to verify our parsing still
matches reality. Fixed past Saturdays, so the slates - and the rankings ESPN reports for
them - don't change."""

import datetime as dt

import pytest

from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.schedule import venue_drift

# The week Ohio State was #1 and hosted Minnesota; ESPN lists 46 FBS games that day.
KNOWN_SATURDAY = dt.date(2025, 10, 4)
# The Red River Rivalry, at the Cotton Bowl in Dallas.
NEUTRAL_SITE_SATURDAY = dt.date(2025, 10, 11)
# Iowa State vs Kansas State, in Dublin.
IRELAND_SATURDAY = dt.date(2025, 8, 23)


@pytest.mark.integration
def test_a_known_saturday_parses_with_rankings_and_known_venues() -> None:
    games = EspnScoreboard(CFB).fetch(KNOWN_SATURDAY)

    assert len(games) > 40
    assert all(game.rankings_reported for game in games)
    ohio_state = next(game for game in games if game.home_team == "Ohio State")
    assert (ohio_state.away_team, ohio_state.home_rank, ohio_state.away_rank) == (
        "Minnesota",
        1,
        None,
    )
    assert ohio_state.stadium is not None
    assert ohio_state.stadium.name == "Ohio Stadium"
    ranked = [game for game in games if game.home_rank or game.away_rank]
    assert 10 <= len(ranked) <= 25
    # The venue table was built from the 2025 season, so none of its venues is a surprise.
    assert venue_drift(games) == []


@pytest.mark.integration
def test_a_neutral_site_game_is_flagged_and_its_venue_found_by_id() -> None:
    games = EspnScoreboard(CFB).fetch(NEUTRAL_SITE_SATURDAY)

    red_river = next(game for game in games if game.neutral_site)
    assert {red_river.home_team, red_river.away_team} == {"Texas", "Oklahoma"}
    assert red_river.stadium is not None
    assert red_river.stadium.name == "Cotton Bowl"


@pytest.mark.integration
def test_a_game_in_ireland_is_international_not_drift() -> None:
    games = EspnScoreboard(CFB).fetch(IRELAND_SATURDAY)

    dublin = next(game for game in games if game.neutral_site)
    assert dublin.stadium is None
    assert dublin.is_venue_drift is False
    assert (dublin.unresolved_reason or "").startswith("international venue")


@pytest.mark.integration
def test_a_date_with_no_games_returns_an_empty_list() -> None:
    # The college season doesn't run in July.
    assert EspnScoreboard(CFB).fetch(dt.date(2025, 7, 15)) == []
