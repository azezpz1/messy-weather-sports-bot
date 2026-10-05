"""The college football configuration: what it covers, how it keeps apart from the NFL bot,
and the venue table it reads."""

import dataclasses
import datetime as dt

import pytest

from messy_weather_sports_bot import cfb
from messy_weather_sports_bot.cfb import CFB, CFB_GAME_DURATION, cfb_catalog, ranked_only
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.sports import SPORTS
from messy_weather_sports_bot.venue_table import load_packaged_table
from messy_weather_sports_bot.weather import GAME_DURATION


def game(home_rank: int | None = None, away_rank: int | None = None) -> Game:
    return Game(
        home_team="Home",
        away_team="Away",
        kickoff=dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC),
        stadium=None,
        home_rank=home_rank,
        away_rank=away_rank,
    )


# ----------------------------------------------------------------- the filter


@pytest.mark.parametrize(
    ("home", "away", "kept"),
    [(4, None, True), (None, 4, True), (4, 12, True), (None, None, False)],
    ids=["home-ranked", "away-ranked", "both-ranked", "neither"],
)
def test_a_game_is_covered_when_either_team_is_ranked(
    home: int | None, away: int | None, kept: bool
) -> None:
    reason = ranked_only(game(home, away))

    assert (reason is None) is kept
    assert reason in (None, "no ranked team")


# ------------------------------------------------------------------ the sport


def test_the_posts_are_worded_as_a_recommendation_of_top_25_games() -> None:
    assert CFB.header_title == "Messy Top 25 college football games to watch"
    assert "forecast" not in CFB.header_title.lower()


def test_the_college_bot_reads_rankings_neutral_sites_and_friendly_team_names() -> None:
    assert CFB.parse_rankings is True
    assert CFB.parse_neutral_site is True
    assert CFB.team_label_fields[0] == "shortDisplayName"  # "OSU" could be two schools
    assert CFB.game_filter is ranked_only


def test_the_college_game_length_is_its_own_constant() -> None:
    # Equal to the NFL's today, but a separate constant so each can be tuned on its own.
    assert CFB.game_duration == CFB_GAME_DURATION
    assert CFB_GAME_DURATION == GAME_DURATION == dt.timedelta(hours=3, minutes=30)


def test_game_days_are_eastern_like_the_nfls() -> None:
    assert (CFB.game_day_timezone, CFB.timezone_label) == (NFL.game_day_timezone, "ET")


# ----------------------------------------------- credentials and state are separate


def test_every_sport_is_registered_by_its_slug() -> None:
    assert SPORTS == {"nfl": NFL, "cfb": CFB}


def test_no_two_sports_share_credentials_or_a_state_file() -> None:
    # Isolation is opt-in (the NFL keeps the original, unprefixed names), so a new sport
    # that forgot to set these would quietly post to another sport's account.
    prefixes = [sport.env_prefix for sport in SPORTS.values()]
    state_keys = [sport.state_key for sport in SPORTS.values()]

    assert len(set(prefixes)) == len(prefixes)
    assert len(set(state_keys)) == len(state_keys)


def test_the_college_bot_sets_its_own_prefix_and_state_key() -> None:
    assert CFB.env_prefix == "CFB_"
    assert CFB.state_key == "cfb"


def test_the_nfl_keeps_the_original_names() -> None:
    assert (NFL.env_prefix, NFL.state_key) == ("", None)


# ------------------------------------------------------------- the venue table


def test_the_catalog_is_the_packaged_table_looked_up_by_id_only() -> None:
    catalog = cfb_catalog()
    rows = load_packaged_table("cfb_venues.json")

    for row in rows:
        stadium = catalog.for_venue(row.id, "")
        assert stadium is not None
        assert stadium.name == row.name
        # By name alone - a "Memorial Stadium" could be anywhere - nothing is found.
        assert catalog.for_venue("", row.name) is None


def test_the_catalog_is_built_once() -> None:
    assert cfb_catalog() is cfb_catalog()


def test_the_catalog_is_not_read_until_it_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A damaged table must only ever break this bot's own run, never the NFL's.
    cfb_catalog.cache_clear()
    calls: list[str] = []

    def spy(filename: str):
        calls.append(filename)
        return load_packaged_table(filename)

    monkeypatch.setattr(cfb, "load_packaged_table", spy)
    try:
        assert calls == []  # importing and configuring CFB read nothing
        CFB.venue_catalog()
        CFB.venue_catalog()
        assert calls == ["cfb_venues.json"]
    finally:
        cfb_catalog.cache_clear()


def test_the_college_sport_is_hashable_and_replaceable_like_any_other() -> None:
    # Its scoreboard parameters are name/value pairs, not a dict, so the frozen dataclass
    # stays hashable.
    assert hash(CFB) == hash(dataclasses.replace(CFB))
    assert dict(CFB.scoreboard_params) == {"groups": "80", "limit": "400"}
