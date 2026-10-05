"""The college football bot end to end - schedule, filter, weather, post - against mocked
ESPN and NWS, and the contracts it shares with the NFL bot: separate credentials, state
files and healthcheck, the preview rule, and exit codes."""

import datetime as dt
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot import cfb
from messy_weather_sports_bot.cfb import CFB, cfb_catalog
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.pipeline import (
    EXIT_NOTHING_POSTED,
    EXIT_OK,
    EXIT_PARTIAL,
    GameDayPipeline,
)
from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.stadiums import stadium_for_team
from tests.support.espn import MISSING, cfb_event, cfb_sample, espn_event
from tests.support.nws import forecast_response, mock_hourly_forecast

TARGET = dt.date(2026, 1, 18)
OHIO_STADIUM = "3861"
ACRISURE_STADIUM = "3752"
LOGGER = "messy_weather_sports_bot"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: TARGET)
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))
    for name in ("HEALTHCHECK_URL", "CFB_HEALTHCHECK_URL", "BLUESKY_HANDLE", "CFB_BLUESKY_HANDLE"):
        monkeypatch.delenv(name, raising=False)


def run(sport: Sport = CFB, *, dry_run: bool = True, **kwargs) -> int:
    return GameDayPipeline(sport).run(["bluesky"], dry_run, **kwargs)


def mock_slate(sport: Sport, events: list[dict]) -> None:
    respx.get(sport.scoreboard_url).mock(return_value=httpx.Response(200, json={"events": events}))


def mock_college_weather(venue_id: str, response: httpx.Response) -> None:
    stadium = cfb_catalog().for_venue(venue_id, "")
    assert stadium is not None
    mock_hourly_forecast(stadium.latitude, stadium.longitude, response=response)


RAIN = forecast_response("Rain", 40, "5 mph", 90)
SUNNY = forecast_response("Sunny", 55, "4 mph", 0)


def mock_messy_ranked_game() -> None:
    mock_slate(CFB, [cfb_event("Ohio State", "Minnesota", home_rank=1)])
    mock_college_weather(OHIO_STADIUM, RAIN)


class RecordingPoster(SocialMediaPoster):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def post(self, text: str) -> PostRef:
        self.texts.append(text)
        return PostRef(id=f"post-{len(self.texts)}", root_id="post-1")

    def reply(self, text: str, parent: PostRef) -> PostRef:
        return self.post(text)


@pytest.fixture
def poster(monkeypatch: pytest.MonkeyPatch) -> RecordingPoster:
    recording = RecordingPoster()
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda names, dry_run, env_prefix="": [recording],
    )
    return recording


# ------------------------------------------------------------- what gets posted


@respx.mock
def test_a_ranked_messy_game_is_posted_as_a_recommendation_with_its_ranking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_messy_ranked_game()

    assert run() == EXIT_OK

    assert capsys.readouterr().out == (
        "--- post ---\n"
        "🌩️ Messy Top 25 college football games to watch — Sun Jan 18\n"
        "🏈 Minnesota @ #1 Ohio State (1:00pm ET): 🌧️ Rain, 40°F, 5mph wind\n"
        "\n"
    )


@respx.mock
def test_a_neutral_site_game_says_vs_and_ranks_only_the_ranked_team(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Red River Rivalry, as ESPN reported it: "home" Texas is unranked, Oklahoma is #6.
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: dt.date(2025, 10, 11)
    )
    mock_slate(CFB, cfb_sample("20251011")["events"])
    mock_college_weather("499", forecast_response("Rain", 55, "12 mph", 90, day="2025-10-11"))

    assert run() == EXIT_OK

    assert "🏈 #6 Oklahoma vs Texas (3:30pm ET): 🌧️ Rain, 55°F, 12mph wind" in (
        capsys.readouterr().out
    )


@respx.mock
def test_an_unranked_game_is_left_out_without_a_single_weather_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Pitt vs Boston College, neither ranked. No NWS routes are mocked: any lookup fails.
    mock_slate(
        CFB,
        [cfb_event("Pitt", "Boston College", venue_id=ACRISURE_STADIUM, venue_name="Acrisure")],
    )

    assert run() == EXIT_OK

    assert capsys.readouterr().out == ""


@respx.mock
def test_a_ranked_game_with_clear_weather_posts_nothing_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    mock_slate(CFB, [cfb_event("Ohio State", "Minnesota", home_rank=1)])
    mock_college_weather(OHIO_STADIUM, SUNNY)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run() == EXIT_OK

    assert capsys.readouterr().out == ""
    assert "No messy-weather games on 2026-01-18; nothing to post." in caplog.text


