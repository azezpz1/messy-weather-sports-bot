"""The venue-table generator script: its exit codes, what it writes, and the User-Agents it
sends. The logic behind it is covered in test_venue_generation.py; this drives `main()`
the way the command line does, against mocked ESPN and Photon."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

from messy_weather_sports_bot.espn import CFB_SCOREBOARD_URL
from messy_weather_sports_bot.venue_generation import PHOTON_URL, RateLimiter
from messy_weather_sports_bot.venue_table import VenueRow, dump_table, parse_table
from messy_weather_sports_bot.weather import USER_AGENT

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_cfb_venues.py"
ONE_DAY = "--range=2025-09-06:2025-09-06"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_cfb_venues", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _scoreboard(*, venue_id: str = "1", name: str = "Old Park", country: str = "USA") -> dict:
    venue = {
        "id": venue_id,
        "fullName": name,
        "indoor": False,
        "address": {"city": "Athens", "state": "GA", "country": country},
    }
    teams = [("home", "61", "Georgia Bulldogs"), ("away", "238", "Vanderbilt Commodores")]
    return {
        "events": [
            {
                "competitions": [
                    {
                        "neutralSite": False,
                        "venue": venue,
                        "competitors": [
                            {"homeAway": side, "team": {"id": team_id, "displayName": team}}
                            for side, team_id, team in teams
                        ],
                    }
                ]
            }
        ]
    }


def _photon(name: str = "Old Park", lat: float = 33.95, lon: float = -83.37) -> dict:
    properties = {
        "name": name,
        "state": "Georgia",
        "city": "Athens",
        "countrycode": "US",
        "osm_type": "W",
        "osm_id": 5,
        "osm_key": "leisure",
        "osm_value": "stadium",
    }
    return {"features": [{"geometry": {"coordinates": [lon, lat]}, "properties": properties}]}


def _no_overrides(tmp_path: Path) -> Path:
    path = tmp_path / "overrides.json"
    path.write_text('{"schema_version": 1, "venues": {}}')
    return path


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.output = tmp_path / "venues.json"
        self.overrides = _no_overrides(tmp_path)
        self.cache = tmp_path / "cache.json"

    def main(self, *extra: str) -> int:
        return script.main(
            [
                f"--output={self.output}",
                f"--overrides={self.overrides}",
                f"--cache={self.cache}",
                ONE_DAY,
                *extra,
            ]
        )


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harvest pauses between days and Photon calls are spaced a second apart; neither
    matters against mocks, and both default to the real `time.sleep`."""
    generation = script.generation
    real_harvest, real_geocoder = generation.harvest, generation.PhotonGeocoder
    monkeypatch.setattr(
        generation, "harvest", lambda client, ranges: real_harvest(client, ranges, sleep=_ignore)
    )
    monkeypatch.setattr(
        generation,
        "PhotonGeocoder",
        lambda client, **kwargs: real_geocoder(client, limiter=RateLimiter(0), **kwargs),
    )


def _ignore(seconds: float) -> None:
    del seconds


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


@pytest.fixture
def espn() -> respx.Route:
    return respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_scoreboard()))


@pytest.fixture
def photon() -> respx.Route:
    return respx.get(PHOTON_URL).mock(return_value=httpx.Response(200, json=_photon()))


# ------------------------------------------------------------------- a normal run


