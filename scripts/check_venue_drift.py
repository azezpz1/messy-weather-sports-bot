#!/usr/bin/env python
"""Flags venues a sport's table doesn't recognize before they silently drop a game's
weather report.

Checks the upcoming week's US-hosted games against the table for `--sport` (the NFL's
`stadiums.py` by default, or the college football table), one ESPN scoreboard date at a
time (the only query shape the scoreboard endpoint is known to support reliably). Run
weekly during each season via `.github/workflows/venue-drift-check.yml`, this catches a
stadium rename or a relocated game mid-week rather than losing a team's games silently on
game day; run more often, it also catches drift closer to when it happens. The logic is in
`messy_weather_sports_bot.drift`.

Exits non-zero (failing the CI job) if any US game's venue isn't recognized, or if a day's
schedule can't be fetched. International venues are skipped deliberately (see
`schedule.skip_reason`) and never count as drift.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from messy_weather_sports_bot.drift import DriftCheckError, check_drift
from messy_weather_sports_bot.sports import SPORTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--sport",
        choices=sorted(SPORTS),
        default="nfl",
        help="Which sport's venue table to check (default: nfl).",
    )
    sport = SPORTS[parser.parse_args(argv).sport]

    try:
        with httpx.Client(timeout=10.0) as client:
            report = check_drift(sport, client)
    except DriftCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Checked {report.games_checked} game(s) from {report.start} to {report.end}.")

    if report.drift:
        print(
            f"\n{len(report.drift)} {sport.slug.upper()} venue(s) not recognized:", file=sys.stderr
        )
        for reason in report.drift:
            print(f"  - {reason}", file=sys.stderr)
        if sport.venue_update_hint:
            print(f"\n{sport.venue_update_hint}", file=sys.stderr)
        return 1

    print("No venue drift detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
