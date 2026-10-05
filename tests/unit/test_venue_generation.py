import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from messy_weather_sports_bot import venue_generation as gen
from messy_weather_sports_bot.espn import CFB_SCOREBOARD_URL
from messy_weather_sports_bot.venue_generation import (
    Geocode,
    GeocodeOutcome,
    Harvest,
    JsonCache,
    PhotonGeocoder,
    RateLimiter,
    build_table,
    format_report,
    observe_scoreboard,
    parse_overrides,
    parse_photon_feature,
    season_ranges,
)
from messy_weather_sports_bot.venue_table import VenueRow, dump_table, parse_table
from messy_weather_sports_bot.venues import StadiumInfo, VenueCatalog

# --------------------------------------------------------------------- fixtures


def cfb_event(
    venue_id: str | None,
    name: str = "Old Park",
    city: str = "Athens",
    state: str = "GA",
    *,
    country: str = "USA",
    indoor: bool = False,
    neutral: bool = False,
    home: tuple[str, str] = ("61", "Georgia Bulldogs"),
    away: tuple[str, str] = ("238", "Vanderbilt Commodores"),
) -> dict:
    venue: dict = {
        "fullName": name,
        "indoor": indoor,
        "address": {"city": city, "state": state, "country": country},
    }
    if venue_id is not None:
        venue["id"] = venue_id
    return {
        "competitions": [
            {
                "neutralSite": neutral,
                "venue": venue,
                "competitors": [
                    {"homeAway": "home", "team": {"id": home[0], "displayName": home[1]}},
                    {"homeAway": "away", "team": {"id": away[0], "displayName": away[1]}},
                ],
            }
        ]
    }


def harvest_of(*events: dict) -> Harvest:
    result = Harvest()
    observe_scoreboard({"events": list(events)}, result)
    return result


def photon_feature(
    name: str,
    lat: float = 33.9,
    lon: float = -83.3,
    *,
    state: str = "Georgia",
    city: str | None = "Athens",
    key: str = "leisure",
    value: str = "stadium",
    osm_type: str = "W",
    osm_id: int = 1,
    country: str = "US",
) -> dict:
    properties = {
        "name": name,
        "state": state,
        "countrycode": country,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_key": key,
        "osm_value": value,
    }
    if city:
        properties["city"] = city
    return {"geometry": {"coordinates": [lon, lat]}, "properties": properties}


def geocode_of(**changes) -> Geocode:
    values = {
        "latitude": 33.9,
        "longitude": -83.3,
        "name": "Old Park",
        "osm_type": "W",
        "osm_id": 1,
        "osm_key": "leisure",
        "osm_value": "stadium",
        "places": ("Athens",),
        "state": "Georgia",
    }
    values.update(changes)
    return Geocode(**values)


class FakeGeocoder:
    """Answers from a name -> outcome table, and records what it was asked."""

    def __init__(self, outcomes: dict[str, GeocodeOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, str, str]] = []

    def geocode(self, name: str, city: str, state: str) -> GeocodeOutcome:
        self.calls.append((name, city, state))
        return self.outcomes.get(name, GeocodeOutcome(None, candidates=("nothing nearby",)))


EMPTY_NFL = VenueCatalog([])


def old_park_row(**changes) -> VenueRow:
    """A row already in the table: Old Park, in Georgia (inside the state's box)."""
    row = VenueRow("1", "Old Park", 33.95, -83.37, False, "Athens", "GA", "stadium", "photon:W5")
    return replace(row, **changes)


def _table(harvested: Harvest, geocoder: FakeGeocoder, **kwargs):
    kwargs.setdefault("nfl_catalog", EMPTY_NFL)
    return build_table(harvested, geocoder=geocoder, **kwargs)


# ------------------------------------------------------------------ observation


def test_observing_a_scoreboard_counts_each_venues_games() -> None:
    harvested = harvest_of(
        cfb_event("1", "Old Park"),
        cfb_event("1", "Old Park", neutral=True),
        cfb_event("2", "New Dome", "Dallas", "TX", indoor=True),
    )

    old_park, new_dome = harvested.venues["1"], harvested.venues["2"]
    assert (old_park.games, old_park.indoor) == (2, False)
    assert (new_dome.games, new_dome.indoor, new_dome.state) == (1, True, "TX")
    assert harvested.events == 3


def test_a_venue_is_indoor_if_espn_ever_said_so() -> None:
    harvested = harvest_of(cfb_event("1"), cfb_event("1", indoor=True), cfb_event("1"))

    assert harvested.venues["1"].indoor is True


def test_a_venue_seen_under_two_names_keeps_the_common_one_and_the_other_as_an_alias() -> None:
    harvested = harvest_of(
        cfb_event("1", "New Name Field"),
        cfb_event("1", "Old Name Field"),
        cfb_event("1", "New Name Field"),
    )

    venue = harvested.venues["1"]
    assert venue.name == "New Name Field"
    assert venue.other_names == ("Old Name Field",)


def test_teams_home_games_exclude_neutral_sites() -> None:
    harvested = harvest_of(
        cfb_event("1"),
        cfb_event("1"),
        cfb_event("9", "Bowl Dome", neutral=True),
    )

    georgia = harvested.teams["61"]
    assert (georgia.games, georgia.home_games) == (3, 2)
    assert dict(georgia.home_venue_ids) == {"1": 2}
    assert harvested.teams["238"].home_games == 0


def test_an_event_without_a_venue_id_is_counted_not_fatal() -> None:
    harvested = harvest_of(cfb_event(None), cfb_event("1"))

    assert harvested.events_without_venue_id == 1
    assert list(harvested.venues) == ["1"]


@pytest.mark.parametrize("payload", [None, [], "x", {"events": None}, {"events": ["x", {}]}])
def test_a_malformed_scoreboard_is_ignored(payload: object) -> None:
    harvested = Harvest()
    observe_scoreboard(payload, harvested)

    assert harvested.venues == {}


# ----------------------------------------------------------------- date ranges


