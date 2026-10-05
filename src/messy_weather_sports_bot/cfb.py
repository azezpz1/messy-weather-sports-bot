"""College football: its `Sport` configuration and the `messy-weather-cfb-bot` entry point.

Covers only games with a team in the poll's top 25 - a full Saturday is 60-80 games, and
the ranked ones are the ones followers tune in for. It posts to its own accounts
(`CFB_`-prefixed credentials, with no fallback to the NFL's), keeps its own state file, and
reads its venues from `data/cfb_venues.json`, which is looked up by ESPN venue id alone.
"""

from __future__ import annotations

import datetime as dt
import sys
from functools import cache
from zoneinfo import ZoneInfo

from messy_weather_sports_bot import cli
from messy_weather_sports_bot.espn import CFB_SCOREBOARD_URL
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.venue_table import catalog_from_rows, load_packaged_table
from messy_weather_sports_bot.venues import VenueCatalog

CFB_GAME_DURATION = dt.timedelta(hours=3, minutes=30)
"""Kickoff to final whistle. Its own constant, so it can be tuned apart from the NFL's."""


@cache
def cfb_catalog() -> VenueCatalog:
    """The packaged college venue table, read the first time it's needed - so a damaged
    file can only ever affect this bot, never the NFL's - and kept for the run."""
    return catalog_from_rows(load_packaged_table("cfb_venues.json"))


def ranked_only(game: Game) -> str | None:
    """Keep a game if either team is in the top 25; otherwise say why it's left out."""
    if game.home_rank is None and game.away_rank is None:
        return "no ranked team"
    return None


CFB = Sport(
    slug="cfb",
    name="Top 25 college football",
    scoreboard_url=CFB_SCOREBOARD_URL,
    # `groups=80` is FBS, and includes FBS teams' games against FCS opponents; a Saturday
    # has up to ~80 games, so ask for more than ESPN's default page.
    scoreboard_params=(("groups", "80"), ("limit", "400")),
    # ESPN files every game under its Eastern date, even the late kickoffs.
    game_day_timezone=ZoneInfo("America/New_York"),
    timezone_label="ET",
    game_duration=CFB_GAME_DURATION,
    header_emoji="\U0001f329️",
    header_title="Messy Top 25 college football games to watch",
    game_emoji="\U0001f3c8",
    venue_catalog=cfb_catalog,
    # Never fall back to the NFL's credentials or state: that could post to the wrong account.
    env_prefix="CFB_",
    state_key="cfb",
    # "OSU" is Ohio State or Oklahoma State; "Ohio State" isn't.
    team_label_fields=("shortDisplayName", "displayName", "location", "abbreviation"),
    parse_neutral_site=True,
    parse_rankings=True,
    venue_update_hint=(
        "Rerun scripts/generate_cfb_venues.py with --range START:END covering the game's date "
        "(its default range stops at today, so it can't see a venue that hasn't hosted a "
        "game yet), or add an override in scripts/data/cfb_venue_overrides.json - see the "
        "README's college football venue data section."
    ),
    game_filter=ranked_only,
)


def main(argv: list[str] | None = None) -> int:
    return cli.run_cli(CFB, argv)


if __name__ == "__main__":
    sys.exit(main())
