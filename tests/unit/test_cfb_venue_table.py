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
from messy_weather_sports_bot.venue_generation import (
    STATE_BOXES,
    US_STATES,
    parse_overrides,
)
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


def _km(a: VenueRow, b: VenueRow) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(haversine))


# ------------------------------------------------------------------- the file


def test_the_packaged_table_is_the_file_in_the_repo() -> None:
    assert parse_table(TABLE_PATH.read_text("utf-8")) == ROWS


def test_the_file_is_in_canonical_form_so_a_hand_edit_shows_up() -> None:
    # Regeneration writes exactly this text; anything else means the file was edited by hand.
    assert TABLE_PATH.read_text("utf-8") == dump_table(ROWS)


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


def test_the_catalog_is_id_only_so_a_name_or_alias_is_never_guessed() -> None:
    catalog = catalog_from_rows(ROWS)

    for row in ROWS:
        for name in (row.name, *row.aliases):
            assert catalog.for_venue("", name) is None, f"{name!r} resolved by name alone"


# ------------------------------------------------------------------- the roofs

# Checked by hand against each stadium: every one of these is a dome, a fixed roof or a
# retractable roof, which the bot treats as covered. Adding to or removing from the table's
# covered set means changing this list on purpose.
COVERED = {
    "1964": "JMA Wireless Dome",
    "3493": "Caesars Superdome",
    "3604": "Alamodome",
    "3605": "Alerus Center",
    "3687": "AT&T Stadium",
    "3714": "Fargodome",
    "3727": "Ford Field",
    "3812": "Lucas Oil Stadium",
    "3891": "Reliant Stadium",
    "3970": "State Farm Stadium",
    "4251": "Chase Field",
    "5348": "Mercedes-Benz Stadium",
    "5455": "Ford Center At The Star",
    "6501": "Allegiant Stadium",
    "7065": "SoFi Stadium",
}

# Famous open-air stadiums: if one of these were marked covered the bot would never
# recommend a snowy game there.
OPEN_AIR = {
    "347": "Camp Randall Stadium",
    "1056": "Rose Bowl",
    "3558": "Michigan Stadium",
    "3626": "Autzen Stadium",
    "3632": "Beaver Stadium",
    "3657": "Bryant-Denny Stadium",
    "3765": "Husky Stadium",
    "3795": "Kyle Field",
    "3853": "Neyland Stadium",
    "3855": "Notre Dame Stadium",
    "3861": "Ohio Stadium",
    "3917": "Sanford Stadium",
}


def test_the_covered_venues_are_exactly_the_ones_checked_by_hand() -> None:
    assert {row.id: row.name for row in ROWS if row.is_covered} == COVERED


@pytest.mark.parametrize(("venue_id", "name"), OPEN_AIR.items())
def test_a_well_known_open_air_stadium_is_not_covered(venue_id: str, name: str) -> None:
    row = ROWS_BY_ID[venue_id]

    assert row.name == name
    assert row.is_covered is False


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
    overrides = parse_overrides(OVERRIDES_PATH.read_text("utf-8"))

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