def test_season_ranges_cover_last_season_and_this_one_so_far() -> None:
    assert season_ranges(dt.date(2026, 10, 4)) == [
        (dt.date(2025, 8, 20), dt.date(2026, 1, 25)),
        (dt.date(2026, 8, 20), dt.date(2026, 10, 4)),
    ]


def test_season_ranges_in_the_offseason_are_just_the_last_season() -> None:
    assert season_ranges(dt.date(2026, 6, 1)) == [(dt.date(2025, 8, 20), dt.date(2026, 1, 25))]


def test_season_ranges_stop_at_today_mid_playoff() -> None:
    assert season_ranges(dt.date(2026, 1, 10)) == [(dt.date(2025, 8, 20), dt.date(2026, 1, 10))]


@respx.mock
def test_harvest_fetches_each_day_with_groups_and_limit_and_pauses_between() -> None:
    route = respx.get(CFB_SCOREBOARD_URL).mock(
        return_value=httpx.Response(200, json={"events": [cfb_event("1")]})
    )
    pauses: list[float] = []

    with httpx.Client() as client:
        result = gen.harvest(
            client,
            [
                (dt.date(2025, 9, 6), dt.date(2025, 9, 7)),
                (dt.date(2025, 9, 13), dt.date(2025, 9, 13)),
            ],
            sleep=pauses.append,
            pause=0.5,
        )

    requested = [dict(call.request.url.params) for call in route.calls]
    assert [p["dates"] for p in requested] == ["20250906", "20250907", "20250913"]
    assert all(p["groups"] == "80" and p["limit"] == "400" for p in requested)
    assert pauses == [0.5, 0.5, 0.5]
    assert (result.days, result.events, result.venues["1"].games) == (3, 3, 3)


@respx.mock
def test_harvest_does_not_send_a_custom_user_agent_header_of_its_own() -> None:
    # ESPN's edge refuses unfamiliar User-Agents; the harvest must leave httpx's default.
    route = respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={}))

    with httpx.Client() as client:
        gen.harvest(client, [(dt.date(2025, 9, 6), dt.date(2025, 9, 6))], sleep=lambda s: None)

    assert route.calls.last.request.headers["user-agent"].startswith("python-httpx/")


@respx.mock
def test_harvest_raises_when_a_day_keeps_failing() -> None:
    respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(503))

    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        gen.harvest(client, [(dt.date(2025, 9, 6), dt.date(2025, 9, 6))], sleep=lambda s: None)


# ---------------------------------------------------------------- photon parsing


def test_a_photon_feature_parses_to_a_geocode() -> None:
    geocode = parse_photon_feature(
        photon_feature("Sanford Stadium", 33.94979, -83.37321, osm_id=598)
    )

    assert geocode is not None
    assert (geocode.latitude, geocode.longitude) == (33.94979, -83.37321)
    assert (geocode.source, geocode.precision) == ("photon:W598", "stadium")
    assert geocode.osm_url == "https://www.openstreetmap.org/way/598"
    assert geocode.places == ("Athens",)


@pytest.mark.parametrize(
    "feature",
    [
        None,
        "x",
        {},
        {"geometry": {"coordinates": [1]}, "properties": {"name": "x", "countrycode": "US"}},
        {"geometry": {"coordinates": ["a", "b"]}, "properties": {"name": "x", "countrycode": "US"}},
        photon_feature("Wembley Stadium", country="GB"),
        {"geometry": {"coordinates": [-83.0, 33.0]}, "properties": {"countrycode": "US"}},
    ],
)
def test_a_photon_feature_missing_what_we_need_is_dropped(feature: object) -> None:
    assert parse_photon_feature(feature) is None


# ------------------------------------------------------------------- geocoding


def _geocoder(**kwargs) -> PhotonGeocoder:
    kwargs.setdefault("limiter", RateLimiter(0))
    return PhotonGeocoder(httpx.Client(), **kwargs)


@respx.mock
def test_an_exact_stadium_in_the_right_state_is_accepted() -> None:
    route = respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(200, json={"features": [photon_feature("Old Park")]})
    )

    outcome = _geocoder().geocode("Old Park", "Athens", "GA")

    assert outcome.result is not None
    assert outcome.result.name == "Old Park"
    assert outcome.notes == ()
    # The stadium-tagged search comes first.
    assert dict(route.calls.last.request.url.params)["osm_tag"] == "leisure:stadium"
    assert dict(route.calls.last.request.url.params)["q"] == "Old Park, Athens, Georgia"


@respx.mock
def test_a_result_in_another_state_is_rejected_and_reported() -> None:
    respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(
            200, json={"features": [photon_feature("Old Park", state="Nebraska", city="Lincoln")]}
        )
    )

    outcome = _geocoder().geocode("Old Park", "Athens", "GA")

    assert outcome.result is None
    assert outcome.candidates == ("Old Park (leisure=stadium, Lincoln, Nebraska)",)


@respx.mock
def test_a_partial_name_match_is_only_trusted_in_the_right_city() -> None:
    features = [
        photon_feature("Memorial Stadium", city="Urbana", osm_id=1, state="Illinois"),
        photon_feature("Memorial Stadium", city="Champaign", osm_id=2, state="Illinois"),
    ]
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": features}))

    outcome = _geocoder().geocode("Gies Memorial Stadium", "Champaign", "IL")

    assert outcome.result is not None
    assert outcome.result.osm_id == 2
    assert outcome.notes == ()


@respx.mock
def test_an_exact_name_in_a_different_city_is_accepted_with_a_note() -> None:
    respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(
            200, json={"features": [photon_feature("Old Park", city="Bogart")]}
        )
    )

    outcome = _geocoder().geocode("Old Park", "Athens", "GA")

    assert outcome.result is not None
    assert outcome.notes == ("city differs: OSM says Bogart",)


@respx.mock
def test_the_right_city_beats_an_exact_name_in_the_wrong_one() -> None:
    features = [
        photon_feature("Memorial Stadium", city="Bloomington", osm_id=1, state="Illinois"),
        photon_feature("Gies Memorial Stadium", city="Champaign", osm_id=2, state="Illinois"),
    ]
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": features}))

    outcome = _geocoder().geocode("Memorial Stadium", "Champaign", "IL")

    assert outcome.result is not None
    assert outcome.result.osm_id == 2


