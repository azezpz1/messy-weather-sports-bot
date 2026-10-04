"""A day's games, and which of them weather applies to.

Fetching them from ESPN is `espn.py`; resolving a game's venue is `venues.py`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from messy_weather_sports_bot.venues import StadiumInfo

if TYPE_CHECKING:
    from messy_weather_sports_bot.sport import Sport


@dataclass(frozen=True)
class Game:
    home_team: str
    away_team: str
    kickoff: dt.datetime
    stadium: StadiumInfo | None
    """None when the venue isn't a recognized, fixed home stadium (e.g. a neutral-site
    or international game) and weather can't be looked up for it."""
    venue_name: str = ""
    """The raw venue name ESPN reported, kept for logging when `stadium` is None or
    covered even though it isn't otherwise needed once `stadium` has been resolved."""
    venue_id: str = ""
    """The raw ESPN venue id, kept for logging when `stadium` is None."""
    unresolved_reason: str | None = None
    """Why `stadium` is None, for `skip_reason()` - not set when `stadium` is resolved."""
    is_venue_drift: bool = False
    """True when `unresolved_reason` signals the venue table is out of date (an
    unrecognized US venue) rather than an expected, ignorable skip - see
    `venue_drift()`."""


def todays_game_day(sport: Sport, now: dt.datetime | None = None) -> dt.date:
    """Today's calendar date in `sport`'s game-day timezone."""
    return sport.game_day_for(now or dt.datetime.now(tz=sport.game_day_timezone))


def outdoor_games(games: list[Game]) -> list[Game]:
    """Games with a known, uncovered stadium - the only ones weather applies to."""
    return [g for g in games if g.stadium is not None and not g.stadium.is_covered]


def skip_reason(game: Game) -> str | None:
    """Why `game` isn't a candidate for a weather lookup, or None if it is one."""
    if game.stadium is None:
        return game.unresolved_reason
    if game.stadium.is_covered:
        return f"covered stadium ({game.stadium.name})"
    return None


def venue_drift(games: list[Game]) -> list[str]:
    """Reasons for games whose US venue isn't recognized - i.e. the venue table has
    drifted out of date (a rename, a relocation to a new venue) rather than an
    expected, ignorable skip (an international game, or an unrecognized home team).
    Used by the scheduled drift-check job so this is caught mid-week instead of
    silently dropping a game.
    """
    return [g.unresolved_reason for g in games if g.is_venue_drift and g.unresolved_reason]
