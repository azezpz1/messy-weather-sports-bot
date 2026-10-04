"""Integrity checks on the shipped college football venue table, `data/cfb_venues.json`.

The table is generated and reviewed, not hand-written, so these don't re-derive it; they
catch what a wrong geocode, a hand edit or a bad regeneration would break: a venue in the
wrong state, a dome reported as open-air, two rows for one stadium, a table that no longer
matches its overrides.
"""

import math
from pathlib import Path

import pytest

from messy_weather_sports_bot.stadiums import NFL_CATALOG
from messy_weather_sports_bot.venue_generation import US_STATES, parse_overrides
from messy_weather_sports_bot.venue_table import (
    PRECISIONS,
    VenueRow,
    catalog_from_rows,
    dump_table,
    load_packaged_table,
    parse_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE_PATH = REPO_ROOT / "src" / "messy_weather_sports_bot" / "data" / "cfb_venues.json"
OVERRIDES_PATH = REPO_ROOT / "scripts" / "data" / "cfb_venue_overrides.json"

ROWS = load_packaged_table("cfb_venues.json")
ROWS_BY_ID = {row.id: row for row in ROWS}
ROW_PARAMS = [pytest.param(row, id=f"{row.id}-{row.name}") for row in ROWS]

# (south, north, west, east), a little generous: the check is "this point is in that
# state, not another one", which is what a wrong-city geocode would break.
STATE_BOXES: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.1, 35.1, -88.6, -84.8),
    "AK": (51.0, 71.5, -180.0, -129.9),
    "AZ": (31.2, 37.1, -114.9, -109.0),
    "AR": (33.0, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.1),
    "CO": (36.9, 41.1, -109.1, -102.0),
    "CT": (40.9, 42.1, -73.8, -71.7),
    "DE": (38.4, 39.9, -75.8, -75.0),
    "DC": (38.78, 39.0, -77.15, -76.9),
    "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.3, 35.1, -85.7, -80.7),
    "HI": (18.8, 22.3, -160.3, -154.7),
    "ID": (41.9, 49.1, -117.3, -111.0),
    "IL": (36.9, 42.6, -91.6, -87.0),
    "IN": (37.7, 41.8, -88.2, -84.7),
    "IA": (40.3, 43.6, -96.7, -90.1),
    "KS": (36.9, 40.1, -102.1, -94.5),
    "KY": (36.4, 39.2, -89.6, -81.9),
    "LA": (28.9, 33.1, -94.1, -88.7),
    "ME": (42.9, 47.5, -71.2, -66.9),
    "MD": (37.8, 39.8, -79.5, -75.0),
    "MA": (41.2, 42.9, -73.6, -69.9),
    "MI": (41.6, 48.4, -90.5, -82.1),
    "MN": (43.4, 49.5, -97.3, -89.4),
    "MS": (30.1, 35.1, -91.7, -88.0),
    "MO": (35.9, 40.7, -95.8, -89.0),
    "MT": (44.3, 49.1, -116.1, -104.0),
    "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (35.0, 42.1, -120.1, -114.0),
    "NH": (42.6, 45.4, -72.6, -70.6),
    "NJ": (38.9, 41.4, -75.6, -73.8),
    "NM": (31.3, 37.1, -109.1, -103.0),
    "NY": (40.4, 45.1, -79.8, -71.8),
    "NC": (33.8, 36.7, -84.4, -75.4),
    "ND": (45.9, 49.1, -104.1, -96.5),
    "OH": (38.3, 42.0, -84.9, -80.5),
    "OK": (33.6, 37.1, -103.1, -94.4),
    "OR": (41.9, 46.3, -124.7, -116.4),
    "PA": (39.7, 42.3, -80.6, -74.6),
    "RI": (41.1, 42.1, -71.9, -71.1),
    "SC": (32.0, 35.3, -83.4, -78.5),
    "SD": (42.4, 45.95, -104.1, -96.4),
    "TN": (34.9, 36.7, -90.4, -81.6),
    "TX": (25.8, 36.6, -106.7, -93.5),
    "UT": (36.9, 42.1, -114.1, -109.0),
    "VT": (42.7, 45.1, -73.5, -71.4),
    "VA": (36.5, 39.5, -83.7, -75.2),
    "WA": (45.5, 49.1, -124.9, -116.9),
    "WV": (37.1, 40.7, -82.7, -77.7),
    "WI": (42.4, 47.4, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.1, -104.0),
    "PR": (17.8, 18.6, -67.4, -65.2),
}


def _km(a: VenueRow, b: VenueRow) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(haversine))


# ------------------------------------------------------------------- the file


def test_the_packaged_table_is_the_file_in_the_repo() -> None:
    assert parse_table(TABLE_PATH.read_text()) == ROWS