@respx.mock
def test_a_sports_complex_mapped_as_a_pitch_is_found_by_the_second_search() -> None:
    pitch = photon_feature(
        "Ching Complex", key="leisure", value="pitch", state="Hawaii", city="Honolulu"
    )
    route = respx.get(gen.PHOTON_URL).mock(
        side_effect=[
            httpx.Response(200, json={"features": []}),
            httpx.Response(200, json={"features": [pitch]}),
        ]
    )

    outcome = _geocoder().geocode("Ching Complex", "Honolulu", "HI")

    assert outcome.result is not None
    assert outcome.result.precision == "other"
    assert outcome.notes == ("mapped as leisure=pitch, not a stadium",)
    assert "osm_tag" not in dict(route.calls.last.request.url.params)


@respx.mock
def test_a_pitch_is_not_accepted_by_the_stadium_only_search() -> None:
    pitch = photon_feature("Old Park", key="leisure", value="pitch")
    respx.get(gen.PHOTON_URL).mock(
        side_effect=[
            httpx.Response(200, json={"features": [pitch]}),  # tagged search: not a stadium class
            httpx.Response(200, json={"features": []}),
        ]
    )

    assert _geocoder().geocode("Old Park", "Athens", "GA").result is None


@respx.mock
def test_other_osm_classes_are_never_accepted() -> None:
    shop = photon_feature("Old Park", key="shop", value="sports")
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": [shop]}))

    outcome = _geocoder().geocode("Old Park", "Athens", "GA")

    assert outcome.result is None
    assert outcome.candidates == ("Old Park (shop=sports, Athens, Georgia)",)


def test_an_unknown_state_code_is_reported_without_a_request() -> None:
    outcome = _geocoder().geocode("Old Park", "Athens", "ZZ")

    assert outcome.result is None
    assert "ZZ" in outcome.notes[0]


@respx.mock
def test_responses_are_cached_so_a_repeat_query_makes_no_request(tmp_path: Path) -> None:
    route = respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(200, json={"features": [photon_feature("Old Park")]})
    )
    cache_path = tmp_path / "cache.json"

    _geocoder(cache=JsonCache(cache_path)).geocode("Old Park", "Athens", "GA")
    requests_after_first = route.call_count
    _geocoder(cache=JsonCache(cache_path)).geocode("Old Park", "Athens", "GA")  # new process

    assert requests_after_first == 1
    assert route.call_count == requests_after_first


@respx.mock
def test_a_search_waits_on_the_rate_limiter_before_each_request() -> None:
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": []}))
    waits: list[str] = []

    class RecordingLimiter(RateLimiter):
        def wait(self) -> None:
            waits.append("wait")

    _geocoder(limiter=RecordingLimiter(0)).geocode("Old Park", "Athens", "GA")

    assert waits == ["wait", "wait"]  # the stadium search, then the fallback search


@respx.mock
def test_a_persistent_server_error_is_raised() -> None:
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        _geocoder().geocode("Old Park", "Athens", "GA")


def test_the_rate_limiter_spaces_calls_apart() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(1.1, clock=lambda: now[0], sleep=sleep)
    limiter.wait()  # first call: nothing to wait for
    now[0] += 0.4
    limiter.wait()  # 0.4s since the last call: wait out the other 0.7s
    now[0] += 5
    limiter.wait()  # long enough ago: no wait

    assert slept == [pytest.approx(0.7)]


def test_a_cache_file_that_is_corrupt_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text("{not json")

    cache = JsonCache(path)

    assert cache.get("anything") is None
    cache.put("k", [1])
    assert json.loads(path.read_text()) == {"k": [1]}


def test_an_in_memory_cache_writes_nothing() -> None:
    cache = JsonCache()
    cache.put("k", [1])

    assert cache.get("k") == [1]


# ------------------------------------------------------------------- overrides


def _overrides(venues: dict) -> dict:
    return parse_overrides(json.dumps({"schema_version": 1, "venues": venues}))


def test_overrides_parse_with_their_notes() -> None:
    overrides = _overrides(
        {
            "5": {"note": "moved", "latitude": 41.0, "longitude": -81.0, "is_covered": True},
            "6": {"note": "not a real venue", "exclude": True},
        }
    )

    assert overrides["5"].latitude == 41.0
    assert overrides["5"].is_covered is True
    assert overrides["6"].exclude is True


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nope", "not valid JSON"),
        ('{"schema_version": 2}', "schema_version 1"),
        ('{"schema_version": 1, "venues": []}', "keyed by ESPN venue id"),
    ],
)
def test_a_malformed_overrides_file_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_overrides(text)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({}, "needs a 'note'"),
        ({"note": " "}, "needs a 'note'"),
        ({"note": "x", "colour": "red"}, "unknown fields"),
        ({"note": "x", "latitude": 1.0}, "latitude and longitude together"),
        ({"note": "x", "latitude": "1", "longitude": 2}, "'latitude' must be a number"),
        ({"note": "x", "aliases": "a"}, "'aliases' must be a list of non-blank strings"),
        ({"note": "x", "aliases": ["ok", " "]}, "'aliases' must be a list of non-blank strings"),
        ({"note": "x", "exclude": "yes"}, "'exclude' must be true or false"),
        ({"note": "x", "is_covered": 1}, "'is_covered' must be true or false"),
        ({"note": "x", "name": " "}, "'name' must be a non-blank string"),
        ({"note": "x", "city": 3}, "'city' must be a non-blank string"),
        ({"note": "x", "state": "ZZ"}, "unknown state code"),
        ({"note": "x", "latitude": 91, "longitude": 0}, "'latitude' out of range"),
        ({"note": "x", "latitude": 0, "longitude": -181}, "'longitude' out of range"),
        ({"note": "x", "latitude": True, "longitude": 0}, "'latitude' must be a number"),
        ("x", "must be an object"),
    ],
)
def test_a_malformed_override_is_rejected_naming_the_venue(override: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _overrides({"7": override})


# ----------------------------------------------------------------------- build


def test_a_new_venue_is_geocoded_and_seeded_covered_from_espn_indoor() -> None:
    dallas = geocode_of(name="New Dome", osm_id=9, latitude=32.78, longitude=-96.8)
    geocoder = FakeGeocoder({"New Dome": GeocodeOutcome(dallas)})
    harvested = harvest_of(cfb_event("2", "New Dome", "Dallas", "TX", indoor=True))

    result = _table(harvested, geocoder)

    (row,) = result.rows
    assert (row.id, row.is_covered, row.source, row.precision) == (
        "2",
        True,
        "photon:W9",
        "stadium",
    )
    assert (row.city, row.state) == ("Dallas", "TX")
    assert geocoder.calls == [("New Dome", "Dallas", "TX")]
    assert [r.id for r, _ in result.report.added] == ["2"]
    assert not result.report.needs_attention


def test_an_open_air_venue_is_not_covered() -> None:
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())})

    (row,) = _table(harvest_of(cfb_event("1")), geocoder).rows

    assert row.is_covered is False


