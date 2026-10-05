"""Parsing college football scoreboards: rankings, labels, neutral sites, and the
defensive skips (a kickoff not set, a team ESPN gave no name, a day's overflow)."""

import dataclasses
import datetime as dt
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import Game
from tests.support.espn import MISSING, cfb_event, cfb_sample, espn_event

DAY = dt.date(2026, 1, 18)


def fetch(events: list[dict], sport=CFB, day: dt.date = DAY) -> list[Game]:
    respx.get(sport.scoreboard_url).mock(return_value=httpx.Response(200, json={"events": events}))
    return EspnScoreboard(sport).fetch(day)


def fetch_sample(date: str, sport=CFB) -> list[Game]:
    payload = cfb_sample(date)
    respx.get(sport.scoreboard_url).mock(return_value=httpx.Response(200, json=payload))
    return EspnScoreboard(sport).fetch(dt.date.fromisoformat(f"{date[:4]}-{date[4:6]}-{date[6:]}"))


# ------------------------------------------------------------------- the request


@respx.mock
def test_the_college_request_asks_for_fbs_games_and_enough_of_them() -> None:
    route = respx.get(CFB.scoreboard_url).mock(return_value=httpx.Response(200, json={}))

    EspnScoreboard(CFB).fetch(DAY)

    assert dict(route.calls.last.request.url.params) == {
        "dates": "20260118",
        "groups": "80",
        "limit": "400",
    }


@respx.mock
def test_the_nfl_request_still_sends_only_the_date() -> None:
    route = respx.get(NFL.scoreboard_url).mock(return_value=httpx.Response(200, json={}))

    EspnScoreboard(NFL).fetch(DAY)

    assert dict(route.calls.last.request.url.params) == {"dates": "20260118"}


@respx.mock
def test_espn_gets_httpxs_default_user_agent() -> None:
    # ESPN's edge refuses an unfamiliar User-Agent with a 403.
    route = respx.get(CFB.scoreboard_url).mock(return_value=httpx.Response(200, json={}))

    EspnScoreboard(CFB).fetch(DAY)

    assert route.calls.last.request.headers["user-agent"].startswith("python-httpx/")


@respx.mock
def test_a_slate_that_fills_the_request_limit_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capped = dataclasses.replace(CFB, scoreboard_params=(("limit", "2"),))
    events = [cfb_event("Ohio State", "Minnesota"), cfb_event("Alabama", "Vanderbilt")]

    with caplog.at_level(logging.WARNING, logger="messy_weather_sports_bot.espn"):
        fetch(events, capped)

    assert "the most it was asked for" in caplog.text


@respx.mock
def test_a_slate_under_the_limit_is_not_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    capped = dataclasses.replace(CFB, scoreboard_params=(("limit", "3"),))

    with caplog.at_level(logging.WARNING, logger="messy_weather_sports_bot.espn"):
        fetch([cfb_event("Ohio State", "Minnesota")], capped)

    assert caplog.text == ""


# -------------------------------------------------------- a real Saturday's slate


@respx.mock
def test_a_real_saturday_parses_ranks_labels_and_venues() -> None:
    games = {game.home_team: game for game in fetch_sample("20251004")}

    assert len(games) == 6
    ohio = games["Ohio State"]
    assert (ohio.away_team, ohio.home_rank, ohio.away_rank) == ("Minnesota", 1, None)
    assert ohio.stadium is not None
    assert ohio.stadium.name == "Ohio Stadium"
    assert (games["UCLA"].home_rank, games["UCLA"].away_rank) == (None, 7)
    assert (games["Alabama"].home_rank, games["Alabama"].away_rank) == (10, 16)
    assert (games["Pitt"].home_rank, games["Pitt"].away_rank) == (None, None)
    assert all(game.rankings_reported for game in games.values())
    assert not any(game.neutral_site for game in games.values())


@respx.mock
def test_a_late_kickoff_belongs_to_the_eastern_game_day_not_the_utc_one() -> None:
    # Duke at Cal kicked off at 02:30 UTC on Oct 5 - 10:30pm ET on the 4th.
    games = {game.home_team: game for game in fetch_sample("20251004")}

    california = games["California"]
    assert california.kickoff == dt.datetime(2025, 10, 5, 2, 30, tzinfo=dt.UTC)
    assert california.stadium is not None


