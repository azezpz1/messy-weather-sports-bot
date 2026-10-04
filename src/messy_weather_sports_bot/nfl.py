"""The NFL: its `Sport` configuration and the `messy-weather-nfl-bot` entry point."""

from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

from messy_weather_sports_bot import cli
from messy_weather_sports_bot.espn import NFL_SCOREBOARD_URL
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.stadiums import NFL_CATALOG
from messy_weather_sports_bot.weather import GAME_DURATION

NFL = Sport(
    slug="nfl",
    name="NFL",
    scoreboard_url=NFL_SCOREBOARD_URL,
    # NFL scheduling (and this bot's cron) revolves around US Eastern game days.
    game_day_timezone=ZoneInfo("America/New_York"),
    timezone_label="ET",
    game_duration=GAME_DURATION,
    header_emoji="\U0001f329️",
    header_title="Messy NFL games to watch",
    game_emoji="\U0001f3c8",
    venue_catalog=lambda: NFL_CATALOG,
)


def main(argv: list[str] | None = None) -> int:
    return cli.run_cli(NFL, argv)


if __name__ == "__main__":
    sys.exit(main())