def test_a_venue_that_is_an_nfl_stadium_copies_its_coordinates_and_roof() -> None:
    nfl = VenueCatalog(
        [
            StadiumInfo(
                "Reliant Stadium", 29.68, -95.41, True, venue_id="3891", aliases=("NRG Stadium",)
            )
        ]
    )
    geocoder = FakeGeocoder()
    harvested = harvest_of(cfb_event("3891", "NRG Stadium", "Houston", "TX"))

    (row,) = _table(harvested, geocoder, nfl_catalog=nfl).rows

    assert (row.latitude, row.longitude, row.is_covered) == (29.68, -95.41, True)
    assert (row.source, row.name) == ("nfl_catalog", "NRG Stadium")
    assert row.aliases == ("Reliant Stadium",)
    assert geocoder.calls == []


def test_an_nfl_venue_that_espn_calls_indoor_is_covered_even_if_the_nfl_table_says_open() -> None:
    nfl = VenueCatalog([StadiumInfo("Old Park", 33.95, -83.37, False, venue_id="1")])

    (row,) = _table(harvest_of(cfb_event("1", indoor=True)), FakeGeocoder(), nfl_catalog=nfl).rows

    assert row.is_covered is True


def test_international_venues_are_skipped_and_reported() -> None:
    harvested = harvest_of(cfb_event("7", "Aviva Stadium", "Dublin", "", country="Ireland"))
    geocoder = FakeGeocoder()

    result = _table(harvested, geocoder)

    assert result.rows == []
    assert result.report.international == ["7 Aviva Stadium (Ireland)"]
    assert geocoder.calls == []


def test_a_venue_missing_its_country_is_not_treated_as_international() -> None:
    harvested = harvest_of(cfb_event("7", "Mystery Field", country=""))
    geocoder = FakeGeocoder({"Mystery Field": GeocodeOutcome(geocode_of(name="Mystery Field"))})

    assert [r.id for r in _table(harvested, geocoder).rows] == ["7"]


def test_a_venue_that_cannot_be_placed_is_reported_not_guessed() -> None:
    geocoder = FakeGeocoder(
        {
            "Odd Field": GeocodeOutcome(
                None, candidates=("Odd Field (leisure=pitch, Elsewhere, Ohio)",)
            )
        }
    )

    result = _table(harvest_of(cfb_event("4", "Odd Field")), geocoder)

    assert result.rows == []
    (unplaced,) = result.report.needs_manual
    assert unplaced.venue_id == "4"
    assert unplaced.reasons == ("rejected: Odd Field (leisure=pitch, Elsewhere, Ohio)",)
    assert result.report.needs_attention


def test_existing_rows_are_kept_without_geocoding_again() -> None:
    existing = [old_park_row()]
    geocoder = FakeGeocoder()

    result = _table(harvest_of(cfb_event("1")), geocoder, existing=existing)

    assert result.rows == existing
    assert result.report.kept == 1
    assert geocoder.calls == []


def test_refresh_recomputes_rows_already_in_the_table() -> None:
    existing = [old_park_row()]
    moved = geocode_of(latitude=33.96, longitude=-83.38)
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(moved)})

    result = _table(harvest_of(cfb_event("1")), geocoder, existing=existing, refresh=True)

    assert (result.rows[0].latitude, result.rows[0].longitude) == (33.96, -83.38)
    assert [(old.latitude, new.latitude) for old, new in result.report.changed] == [(33.95, 33.96)]


def test_a_row_not_hosted_this_time_stays_in_the_table() -> None:
    existing = [VenueRow("9", "Quiet Field", 42.0, -93.6, False, "Ames", "IA", "manual", "manual")]

    result = _table(
        harvest_of(cfb_event("1")),
        FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())}),
        existing=existing,
    )

    assert [r.id for r in result.rows] == ["1", "9"]


def test_an_override_with_coordinates_replaces_a_geocode_and_skips_the_lookup() -> None:
    overrides = _overrides(
        {"1": {"note": "OSM has the old site", "latitude": 33.123456, "longitude": -83.5}}
    )
    geocoder = FakeGeocoder()

    result = _table(harvest_of(cfb_event("1")), geocoder, overrides=overrides)

    (row,) = result.rows
    assert (row.latitude, row.longitude, row.precision, row.source) == (
        33.12346,
        -83.5,
        "manual",
        "manual",
    )
    assert geocoder.calls == []
    assert result.report.added[0][1] == ("manual: OSM has the old site",)


def test_an_override_can_force_a_retractable_roof_covered_and_add_aliases() -> None:
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())})
    overrides = _overrides(
        {"1": {"note": "retractable roof", "is_covered": True, "aliases": ["The Old"]}}
    )

    (row,) = _table(harvest_of(cfb_event("1")), geocoder, overrides=overrides).rows

    assert (row.is_covered, row.aliases) == (True, ("The Old",))
    assert row.source == "photon:W1"  # coordinates untouched


def test_an_override_applies_to_a_row_that_is_already_in_the_table() -> None:
    existing = [old_park_row()]
    overrides = _overrides({"1": {"note": "roof", "is_covered": True}})

    result = _table(
        harvest_of(cfb_event("1")), FakeGeocoder(), existing=existing, overrides=overrides
    )

    assert result.rows[0].is_covered is True
    assert [(old.is_covered, new.is_covered) for old, new in result.report.changed] == [
        (False, True)
    ]