@respx.mock
def test_a_run_writes_the_table_and_exits_zero(
    run: Run, espn: respx.Route, photon: respx.Route, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main() == 0

    (row,) = parse_table(run.output.read_text())
    assert (row.id, row.name, row.latitude, row.longitude) == ("1", "Old Park", 33.95, -83.37)
    captured = capsys.readouterr()
    assert "Wrote 1 venue(s)" in captured.out
    assert "1 venue(s)" in captured.err  # the harvest summary


@respx.mock
def test_espn_gets_httpxs_default_user_agent_and_the_geocoder_identifies_the_project(
    run: Run, espn: respx.Route, photon: respx.Route
) -> None:
    # ESPN's edge answers 403 to an unfamiliar User-Agent; OpenStreetMap's policy wants one.
    run.main()

    assert espn.calls.last.request.headers["user-agent"].startswith("python-httpx/")
    assert photon.calls.last.request.headers["user-agent"] == USER_AGENT


@respx.mock
def test_a_dry_run_prints_the_report_and_writes_nothing(
    run: Run, espn: respx.Route, photon: respx.Route, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run.main("--dry-run") == 0

    assert not run.output.exists()
    assert "Added" in capsys.readouterr().out


@respx.mock
def test_a_venue_that_cannot_be_placed_exits_two_but_still_writes_the_table(
    run: Run, espn: respx.Route, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(PHOTON_URL).mock(return_value=httpx.Response(200, json={"features": []}))

    assert run.main() == 2

    assert parse_table(run.output.read_text()) == []
    assert "Needs a person" in capsys.readouterr().out


@respx.mock
def test_an_override_that_matches_nothing_exits_two(
    run: Run, espn: respx.Route, photon: respx.Route
) -> None:
    run.overrides.write_text(
        json.dumps({"schema_version": 1, "venues": {"99": {"note": "stale", "exclude": True}}})
    )

    assert run.main() == 2


@respx.mock
def test_rows_already_in_the_table_are_kept_unless_refresh_is_given(
    run: Run, espn: respx.Route
) -> None:
    old = VenueRow("1", "Old Park", 33.9, -83.3, False, "Athens", "GA", "stadium", "photon:W1")
    run.output.write_text(dump_table([old]))
    photon = respx.get(PHOTON_URL).mock(return_value=httpx.Response(200, json=_photon()))

    assert run.main() == 0
    assert parse_table(run.output.read_text()) == [old]
    assert photon.call_count == 0

    assert run.main("--refresh") == 0
    (row,) = parse_table(run.output.read_text())
    assert (row.latitude, row.longitude) == (33.95, -83.37)


# ------------------------------------------------------------------ error exits


@respx.mock
def test_a_failing_espn_exits_one_and_writes_nothing(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(503))

    assert run.main() == 1

    assert not run.output.exists()
    assert capsys.readouterr().err.startswith("error: HTTPStatusError")


@respx.mock
def test_a_non_json_espn_body_exits_one_instead_of_a_traceback(run: Run) -> None:
    respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(200, text="<html>blocked"))

    assert run.main() == 1
    assert not run.output.exists()


@respx.mock
def test_a_harvest_with_no_games_is_an_error_not_an_empty_table(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    run.output.write_text(dump_table([]))
    before = run.output.read_text()

    assert run.main() == 1

    assert run.output.read_text() == before
    assert "no games" in capsys.readouterr().err


@respx.mock
def test_a_failing_geocoder_exits_one(run: Run, espn: respx.Route) -> None:
    respx.get(PHOTON_URL).mock(return_value=httpx.Response(500))

    assert run.main() == 1
    assert not run.output.exists()


def test_an_overrides_path_that_does_not_exist_is_an_error(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    run.overrides = run.overrides.parent / "missing.json"

    assert run.main() == 1
    assert capsys.readouterr().err.startswith("error:")


def test_a_malformed_overrides_file_is_an_error(run: Run) -> None:
    run.overrides.write_text('{"schema_version": 1, "venues": {"7": {"note": "x", "exclude": 1}}}')

    assert run.main() == 1


def test_an_unreadable_existing_table_is_an_error_not_overwritten(run: Run) -> None:
    run.output.write_text("not json")

    assert run.main() == 1
    assert run.output.read_text() == "not json"


@respx.mock
def test_a_table_the_runtime_could_not_read_back_is_never_written(
    run: Run, espn: respx.Route, photon: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "dump_table", lambda rows: "[]")

    assert run.main() == 1
    assert not run.output.exists()


def test_the_default_overrides_file_may_be_absent(
    run: Run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(script, "DEFAULT_OVERRIDES", tmp_path / "absent.json")
    with respx.mock:
        respx.get(CFB_SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_scoreboard()))
        respx.get(PHOTON_URL).mock(return_value=httpx.Response(200, json=_photon()))

        status = script.main(
            [f"--output={run.output}", f"--cache={run.cache}", ONE_DAY],
        )

    assert status == 0


@pytest.mark.parametrize("text", ["garbage", "2025-09-07:2025-09-06", "2025-09-06"])
def test_a_bad_date_range_is_refused_by_the_parser(text: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        script.main([f"--range={text}"])

    assert exit_info.value.code == 2


def test_an_error_message_never_prints_a_raw_control_character(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script._fail("bad\nname\x1b[31m") == 1

    err = capsys.readouterr().err
    assert err == "error: bad\\nname\\x1b[31m\n"
