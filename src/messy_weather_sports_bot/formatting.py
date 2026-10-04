"""Render messiness-sorted games into post text, threaded across Bluesky's char limit."""

from __future__ import annotations

import datetime as dt

from messy_weather_sports_bot.alerts import AlertSeverity
from messy_weather_sports_bot.messiness import EMOJI, GameWeather
from messy_weather_sports_bot.sport import Sport

# Bluesky's real limit is 300 graphemes; stay conservative since multi-codepoint emoji
# can count as more than one grapheme and Python's len() undercounts that.
MAX_POST_LENGTH = 280

# Show the wind chill / heat index next to the temperature once it's this far from the
# air temperature - otherwise a 🥶 next to "35°F" wouldn't explain itself.
FEELS_LIKE_MIN_DIFFERENCE_F = 5


def format_kickoff(kickoff: dt.datetime, sport: Sport) -> str:
    """Kickoff time in the sport's game-day timezone, e.g. "1:00pm ET"."""
    local = kickoff.astimezone(sport.game_day_timezone)
    hour = local.hour % 12 or 12
    period = "am" if local.hour < 12 else "pm"
    return f"{hour}:{local.minute:02d}{period} {sport.timezone_label}"


def format_game_line(gw: GameWeather, sport: Sport) -> str:
    emoji = EMOJI[gw.condition]
    weather = gw.weather
    kickoff = format_kickoff(gw.game.kickoff, sport)
    parts = [f"{weather.short_forecast}"]
    if weather.temperature_f is not None:
        temperature = f"{weather.temperature_f}°F"
        feels_like = weather.feels_like_f
        if (
            feels_like is not None
            and abs(feels_like - weather.temperature_f) >= FEELS_LIKE_MIN_DIFFERENCE_F
        ):
            temperature += f" (feels {round(feels_like)}°F)"
        parts.append(temperature)
    if weather.wind_speed_mph > 0:
        parts.append(f"{weather.wind_speed_mph:g}mph wind")
    line = (
        f"{sport.game_emoji} {gw.game.away_team} @ {gw.game.home_team} ({kickoff}): "
        f"{emoji} {', '.join(parts)}"
    )
    if gw.alert is not None:
        line += f" ⚠️ {gw.alert.event}"
    return line


def format_header(date: dt.date, sport: Sport, *, has_warning: bool = False) -> str:
    header = f"{sport.header_emoji} {sport.header_title} — {date:%a %b} {date.day}"
    if has_warning:
        header += " ⚠️ Alerts in effect"
    return header


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def build_post_texts(
    games: list[GameWeather], date: dt.date, sport: Sport, max_length: int = MAX_POST_LENGTH
) -> list[str]:
    """Build one or more post texts (a thread) covering every game, each within max_length."""
    if not games:
        return []

    has_warning = any(
        gw.alert is not None and gw.alert.severity is AlertSeverity.WARNING for gw in games
    )
    header = format_header(date, sport, has_warning=has_warning)
    # Reserve room for the header so it always fits alongside at least one game line -
    # otherwise a single oversized line could force a header-only first post.
    line_budget = max_length - len(header) - 1
    if line_budget < 1:
        raise ValueError(
            f"max_length={max_length} is too small to fit the header ({len(header)} chars) "
            "plus at least one game line"
        )
    lines = [_truncate(format_game_line(gw, sport), line_budget) for gw in games]

    chunks: list[str] = []
    current: list[str] = [header]
    current_length = len(header)

    for line in lines:
        addition = len(line) + 1  # + newline joining it to the chunk
        if current_length + addition > max_length:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
        else:
            current.append(line)
            current_length += addition

    chunks.append("\n".join(current))

    return chunks