def test_an_override_can_exclude_a_venue() -> None:
    overrides = _overrides({"1": {"note": "not a game venue", "exclude": True}})
    geocoder = FakeGeocoder()

    result = _table(harvest_of(cfb_event("1")), geocoder, overrides=overrides)

    assert result.rows == []
    assert result.report.excluded == ["1 Old Park: not a game venue"]
    assert geocoder.calls == []


def test_an_override_can_supply_a_whole_venue_the_scoreboards_never_showed() -> None:
    overrides = _overrides(
        {
            "8": {
                "note": "new stadium, no games yet",
                "name": "Brand New Stadium",
                "latitude": 40.0,
                "longitude": -100.0,
                "is_covered": False,
                "city": "Lincoln",
                "state": "NE",
            },
            "9": {"note": "incomplete, so not added", "name": "Half A Row"},
        }
    )

    result = _table(Harvest(), FakeGeocoder(), overrides=overrides)

    assert [r.id for r in result.rows] == ["8"]
    assert result.rows[0].source == "manual"


def test_a_team_whose_home_games_are_all_at_unplaced_venues_is_a_coverage_gap() -> None:
    games = [
        cfb_event("1", home=("61", "Georgia Bulldogs"), away=(str(n), f"Opponent {n}"))
        for n in range(7)
    ]
    geocoder = FakeGeocoder()  # places nothing

    result = _table(harvest_of(*games), geocoder)

    assert result.report.coverage_gaps == [
        "Georgia Bulldogs: hosted 7 game(s) at venues not in the table"
    ]
    assert result.report.needs_attention


def test_a_team_that_only_ever_played_on_the_road_is_a_coverage_gap() -> None:
    games = [
        cfb_event(
            "1", neutral=True, home=("61", "Georgia Bulldogs"), away=(str(n), f"Opponent {n}")
        )
        for n in range(7)
    ]
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())})

    gaps = _table(harvest_of(*games), geocoder).report.coverage_gaps

    assert "Georgia Bulldogs: hosted no non-neutral game" in gaps


def test_a_team_seen_in_only_a_few_games_is_not_held_to_coverage() -> None:
    # An FCS opponent that turned up once for a guarantee game isn't an FBS home field.
    result = _table(
        harvest_of(cfb_event("1")), FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())})
    )

    assert result.report.coverage_gaps == []


def test_the_table_is_identical_whatever_order_the_games_arrive_in() -> None:
    events = [
        cfb_event("30", "C Field", "Cityc", "TX"),
        cfb_event("4", "A Field", "Citya", "OH"),
        cfb_event("200", "B Field", "Cityb", "FL"),
    ]
    places = {"A Field": (40.0, -83.0), "B Field": (28.0, -82.0), "C Field": (31.0, -97.0)}
    geocoder = FakeGeocoder(
        {
            name: GeocodeOutcome(geocode_of(name=name, osm_id=i, latitude=lat, longitude=lon))
            for i, (name, (lat, lon)) in enumerate(places.items())
        }
    )

    forward = dump_table(_table(harvest_of(*events), geocoder).rows)
    backward = dump_table(_table(harvest_of(*reversed(events)), geocoder).rows)

    assert forward == backward


def test_the_report_lists_added_rows_with_a_map_link_and_what_needs_a_human() -> None:
    geocoder = FakeGeocoder(
        {
            "Old Park": GeocodeOutcome(
                geocode_of(osm_id=77), notes=("city differs: OSM says Bogart",)
            ),
            "Odd Field": GeocodeOutcome(
                None, candidates=("Odd Field (leisure=pitch, Elsewhere, Ohio)",)
            ),
        }
    )
    harvested = harvest_of(
        cfb_event("1"),
        cfb_event("4", "Odd Field"),
        cfb_event("7", "Aviva Stadium", "Dublin", "", country="Ireland"),
    )

    result = _table(harvested, geocoder)
    text = format_report(result, venues_seen=len(harvested.venues))

    assert "3 venue(s) seen, 1 in the table" in text
    assert "https://www.openstreetmap.org/way/77" in text
    assert "city differs: OSM says Bogart" in text
    assert "Needs a person" in text
    assert "Odd Field (leisure=pitch, Elsewhere, Ohio)" in text
    assert "Skipped as international: 7 Aviva Stadium (Ireland)" in text


def test_a_venue_observation_exposes_its_address_for_the_international_check() -> None:
    observation = harvest_of(cfb_event("1")).venues["1"]

    assert observation.address == {"city": "Athens", "state": "GA", "country": "USA"}


# ------------------------------------------------- name matching (real ESPN/OSM pairs)


@pytest.mark.parametrize(
    ("espn", "osm"),
    [
        ("H. A. Chapman Stadium", "HA Chapman Stadium"),
        ("M. M. Roberts Stadium", "MM Roberts Stadium"),
        ("S.B. Ballard Stadium", "SB Ballard Stadium"),
        ("Houchens Industries-L.T. Smith Stadium", "Houchens Industries-LT Smith Stadium"),
        ("Ross-Ade Stadium", "Ross–Ade Stadium"),  # an en dash must not glue the words
        ("Warren McGuirk Alumni Stadium", "Warren P. McGuirk Alumni Stadium"),
        ("Cafe Field", "Café Field"),
    ],
)
def test_names_that_differ_only_in_punctuation_initials_or_accents_compare_equal(
    espn: str, osm: str
) -> None:
    assert gen._tokens(espn) == gen._tokens(osm)


def test_a_us_bank_style_initialism_stays_one_word() -> None:
    assert gen._tokens("U.S. Bank Stadium") == ("us", "bank", "stadium")


def test_a_disambiguating_suffix_is_dropped_from_a_name() -> None:
    assert gen._clean_name("Memorial Stadium (Norman, OK)") == "Memorial Stadium"
    assert gen._clean_name("Alumni Stadium") == "Alumni Stadium"
    assert gen._clean_name("(Odd)") == "(Odd)"  # never reduced to nothing


