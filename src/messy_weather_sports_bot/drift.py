"""The venue drift check: do the upcoming games' venues all appear in a sport's table?

A stadium rename or a relocated game that the table doesn't know about silently drops that
game from the day's report, so this scans the coming week's games - all of them, not just
the ones the sport covers, since any venue can host a covered game next week - and lists
every US venue the sport's catalog doesn't recognize. International venues are skipped on
purpose and never count (see `schedule.skip_reason`). `scripts/check_venue_drift.py` is the
command line; `.github/workflows/venue-drift-check.yml` runs it on a schedule.

ESPN is asked with the client's default User-Agent: its edge refuses unfamiliar ones.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import httpx

from messy_weather_sports_bot.espn import EspnScoreboard
from messy_weather_sports_bot.schedule import todays_game_day, venue_drift
from messy_weather_sports_bot.sport import Sport

DAYS_TO_CHECK = 8
"""A week ahead plus one day of slack, so a weekly cron catches every game at
least once even if a run is skipped or shifts by a day."""


class DriftCheckError(Exception):
    """A day's schedule couldn't be fetched, so the check can't say whether there's drift."""


@dataclass
class DriftReport:
    start: dt.date
    end: dt.date
    games_checked: int = 0
    drift: list[str] = field(default_factory=list)
    """Why each unrecognized venue wasn't, e.g. 'unrecognized venue "X" (espn venue id ...)'."""


def check_drift(
    sport: Sport,
    client: httpx.Client,
    *,
    start: dt.date | None = None,
    days: int = DAYS_TO_CHECK,
) -> DriftReport:
    """Scan `days` game days from `start` (default today) for venues `sport` doesn't know.
    Raises DriftCheckError if a day's schedule can't be fetched."""
    start = start or todays_game_day(sport)
    report = DriftReport(start=start, end=start + dt.timedelta(days=days - 1))
    scoreboard = EspnScoreboard(sport, client)
    for offset in range(days):
        date = start + dt.timedelta(days=offset)
        try:
            games = scoreboard.fetch(date)
        except (httpx.HTTPError, ValueError) as exc:
            raise DriftCheckError(f"could not fetch schedule for {date}: {exc}") from exc
        report.games_checked += len(games)
        report.drift.extend(venue_drift(games))
    return report
