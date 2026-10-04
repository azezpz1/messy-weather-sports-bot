import dataclasses

import pytest

from messy_weather_sports_bot.stadiums import STADIUMS, stadium_for_team, stadium_for_venue
from messy_weather_sports_bot.venues import VenueCatalog


def test_all_32_teams_present() -> None:
    assert len(STADIUMS) == 32


@pytest.mark.parametrize("team", ["XYZ", "FOO", ""])
def test_unknown_team_raises(team: str) -> None:
    with pytest.raises(KeyError):
        stadium_for_team(team)


@pytest.mark.parametrize(
    "team", ["NO", "DET", "MIN", "LV", "LAC", "LAR", "DAL", "ARI", "HOU", "IND", "ATL"]
)
def test_known_covered_stadiums(team: str) -> None:
    assert stadium_for_team(team).is_covered is True


@pytest.mark.parametrize("team", ["GB", "CHI", "KC", "DEN", "BUF", "NE", "SEA", "PIT"])
def test_known_outdoor_stadiums(team: str) -> None:
    assert stadium_for_team(team).is_covered is False


def test_stadium_for_venue_matches_by_current_name() -> None:
    assert stadium_for_venue("", "Lambeau Field") is stadium_for_team("GB")


def test_stadium_for_venue_matches_by_alias() -> None:
    # PIT's stadium was renamed from Heinz Field to Acrisure Stadium in 2022.
    assert stadium_for_venue("", "Heinz Field") is stadium_for_team("PIT")


def test_production_venue_id_resolves_a_renamed_venue_without_an_alias() -> None:
    # PIT's confirmed ESPN venue id (pulled from a live scoreboard response), used
    # here with a name that isn't PIT's current name *or* a listed alias - proving
    # ID-first resolution works against the shipped STADIUMS table, not just a
    # monkeypatched one.
    pit = stadium_for_team("PIT")
    assert pit.venue_id == "3752"
    assert stadium_for_venue("3752", "Some Future Sponsor Field") is pit


def test_houston_resolves_under_both_reliant_and_nrg_names() -> None:
    # The stadium went back from NRG Stadium to Reliant Stadium in August 2026
    # (ESPN venue id 3891); the weekly venue-drift check flagged it on 2026-09-28.
    hou = stadium_for_team("HOU")
    assert hou.name == "Reliant Stadium"
    assert stadium_for_venue("3891", "Reliant Stadium") is hou
    assert stadium_for_venue("", "Reliant Stadium") is hou
    assert stadium_for_venue("", "NRG Stadium") is hou


def test_a_catalog_matches_by_id_even_with_an_unknown_name() -> None:
    renamed = dataclasses.replace(stadium_for_team("DET"), venue_id="3902")

    assert VenueCatalog([renamed]).for_venue("3902", "Some Future Name") is renamed


def test_stadium_for_venue_returns_none_for_unknown_venue() -> None:
    assert stadium_for_venue("", "Tottenham Hotspur Stadium") is None
    assert stadium_for_venue("999999", "") is None
