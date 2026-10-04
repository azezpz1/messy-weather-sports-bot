import dataclasses
import datetime as dt
import importlib.metadata
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot import cli
from messy_weather_sports_bot.cli import configure_logging, logger
from messy_weather_sports_bot.nfl import NFL, main
from messy_weather_sports_bot.pipeline import (
    EXIT_NOTHING_POSTED,
    EXIT_OK,
    EXIT_PARTIAL,
    GameDayPipeline,
)
from tests.support.espn import espn_event
from tests.support.nws import clear_period_response, mock_hourly_forecast, rainy_period_response

SCOREBOARD_URL = NFL.scoreboard_url
LOGGER_NAME = "messy_weather_sports_bot"

GB_LAT, GB_LON = 44.5013, -88.0622
BUF_LAT, BUF_LON = 42.77306, -78.79222
TARGET_DATE = dt.date(2026, 1, 18)


def run(
    platform_names: list[str], dry_run: bool, force: bool = False, date: dt.date | None = None
) -> int:
    return GameDayPipeline(NFL).run(platform_names, dry_run, force, date)


def parse_args(argv: list[str]):
    return cli.parse_args(NFL, argv)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.todays_game_day", lambda sport: TARGET_DATE
    )


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Every test gets its own state directory, so a run in one test can never see -
    # or leave behind - state from another.
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "state"))


def _two_game_schedule() -> dict:
    return {
        "events": [
            espn_event("GB", "CHI", "Lambeau Field"),
            espn_event("BUF", "NE", "Highmark Stadium"),
        ]
    }


def _covered_and_international_schedule() -> dict:
    return {
        "events": [
            espn_event("MIN", "DET", "U.S. Bank Stadium"),
            espn_event("JAX", "PHI", "Tottenham Hotspur Stadium", country="England"),
        ]
    }


@respx.mock
def test_one_failing_stadium_still_posts_the_other_games(capsys: pytest.CaptureFixture) -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    # BUF's forecast is persistently broken - api.weather.gov's real-world 500s.
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    out = capsys.readouterr().out
    assert exit_code == EXIT_PARTIAL
    assert "CHI @ GB" in out
    assert "NE @ BUF" not in out


@respx.mock
def test_nws_failure_on_every_game_returns_nothing_posted() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=httpx.Response(500))
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_NOTHING_POSTED


@respx.mock
def test_espn_failure_returns_nothing_posted() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_NOTHING_POSTED


@respx.mock
def test_all_games_succeed_returns_ok() -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK


@respx.mock
def test_an_active_warning_appears_on_the_game_line(capsys: pytest.CaptureFixture) -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    # Override GB's default "no active alerts" mock with an active, overlapping warning.
    respx.get(f"https://api.weather.gov/alerts/active?point={GB_LAT},{GB_LON}").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "properties": {
                            "event": "Winter Storm Warning",
                            "onset": "2026-01-18T17:00:00+00:00",
                            "ends": "2026-01-18T23:00:00+00:00",
                        }
                    }
                ]
            },
        )
    )

    exit_code = run(platform_names=[], dry_run=True)

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Winter Storm Warning" in out


@respx.mock
def test_a_failed_alert_lookup_does_not_skip_the_game(capsys: pytest.CaptureFixture) -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    # GB's alert lookup is persistently broken - the game must still post, with no alert.
    respx.get(f"https://api.weather.gov/alerts/active?point={GB_LAT},{GB_LON}").mock(
        return_value=httpx.Response(500)
    )

    exit_code = run(platform_names=[], dry_run=True)

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "CHI @ GB" in out


