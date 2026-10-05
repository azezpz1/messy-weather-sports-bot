import pytest

from messy_weather_sports_bot.venues import StadiumInfo, VenueCatalog, resolve_venue

OLD_PARK = StadiumInfo(
    "Old Park", 40.0, -80.0, is_covered=False, venue_id="1", aliases=("The Old",)
)
NEW_DOME = StadiumInfo("New Dome", 41.0, -81.0, is_covered=True, venue_id="2")
# Lists the same building as OLD_PARK, as two teams that share a stadium do.
OLD_PARK_TWIN = StadiumInfo("Old Park", 40.0, -80.0, is_covered=False, venue_id="1")
NO_ID = StadiumInfo("Unnumbered Field", 42.0, -82.0, is_covered=False)


def test_a_venue_id_wins_over_a_conflicting_name() -> None:
    catalog = VenueCatalog([OLD_PARK, NEW_DOME])
    # The id says New Dome even though the name says Old Park.
    assert catalog.for_venue("2", "Old Park") is NEW_DOME


def test_falls_back_to_the_name_when_the_id_is_unknown_or_blank() -> None:
    catalog = VenueCatalog([OLD_PARK, NEW_DOME])
    assert catalog.for_venue("999", "New Dome") is NEW_DOME
    assert catalog.for_venue("", "New Dome") is NEW_DOME


def test_matches_a_historical_name_by_alias() -> None:
    assert VenueCatalog([OLD_PARK]).for_venue("", "The Old") is OLD_PARK


def test_a_stadium_without_a_venue_id_matches_by_name() -> None:
    assert VenueCatalog([NO_ID]).for_venue("", "Unnumbered Field") is NO_ID


def test_the_first_stadium_wins_when_two_share_an_id() -> None:
    first = StadiumInfo("First Name", 40.0, -80.0, is_covered=False, venue_id="1")
    second = StadiumInfo("Second Name", 41.0, -81.0, is_covered=False, venue_id="1")

    assert VenueCatalog([first, second]).for_venue("1", "") is first


def test_the_same_building_listed_twice_still_resolves_by_name() -> None:
    catalog = VenueCatalog([OLD_PARK, OLD_PARK_TWIN])

    assert catalog.for_venue("", "Old Park") is OLD_PARK


def test_an_id_only_catalog_ignores_names_and_aliases() -> None:
    catalog = VenueCatalog([OLD_PARK, NEW_DOME], match_names=False)

    assert catalog.for_venue("1", "") is OLD_PARK
    assert catalog.for_venue("", "Old Park") is None
    assert catalog.for_venue("", "The Old") is None
    assert catalog.for_venue("999", "New Dome") is None  # an unknown id isn't rescued by a name


def test_an_id_only_catalog_keeps_the_first_stadium_for_a_shared_id() -> None:
    first = StadiumInfo("First Name", 40.0, -80.0, is_covered=False, venue_id="1")
    second = StadiumInfo("Second Name", 41.0, -81.0, is_covered=False, venue_id="1")

    assert VenueCatalog([first, second], match_names=False).for_venue("1", "") is first


def test_a_town_sharing_a_name_with_a_known_stadium_is_reported_as_drift_not_guessed() -> None:
    # "Old Park" is also the name of a stadium elsewhere; an id-only catalog doesn't know id 77.
    catalog = VenueCatalog([OLD_PARK], match_names=False)

    result = resolve_venue(catalog, "XYZ", {"fullName": "Old Park", "id": "77"}, venue_present=True)

    assert result[0] is None
    assert result[2] is True
    assert "Old Park" in str(result[1])


def test_returns_none_for_an_unknown_venue() -> None:
    catalog = VenueCatalog([OLD_PARK])
    assert catalog.for_venue("999", "Somewhere Else") is None


def test_a_blank_id_and_name_never_match() -> None:
    blank_named = StadiumInfo("", 43.0, -83.0, is_covered=False)
    assert VenueCatalog([blank_named]).for_venue("", "") is None


def test_for_team_returns_the_teams_usual_stadium_or_raises() -> None:
    catalog = VenueCatalog([OLD_PARK], by_team={"OLD": OLD_PARK})
    assert catalog.for_team("OLD") is OLD_PARK
    with pytest.raises(KeyError):
        catalog.for_team("XYZ")


def test_a_catalog_is_built_once_and_ignores_later_changes_to_its_inputs() -> None:
    by_team = {"OLD": OLD_PARK}
    stadiums = [OLD_PARK]
    catalog = VenueCatalog(stadiums, by_team=by_team)

    by_team["NEW"] = NEW_DOME
    stadiums.append(NEW_DOME)

    assert catalog.for_venue("2", "New Dome") is None
    with pytest.raises(KeyError):
        catalog.for_team("NEW")


def test_resolve_venue_falls_back_to_the_home_team_only_when_espn_sent_no_venue() -> None:
    catalog = VenueCatalog([OLD_PARK], by_team={"OLD": OLD_PARK})

    absent = resolve_venue(catalog, "OLD", {}, venue_present=False)
    explicit_empty = resolve_venue(catalog, "OLD", {}, venue_present=True)

    assert absent == (OLD_PARK, None, False)
    assert explicit_empty[0] is None
    assert explicit_empty[2] is True  # an unrecognized US venue is drift, not a guess


def test_resolve_venue_marks_an_espn_indoor_game_as_covered() -> None:
    catalog = VenueCatalog([OLD_PARK])

    stadium, reason, is_drift = resolve_venue(
        catalog, "OLD", {"id": "1", "fullName": "Old Park", "indoor": True}, venue_present=True
    )

    assert stadium is not None
    assert stadium.is_covered is True
    assert (stadium.latitude, stadium.longitude) == (OLD_PARK.latitude, OLD_PARK.longitude)
    assert (reason, is_drift) == (None, False)


def test_resolve_venue_reports_an_unrecognized_home_team_without_a_venue() -> None:
    catalog = VenueCatalog([OLD_PARK], by_team={"OLD": OLD_PARK})

    assert resolve_venue(catalog, "XYZ", {}, venue_present=False) == (
        None,
        "unrecognized home team 'XYZ'",
        False,
    )


def test_a_catalog_without_team_stadiums_says_no_venue_was_reported() -> None:
    # A college catalog has no "home team's usual stadium" to fall back on, so a team isn't
    # "unrecognized" - ESPN just didn't say where the game is.
    catalog = VenueCatalog([OLD_PARK])

    assert catalog.has_team_stadiums is False
    assert resolve_venue(catalog, "Ohio State", {}, venue_present=False) == (
        None,
        "no venue reported for 'Ohio State'",
        False,
    )


def test_a_catalog_with_team_stadiums_still_names_an_unrecognized_team() -> None:
    catalog = VenueCatalog([OLD_PARK], by_team={"OLD": OLD_PARK})

    assert catalog.has_team_stadiums is True
