"""CLI entry point: fetch today's outdoor NFL games, check the weather, and post."""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from collections.abc import Sequence

import httpx

from messy_weather_nfl_bot import healthcheck, state
from messy_weather_nfl_bot.formatting import build_post_texts, format_kickoff
from messy_weather_nfl_bot.messiness import GameWeather, evaluate_game, sort_by_messiness
from messy_weather_nfl_bot.poster import POSTERS
from messy_weather_nfl_bot.poster.base import PartialThreadError, SocialMediaPoster
from messy_weather_nfl_bot.poster.console import ConsolePoster
from messy_weather_nfl_bot.schedule import get_todays_games, skip_reason, todays_local_date
from messy_weather_nfl_bot.weather import WeatherReport, get_forecast

logger = logging.getLogger("messy_weather_nfl_bot")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"

# So cron wrappers and health checks can tell "nothing posted" from "posted, but
# degraded" from a clean run.
EXIT_OK = 0
EXIT_NOTHING_POSTED = 1
EXIT_PARTIAL = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platforms",
        default="bluesky",
        help="Comma-separated list of platforms to post to (default: bluesky).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be posted instead of posting to any real platform.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Repost even if today's thread already finished on a platform, ignoring saved state."
        ),
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose", action="store_true", help="Log DEBUG-level detail in addition to INFO."
    )
    verbosity.add_argument("--quiet", action="store_true", help="Only log warnings and errors.")
    return parser.parse_args(argv)