def test_the_part_after_at_is_a_second_name_to_search_for() -> None:
    assert gen._query_names("Zoeller Field at Dix Stadium") == [
        "Zoeller Field at Dix Stadium",
        "Dix Stadium",
    ]
    assert gen._query_names("Sanford Stadium") == ["Sanford Stadium"]


@respx.mock
def test_initials_and_dashes_do_not_stop_a_stadium_from_matching() -> None:
    features = [photon_feature("HA Chapman Stadium", state="Oklahoma", city="Tulsa")]
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": features}))

    outcome = _geocoder().geocode("H. A. Chapman Stadium", "Tulsa", "OK")

    assert outcome.result is not None
    assert outcome.notes == ()


@respx.mock
def test_a_disambiguating_suffix_is_left_out_of_the_search_and_the_match() -> None:
    feature = photon_feature(
        "Gaylord Family - Oklahoma Memorial Stadium", state="Oklahoma", city="Norman"
    )
    route = respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(200, json={"features": [feature]})
    )

    outcome = _geocoder().geocode("Memorial Stadium (Norman, OK)", "Norman", "OK")

    assert outcome.result is not None
    assert dict(route.calls.last.request.url.params)["q"] == "Memorial Stadium, Norman, Oklahoma"


@respx.mock
def test_the_part_after_at_is_searched_when_the_full_name_finds_nothing() -> None:
    dix = photon_feature("Dix Stadium", state="Ohio", city="Kent")
    route = respx.get(gen.PHOTON_URL).mock(
        side_effect=[
            httpx.Response(200, json={"features": []}),
            httpx.Response(200, json={"features": [dix]}),
        ]
    )

    outcome = _geocoder().geocode("Zoeller Field at Dix Stadium", "Kent", "OH")

    assert outcome.result is not None
    assert outcome.result.name == "Dix Stadium"
    queries = [dict(call.request.url.params)["q"] for call in route.calls]
    assert queries == [
        "Zoeller Field at Dix Stadium, Kent, Ohio",
        "Dix Stadium, Kent, Ohio",
    ]


@respx.mock
def test_a_city_matches_when_one_names_words_are_within_the_others() -> None:
    feature = photon_feature("Fred C. Yager Stadium", state="Ohio", city="Oxford Township")
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": [feature]}))

    outcome = _geocoder().geocode("Yager Stadium", "Oxford", "OH")

    assert outcome.result is not None
    assert outcome.notes == ()  # a partial name is only accepted in the right city


def test_a_non_finite_coordinate_in_the_overrides_file_is_rejected() -> None:
    text = '{"schema_version": 1, "venues": {"7": {"note": "x", "latitude": NaN, "longitude": 0}}}'

    with pytest.raises(ValueError, match="'latitude' must be a number"):
        parse_overrides(text)


# ----------------------------------------------------------- applying overrides


def test_an_override_with_nothing_to_change_leaves_the_row_alone() -> None:
    row = old_park_row()

    assert gen._apply_override(row, gen.Override(note="just a note")) is row


def test_an_override_changes_only_the_fields_it_names() -> None:
    row = old_park_row(aliases=("The Old",))

    renamed = gen._apply_override(row, gen.Override(note="x", name="New Park", state="SC"))
    recoated = gen._apply_override(row, gen.Override(note="x", is_covered=True, city="Bogart"))

    assert renamed == replace(row, name="New Park", state="SC")
    assert recoated == replace(row, is_covered=True, city="Bogart")


def test_override_coordinates_make_the_row_manual_and_are_rounded() -> None:
    row = old_park_row()
    override = gen.Override(note="x", latitude=33.1234567, longitude=-83.7654321)

    moved = gen._apply_override(row, override)

    assert (moved.latitude, moved.longitude) == (33.12346, -83.76543)
    assert (moved.precision, moved.source) == ("manual", "manual")


def test_a_whole_row_from_an_override_has_every_field_from_the_override() -> None:
    override = gen.Override(
        note="x",
        name="Brand New Stadium",
        aliases=("The New",),
        latitude=40.8,
        longitude=-96.7,
        is_covered=True,
        city="Lincoln",
        state="NE",
    )

    assert gen._row_from_override("8", override) == VenueRow(
        "8",
        "Brand New Stadium",
        40.8,
        -96.7,
        True,
        "Lincoln",
        "NE",
        "manual",
        "manual",
        aliases=("The New",),
    )


@pytest.mark.parametrize(
    "missing", ["name", "latitude", "longitude", "is_covered", "city", "state"]
)
def test_an_override_missing_any_field_is_not_enough_for_a_whole_row(missing: str) -> None:
    complete = gen.Override(
        note="x",
        name="Brand New Stadium",
        latitude=40.8,
        longitude=-96.7,
        is_covered=False,
        city="Lincoln",
        state="NE",
    )

    assert gen._row_from_override("8", complete) is not None
    assert gen._row_from_override("8", replace(complete, **{missing: None})) is None


def test_an_indoor_venue_given_coordinates_by_an_override_is_covered() -> None:
    overrides = _overrides({"1": {"note": "OSM lacks it", "latitude": 33.9, "longitude": -83.3}})

    (row,) = _table(
        harvest_of(cfb_event("1", indoor=True)), FakeGeocoder(), overrides=overrides
    ).rows

    assert (row.is_covered, row.source) == (True, "manual")


# ------------------------------------------------- rows that must not be written


def test_a_sound_row_has_no_problems() -> None:
    assert gen.row_problems(old_park_row()) == []


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"name": " "}, "name is blank"),
        ({"city": ""}, "city is blank"),
        ({"state": ""}, "state is blank"),
        ({"state": "ZZ"}, "unknown state 'ZZ'"),
        ({"longitude": 83.37}, "coordinates 33.95000, 83.37000 are outside GA"),
        ({"latitude": 0.0, "longitude": 0.0}, "outside GA"),
    ],
)
def test_row_problems_names_what_is_wrong(changes: dict, problem: str) -> None:
    assert any(problem in found for found in gen.row_problems(old_park_row(**changes)))


def test_a_blank_state_is_reported_as_blank_not_also_as_unknown() -> None:
    assert gen.row_problems(old_park_row(state="")) == ["state is blank"]


