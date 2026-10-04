#!/usr/bin/env python
"""Flags NFL venues `stadiums.py` doesn't recognize before they silently drop a
game's weather report.

Checks the upcoming week's US-hosted games against `stadiums.py`, one ESPN
scoreboard date at a time (the only query shape the scoreboard endpoint is known
to support reliably). Run weekly during the season via
`.github/workflows/venue-drift-check.yml`, this catches a stadium rename or a
relocated game mid-week rather than losing a team's home games silently on
Sunday morning; run more often, it also catches drift closer to when it happens.

Exits non-zero (failing the CI job) if any US game's venue isn't recognized.
International venues are skipped deliberately (see `schedule.skip_reason`) and
never count as drift.
"""

from __future__ import annotations

import datetime as dt
import sys

import httpx

from messy_weather_sports_bot.schedule import get_todays_games, todays_local_date, venue_drift

DAYS_TO_CHECK = 8
"""A week ahead plus one day of slack, so a weekly cron catches every game at
least once even if a run is skipped or shifts by a day."""


def main() -> int:
    start = todays_local_date()
    drift: list[str] = []
    games_checked = 0

    with httpx.Client(timeout=10.0) as client:
        for offset in range(DAYS_TO_CHECK):
            date = start + dt.timedelta(days=offset)
            try:
                games = get_todays_games(date, client=client)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"error: could not fetch schedule for {date}: {exc}", file=sys.stderr)
                return 1
            games_checked += len(games)
            drift.extend(venue_drift(games))

    end = start + dt.timedelta(days=DAYS_TO_CHECK - 1)
    print(f"Checked {games_checked} game(s) from {start} to {end}.")

    if drift:
        print(f"\n{len(drift)} venue(s) not recognized in stadiums.py:", file=sys.stderr)
        for reason in drift:
            print(f"  - {reason}", file=sys.stderr)
        print(
            "\nUpdate src/messy_weather_sports_bot/stadiums.py with the new venue.", file=sys.stderr
        )
        return 1

    print("No venue drift detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
