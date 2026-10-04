"""A sport this bot reports on: everything that differs between, say, the NFL and
college football, gathered in one place so each is a configuration of the same pipeline.

Shared code (the schedule fetch, weather lookup, scoring, post formatting, posting and
state) takes a `Sport` rather than reaching for NFL constants.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from messy_weather_sports_bot.venues import VenueCatalog


@dataclass(frozen=True)
class Sport:
    slug: str
    """Short, stable identifier, e.g. "nfl"."""
    name: str
    """How log lines refer to the sport, e.g. "NFL" in "No outdoor NFL games on ..."."""
    scoreboard_url: str
    """ESPN's public scoreboard endpoint for this sport."""
    game_day_timezone: ZoneInfo
    """The timezone whose calendar day a game belongs to, and kickoffs are shown in."""
    timezone_label: str
    """Suffix shown after a kickoff time in `game_day_timezone`, e.g. "ET"."""
    game_duration: dt.timedelta
    """Roughly how long a game runs, kickoff to final whistle - the span whose weather
    matters."""
    header_emoji: str
    header_title: str
    """The post header, worded as a recommendation: "Messy NFL games to watch"."""
    game_emoji: str
    venue_catalog: Callable[[], VenueCatalog]
    """Called each time a schedule is parsed, so a sport may build its catalog on demand
    rather than at import. (The NFL's is a module-level constant, built when `stadiums`
    is first imported - which importing `nfl` already does.)"""
    env_prefix: str = ""
    """Prefix on this sport's environment variables (credentials, healthcheck URL), so
    sports can post to different accounts. Empty keeps the original, unprefixed names."""
    state_key: str | None = None
    """Marks this sport's per-day post state file, so two sports posting on the same
    day don't read each other's progress. None keeps the original `<date>.json` name."""

    def game_day_for(self, instant: dt.datetime) -> dt.date:
        """The game day `instant` falls on. Raises ValueError for a timezone-naive
        datetime rather than silently assuming the machine's local timezone."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError(f"expected a timezone-aware datetime, got naive {instant!r}")
        return instant.astimezone(self.game_day_timezone).date()