@respx.mock
def test_one_poster_failing_still_lets_the_others_post(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    class BrokenPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            raise RuntimeError("platform is down")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            raise RuntimeError("platform is down")

    posted_texts: list[str] = []

    class RecordingPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            posted_texts.append(text)
            return PostRef(id="1", root_id="1")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            posted_texts.append(text)
            return PostRef(id="2", root_id=parent.root_id)

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [BrokenPoster(), RecordingPoster()],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    exit_code = run(platform_names=["bluesky"], dry_run=False)

    assert exit_code == EXIT_PARTIAL
    assert posted_texts  # the working poster still ran, despite the broken one raising
    assert "platform is down" in caplog.text


@respx.mock
def test_a_partially_posted_thread_is_partial_not_nothing_posted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # If the root of a thread published before a later reply failed, the poster did
    # publish something - that must not be reported as EXIT_NOTHING_POSTED.
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    class FailsAfterRootPost(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            raise RuntimeError("platform outage")

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [FailsAfterRootPost()],
    )
    # Force a multi-post thread (root + reply) regardless of formatting specifics -
    # what's under test here is the poster/exit-code interaction, not chunking.
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_post_texts",
        lambda ranked, date, sport: ["root post", "reply post"],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    exit_code = run(platform_names=["bluesky"], dry_run=False)

    assert exit_code == EXIT_PARTIAL
    assert "Partially posted" in caplog.text


@respx.mock
def test_skipped_games_log_their_reason(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(
        return_value=httpx.Response(200, json=_covered_and_international_schedule())
    )

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK  # no outdoor games today at all
    assert "DET @ MIN — skipped: covered stadium (U.S. Bank Stadium)" in caplog.text
    assert 'PHI @ JAX — skipped: international venue "Tottenham Hotspur Stadium"' in caplog.text


@respx.mock
def test_forecast_unavailable_is_logged_as_a_skip_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_PARTIAL
    assert "NE @ BUF — skipped: forecast unavailable" in caplog.text


@respx.mock
def test_included_game_logs_score_and_weather_detail(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    run(platform_names=[], dry_run=True)

    assert "CHI @ GB" in caplog.text
    assert "included: score" in caplog.text
    assert "Rain, 40°F, 5mph" in caplog.text
    assert "1 hourly period" in caplog.text


@respx.mock
def test_nice_weather_games_are_left_out_of_the_post(
    capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression for 2026-09-27: every outdoor game was posted, sunny ones included.
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=clear_period_response())

    exit_code = run(platform_names=[], dry_run=True)

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "CHI @ GB" in out
    assert "NE @ BUF" not in out
    assert "NE @ BUF 1:00pm ET — skipped: not messy" in caplog.text
    assert "Run summary: 2 game(s) found, 2 outdoor, 2 evaluated, 1 messy" in caplog.text


@respx.mock
def test_a_day_with_no_messy_games_posts_nothing_and_is_ok(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    def _no_posters(platform_names, dry_run, env_prefix):
        raise AssertionError("nothing should be posted on a nice-weather day")

    monkeypatch.setattr("messy_weather_sports_bot.pipeline.build_posters", _no_posters)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=clear_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=clear_period_response())

    exit_code = run(platform_names=["bluesky"], dry_run=False)

    assert exit_code == EXIT_OK
    assert "No messy-weather games" in caplog.text
    assert "Run summary: 2 game(s) found, 2 outdoor, 2 evaluated, 0 messy" in caplog.text


@respx.mock
def test_no_messy_games_with_a_missing_forecast_is_partial() -> None:
    # The game whose forecast couldn't be fetched might have been the messy one, so an
    # empty day with a gap in the data is degraded, not clean.
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=clear_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_PARTIAL


@respx.mock
def test_run_summary_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    run(platform_names=[], dry_run=True)

    assert "Run summary: 2 game(s) found, 2 outdoor, 2 evaluated, 2 messy" in caplog.text


@respx.mock
def test_run_summary_is_logged_even_when_no_outdoor_games(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_OK
    assert "Run summary: 0 game(s) found, 0 outdoor, 0 evaluated, 0 messy" in caplog.text


@respx.mock
def test_run_summary_is_logged_even_when_every_forecast_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=httpx.Response(500))
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_NOTHING_POSTED
    assert "Run summary: 2 game(s) found, 2 outdoor, 0 evaluated, 0 messy" in caplog.text


@respx.mock
def test_healthcheck_pings_start_and_end_with_matching_rid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    start_route = respx.get("https://hc-ping.com/test-uuid/start").mock(
        return_value=httpx.Response(200)
    )
    end_route = respx.post("https://hc-ping.com/test-uuid/0").mock(return_value=httpx.Response(200))

    exit_code = main(["--dry-run"])

    assert exit_code == EXIT_OK
    assert start_route.called
    assert end_route.called
    start_rid = start_route.calls.last.request.url.params["rid"]
    end_rid = end_route.calls.last.request.url.params["rid"]
    assert start_rid == end_rid
    assert end_route.calls.last.request.content  # the summary/log-tail body


@respx.mock
def test_no_healthcheck_url_means_no_pings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))

    exit_code = main(["--dry-run"])

    assert exit_code == EXIT_OK
    assert not any(call.request.url.host == "hc-ping.com" for call in respx.calls)


@respx.mock
def test_a_failed_healthcheck_ping_never_changes_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    respx.get("https://hc-ping.com/test-uuid/start").mock(return_value=httpx.Response(500))
    respx.post("https://hc-ping.com/test-uuid/0").mock(return_value=httpx.Response(500))

    exit_code = main(["--dry-run"])

    assert exit_code == EXIT_OK


@respx.mock
def test_an_unhandled_exception_still_sends_a_failure_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get("https://hc-ping.com/test-uuid/start").mock(return_value=httpx.Response(200))
    end_route = respx.post(f"https://hc-ping.com/test-uuid/{EXIT_NOTHING_POSTED}").mock(
        return_value=httpx.Response(200)
    )
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.GameDayPipeline.run",
        lambda self, platform_names, dry_run, force, date: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        main(["--dry-run"])

    assert end_route.called


INSTALLED_VERSION = importlib.metadata.version("messy-weather-sports-bot")


@respx.mock
def test_the_running_version_is_logged_first(caplog: pytest.LogCaptureFixture) -> None:
    # So a cron log says which release produced it - e.g. to tell a Pi still on an
    # old release from one whose update job silently never ran.
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))

    main(["--dry-run"])

    assert (
        caplog.records[0].getMessage() == f"messy-weather-sports-bot {INSTALLED_VERSION} starting"
    )