def test_a_venue_whose_name_espn_left_blank_is_refused_not_written_unreadable() -> None:
    geocoder = FakeGeocoder({"": GeocodeOutcome(geocode_of())})

    result = _table(harvest_of(cfb_event("1", name="")), geocoder)

    assert result.rows == []
    (unplaced,) = result.report.needs_manual
    assert unplaced.reasons == ("row refused: name is blank",)
    assert result.report.needs_attention


def test_an_override_can_supply_the_name_espn_left_blank() -> None:
    overrides = _overrides(
        {"1": {"note": "no name", "name": "Old Park", "latitude": 33.9, "longitude": -83.3}}
    )

    result = _table(harvest_of(cfb_event("1", name="")), FakeGeocoder(), overrides=overrides)

    assert [(row.id, row.name) for row in result.rows] == [("1", "Old Park")]
    assert not result.report.needs_attention
    parse_table(dump_table(result.rows))  # and it reads back


def test_a_venue_whose_state_espn_left_blank_is_refused() -> None:
    overrides = _overrides({"1": {"note": "x", "latitude": 33.9, "longitude": -83.3}})

    result = _table(harvest_of(cfb_event("1", state="")), FakeGeocoder(), overrides=overrides)

    assert result.rows == []
    assert result.report.needs_manual[0].reasons == ("row refused: state is blank",)


def test_an_override_with_coordinates_outside_the_state_is_refused() -> None:
    # A dropped minus sign puts the stadium in China.
    overrides = _overrides({"1": {"note": "typo", "latitude": 33.9, "longitude": 83.3}})

    result = _table(harvest_of(cfb_event("1")), FakeGeocoder(), overrides=overrides)

    assert result.rows == []
    (unplaced,) = result.report.needs_manual
    assert "outside GA" in unplaced.reasons[0]


def test_a_refused_override_leaves_the_existing_row_in_place() -> None:
    existing = [old_park_row()]
    overrides = _overrides({"1": {"note": "typo", "latitude": 33.9, "longitude": 83.3}})

    result = _table(
        harvest_of(cfb_event("1")), FakeGeocoder(), existing=existing, overrides=overrides
    )

    assert result.rows == existing
    assert result.report.changed == []
    assert result.report.kept == 1
    assert "kept the row already in the table" in result.report.needs_manual[0].reasons
    assert result.report.needs_attention


# --------------------------------------------------------- rows are not lost


def test_a_refresh_that_cannot_place_a_venue_keeps_its_old_row_and_says_so() -> None:
    existing = [old_park_row()]

    result = _table(harvest_of(cfb_event("1")), FakeGeocoder(), existing=existing, refresh=True)

    assert result.rows == existing
    (unplaced,) = result.report.needs_manual
    assert unplaced.reasons == ("rejected: nothing nearby", "kept the row already in the table")
    assert result.report.removed == []
    assert result.report.needs_attention


def test_every_row_in_the_table_is_counted_once_as_kept_added_or_changed() -> None:
    existing = [old_park_row(), old_park_row(id="2", name="Gone Field")]
    geocoder = FakeGeocoder({"New Dome": GeocodeOutcome(geocode_of(name="New Dome"))})
    harvested = harvest_of(cfb_event("1"), cfb_event("3", "New Dome"), cfb_event("2", "Gone Field"))
    overrides = _overrides({"2": {"note": "roof", "is_covered": True}})

    result = _table(harvested, geocoder, existing=existing, overrides=overrides, refresh=False)

    report = result.report
    assert len(result.rows) == report.kept + len(report.added) + len(report.changed)


def test_a_venue_that_turns_out_international_is_not_dropped_without_refresh() -> None:
    existing = [old_park_row()]
    harvested = harvest_of(cfb_event("1", country="Ireland"))

    result = _table(harvested, FakeGeocoder(), existing=existing)

    assert result.rows == existing
    assert result.report.removed == []


def test_a_refresh_that_finds_a_venue_international_removes_and_reports_it() -> None:
    existing = [old_park_row()]
    harvested = harvest_of(cfb_event("1", country="Ireland"))

    result = _table(harvested, FakeGeocoder(), existing=existing, refresh=True)

    assert result.rows == []
    assert result.report.removed == existing
    assert result.report.international == ["1 Old Park (Ireland)"]


def test_an_exclusion_removes_a_row_already_in_the_table_and_reports_it() -> None:
    existing = [old_park_row()]
    overrides = _overrides({"1": {"note": "not a game venue", "exclude": True}})

    result = _table(
        harvest_of(cfb_event("1")), FakeGeocoder(), existing=existing, overrides=overrides
    )

    assert result.rows == []
    assert result.report.removed == existing
    assert result.report.excluded == ["1 Old Park: not a game venue"]


def test_international_is_decided_by_most_of_a_venues_games_not_the_last_one() -> None:
    mostly_abroad = harvest_of(
        cfb_event("1", country="Ireland"),
        cfb_event("1", country="Ireland"),
        cfb_event("1", country="USA"),
    )
    mostly_home = harvest_of(
        cfb_event("1", country="Ireland"), cfb_event("1"), cfb_event("1"), cfb_event("1")
    )
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())})

    assert _table(mostly_abroad, geocoder).report.international == ["1 Old Park (Ireland)"]
    assert [row.id for row in _table(mostly_home, geocoder).rows] == ["1"]


def test_a_game_that_omits_a_field_cannot_blank_it_out() -> None:
    harvested = harvest_of(cfb_event("1", "Old Park"), cfb_event("1", "", city="", state=""))

    venue = harvested.venues["1"]
    assert (venue.name, venue.city, venue.state, venue.games) == ("Old Park", "Athens", "GA", 2)


def test_a_venue_with_no_name_in_any_game_describes_itself_as_unnamed() -> None:
    harvested = harvest_of(cfb_event("1", "", city="", state=""))

    assert harvested.venues["1"].describe() == "(unnamed), ?, ? (1 game(s))"


# ------------------------------------------------------------ unused overrides


