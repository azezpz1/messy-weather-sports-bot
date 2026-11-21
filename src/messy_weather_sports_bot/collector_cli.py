"""Command-line access to forecast collection and its schedules."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from collections.abc import Sequence

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from messy_weather_sports_bot import scheduler, scheduler_store
from messy_weather_sports_bot.sports import SPORTS

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="messy-weather-collector", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Refresh a sport now")
    collect.add_argument("--sport", required=True, choices=sorted(SPORTS))
    commands.add_parser("worker", help="Run the recurring collection worker")
    schedule = commands.add_parser("schedule", help="Inspect or change a recurring schedule")
    actions = schedule.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="Show schedules and latest executions as JSON")
    interval = actions.add_parser("set-interval", help="Set the interval in whole hours")
    interval.add_argument("sport", choices=sorted(SPORTS))
    interval.add_argument("hours", type=int, metavar="HOURS")
    for action in ("pause", "resume"):
        actions.add_parser(action).add_argument("sport", choices=sorted(SPORTS))
    args = parser.parse_args(argv)
    if (
        args.command == "schedule"
        and args.action == "set-interval"
        and not 1 <= args.hours <= scheduler_store.MAX_INTERVAL_SECONDS // 3600
    ):
        interval.error("HOURS must be between 1 and 8760")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = dt.datetime.now(dt.UTC)
    try:
        scheduler_store.initialize(now)
        if args.command == "worker":
            scheduler.run_worker()
        elif args.command == "collect":
            outcome = scheduler.run_once(args.sport)
            if outcome is None:
                logger.warning("Collection is already running")
            return {"complete": 0, "failed": 1, "partial": 2, None: 2}[outcome]
        elif args.action == "status":
            print(json.dumps([dict(row) for row in scheduler_store.status()], indent=2))
        elif args.action == "set-interval":
            scheduler_store.configure(args.sport, now, interval_seconds=args.hours * 3600)
        else:
            scheduler_store.configure(args.sport, now, enabled=args.action == "resume")
    except KeyboardInterrupt:
        return 130
    except (OSError, SQLAlchemyError, CommandError) as exc:
        logger.error("Collection storage unavailable (%s)", type(exc).__name__)
        return 1
    return 0