def configure_logging(
    *,
    verbose: bool = False,
    quiet: bool = False,
    extra_handlers: Sequence[logging.Handler] = (),
) -> None:
    """Set up this package's logger: INFO by default, with timestamps so cron logs are
    readable. `extra_handlers` (e.g. one writing into an in-memory buffer for a
    healthcheck ping body) are attached alongside the default stream handler.

    Configures our own logger rather than the root logger, so this can be called
    (and re-called) without disturbing handlers anything else - e.g. a test runner's
    log capture - has attached at the root.

    `--quiet` only raises the console handler's threshold, not the logger's - INFO
    records (the per-game lines, the run summary) still reach `extra_handlers`, so a
    healthcheck ping body isn't missing them just because the console stayed quiet.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING if quiet else logging.NOTSET)
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(stream_handler)

    for handler in extra_handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)


def build_posters(platform_names: list[str], dry_run: bool) -> list[SocialMediaPoster]:
    if dry_run:
        return [ConsolePoster()]

    posters = []
    for name in platform_names:
        try:
            poster_cls = POSTERS[name]
        except KeyError:
            known = sorted(POSTERS)
            raise SystemExit(f"Unknown platform: {name!r}. Known platforms: {known}") from None
        posters.append(poster_cls())
    return posters


def _weather_detail(weather: WeatherReport) -> str:
    parts = [weather.short_forecast]
    if weather.temperature_f is not None:
        parts.append(f"{weather.temperature_f}°F")
    if weather.wind_speed_mph > 0:
        parts.append(f"{weather.wind_speed_mph:g}mph")
    return ", ".join(parts)


def _log_run_summary(
    games_found: int | None,
    outdoor_count: int,
    evaluated_count: int,
    platforms_posted: list[str],
    thread_root: str | None,
) -> None:
    """`games_found` is None when the schedule couldn't be fetched at all - every other
    exit path from `run()` calls this, so the healthcheck completion body always
    carries a summary line."""
    logger.info(
        "Run summary: %s game(s) found, %d outdoor, %d evaluated, platforms: %s%s",
        "unavailable" if games_found is None else games_found,
        outdoor_count,
        evaluated_count,
        ", ".join(platforms_posted) or "none",
        f", thread: {thread_root}" if thread_root else "",
    )


def _record_state(
    day_state: state.DayState | None, platform: str, refs: list[state.PostRef], *, completed: bool
) -> None:
    """Persist `refs` for `platform`, without letting a state-write failure (a full
    disk, a read-only directory) change the posting outcome or escape `run()` -
    `refs` already published successfully regardless of whether this write does."""
    if day_state is None:
        return
    try:
        day_state.record(platform, refs, completed=completed)
    except OSError as exc:
        logger.warning("Could not save post state for %s: %s", platform, exc)


def run(platform_names: list[str], dry_run: bool, force: bool = False) -> int:
    date = todays_local_date()
    try:
        games = get_todays_games(date)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Could not fetch today's NFL schedule: %s", exc)
        _log_run_summary(None, 0, 0, [], None)
        return EXIT_NOTHING_POSTED

    evaluated: list[GameWeather] = []
    outdoor_count = 0
    games_missing = 0
    for game in games:
        matchup = f"{game.away_team} @ {game.home_team}"
        reason = skip_reason(game)
        if reason is not None:
            logger.info("%s — skipped: %s", matchup, reason)
            continue

        outdoor_count += 1
        assert game.stadium is not None  # guaranteed by skip_reason() returning None above
        try:
            forecasts = get_forecast(game.stadium.latitude, game.stadium.longitude, game.kickoff)
        except (httpx.HTTPError, ValueError) as exc:
            games_missing += 1
            logger.info("%s — skipped: forecast unavailable (%s)", matchup, exc)
            continue

        gw = evaluate_game(game, forecasts)
        evaluated.append(gw)
        period_word = "period" if len(forecasts) == 1 else "periods"
        logger.info(
            "%s %s — included: score %.1f (%s) across %d hourly %s",
            matchup,
            format_kickoff(game.kickoff),
            gw.score,
            _weather_detail(gw.weather),
            len(forecasts),
            period_word,
        )

    if outdoor_count == 0:
        logger.info("No outdoor NFL games on %s; nothing to post.", date.isoformat())
        _log_run_summary(len(games), outdoor_count, len(evaluated), [], None)
        return EXIT_OK

    if not evaluated:
        logger.error("Forecast unavailable for every outdoor game today; nothing to post.")
        _log_run_summary(len(games), outdoor_count, len(evaluated), [], None)
        return EXIT_NOTHING_POSTED

    ranked = sort_by_messiness(evaluated)
    post_texts = build_post_texts(ranked, date)
    posters = build_posters(platform_names, dry_run)

    # A dry run must never touch the state file - only construct a DayState (which
    # reads it) when actually posting for real.
    day_state = None if dry_run else state.DayState.load(state.state_file_path(date))

    platforms_posted: list[str] = []
    platforms_failed = 0
    platforms_degraded = 0
    thread_root: str | None = None
    for poster in posters:
        platform = type(poster).__name__
        resume: list[state.PostRef] | None = None
        if day_state is not None and not force:
            platform_state = day_state.for_platform(platform)
            if platform_state.completed:
                logger.info(
                    "%s already posted today's thread; skipping (use --force to repost).",
                    platform,
                )
                platforms_posted.append(platform)
                if platform_state.posts and thread_root is None:
                    thread_root = platform_state.posts[0].id
                continue
            resume = platform_state.posts or None

        try:
            refs = poster.post_thread(post_texts, resume=resume)
            platforms_posted.append(platform)
            _record_state(day_state, platform, refs, completed=True)
            if refs and thread_root is None:
                thread_root = refs[0].id
        except PartialThreadError as exc:
            # Some posts in the thread went out before it failed - not "nothing
            # posted", but still worth flagging as degraded.
            platforms_degraded += 1
            _record_state(day_state, platform, exc.posted, completed=False)
            logger.warning(
                "Partially posted to %s (%d/%d posts before failing): %s",
                platform,
                len(exc.posted),
                len(post_texts),
                exc,
            )
        except Exception as exc:  # isolate one platform's outage from the rest
            platforms_failed += 1
            logger.error("Failed to post to %s: %s", platform, exc)

    if platforms_failed == len(posters):
        exit_code = EXIT_NOTHING_POSTED
    elif games_missing or platforms_failed or platforms_degraded:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_OK

    _log_run_summary(len(games), outdoor_count, len(evaluated), platforms_posted, thread_root)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_buffer = io.StringIO()
    configure_logging(
        verbose=args.verbose, quiet=args.quiet, extra_handlers=[logging.StreamHandler(log_buffer)]
    )
    platform_names = [p.strip() for p in args.platforms.split(",") if p.strip()]

    healthcheck_url = os.environ.get(healthcheck.ENV_VAR)
    run_id = healthcheck.new_run_id()
    if healthcheck_url:
        healthcheck.ping_start(healthcheck_url, run_id)

    try:
        exit_code = run(platform_names, args.dry_run, args.force)
    except BaseException:
        # An unhandled exception means no completion ping below would ever fire -
        # Healthchecks would only notice once the run's grace period expires. Report
        # the failure immediately instead, then let the exception keep propagating.
        if healthcheck_url:
            body = f"exit code: unhandled exception\n\n{log_buffer.getvalue()}"
            healthcheck.ping_end(healthcheck_url, run_id, EXIT_NOTHING_POSTED, body)
        raise

    if healthcheck_url:
        body = f"exit code: {exit_code}\n\n{log_buffer.getvalue()}"
        healthcheck.ping_end(healthcheck_url, run_id, exit_code, body)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