@respx.mock
def test_a_neutral_site_game_resolves_its_venue_by_id_and_reads_the_flag() -> None:
    # The Red River Rivalry: "home" Texas is unranked, the visiting Oklahoma is #6.
    (game,) = fetch_sample("20251011")

    assert game.neutral_site is True
    assert (game.home_team, game.away_team) == ("Texas", "Oklahoma")
    assert (game.home_rank, game.away_rank) == (None, 6)
    assert game.stadium is not None
    assert game.stadium.name == "Cotton Bowl"


@respx.mock
def test_a_game_abroad_is_left_out_as_international_not_as_drift() -> None:
    (game,) = fetch_sample("20250823")  # Iowa State vs Kansas State, in Dublin

    assert game.stadium is None
    assert game.unresolved_reason is not None
    assert game.unresolved_reason.startswith("international venue")
    assert game.is_venue_drift is False
    assert (game.home_rank, game.away_rank) == (17, 22)


@respx.mock
def test_the_college_flags_are_off_for_the_nfl() -> None:
    # The same college payload read as the NFL: no ranks, no neutral-site reading.
    payload = cfb_sample("20251011")
    nfl_like = dataclasses.replace(NFL, team_label_fields=("shortDisplayName",))
    respx.get(nfl_like.scoreboard_url).mock(return_value=httpx.Response(200, json=payload))

    (game,) = EspnScoreboard(nfl_like).fetch(dt.date(2025, 10, 11))

    assert game.neutral_site is False
    assert (game.home_rank, game.away_rank, game.rankings_reported) == (None, None, False)


# ------------------------------------------------------------------- rankings


RANK_CASES = [
    pytest.param(1, 1, True, id="first"),
    pytest.param(4, 4, True, id="four"),
    pytest.param(25, 25, True, id="last-place-in-the-poll"),
    pytest.param(26, None, True, id="just-outside"),
    pytest.param(0, None, True, id="zero"),
    pytest.param(-3, None, True, id="negative"),
    pytest.param(99, None, True, id="unranked"),
    pytest.param("4", None, False, id="a-string"),
    pytest.param(4.0, None, False, id="a-float"),
    pytest.param(True, None, False, id="a-bool-is-not-a-rank"),
    pytest.param(None, None, False, id="null"),
]


@respx.mock
@pytest.mark.parametrize(("current", "rank", "reported"), RANK_CASES)
def test_only_an_integer_from_1_to_25_is_a_ranking(
    current: object, rank: int | None, reported: bool
) -> None:
    # The visitor carries no ranking, so `rankings_reported` is about the home team alone.
    (game,) = fetch([cfb_event("Ohio State", "Minnesota", home_rank=current, away_curated=MISSING)])

    assert (game.home_rank, game.rankings_reported) == (rank, reported)
    assert game.away_rank is None


@respx.mock
@pytest.mark.parametrize(
    "curated",
    [MISSING, {}, "oops", [], {"current": None}],
    ids=["missing", "empty", "string", "list", "null-current"],
)
def test_a_ranking_field_that_is_missing_or_malformed_is_unranked_and_unreported(
    curated: object,
) -> None:
    event = cfb_event("Ohio State", "Minnesota", home_curated=curated, away_curated=curated)

    (game,) = fetch([event])

    assert (game.home_rank, game.away_rank, game.rankings_reported) == (None, None, False)


@respx.mock
def test_rankings_are_reported_when_either_team_carries_one() -> None:
    (game,) = fetch([cfb_event("Ohio State", "Minnesota", home_curated=MISSING, away_rank=99)])

    assert game.rankings_reported is True


# --------------------------------------------------------------------- labels


@respx.mock
def test_teams_are_labelled_with_the_short_display_name_not_the_abbreviation() -> None:
    (game,) = fetch([cfb_event("Ohio State", "Oklahoma State")])

    assert (game.home_team, game.away_team) == ("Ohio State", "Oklahoma State")