def test_the_file_is_in_canonical_form_so_a_hand_edit_shows_up() -> None:
    # Regeneration writes exactly this text; anything else means the file was edited by hand.
    assert TABLE_PATH.read_text() == dump_table(ROWS)


def test_the_table_covers_the_fbs() -> None:
    # ~135 FBS home fields plus bowl, neutral-site and FCS-opponent venues.
    assert len(ROWS) >= 150


def test_venue_ids_are_unique() -> None:
    assert len({row.id for row in ROWS}) == len(ROWS)


def test_every_row_has_a_known_precision() -> None:
    assert {row.precision for row in ROWS} <= set(PRECISIONS)


# -------------------------------------------------------------------- places


@pytest.mark.parametrize("row", ROW_PARAMS)
def test_coordinates_are_a_real_place_in_the_united_states(row: VenueRow) -> None:
    assert (row.latitude, row.longitude) != (0.0, 0.0)
    assert 17.5 <= row.latitude <= 72
    assert -180 <= row.longitude <= -65


@pytest.mark.parametrize("row", ROW_PARAMS)
def test_coordinates_are_inside_the_rows_state(row: VenueRow) -> None:
    assert row.state in STATE_BOXES, f"unknown state code {row.state!r}"
    south, north, west, east = STATE_BOXES[row.state]
    assert south <= row.latitude <= north, f"{row.name}: latitude {row.latitude} not in {row.state}"
    assert west <= row.longitude <= east, (
        f"{row.name}: longitude {row.longitude} not in {row.state}"
    )


def test_every_state_box_is_for_a_state_the_generator_knows() -> None:
    assert set(STATE_BOXES) == set(US_STATES)


def test_no_two_rows_are_the_same_stadium_under_different_ids() -> None:
    for index, first in enumerate(ROWS):
        for second in ROWS[index + 1 :]:
            if first.name.casefold() == second.name.casefold():
                assert _km(first, second) > 5, f"{first.id} and {second.id}: {first.name} twice"


@pytest.mark.parametrize("row", ROW_PARAMS)
def test_aliases_neither_repeat_the_name_nor_each_other(row: VenueRow) -> None:
    assert row.name not in row.aliases
    assert len(set(row.aliases)) == len(row.aliases)


# ---------------------------------------------------------------- the catalog


def test_every_row_resolves_through_the_catalog_by_its_own_id() -> None:
    catalog = catalog_from_rows(ROWS)

    for row in ROWS:
        stadium = catalog.for_venue(row.id, "")
        assert stadium is not None, row.id
        assert (stadium.latitude, stadium.longitude) == (row.latitude, row.longitude)


def test_a_venue_name_shared_by_different_places_is_not_guessed_from_the_name() -> None:
    catalog = catalog_from_rows(ROWS)
    names = [row.name for row in ROWS]

    for name in {name for name in names if names.count(name) > 1}:
        assert catalog.for_venue("", name) is None, f"{name!r} resolved by name alone"


# ------------------------------------------------------------ the NFL overlap


def test_rows_for_nfl_stadiums_carry_the_nfl_tables_coordinates_and_roof() -> None:
    from_nfl = [row for row in ROWS if row.source == "nfl_catalog"]
    assert from_nfl, "expected some venues shared with the NFL"

    for row in from_nfl:
        stadium = NFL_CATALOG.for_venue(row.id, row.name)
        assert stadium is not None, f"{row.name} is marked nfl_catalog but isn't in the NFL table"
        assert (row.latitude, row.longitude) == (
            round(stadium.latitude, 5),
            round(stadium.longitude, 5),
        )
        assert row.is_covered or not stadium.is_covered


def test_a_venue_that_is_in_the_nfl_table_is_never_given_different_coordinates() -> None:
    for row in ROWS:
        stadium = NFL_CATALOG.for_venue(row.id, row.name)
        if stadium is not None:
            assert row.source == "nfl_catalog", f"{row.name} is an NFL stadium but not copied"


# ------------------------------------------------------------------ overrides


def test_overrides_are_reflected_in_the_table() -> None:
    overrides = parse_overrides(OVERRIDES_PATH.read_text())

    for venue_id, override in overrides.items():
        if override.exclude:
            assert venue_id not in ROWS_BY_ID, f"{venue_id} is excluded but present"
            continue
        row = ROWS_BY_ID.get(venue_id)
        assert row is not None, f"override for {venue_id} has no row in the table"
        if override.latitude is not None and override.longitude is not None:
            assert (row.latitude, row.longitude) == (
                round(override.latitude, 5),
                round(override.longitude, 5),
            )
            assert (row.precision, row.source) == ("manual", "manual")
        if override.is_covered is not None:
            assert row.is_covered is override.is_covered
        if override.aliases is not None:
            assert row.aliases == override.aliases
