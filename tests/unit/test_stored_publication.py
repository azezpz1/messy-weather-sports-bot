"""Exercise collection and publication against the same slate and forecast fixtures."""

import datetime as dt
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from messy_weather_sports_bot import collector, scheduler_store
from messy_weather_sports_bot.cfb import CFB, cfb_catalog
from messy_weather_sports_bot.cfb import main as cfb_main
from messy_weather_sports_bot.collection import forecast_db_transaction
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.nfl import main as nfl_main
from messy_weather_sports_bot.pipeline import EXIT_NOTHING_POSTED, EXIT_OK, EXIT_PARTIAL
from messy_weather_sports_bot.poster.console import ConsolePoster
from messy_weather_sports_bot.stadiums import stadium_for_team
from tests.support.espn import cfb_event, espn_event
from tests.support.nws import forecast_response, mock_hourly_forecast


@pytest.fixture
def game_day(monkeypatch, tmp_path):
    now = dt.datetime.now(dt.UTC)
    day = NFL.game_day_for(now) + dt.timedelta(days=1)
    monkeypatch.setattr("messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: day)
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("CFB_HEALTHCHECK_URL", raising=False)
    return now, day


def _mock_slate(sport, day, *, clear=False):
    kickoff = f"{day.isoformat()}T13:00:00-05:00"
    if sport == NFL:
        events = [
            espn_event("BUF", "NE", kickoff=kickoff),
            espn_event("GB", "CHI", kickoff=kickoff),
            espn_event("PIT", "CLE", kickoff=kickoff),
        ]
        stadiums = [stadium_for_team(team) for team in ("BUF", "GB", "PIT")]
    else:
        events = [
            cfb_event("Ohio State", "Michigan", home_rank=3, kickoff=kickoff, neutral=True),
            cfb_event("Pitt", "Boston College", home_rank=8, venue_id="3752", kickoff=kickoff),
            cfb_event("Clemson", "Miami", home_rank=11, venue_id="3652", kickoff=kickoff),
            cfb_event("Ohio", "Toledo", kickoff=kickoff),
        ]
        stadiums = [cfb_catalog().for_venue(venue, "") for venue in ("3861", "3752", "3652")]

    respx.get(sport.scoreboard_url).respond(json={"events": events})
    weather = [("Heavy Snow", 27, 90), ("Rain", 42, 85), ("Sunny", 55, 0)]
    alerts = [
        {
            "properties": {
                "event": "Winter Storm Warning",
                "onset": f"{day.isoformat()}T12:00:00-05:00",
                "ends": f"{day.isoformat()}T20:00:00-05:00",
            }
        }
    ]
    for index, (stadium, (summary, temperature, odds)) in enumerate(
        zip(stadiums, weather, strict=True)
    ):
        assert stadium is not None
        if clear:
            summary, temperature, odds = "Sunny", 55, 0
        mock_hourly_forecast(
            stadium.latitude,
            stadium.longitude,
            response=forecast_response(summary, temperature, "7 mph", odds, day=day.isoformat()),
            alerts=alerts if index == 0 else [],
        )


def _collect_day(sport, now, day):
    return next(result for result in collector.collect_window(sport, now) if result.game_day == day)


def _save(sport, now, result):
    scheduler_store.initialize(now)
    execution = scheduler_store.claim_manual(sport.slug, now)
    scheduler_store.save_day(execution, result)
    scheduler_store.finish(execution, dt.datetime.now(dt.UTC), "complete")


@pytest.mark.parametrize(
    ("sport", "main", "first", "second"),
    [
        (NFL, nfl_main, "NE @ BUF", "CHI @ GB"),
        (CFB, cfb_main, "Michigan vs #3 Ohio State", "Boston College @ #8 Pitt"),
    ],
)
def test_stored_preview_matches_live(sport, main, first, second, game_day, capsys):
    now, day = game_day
    with respx.mock:
        _mock_slate(sport, day)
        _save(sport, now, _collect_day(sport, now, day))
        calls_after_collection = len(respx.calls)
        assert main(["--dry-run", "--live"]) == EXIT_OK
        assert len(respx.calls) > calls_after_collection
        live = capsys.readouterr().out
    db_path = Path(os.environ["MESSY_WEATHER_DB_PATH"])
    before = db_path.read_bytes()

    # Running against saved forecasts alone must work with every HTTP endpoint offline.
    with respx.mock:
        assert main(["--dry-run", "--date", day.isoformat()]) == EXIT_OK

    assert capsys.readouterr().out == live
    assert live.index(first) < live.index(second)
    assert "Winter Storm Warning" in live
    assert "Sunny" not in live
    assert "Clemson" not in live
    assert "Toledo" not in live
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("empty", [False, True])
def test_verified_quiet_day_can_skip_live_fetch(game_day, capsys, empty):
    now, day = game_day
    with respx.mock:
        if empty:
            respx.get(NFL.scoreboard_url).respond(json={"events": []})
        else:
            _mock_slate(NFL, day, clear=True)
        _save(NFL, now, _collect_day(NFL, now, day))

    with respx.mock:
        assert nfl_main(["--dry-run"]) == EXIT_OK
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("problem", ["stale", "gapped", "missing", "newer-failure", "legacy"])
def test_saved_data_needs_a_verified_fresh_complete_slate(game_day, problem, capsys):
    now, day = game_day
    with respx.mock:
        _mock_slate(NFL, day)
        result = _collect_day(NFL, now, day)
        _save(NFL, now, result)

    if problem == "newer-failure":
        _save(
            NFL,
            now,
            collector.CollectedDay(day, dt.datetime.now(dt.UTC), "schedule_unavailable"),
        )
    else:
        with forecast_db_transaction() as db:
            if problem == "stale":
                db.exec_driver_sql(
                    "UPDATE collection_runs SET started_at = ?",
                    ((now - dt.timedelta(hours=13)).isoformat(),),
                )
            elif problem == "gapped":
                db.exec_driver_sql(
                    "UPDATE forecast_periods SET ends_at = ?", (f"{day}T14:00:00-05:00",)
                )
                db.exec_driver_sql(
                    """
                    INSERT INTO forecast_periods
                    SELECT observation_id, 1, ?, ?, short_forecast, temperature_f,
                           wind_speed_mph, precipitation_probability, relative_humidity,
                           feels_like_f, 0
                    FROM forecast_periods WHERE period_index = 0
                    """,
                    (f"{day}T15:00:00-05:00", f"{day}T18:00:00-05:00"),
                )
            elif problem == "missing":
                db.exec_driver_sql("DELETE FROM forecast_periods WHERE observation_id = 1")
                db.exec_driver_sql("DELETE FROM game_observations WHERE id = 1")
            else:
                db.exec_driver_sql("UPDATE collection_runs SET publication_game_count = NULL")

    with respx.mock:
        respx.get(NFL.scoreboard_url).respond(503)
        assert nfl_main(["--dry-run"]) == EXIT_NOTHING_POSTED
    assert capsys.readouterr().out == ""


def test_live_refresh_can_replace_incomplete_stored_data(game_day, capsys):
    now, day = game_day
    with respx.mock:
        _mock_slate(NFL, day)
        result = _collect_day(NFL, now, day)
        _save(NFL, now, replace(result, outcome="partial"))
        assert nfl_main(["--dry-run"]) == EXIT_OK
    assert "NE @ BUF" in capsys.readouterr().out


def test_live_forecast_without_full_game_coverage_is_not_recommended(game_day, capsys):
    _, day = game_day
    stadium = stadium_for_team("GB")
    response = forecast_response("Heavy Snow", 27, "15 mph", 90, day=day.isoformat()).json()
    response["properties"]["periods"][0]["endTime"] = f"{day}T14:00:00-05:00"
    with respx.mock:
        event = espn_event("GB", "CHI", kickoff=f"{day}T13:00:00-05:00")
        respx.get(NFL.scoreboard_url).respond(json={"events": [event]})
        mock_hourly_forecast(
            stadium.latitude, stadium.longitude, response=httpx.Response(200, json=response)
        )
        assert nfl_main(["--dry-run"]) == EXIT_NOTHING_POSTED
    assert capsys.readouterr().out == ""


def test_live_refresh_needs_an_explicit_scoreboard_slate(game_day, capsys):
    with respx.mock:
        respx.get(NFL.scoreboard_url).respond(json={})
        assert nfl_main(["--dry-run"]) == EXIT_NOTHING_POSTED
    assert capsys.readouterr().out == ""


def test_collection_after_kickoff_cannot_certify_an_empty_day(game_day, capsys):
    _, day = game_day
    local_evening = dt.datetime.combine(day, dt.time(21), tzinfo=NFL.game_day_timezone)
    with respx.mock:
        _mock_slate(NFL, day)
        result = _collect_day(NFL, local_evening, day)
        assert result.games_found == 0
        _save(NFL, local_evening, result)

    with respx.mock:
        respx.get(NFL.scoreboard_url).respond(503)
        assert nfl_main(["--dry-run"]) == EXIT_NOTHING_POSTED
    assert capsys.readouterr().out == ""


def test_newer_verified_empty_day_supersedes_an_older_messy_slate(game_day, capsys):
    now, day = game_day
    with respx.mock:
        _mock_slate(NFL, day)
        _save(NFL, now, _collect_day(NFL, now, day))
        respx.get(NFL.scoreboard_url).respond(json={"events": []})
        _save(NFL, now, _collect_day(NFL, now, day))

    with respx.mock:
        assert nfl_main(["--dry-run"]) == EXIT_OK
    assert capsys.readouterr().out == ""


def test_a_ranked_game_with_no_venue_cannot_certify_a_quiet_slate(game_day, capsys):
    now, day = game_day
    event = cfb_event("Ohio State", "Michigan", home_rank=3, kickoff=f"{day}T13:00:00-05:00")
    del event["competitions"][0]["venue"]
    with respx.mock:
        respx.get(CFB.scoreboard_url).respond(json={"events": [event]})
        _save(CFB, now, _collect_day(CFB, now, day))

    with respx.mock:
        respx.get(CFB.scoreboard_url).respond(503)
        assert cfb_main(["--dry-run"]) == EXIT_NOTHING_POSTED
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("retry_temperature", [27, 26, None])
def test_stored_partial_thread_only_resumes_with_a_matching_prefix(
    game_day, monkeypatch, capsys, retry_temperature
):
    now, day = game_day

    class ReplyOutage(ConsolePoster):
        failing = True

        def reply(self, text, parent):
            if self.failing:
                raise RuntimeError("temporary reply outage")
            return super().reply(text, parent)

    poster = ReplyOutage()
    prefixes = []

    def build_posters(platform_names, dry_run, env_prefix):
        prefixes.append(env_prefix)
        return [poster]

    monkeypatch.setattr("messy_weather_sports_bot.pipeline.build_posters", build_posters)
    long_names = ("Ohio " * 12, "Pittsburgh " * 7)

    def serve_thread(root_temperature):
        kickoff = f"{day}T13:00:00-05:00"
        events = [
            cfb_event(long_names[0], "Michigan", home_rank=3, kickoff=kickoff),
            cfb_event(
                long_names[1], "Boston College", away_rank=8, venue_id="3752", kickoff=kickoff
            ),
        ]
        respx.get(CFB.scoreboard_url).respond(json={"events": events})
        for venue, summary, temperature in (
            ("3861", "Heavy Snow", root_temperature),
            ("3752", "Rain", 44),
        ):
            stadium = cfb_catalog().for_venue(venue, "")
            assert stadium is not None
            mock_hourly_forecast(
                stadium.latitude,
                stadium.longitude,
                response=forecast_response(summary, temperature, "15 mph", 95, day=day.isoformat()),
            )

    with respx.mock:
        serve_thread(27)
        _save(CFB, now, _collect_day(CFB, now, day))

    with respx.mock:
        assert cfb_main([]) == EXIT_PARTIAL
    assert len(poster.posted) == 1
    first_post = capsys.readouterr().out
    assert "Heavy Snow" in first_post and "Boston College" not in first_post

    poster.failing = False
    with respx.mock:
        if retry_temperature is None:
            respx.get(CFB.scoreboard_url).respond(json={"events": []})
        else:
            serve_thread(retry_temperature)
        assert cfb_main(["--live"]) == (EXIT_OK if retry_temperature == 27 else EXIT_PARTIAL)
    retry = capsys.readouterr().out
    if retry_temperature != 27:
        assert retry == ""
        assert len(poster.posted) == 1
        # The original stored slate can still complete the outstanding thread.
        with respx.mock:
            assert cfb_main([]) == EXIT_OK
        retry = capsys.readouterr().out

    assert "--- reply to console-1 ---" in retry
    assert "Boston College" in retry
    with respx.mock:
        assert cfb_main([]) == EXIT_OK
    assert capsys.readouterr().out == ""
    assert len(poster.posted) == 2
    assert set(prefixes) == {"CFB_"}
    state_dir = Path(os.environ["MESSY_WEATHER_STATE_DIR"])
    assert sorted(path.name for path in state_dir.glob("*.json")) == [f"{day}.cfb.json"]
