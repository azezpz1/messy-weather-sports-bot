import copy
import json

import pytest

from messy_weather_sports_bot.venue_table import (
    SCHEMA_VERSION,
    VenueRow,
    catalog_from_rows,
    dump_table,
    id_order,
    parse_table,
)


def _row(
    venue_id: str = "101",
    *,
    name: str = "Old Park",
    is_covered: bool = False,
    precision: str = "stadium",
    source: str = "photon:W1",
    aliases: tuple[str, ...] = ("The Old",),
) -> VenueRow:
    return VenueRow(
        id=venue_id,
        name=name,
        latitude=40.123456789,
        longitude=-80.987654321,
        is_covered=is_covered,
        city="Springfield",
        state="OH",
        precision=precision,
        source=source,
        aliases=aliases,
    )


def _payload(**row_changes) -> dict:
    row = json.loads(dump_table([_row()]))["venues"][0]
    row.update(row_changes)
    return {"schema_version": SCHEMA_VERSION, "venues": [row]}


def _parse_payload(payload: dict) -> list[VenueRow]:
    return parse_table(json.dumps(payload))


def test_dump_sorts_rows_by_numeric_id_and_ends_with_a_newline() -> None:
    text = dump_table([_row("1000"), _row("99"), _row("abc"), _row("101")])

    assert [v["id"] for v in json.loads(text)["venues"]] == ["99", "101", "1000", "abc"]
    assert text.endswith("}\n")


def test_dump_rounds_coordinates_and_sorts_keys() -> None:
    venue = json.loads(dump_table([_row()]))["venues"][0]

    assert (venue["latitude"], venue["longitude"]) == (40.12346, -80.98765)
    assert list(venue) == sorted(venue)


def test_dump_is_the_same_whatever_order_the_rows_come_in() -> None:
    rows = [_row("3"), _row("1"), _row("2")]

    assert dump_table(rows) == dump_table(reversed(rows))


def test_a_dumped_table_parses_back_to_the_same_rows() -> None:
    rows = [_row("1", is_covered=True, aliases=()), _row("2", precision="manual", source="manual")]

    parsed = parse_table(dump_table(rows))

    # Coordinates are rounded on the way out, so compare the rounded (JSON) forms.
    assert [row.to_json() for row in parsed] == [row.to_json() for row in rows]
    assert parsed[0].aliases == ()
    assert parsed[1].precision == "manual"


def test_id_order_puts_numeric_ids_first_in_numeric_order() -> None:
    assert sorted(["10", "9", "x", "100"], key=id_order) == ["9", "10", "100", "x"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        ('{"schema_version": 2, "venues": []}', "unsupported venue table schema_version"),
        ('{"schema_version": 1}', "'venues' must be a list"),
        ('{"schema_version": 1, "venues": ["x"]}', r"venues\[0\] must be an object"),
    ],
)
def test_a_malformed_table_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_table(text)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": ""}, "'id' must not be blank"),
        ({"name": "  "}, "'name' must not be blank"),
        ({"name": 7}, "'name' must be str"),
        ({"latitude": "40"}, "'latitude' must be a number"),
        ({"latitude": True}, "'latitude' must be a number"),
        ({"latitude": 91}, "'latitude' out of range"),
        ({"longitude": -181}, "'longitude' out of range"),
        ({"is_covered": 1}, "'is_covered' must be bool"),
        ({"precision": "guess"}, "'precision' must be one of"),
        ({"aliases": "The Old"}, "'aliases' must be a list of strings"),
        ({"aliases": [1]}, "'aliases' must be a list of strings"),
        ({"city": ""}, "'city' must not be blank"),
        ({"source": ""}, "'source' must not be blank"),
    ],
)
def test_a_malformed_row_is_rejected_naming_the_problem(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_payload(_payload(**change))


def test_a_missing_field_is_rejected() -> None:
    payload = _payload()
    del payload["venues"][0]["state"]

    with pytest.raises(ValueError, match="'state' must be str"):
        _parse_payload(payload)


def test_a_duplicate_venue_id_is_rejected() -> None:
    payload = _payload()
    payload["venues"].append(copy.deepcopy(payload["venues"][0]))

    with pytest.raises(ValueError, match="duplicate venue id"):
        _parse_payload(payload)


def test_an_integer_coordinate_is_accepted_as_a_float() -> None:
    (row,) = _parse_payload(_payload(latitude=40, longitude=-80))

    assert (row.latitude, row.longitude) == (40.0, -80.0)


def test_a_catalog_resolves_by_id_then_name_and_alias() -> None:
    catalog = catalog_from_rows([_row("101"), _row("102", name="New Dome", aliases=())])

    by_id = catalog.for_venue("102", "Whatever")
    by_alias = catalog.for_venue("", "The Old")
    assert by_id is not None
    assert by_id.name == "New Dome"
    assert by_alias is not None
    assert by_alias.venue_id == "101"
    assert catalog.for_venue("999", "Nowhere") is None


def test_a_college_catalog_never_guesses_a_stadium_from_the_home_team() -> None:
    # Teams play at neutral sites too often for "the home team's stadium" to be a safe guess.
    with pytest.raises(KeyError):
        catalog_from_rows([_row()]).for_team("OSU")


def test_a_row_converts_to_stadium_info_keyed_by_its_venue_id() -> None:
    stadium = _row("101", is_covered=True).to_stadium()

    assert (stadium.venue_id, stadium.name, stadium.is_covered) == ("101", "Old Park", True)
    assert stadium.aliases == ("The Old",)