def test_an_override_that_matches_no_venue_is_reported_and_needs_attention() -> None:
    overrides = _overrides({"99": {"note": "stale", "latitude": 33.9, "longitude": -83.3}})

    result = _table(harvest_of(cfb_event("1")), FakeGeocoder(), overrides=overrides)

    assert result.report.unused_overrides == ["99: stale"]
    assert result.report.needs_attention


def test_an_exclusion_that_matches_nothing_is_unused_too() -> None:
    overrides = _overrides({"99": {"note": "typo in the id", "exclude": True}})

    result = _table(harvest_of(), FakeGeocoder(), overrides=overrides)

    assert result.report.unused_overrides == ["99: typo in the id"]


def test_an_override_for_a_skipped_international_venue_is_unused() -> None:
    overrides = _overrides({"1": {"note": "x", "latitude": 33.9, "longitude": -83.3}})

    result = _table(
        harvest_of(cfb_event("1", country="Ireland")), FakeGeocoder(), overrides=overrides
    )

    assert result.report.unused_overrides == ["1: x"]


def test_overrides_that_were_applied_are_not_reported_as_unused() -> None:
    overrides = _overrides(
        {
            "1": {"note": "coordinates", "latitude": 33.9, "longitude": -83.3},
            "2": {"note": "excluded", "exclude": True},
            "3": {"note": "roof, on an existing row", "is_covered": True},
            "8": {
                "note": "whole row",
                "name": "Brand New Stadium",
                "latitude": 40.8,
                "longitude": -96.7,
                "is_covered": False,
                "city": "Lincoln",
                "state": "NE",
            },
        }
    )
    existing = [old_park_row(id="3", name="Third Field")]

    result = _table(
        harvest_of(cfb_event("1"), cfb_event("2", "Two Field"), cfb_event("3", "Third Field")),
        FakeGeocoder(),
        existing=existing,
        overrides=overrides,
    )

    assert result.report.unused_overrides == []
    assert not result.report.needs_attention


# --------------------------------------------------------------- report details


def test_a_change_lists_the_fields_that_differ() -> None:
    old = old_park_row()
    new = replace(old, name="New Park", aliases=("Old Park",), is_covered=True)

    assert gen._describe_change(old, new) == (
        "name 'Old Park' -> 'New Park'; is_covered False -> True; aliases () -> ('Old Park',)"
    )


def test_a_change_never_prints_a_raw_newline_from_an_api_name() -> None:
    old = old_park_row()

    text = gen._describe_change(old, replace(old, name="Bad\nName"))

    assert "\n" not in text
    assert "Bad\\nName" in text


def test_the_report_lists_removed_rows_and_unused_overrides() -> None:
    existing = [old_park_row()]
    overrides = _overrides(
        {"1": {"note": "gone", "exclude": True}, "99": {"note": "stale", "exclude": True}}
    )
    harvested = harvest_of(cfb_event("1"))

    result = _table(harvested, FakeGeocoder(), existing=existing, overrides=overrides)
    text = format_report(result, venues_seen=len(harvested.venues))

    assert "1 removed" in text
    assert "Removed from the table:" in text
    assert "Overrides that matched no venue" in text
    assert "99: stale" in text


# ------------------------------------------------------- geocoder edge cases


@respx.mock
def test_a_partial_name_found_only_in_the_wrong_city_is_rejected() -> None:
    feature = photon_feature("Memorial Stadium", city="Urbana", state="Illinois")
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": [feature]}))

    outcome = _geocoder().geocode("Gies Memorial Stadium", "Champaign", "IL")

    assert outcome.result is None
    assert outcome.candidates == ("Memorial Stadium (leisure=stadium, Urbana, Illinois)",)


@respx.mock
def test_a_blank_espn_city_never_vouches_for_a_partial_name() -> None:
    feature = photon_feature("Memorial Stadium", city="Champaign", state="Illinois")
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": [feature]}))

    assert _geocoder().geocode("Gies Memorial Stadium", "", "IL").result is None


@respx.mock
def test_a_blank_espn_name_is_reported_without_asking_photon() -> None:
    outcome = _geocoder().geocode("  ", "Athens", "GA")  # respx rejects any request

    assert outcome.result is None
    assert outcome.notes == ("ESPN gave no venue name",)


@respx.mock
def test_a_city_matches_when_espn_gives_the_longer_name() -> None:
    feature = photon_feature("Fred C. Yager Stadium", state="Ohio", city="Oxford")
    respx.get(gen.PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": [feature]}))

    outcome = _geocoder().geocode("Yager Stadium", "Oxford Township", "OH")

    assert outcome.result is not None
    assert outcome.notes == ()


def test_a_name_with_no_letters_or_digits_matches_nothing() -> None:
    candidate = geocode_of(name="---")

    assert gen._judge(candidate, "---", "Athens", "Georgia", gen.STADIUM_CLASSES) is None
    assert gen._judge(geocode_of(), "---", "Athens", "Georgia", gen.STADIUM_CLASSES) is None


@respx.mock
def test_a_right_city_match_from_the_wider_search_beats_a_wrong_city_one_from_the_first() -> None:
    wrong_town = photon_feature("Memorial Stadium", city="Bloomington", osm_id=1, state="Illinois")
    right_town = photon_feature(
        "Gies Memorial Stadium", city="Champaign", osm_id=2, state="Illinois", value="pitch"
    )
    respx.get(gen.PHOTON_URL).mock(
        side_effect=[
            httpx.Response(200, json={"features": [wrong_town]}),  # the stadium-tagged search
            httpx.Response(200, json={"features": [right_town]}),  # the wider search
        ]
    )

    outcome = _geocoder().geocode("Memorial Stadium", "Champaign", "IL")

    assert outcome.result is not None
    assert outcome.result.osm_id == 2
    assert outcome.notes == ("mapped as leisure=pitch, not a stadium",)


@respx.mock
def test_the_first_search_is_enough_when_it_finds_the_right_city() -> None:
    route = respx.get(gen.PHOTON_URL).mock(
        return_value=httpx.Response(200, json={"features": [photon_feature("Old Park")]})
    )

    _geocoder().geocode("Old Park", "Athens", "GA")

    assert route.call_count == 1
