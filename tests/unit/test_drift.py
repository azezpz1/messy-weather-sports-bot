"""The venue drift check: which games it looks at, what counts as drift, and the command
line around it."""

import datetime as dt
import importlib.util
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.drift import DAYS_TO_CHECK, DriftCheckError, check_drift
from messy_weather_sports_bot.nfl import NFL
from tests.support.espn import cfb_event, espn_event

START = dt.date(2026, 1, 11)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_venue_drift.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_venue_drift", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def mock_week(sport, events_by_day: dict[str, list[dict]] | None = None) -> respx.Route:
    """Answer each scoreboard date with its own events (an empty slate by default)."""
    events_by_day = events_by_day or {}

    def respond(request: httpx.Request) -> httpx.Response:
        day = request.url.params["dates"]
        return httpx.Response(200, json={"events": events_by_day.get(day, [])})

    return respx.get(sport.scoreboard_url).mock(side_effect=respond)


def at_kickoff(day: str, event: dict) -> dict:
    kickoff = f"{day[:4]}-{day[4:6]}-{day[6:]}T18:00Z"
    event["date"] = event["competitions"][0]["date"] = kickoff
    return event


@respx.mock
def test_it_checks_a_week_ahead_plus_a_day_of_slack() -> None:
    route = mock_week(CFB)

    with httpx.Client() as client:
        report = check_drift(CFB, client, start=START)

    days = [call.request.url.params["dates"] for call in route.calls]
    assert days == [f"202601{day:02d}" for day in range(11, 11 + DAYS_TO_CHECK)]
    assert (report.start, report.end) == (START, dt.date(2026, 1, 18))
    assert (report.games_checked, report.drift) == (0, [])


@respx.mock
def test_every_game_is_checked_not_only_the_ranked_ones() -> None:
    # Any venue can host a ranked team next week, so an unranked game at an unknown venue
    # still counts - the sport's filter is not applied.
    unknown = cfb_event("Home", "Away", venue_id="99999", venue_name="Brand New Stadium")
    mock_week(CFB, {"20260111": [at_kickoff("20260111", unknown)]})

    with httpx.Client() as client:
        report = check_drift(CFB, client, start=START)

    assert report.games_checked == 1
    assert len(report.drift) == 1
    assert 'unrecognized venue "Brand New Stadium"' in report.drift[0]


@respx.mock
def test_a_recognized_international_or_covered_venue_is_not_drift() -> None:
    abroad = cfb_event("Kansas St", "Iowa State", venue_id="3504", venue_name="Aviva Stadium")
    abroad["competitions"][0]["venue"]["address"] = {"city": "Dublin", "country": "Ireland"}
    dome = cfb_event("Georgia", "Alabama", venue_id="5348", venue_name="Mercedes-Benz Stadium")
    mock_week(
        CFB,
        {"20260111": [at_kickoff("20260111", abroad)], "20260112": [at_kickoff("20260112", dome)]},
    )

    with httpx.Client() as client:
        report = check_drift(CFB, client, start=START)

    assert (report.games_checked, report.drift) == (2, [])


@respx.mock
def test_the_nfl_is_checked_against_its_own_table() -> None:
    new_stadium = at_kickoff("20260111", espn_event("CHI", "GB", "Brand New Soldier Field"))
    mock_week(NFL, {"20260111": [new_stadium]})

    with httpx.Client() as client:
        report = check_drift(NFL, client, start=START)

    assert len(report.drift) == 1


@respx.mock
def test_a_day_that_cannot_be_fetched_is_an_error_not_a_pass() -> None:
    respx.get(CFB.scoreboard_url).mock(return_value=httpx.Response(503))

    with httpx.Client() as client, pytest.raises(DriftCheckError, match="could not fetch"):
        check_drift(CFB, client, start=START)


@respx.mock
def test_espn_is_asked_with_the_default_user_agent() -> None:
    route = mock_week(CFB)

    with httpx.Client() as client:
        check_drift(CFB, client, start=START, days=1)

    assert route.calls.last.request.headers["user-agent"].startswith("python-httpx/")


# ------------------------------------------------------------------ the command


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("messy_weather_sports_bot.drift.todays_game_day", lambda sport: START)


@respx.mock
def test_the_command_defaults_to_the_nfl_and_exits_zero_when_all_is_known(
    today: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = mock_week(NFL)
    mock_week(CFB)

    assert script.main([]) == 0

    assert route.called
    assert "No venue drift detected." in capsys.readouterr().out


@respx.mock
def test_the_command_checks_college_football_with_sport_cfb(
    today: None, capsys: pytest.CaptureFixture[str]
) -> None:
    nfl_route = mock_week(NFL)
    cfb_route = mock_week(CFB)

    assert script.main(["--sport", "cfb"]) == 0

    assert cfb_route.called
    assert not nfl_route.called
    assert capsys.readouterr().out.startswith("Checked 0 game(s) from 2026-01-11 to 2026-01-18.")
    # The script builds its own client: ESPN must see httpx's default User-Agent, and the
    # college request must carry the same parameters as the bot's.
    request = cfb_route.calls.last.request
    assert request.headers["user-agent"].startswith("python-httpx/")
    assert dict(request.url.params) == {"dates": "20260118", "groups": "80", "limit": "400"}


@respx.mock
def test_the_command_fails_and_names_the_venue_and_the_fix_when_there_is_drift(
    today: None, capsys: pytest.CaptureFixture[str]
) -> None:
    unknown = cfb_event("Home", "Away", venue_id="99999", venue_name="Brand New Stadium")
    mock_week(CFB, {"20260111": [at_kickoff("20260111", unknown)]})

    assert script.main(["--sport", "cfb"]) == 1

    err = capsys.readouterr().err
    assert "1 CFB venue(s) not recognized:" in err
    assert "Brand New Stadium" in err
    assert "generate_cfb_venues.py" in err


@respx.mock
def test_the_nfl_failure_points_at_stadiums_py(
    today: None, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_week(NFL, {"20260111": [at_kickoff("20260111", espn_event("CHI", "GB", "New Field"))]})

    assert script.main(["--sport", "nfl"]) == 1

    assert "stadiums.py" in capsys.readouterr().err


@respx.mock
def test_the_command_fails_when_a_schedule_cannot_be_fetched(
    today: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(CFB.scoreboard_url).mock(return_value=httpx.Response(503))

    assert script.main(["--sport", "cfb"]) == 1

    assert capsys.readouterr().err.startswith("error: could not fetch schedule for 2026-01-11")


def test_an_unknown_sport_is_refused_by_the_parser() -> None:
    with pytest.raises(SystemExit) as exit_info:
        script.main(["--sport", "mlb"])

    assert exit_info.value.code == 2
