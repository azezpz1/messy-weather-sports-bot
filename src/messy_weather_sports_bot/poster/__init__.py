from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster
from messy_weather_sports_bot.poster.bluesky import BlueskyPoster
from messy_weather_sports_bot.poster.console import ConsolePoster

POSTERS: dict[str, type[SocialMediaPoster]] = {
    "bluesky": BlueskyPoster,
    "console": ConsolePoster,
}

__all__ = ["POSTERS", "BlueskyPoster", "ConsolePoster", "PostRef", "SocialMediaPoster"]
