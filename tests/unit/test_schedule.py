import dataclasses
import datetime as dt

import httpx
import pytest
import respx

from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import Game, outdoor_games, skip_reason, venue_drift
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.stadiums import stadium_for_team
from messy_weather_sports_bot.venues import VenueCatalog
from tests.support.espn import espn_event

SCOREBOARD_URL = NFL.scoreboard_url
TARGET_DATE = dt.date(2026, 1, 18)


def get_todays_games(
    date: dt.date, client: httpx.Client | None = None, sport: Sport = NFL
) -> list[Game]:
    return EspnScoreboard(sport, client).fetch(date)


def _event(
    home: str,
    away: str,
    kickoff: str,
    venue_name: str,
    venue_indoor: bool = False,
    venue_id: str = "",
    venue_country: str = "USA",
) -> dict:
    return espn_event(
        home,
        away,
        venue_name,
        kickoff=kickoff,
        venue_id=venue_id,
        indoor=venue_indoor,
        country=venue_country,
    )


@respx.mock
def test_parses_basic_outdoor_game() -> None:
    payload = {
        "events": [
            _event("GB", "CHI", "2026-01-18T18:00Z", "Lambeau Field"),
        ]
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert len(games) == 1
    game = games[0]
    assert game.home_team == "GB"
    assert game.away_team == "CHI"
    assert game.stadium is not None
    assert game.stadium.is_covered is False


@respx.mock
def test_known_covered_stadium_is_resolved_as_covered() -> None:
    payload = {"events": [_event("NO", "TB", "2026-01-18T18:00Z", "Caesars Superdome")]}
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is not None
    assert games[0].stadium.is_covered is True
    assert outdoor_games(games) == []


@respx.mock
def test_international_game_has_no_resolvable_stadium() -> None:
    payload = {
        "events": [
            _event("JAX", "NE", "2026-01-18T18:00Z", "Wembley Stadium", venue_country="England")
        ],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is False
    assert outdoor_games(games) == []
    assert venue_drift(games) == []


@respx.mock
def test_relocated_game_resolves_to_the_new_venues_coordinates() -> None:
    # A BUF "home" game actually played at Ford Field (DET's stadium) - both are
    # known outdoor/covered NFL venues, so the game should resolve to Ford Field's
    # coordinates, not BUF's usual stadium or a dropped game.
    payload = {"events": [_event("BUF", "MIA", "2026-01-18T18:00Z", "Ford Field")]}
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is not None
    assert games[0].stadium.name == "Ford Field"
    assert games[0].stadium.latitude == stadium_for_team("DET").latitude
    assert games[0].stadium.longitude == stadium_for_team("DET").longitude


@respx.mock
def test_renamed_venue_still_resolves_by_alias() -> None:
    # PIT's stadium was renamed from Heinz Field to Acrisure Stadium in 2022;
    # ESPN reporting the old name should still resolve to PIT's stadium.
    payload = {"events": [_event("PIT", "BAL", "2026-01-18T18:00Z", "Heinz Field")]}
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is not None
    assert games[0].stadium.name == stadium_for_team("PIT").name


@respx.mock
def test_renamed_venue_resolves_by_stable_id_even_with_an_unknown_name() -> None:
    gb_stadium = stadium_for_team("GB")
    renamed = dataclasses.replace(gb_stadium, venue_id="3810")
    catalog = VenueCatalog([renamed], by_team={"GB": renamed})
    sport = dataclasses.replace(NFL, venue_catalog=lambda: catalog)
    payload = {
        "events": [_event("GB", "CHI", "2026-01-18T18:00Z", "Some Sponsor Field", venue_id="3810")]
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE, sport=sport)

    assert games[0].stadium is not None
    assert games[0].stadium.latitude == gb_stadium.latitude
    assert games[0].stadium.longitude == gb_stadium.longitude


@respx.mock
def test_shipped_venue_id_resolves_a_renamed_venue_without_monkeypatching() -> None:
    # PIT's confirmed ESPN venue id, already in the shipped STADIUMS table (pulled
    # from a live scoreboard response) - proves ID-first resolution works end to end
    # against production data, not just a monkeypatched stadium record.
    pit_stadium = stadium_for_team("PIT")
    payload = {
        "events": [
            _event("PIT", "BAL", "2026-01-18T18:00Z", "Some Future Sponsor Field", venue_id="3752")
        ]
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is not None
    assert games[0].stadium.latitude == pit_stadium.latitude
    assert games[0].stadium.longitude == pit_stadium.longitude


@respx.mock
def test_unrecognized_us_venue_is_logged_as_drift_with_id_and_name() -> None:
    payload = {
        "events": [_event("BUF", "MIA", "2026-01-18T18:00Z", "Some New Stadium", venue_id="99999")],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is True
    assert games[0].unresolved_reason is not None
    assert "Some New Stadium" in games[0].unresolved_reason
    assert "99999" in games[0].unresolved_reason
    assert venue_drift(games) == [games[0].unresolved_reason]


@respx.mock
def test_unrecognized_team_with_no_venue_data_has_no_resolvable_stadium() -> None:
    # ESPN omits the "venue" key entirely - not just an empty name/id within it.
    payload = {
        "events": [
            {
                "date": "2026-01-18T18:00Z",
                "competitions": [
                    {
                        "date": "2026-01-18T18:00Z",
                        "competitors": [
                            {"homeAway": "home", "team": {"abbreviation": "XYZ"}},
                            {"homeAway": "away", "team": {"abbreviation": "MIA"}},
                        ],
                    }
                ],
            }
        ],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is False
    assert games[0].unresolved_reason == "unrecognized home team 'XYZ'"


@respx.mock
def test_present_but_incomplete_international_venue_is_not_a_home_stadium_fallback() -> None:
    # A venue object with an address but no id/name is *present*, not absent - it
    # must not fall back to the home team's usual stadium (which could be a
    # different, wrong location), even though id and name are both blank.
    payload = {
        "events": [
            _event("JAX", "NE", "2026-01-18T18:00Z", "", venue_country="England"),
        ],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is False
    assert games[0].unresolved_reason == 'international venue ""'


@respx.mock
def test_explicit_empty_venue_object_is_not_a_home_stadium_fallback() -> None:
    # ESPN sending `"venue": {}` is a *present* (if empty) venue object, distinct
    # from omitting the "venue" key entirely - it must not be treated as "no venue
    # data" and silently resolved to the home team's usual stadium.
    payload = {
        "events": [
            {
                "date": "2026-01-18T18:00Z",
                "competitions": [
                    {
                        "date": "2026-01-18T18:00Z",
                        "venue": {},
                        "competitors": [
                            {"homeAway": "home", "team": {"abbreviation": "BUF"}},
                            {"homeAway": "away", "team": {"abbreviation": "MIA"}},
                        ],
                    }
                ],
            }
        ],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is True
    assert games[0].unresolved_reason == "unrecognized venue \"\" (espn venue id '')"


@respx.mock
def test_unknown_country_is_drift_not_silently_treated_as_international() -> None:
    # A present venue with a name we don't recognize and no country at all should
    # be reported as drift (loud), not silently classified as international (which
    # `venue_drift()` ignores) - an unrecognized US venue must never go quiet just
    # because ESPN happened to omit the country field.
    payload = {
        "events": [_event("BUF", "MIA", "2026-01-18T18:00Z", "Some New Stadium", venue_country="")],
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is None
    assert games[0].is_venue_drift is True
    assert venue_drift(games) == [games[0].unresolved_reason]


@respx.mock
def test_espn_indoor_override_treats_normally_open_stadium_as_covered() -> None:
    payload = {
        "events": [
            _event("GB", "CHI", "2026-01-18T18:00Z", "Lambeau Field", venue_indoor=True),
        ]
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert games[0].stadium is not None
    assert games[0].stadium.is_covered is True


@respx.mock
def test_events_outside_target_date_are_excluded() -> None:
    payload = {
        "events": [
            # 9:30pm ET Jan 18 -> still Jan 18 in the game-day timezone.
            _event("GB", "CHI", "2026-01-19T02:30Z", "Lambeau Field"),
            # 1pm ET Jan 19 -> Jan 19, should be excluded.
            _event("KC", "DEN", "2026-01-19T18:00Z", "GEHA Field at Arrowhead Stadium"),
        ]
    }
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert len(games) == 1
    assert games[0].home_team == "GB"


@respx.mock
def test_no_games_returns_empty_list() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    assert get_todays_games(TARGET_DATE) == []


@respx.mock
def test_missing_events_key_is_treated_as_no_games() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={}))
    assert get_todays_games(TARGET_DATE) == []


@respx.mock
def test_non_object_payload_raises() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(ValueError, match="expected object"):
        get_todays_games(TARGET_DATE)


@respx.mock
def test_malformed_events_shape_raises() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": "oops"}))
    with pytest.raises(ValueError, match="expected a list"):
        get_todays_games(TARGET_DATE)


@respx.mock
def test_non_object_event_item_raises() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": ["oops"]}))
    with pytest.raises(ValueError, match="expected object"):
        get_todays_games(TARGET_DATE)


@respx.mock
def test_retries_a_5xx_from_espn_and_still_succeeds() -> None:
    payload = {"events": [_event("GB", "CHI", "2026-01-18T18:00Z", "Lambeau Field")]}
    respx.get(SCOREBOARD_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=payload)]
    )

    games = get_todays_games(TARGET_DATE)

    assert len(games) == 1
    assert games[0].home_team == "GB"


@respx.mock
def test_raises_after_persistent_5xx_from_espn() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        get_todays_games(TARGET_DATE)


def _game(home: str, away: str, venue_name: str, unresolved_reason: str | None = None) -> Game:
    return Game(
        home_team=home,
        away_team=away,
        kickoff=dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC),
        stadium=stadium_for_team(home) if home in {"MIN", "GB"} else None,
        venue_name=venue_name,
        unresolved_reason=unresolved_reason,
    )


def test_skip_reason_is_none_for_an_outdoor_game() -> None:
    assert skip_reason(_game("GB", "CHI", "Lambeau Field")) is None


def test_skip_reason_names_a_covered_stadium() -> None:
    game = Game(
        home_team="MIN",
        away_team="DET",
        kickoff=dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC),
        stadium=stadium_for_team("MIN"),
        venue_name="U.S. Bank Stadium",
    )
    assert skip_reason(game) == "covered stadium (U.S. Bank Stadium)"


def test_skip_reason_names_an_international_venue() -> None:
    game = _game(
        "JAX",
        "PHI",
        "Tottenham Hotspur Stadium",
        unresolved_reason='international venue "Tottenham Hotspur Stadium"',
    )
    assert skip_reason(game) == 'international venue "Tottenham Hotspur Stadium"'


def test_skip_reason_names_an_unrecognized_team() -> None:
    game = _game("XYZ", "PHI", "Some Stadium", unresolved_reason="unrecognized home team 'XYZ'")
    assert skip_reason(game) == "unrecognized home team 'XYZ'"


@respx.mock
def test_a_kickoff_without_a_timezone_skips_only_that_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Rejected rather than assumed to be in the machine's timezone (which could put the
    # game on the wrong day) - but one odd entry mustn't cost the rest of the slate.
    odd = _event("BUF", "NE", "2026-01-18T18:00", "Highmark Stadium")
    odd["shortName"] = "NE @ BUF"
    payload = {"events": [odd, _event("GB", "CHI", "2026-01-18T18:00Z", "Lambeau Field")]}
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    games = get_todays_games(TARGET_DATE)

    assert [game.home_team for game in games] == ["GB"]
    warning = next(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "'NE @ BUF'" in warning
    assert "'2026-01-18T18:00'" in warning