@respx.mock
def test_the_healthcheck_completion_body_includes_the_running_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    respx.get("https://hc-ping.com/test-uuid/start").mock(return_value=httpx.Response(200))
    end_route = respx.post("https://hc-ping.com/test-uuid/0").mock(return_value=httpx.Response(200))

    main(["--dry-run"])

    body = end_route.calls.last.request.content.decode()
    assert f"messy-weather-sports-bot {INSTALLED_VERSION} starting" in body


@respx.mock
def test_the_failure_ping_after_a_crash_includes_the_running_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get("https://hc-ping.com/test-uuid/start").mock(return_value=httpx.Response(200))
    end_route = respx.post(f"https://hc-ping.com/test-uuid/{EXIT_NOTHING_POSTED}").mock(
        return_value=httpx.Response(200)
    )
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.GameDayPipeline.run",
        lambda self, platform_names, dry_run, force, date: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        main(["--dry-run"])

    body = end_route.calls.last.request.content.decode()
    assert f"messy-weather-sports-bot {INSTALLED_VERSION} starting" in body


@respx.mock
def test_an_uninstalled_package_logs_an_unknown_version_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # e.g. run straight from a source tree that was never `uv sync`ed.
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))

    def _not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", _not_installed)

    assert main(["--dry-run"]) == EXIT_OK
    assert "messy-weather-sports-bot (unknown version) starting" in caplog.text


def test_verbose_and_quiet_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--verbose", "--quiet"])


def test_force_flag_defaults_to_false() -> None:
    assert parse_args([]).force is False
    assert parse_args(["--force"]).force is True


@pytest.mark.parametrize(
    ("flags", "expected_logger_level", "expected_console_level"),
    [
        ([], logging.INFO, logging.NOTSET),
        (["--verbose"], logging.DEBUG, logging.NOTSET),
        # --quiet only raises the console handler's threshold - the logger itself stays
        # at INFO, so a healthcheck ping body isn't missing the summary just because
        # the console stayed quiet (see test_quiet_mode_still_captures_the_run_summary).
        (["--quiet"], logging.INFO, logging.WARNING),
    ],
)
def test_configure_logging_sets_the_level_from_cli_flags(
    flags: list[str], expected_logger_level: int, expected_console_level: int
) -> None:
    args = parse_args(flags)
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    assert logger.level == expected_logger_level
    assert logger.handlers[0].level == expected_console_level


@respx.mock
def test_run_summary_is_logged_even_when_the_schedule_fetch_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(500))

    exit_code = run(platform_names=[], dry_run=True)

    assert exit_code == EXIT_NOTHING_POSTED
    assert "Run summary: unavailable game(s) found, 0 outdoor, 0 evaluated, 0 messy" in caplog.text


