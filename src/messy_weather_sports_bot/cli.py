"""Command line for a sport's bot: parse arguments, set up logging, run the game-day
pipeline, and report the run to a healthcheck. Each sport's entry module (`nfl.py`, ...)
is a thin call into `run_cli` with its own `Sport`."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import io
import logging
import os
import re
from collections.abc import Sequence

from messy_weather_sports_bot import healthcheck
from messy_weather_sports_bot.pipeline import EXIT_NOTHING_POSTED, GameDayPipeline
from messy_weather_sports_bot.sport import Sport

# The package's own logger: every module logs under it (their `getLogger(__name__)`
# loggers are its children), so handlers attached here - including the in-memory one
# behind a healthcheck ping body - see the whole run.
logger = logging.getLogger("messy_weather_sports_bot")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"

DISTRIBUTION_NAME = "messy-weather-sports-bot"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_date(value: str) -> dt.date:
    # fromisoformat also accepts compact ("20261004") and ISO week dates on 3.11+.
    try:
        if not _DATE_RE.fullmatch(value):
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}: expected YYYY-MM-DD") from None


def parse_args(sport: Sport, argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Fetch today's outdoor {sport.name} games, check the weather, and post."
    )
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
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            f"Report on this game day instead of today ({sport.timezone_label}). For any "
            "other day the run is forced to --dry-run, so it can't post the wrong day's report."
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


def running_version() -> str | None:
    """The installed package version - the one the release workflow bumps in
    pyproject.toml and `uv sync` installs - or None if the package isn't installed
    (e.g. run straight from a source tree that was never synced)."""
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def run_cli(sport: Sport, argv: list[str] | None = None) -> int:
    args = parse_args(sport, argv)
    log_buffer = io.StringIO()
    configure_logging(
        verbose=args.verbose, quiet=args.quiet, extra_handlers=[logging.StreamHandler(log_buffer)]
    )
    # First, so every run's log - and the healthcheck body built from it, even after a
    # crash - says which release produced it.
    version = running_version()
    logger.info("%s %s starting", DISTRIBUTION_NAME, version if version else "(unknown version)")
    platform_names = [p.strip() for p in args.platforms.split(",") if p.strip()]

    healthcheck_url = os.environ.get(healthcheck.env_var_name(sport.env_prefix))
    run_id = healthcheck.new_run_id()
    if healthcheck_url:
        healthcheck.ping_start(healthcheck_url, run_id)

    try:
        exit_code = GameDayPipeline(sport).run(platform_names, args.dry_run, args.force, args.date)
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
