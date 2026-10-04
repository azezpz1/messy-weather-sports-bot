import datetime as dt
import json
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
    VenueObservation,
    build_table,
    format_report,
    observe_scoreboard,
    parse_overrides,
    parse_photon_feature,
    season_ranges,
)
from messy_weather_sports_bot.venue_table import VenueRow, dump_table
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
    assert (old_park.games, old_park.neutral_games, old_park.indoor) == (2, 1, False)
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
    assert "ZZ" in outcome.candidates[0]


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
        ({"note": "x", "aliases": "a"}, "'aliases' must be a list of strings"),
        ("x", "must be an object"),
    ],
)
def test_a_malformed_override_is_rejected_naming_the_venue(override: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _overrides({"7": override})


# ----------------------------------------------------------------------- build


def test_a_new_venue_is_geocoded_and_seeded_covered_from_espn_indoor() -> None:
    geocoder = FakeGeocoder({"New Dome": GeocodeOutcome(geocode_of(name="New Dome", osm_id=9))})
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
    nfl = VenueCatalog([StadiumInfo("Old Park", 40.0, -80.0, False, venue_id="1")])

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
    ((observation, rejected),) = result.report.needs_manual
    assert observation.venue_id == "4"
    assert rejected == ("Odd Field (leisure=pitch, Elsewhere, Ohio)",)
    assert result.report.needs_attention


def test_existing_rows_are_kept_without_geocoding_again() -> None:
    existing = [VenueRow("1", "Old Park", 1.0, 2.0, False, "Athens", "GA", "stadium", "photon:W5")]
    geocoder = FakeGeocoder()

    result = _table(harvest_of(cfb_event("1")), geocoder, existing=existing)

    assert result.rows == existing
    assert result.report.kept == 1
    assert geocoder.calls == []


def test_refresh_recomputes_rows_already_in_the_table() -> None:
    existing = [VenueRow("1", "Old Park", 1.0, 2.0, False, "Athens", "GA", "stadium", "photon:W5")]
    geocoder = FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of(latitude=3.0, longitude=4.0))})

    result = _table(harvest_of(cfb_event("1")), geocoder, existing=existing, refresh=True)

    assert (result.rows[0].latitude, result.rows[0].longitude) == (3.0, 4.0)
    assert [(old.latitude, new.latitude) for old, new in result.report.changed] == [(1.0, 3.0)]


def test_a_row_not_hosted_this_time_stays_in_the_table() -> None:
    existing = [VenueRow("9", "Quiet Field", 1.0, 2.0, False, "Ames", "IA", "manual", "manual")]

    result = _table(
        harvest_of(cfb_event("1")),
        FakeGeocoder({"Old Park": GeocodeOutcome(geocode_of())}),
        existing=existing,
    )

    assert [r.id for r in result.rows] == ["1", "9"]


def test_an_override_with_coordinates_replaces_a_geocode_and_skips_the_lookup() -> None:
    overrides = _overrides(
        {"1": {"note": "OSM has the old site", "latitude": 10.123456, "longitude": -20.5}}
    )
    geocoder = FakeGeocoder()

    result = _table(harvest_of(cfb_event("1")), geocoder, overrides=overrides)

    (row,) = result.rows
    assert (row.latitude, row.longitude, row.precision, row.source) == (
        10.12346,
        -20.5,
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
    existing = [VenueRow("1", "Old Park", 1.0, 2.0, False, "Athens", "GA", "stadium", "photon:W5")]
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
    geocoder = FakeGeocoder(
        {
            n: GeocodeOutcome(geocode_of(name=n, osm_id=i))
            for i, n in enumerate(["A Field", "B Field", "C Field"])
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
    assert "Needs manual coordinates" in text
    assert "Odd Field (leisure=pitch, Elsewhere, Ohio)" in text
    assert "Skipped as international: 7 Aviva Stadium (Ireland)" in text


def test_a_venue_observation_exposes_its_address_for_the_international_check() -> None:
    observation = VenueObservation("1", city="Athens", state="GA", country="USA")

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