@respx.mock
def test_a_ranked_game_at_an_unrecognized_venue_is_skipped_and_logged(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    mock_slate(
        CFB,
        [
            cfb_event(
                "Ohio State",
                "Minnesota",
                home_rank=1,
                venue_id="99999",
                venue_name="Brand New Stadium",
            )
        ],
    )

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run() == EXIT_OK

    assert capsys.readouterr().out == ""
    assert 'unrecognized venue "Brand New Stadium"' in caplog.text
    assert "No outdoor Top 25 college football games on 2026-01-18" in caplog.text


@respx.mock
def test_a_ranked_game_in_a_dome_is_skipped_as_covered(caplog: pytest.LogCaptureFixture) -> None:
    mock_slate(
        CFB,
        [
            cfb_event(
                "Alabama",
                "Georgia",
                home_rank=2,
                venue_id="5348",
                venue_name="Mercedes-Benz Stadium",
            )
        ],
    )

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run() == EXIT_OK

    assert "skipped: covered stadium (Mercedes-Benz Stadium)" in caplog.text


@respx.mock
def test_the_filtered_games_are_only_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    mock_slate(CFB, [cfb_event("Pitt", "Boston College", venue_id=ACRISURE_STADIUM)])

    with caplog.at_level(logging.INFO, logger=LOGGER):
        run()

    assert "no ranked team" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        run()

    assert "Boston College @ Pitt — skipped: no ranked team" in caplog.text


# ------------------------------------------------- ESPN stopped sending rankings


def unranked_slate(count: int, **kwargs) -> list[dict]:
    return [cfb_event(f"Home {n}", f"Away {n}", **kwargs) for n in range(count)]


@respx.mock
def test_a_big_slate_with_no_ranking_fields_is_a_degraded_run_not_a_clean_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = unranked_slate(8, home_curated=MISSING, away_curated=MISSING)
    mock_slate(CFB, events)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        exit_code = run()

    assert exit_code == EXIT_PARTIAL
    assert "no rankings for any of 8 Top 25 college football games" in caplog.text


@respx.mock
def test_a_big_slate_where_everyone_is_unranked_is_still_a_clean_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_slate(CFB, unranked_slate(8))  # every team carries ESPN's 99 for "unranked"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert run() == EXIT_OK

    assert caplog.text == ""


@respx.mock
def test_a_small_slate_with_no_ranking_fields_is_not_enough_to_suspect_a_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_slate(CFB, unranked_slate(7, home_curated=MISSING, away_curated=MISSING))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert run() == EXIT_OK

    assert caplog.text == ""


@respx.mock
def test_the_nfl_never_expects_rankings(caplog: pytest.LogCaptureFixture) -> None:
    homes = ["MIN", "DET", "ATL", "NO", "LV", "DAL", "IND", "HOU"]  # all covered stadiums
    mock_slate(NFL, [espn_event(home, "CHI") for home in homes])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert run(NFL) == EXIT_OK

    assert caplog.text == ""


@respx.mock
def test_rankings_that_disappear_do_not_block_a_game_that_still_posts() -> None:
    # One ranked, messy game among many that carry no ranking field: it is still posted
    # (the guard only matters when nothing is).
    events = [cfb_event("Ohio State", "Minnesota", home_rank=1), *unranked_slate(8)]
    mock_slate(CFB, events)
    mock_college_weather(OHIO_STADIUM, RAIN)

    assert run() == EXIT_OK


# --------------------------------------------- separate accounts, state, healthcheck


@respx.mock
def test_the_college_bot_refuses_to_post_with_only_the_nfls_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fallback to BLUESKY_HANDLE would post college games to the NFL account.
    monkeypatch.setenv("BLUESKY_HANDLE", "nfl.example.com")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "nfl-password")
    mock_messy_ranked_game()

    with pytest.raises(KeyError, match="CFB_BLUESKY_HANDLE"):
        run(dry_run=False)


@respx.mock
def test_the_two_sports_keep_separate_state_files_on_the_same_day(
    poster: RecordingPoster, tmp_path
) -> None:
    mock_messy_ranked_game()
    mock_slate(NFL, [espn_event("GB", "CHI")])
    green_bay = stadium_for_team("GB")
    mock_hourly_forecast(green_bay.latitude, green_bay.longitude, response=RAIN)

    assert run(CFB, dry_run=False) == EXIT_OK
    assert run(NFL, dry_run=False) == EXIT_OK

    assert len(poster.texts) == 2  # the NFL's thread wasn't mistaken for the college one
    assert sorted(path.name for path in (tmp_path / "state").iterdir()) == [
        "2026-01-18.cfb.json",
        "2026-01-18.json",
    ]


@respx.mock
def test_a_finished_thread_is_not_posted_twice_unless_forced(poster: RecordingPoster) -> None:
    mock_messy_ranked_game()

    run(dry_run=False)
    run(dry_run=False)
    assert len(poster.texts) == 1

    run(dry_run=False, force=True)
    assert len(poster.texts) == 2


@respx.mock
def test_a_preview_of_another_day_is_forced_to_a_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    # Today is the 19th, so the 18th is a preview. With no credentials set, a real run
    # would fail looking for CFB_BLUESKY_HANDLE - so printing instead proves the dry run.
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: TARGET + dt.timedelta(1)
    )
    mock_messy_ranked_game()

    assert run(dry_run=False, date=TARGET) == EXIT_OK

    assert "Minnesota @ #1 Ohio State" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("sport", [NFL, CFB], ids=lambda sport: sport.slug)
@respx.mock
def test_an_unreachable_schedule_is_nothing_posted_for_either_sport(sport: Sport) -> None:
    respx.get(sport.scoreboard_url).mock(return_value=httpx.Response(503))

    assert run(sport) == EXIT_NOTHING_POSTED


@respx.mock
def test_a_damaged_venue_table_fails_only_the_college_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfb_catalog.cache_clear()

    def damaged(filename: str):
        raise ValueError("cfb_venues.json: not valid JSON")

    monkeypatch.setattr(cfb, "load_packaged_table", damaged)
    mock_slate(CFB, [cfb_event("Ohio State", "Minnesota", home_rank=1)])
    mock_slate(NFL, [espn_event("MIN", "DET")])
    try:
        assert run(CFB) == EXIT_NOTHING_POSTED
        assert run(NFL) == EXIT_OK
    finally:
        cfb_catalog.cache_clear()
