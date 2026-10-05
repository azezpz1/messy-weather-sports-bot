"""A sport this bot reports on: everything that differs between, say, the NFL and
college football, gathered in one place so each is a configuration of the same pipeline.

Shared code (the schedule fetch, weather lookup, scoring, post formatting, posting and
state) takes a `Sport` rather than reaching for NFL constants.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from messy_weather_sports_bot.venues import VenueCatalog

if TYPE_CHECKING:
    from messy_weather_sports_bot.schedule import Game


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
    scoreboard_params: tuple[tuple[str, str], ...] = ()
    """Query parameters sent with every scoreboard request, besides `dates` - as name/value
    pairs so the sport stays hashable. The NFL sends none."""
    team_label_fields: tuple[str, ...] = ("abbreviation",)
    """ESPN `team` fields to label a team with, first non-empty one wins. A sport whose
    abbreviations read ambiguously ("OSU") lists a friendlier field ahead of it."""
    parse_neutral_site: bool = False
    """Whether a game's `neutralSite` flag is read, so the post can say "vs" rather than
    "@" - the home team isn't hosting."""
    parse_rankings: bool = False
    """Whether each team's `curatedRank` is read into the game, for post labels and
    `game_filter`."""
    venue_update_hint: str = ""
    """What to do when the drift check finds a venue the sport's table doesn't know."""
    game_filter: Callable[[Game], str | None] | None = None
    """Decides which games the sport covers: returns why a game is left out, or None to
    keep it. Applied before any weather lookup, so a game left out costs no requests."""

    @property
    def event_limit(self) -> int | None:
        """The most events a scoreboard request asks for (its `limit` parameter), if it sets
        one - a response that size may have been cut short."""
        value = dict(self.scoreboard_params).get("limit", "")
        return int(value) if value.isascii() and value.isdigit() else None

    def game_day_for(self, instant: dt.datetime) -> dt.date:
        """The game day `instant` falls on. Raises ValueError for a timezone-naive
        datetime rather than silently assuming the machine's local timezone."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError(f"expected a timezone-aware datetime, got naive {instant!r}")
        return instant.astimezone(self.game_day_timezone).date()