@respx.mock
def test_running_twice_posts_once_and_the_second_run_skips(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    post_calls = {"count": 0}

    class RecordingPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            post_calls["count"] += 1
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            post_calls["count"] += 1
            return PostRef(id=f"reply-{post_calls['count']}", root_id=parent.root_id)

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [RecordingPoster()],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    first_exit = run(platform_names=["bluesky"], dry_run=False)
    calls_after_first_run = post_calls["count"]
    second_exit = run(platform_names=["bluesky"], dry_run=False)

    assert first_exit == EXIT_OK
    assert second_exit == EXIT_OK
    assert post_calls["count"] == calls_after_first_run  # nothing new posted the second time
    assert "already posted today's thread; skipping" in caplog.text


@respx.mock
def test_force_reposts_even_though_todays_thread_already_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    post_calls = {"count": 0}

    class RecordingPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            post_calls["count"] += 1
            return PostRef(id=f"root-{post_calls['count']}", root_id=f"root-{post_calls['count']}")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            post_calls["count"] += 1
            return PostRef(id=f"reply-{post_calls['count']}", root_id=parent.root_id)

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [RecordingPoster()],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    run(platform_names=["bluesky"], dry_run=False)
    calls_after_first_run = post_calls["count"]
    exit_code = run(platform_names=["bluesky"], dry_run=False, force=True)

    assert exit_code == EXIT_OK
    assert post_calls["count"] > calls_after_first_run  # --force posted again


@respx.mock
def test_a_partial_thread_resumes_from_the_last_successful_post_on_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from messy_weather_sports_bot.poster.base import (
        PUBLISH_MAX_ATTEMPTS,
        PostRef,
        SocialMediaPoster,
    )

    posted_texts: list[str] = []

    class FailsFirstRepliesThenWorks(SocialMediaPoster):
        """Every reply() call fails until enough calls have happened to exhaust
        post_thread's own retries on the first attempted reply - so the first run's
        thread stalls after the root, and only a fresh run's replies succeed."""

        def __init__(self) -> None:
            self.calls = 0

        def post(self, text: str) -> PostRef:
            posted_texts.append(text)
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            self.calls += 1
            if self.calls <= PUBLISH_MAX_ATTEMPTS:
                raise RuntimeError("platform outage")
            posted_texts.append(text)
            return PostRef(id=f"reply-{text}", root_id=parent.root_id)

    poster = FailsFirstRepliesThenWorks()
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [poster],
    )
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_post_texts",
        lambda ranked, date, sport: ["root post", "reply 1", "reply 2"],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    first_exit = run(platform_names=["bluesky"], dry_run=False)
    assert first_exit == EXIT_PARTIAL
    assert posted_texts == ["root post"]  # only the root went out before it stalled

    second_exit = run(platform_names=["bluesky"], dry_run=False)

    assert second_exit == EXIT_OK
    # The root was never reposted on the resumed run - only the remaining replies went out.
    assert posted_texts == ["root post", "reply 1", "reply 2"]


@respx.mock
def test_dry_run_never_reads_or_writes_state(tmp_path) -> None:
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())

    run(platform_names=[], dry_run=True)
    run(platform_names=[], dry_run=True)

    assert not (tmp_path / "state").exists()


@respx.mock
def test_a_state_write_failure_does_not_change_the_posting_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A successful post must count as posted even if persisting that fact to disk
    # fails (a full disk, a read-only state dir) - the post already went out for
    # real, regardless of whether the state write did.
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    class RecordingPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            return PostRef(id="reply", root_id=parent.root_id)

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [RecordingPoster()],
    )

    def _broken_record(self, name, posts, *, completed):
        raise OSError("disk full")

    monkeypatch.setattr("messy_weather_sports_bot.state.DayState.record", _broken_record)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    exit_code = run(platform_names=["bluesky"], dry_run=False)

    assert exit_code == EXIT_OK
    assert "Could not save post state" in caplog.text


@respx.mock
def test_a_state_write_failure_after_a_partial_thread_does_not_escape_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Same as above, but for the other call site: recording state after a
    # PartialThreadError must not let an OSError there propagate out of run() -
    # the partial-posting outcome already happened for real.
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    class FailsAfterRootPost(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            raise RuntimeError("platform outage")

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [FailsAfterRootPost()],
    )
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_post_texts",
        lambda ranked, date, sport: ["root post", "reply post"],
    )

    def _broken_record(self, name, posts, *, completed):
        raise OSError("disk full")

    monkeypatch.setattr("messy_weather_sports_bot.state.DayState.record", _broken_record)
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    exit_code = run(platform_names=["bluesky"], dry_run=False)

    assert exit_code == EXIT_PARTIAL
    assert "Could not save post state" in caplog.text
    assert "Partially posted" in caplog.text


@respx.mock
def test_quiet_mode_still_captures_the_run_summary_for_the_healthcheck_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --quiet silences the console, but the healthcheck ping body must still carry the
    # run summary - it's captured by a separate handler on the same (INFO-level) logger.
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/test-uuid")
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    respx.get("https://hc-ping.com/test-uuid/start").mock(return_value=httpx.Response(200))
    end_route = respx.post(f"https://hc-ping.com/test-uuid/{EXIT_OK}").mock(
        return_value=httpx.Response(200)
    )

    exit_code = main(["--dry-run", "--quiet"])

    assert exit_code == EXIT_OK
    assert b"Run summary" in end_route.calls.last.request.content


