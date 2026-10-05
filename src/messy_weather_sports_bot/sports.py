"""Every sport the bot covers, by slug - for code that works across all of them (the venue
drift check, the tests that keep their credentials and state apart)."""

from __future__ import annotations

from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.sport import Sport

SPORTS: dict[str, Sport] = {sport.slug: sport for sport in (NFL, CFB)}