@respx.mock
@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ((), "Ohio State"),
        (("shortDisplayName",), "Ohio State Mascots"),
        (("shortDisplayName", "displayName"), "Ohio State"),  # `location`
        (("shortDisplayName", "displayName", "location"), "OS"),  # `abbreviation`
    ],
)
def test_a_label_falls_back_through_the_teams_name_fields(
    dropped: tuple[str, ...], expected: str
) -> None:
    event = cfb_event("Ohio State", "Minnesota")
    for name in dropped:
        del event["competitions"][0]["competitors"][0]["team"][name]

    (game,) = fetch([event])

    assert game.home_team == expected


@respx.mock
def test_a_blank_name_field_is_skipped_for_the_next_one() -> None:
    event = cfb_event("Ohio State", "Minnesota")
    event["competitions"][0]["competitors"][0]["team"]["shortDisplayName"] = "  "

    (game,) = fetch([event])

    assert game.home_team == "Ohio State Mascots"


@respx.mock
def test_an_event_with_an_unnamed_team_is_skipped_with_a_warning_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = cfb_event("Ohio State", "Minnesota")
    broken["competitions"][0]["competitors"][1]["team"] = {"id": "1"}
    fine = cfb_event("Alabama", "Vanderbilt")

    with caplog.at_level(logging.WARNING, logger="messy_weather_sports_bot.espn"):
        games = fetch([broken, fine])

    assert [game.home_team for game in games] == ["Alabama"]
    assert "Minnesota @ Ohio State" in caplog.text
    assert "a team has none of shortDisplayName" in caplog.text


@respx.mock
def test_an_nfl_event_with_an_unnamed_team_is_skipped_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = espn_event("GB", "CHI")
    del broken["competitions"][0]["competitors"][0]["team"]["abbreviation"]

    with caplog.at_level(logging.WARNING, logger="messy_weather_sports_bot.espn"):
        games = fetch([broken, espn_event("BUF", "NE")], NFL)

    assert [game.home_team for game in games] == ["BUF"]
    assert "a team has none of abbreviation" in caplog.text


@respx.mock
def test_a_team_that_is_not_an_object_is_skipped_not_a_crash() -> None:
    event = cfb_event("Ohio State", "Minnesota")
    event["competitions"][0]["competitors"][0]["team"] = "Ohio State"

    assert fetch([event]) == []


# --------------------------------------------------------------- skipped events


@respx.mock
def test_a_game_whose_kickoff_is_not_set_yet_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    placeholder = cfb_event("Ohio State", "Minnesota", time_valid=False)

    with caplog.at_level(logging.INFO, logger="messy_weather_sports_bot.espn"):
        games = fetch([placeholder, cfb_event("Alabama", "Vanderbilt")])

    assert [game.home_team for game in games] == ["Alabama"]
    assert "Minnesota @ Ohio State" in caplog.text
    assert "kickoff time is not set" in caplog.text


@respx.mock
def test_a_game_on_another_day_is_skipped_and_the_log_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    next_day = cfb_event("Ohio State", "Minnesota", kickoff="2026-01-19T18:00Z")

    with caplog.at_level(logging.INFO, logger="messy_weather_sports_bot.espn"):
        games = fetch([next_day])

    assert games == []
    assert "falls on 2026-01-19, not 2026-01-18" in caplog.text


@respx.mock
def test_a_kickoff_with_no_timezone_is_still_skipped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    naive = cfb_event("Ohio State", "Minnesota", kickoff="2026-01-18T13:00")

    with caplog.at_level(logging.WARNING, logger="messy_weather_sports_bot.espn"):
        games = fetch([naive])

    assert games == []
    assert "has no timezone" in caplog.text


@respx.mock
def test_an_event_without_a_venue_falls_back_to_no_stadium_not_a_wrong_one() -> None:
    event = cfb_event("Ohio State", "Minnesota")
    del event["competitions"][0]["venue"]

    (game,) = fetch([event])

    # There is no "home team's usual stadium" for a college team (they play neutral-site
    # games too often to guess), so the game just can't be placed.
    assert game.stadium is None
    assert game.is_venue_drift is False