def test_date_flag_parses_and_defaults_to_none() -> None:
    assert parse_args([]).date is None
    assert parse_args(["--date", "2026-10-04"]).date == dt.date(2026, 10, 4)


def test_date_flag_rejects_a_malformed_date() -> None:
    for bad in ["10/04/2026", "20261004", "2026-W41-7", "2026-02-30"]:
        with pytest.raises(SystemExit):
            parse_args(["--date", bad])


@respx.mock
def test_another_date_is_forced_to_dry_run(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    # "Today" is a different day from the (mocked) game day being previewed.
    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.todays_game_day",
        lambda sport: TARGET_DATE + dt.timedelta(days=1),
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    dry_run_seen: list[bool] = []

    def _capture_posters(platform_names: list[str], dry_run: bool, env_prefix: str) -> list:
        dry_run_seen.append(dry_run)
        return []

    monkeypatch.setattr("messy_weather_sports_bot.pipeline.build_posters", _capture_posters)

    run(platform_names=["bluesky"], dry_run=False, date=TARGET_DATE)

    assert dry_run_seen == [True]
    assert "forcing --dry-run" in caplog.text


@respx.mock
def test_two_sports_posting_on_the_same_day_do_not_share_post_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    posts: list[str] = []

    class RecordingPoster(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            posts.append(text)
            return PostRef(id=f"root-{len(posts)}", root_id=f"root-{len(posts)}")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            posts.append(text)
            return PostRef(id=f"reply-{len(posts)}", root_id=parent.root_id)

    monkeypatch.setattr(
        "messy_weather_sports_bot.pipeline.build_posters",
        lambda platform_names, dry_run, env_prefix: [RecordingPoster()],
    )
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json=_two_game_schedule()))
    mock_hourly_forecast(GB_LAT, GB_LON, response=rainy_period_response())
    mock_hourly_forecast(BUF_LAT, BUF_LON, response=rainy_period_response())
    other_sport = dataclasses.replace(NFL, slug="other", state_key="other")

    GameDayPipeline(NFL).run(["bluesky"], dry_run=False)
    posts_after_first_sport = len(posts)
    GameDayPipeline(other_sport).run(["bluesky"], dry_run=False)
    posts_after_second_sport = len(posts)
    GameDayPipeline(NFL).run(["bluesky"], dry_run=False)

    assert posts_after_first_sport > 0
    # The second sport's thread went out even though the first had already completed...
    assert posts_after_second_sport == 2 * posts_after_first_sport
    # ...and the first sport still sees its own saved state, so a rerun posts nothing.
    assert len(posts) == posts_after_second_sport


def test_build_posters_hands_the_sports_env_prefix_to_each_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from messy_weather_sports_bot.pipeline import build_posters
    from messy_weather_sports_bot.poster import POSTERS
    from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

    prefixes_seen: list[str] = []

    class SpyPoster(SocialMediaPoster):
        @classmethod
        def from_env(cls, env_prefix: str = ""):
            prefixes_seen.append(env_prefix)
            return cls()

        def post(self, text: str) -> PostRef:
            raise NotImplementedError

        def reply(self, text: str, parent: PostRef) -> PostRef:
            raise NotImplementedError

    monkeypatch.setitem(POSTERS, "bluesky", SpyPoster)

    build_posters(["bluesky"], dry_run=False, env_prefix="CFB_")

    assert prefixes_seen == ["CFB_"]


@respx.mock
def test_a_prefixed_sport_reports_to_its_own_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/first-uuid")
    monkeypatch.setenv("CFB_HEALTHCHECK_URL", "https://hc-ping.com/other-uuid")
    respx.get(SCOREBOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    first_start = respx.get("https://hc-ping.com/first-uuid/start").mock(
        return_value=httpx.Response(200)
    )
    other_start = respx.get("https://hc-ping.com/other-uuid/start").mock(
        return_value=httpx.Response(200)
    )
    other_end = respx.post(f"https://hc-ping.com/other-uuid/{EXIT_OK}").mock(
        return_value=httpx.Response(200)
    )
    other_sport = dataclasses.replace(NFL, slug="other", env_prefix="CFB_")

    exit_code = cli.run_cli(other_sport, ["--dry-run"])

    assert exit_code == EXIT_OK
    assert other_start.called
    assert other_end.called
    assert not first_start.called
