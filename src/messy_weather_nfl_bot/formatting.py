"""Render messiness-sorted games into post text, threaded across Bluesky's char limit."""

from __future__ import annotations

import datetime as dt

from messy_weather_nfl_bot.alerts import AlertSeverity
from messy_weather_nfl_bot.messiness import EMOJI, GameWeather
from messy_weather_nfl_bot.schedule import GAME_DAY_TIMEZONE

# Bluesky's real limit is 300 graphemes; stay conservative since multi-codepoint emoji
# can count as more than one grapheme and Python's len() undercounts that.
MAX_POST_LENGTH = 280


def format_kickoff(kickoff: dt.datetime) -> str:
    """Kickoff time in the NFL's Eastern game-day timezone, e.g. "1:00pm ET"."""
    local = kickoff.astimezone(GAME_DAY_TIMEZONE)
    hour = local.hour % 12 or 12
    period = "am" if local.hour < 12 else "pm"
    return f"{hour}:{local.minute:02d}{period} ET"


def format_game_line(gw: GameWeather) -> str:
    emoji = EMOJI[gw.condition]
    weather = gw.weather
    kickoff = format_kickoff(gw.game.kickoff)
    parts = [f"{weather.short_forecast}"]
    if weather.temperature_f is not None:
        parts.append(f"{weather.temperature_f}°F")
    if weather.wind_speed_mph > 0:
        parts.append(f"{weather.wind_speed_mph:g}mph wind")
    line = (
        f"\U0001f3c8 {gw.game.away_team} @ {gw.game.home_team} ({kickoff}): "
        f"{emoji} {', '.join(parts)}"
    )
    if gw.alert is not None:
        line += f" ⚠️ {gw.alert.event}"
    return line


def format_header(date: dt.date, *, has_warning: bool = False) -> str:
    header = f"\U0001f329️ Messy NFL games to watch — {date:%a %b} {date.day}"
    if has_warning:
        header += " ⚠️ Alerts in effect"
    return header


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def build_post_texts(
    games: list[GameWeather], date: dt.date, max_length: int = MAX_POST_LENGTH
) -> list[str]:
    """Build one or more post texts (a thread) covering every game, each within max_length."""
    if not games:
        return []

    has_warning = any(
        gw.alert is not None and gw.alert.severity is AlertSeverity.WARNING for gw in games
    )
    header = format_header(date, has_warning=has_warning)
    # Reserve room for the header so it always fits alongside at least one game line -
    # otherwise a single oversized line could force a header-only first post.
    line_budget = max_length - len(header) - 1
    if line_budget < 1:
        raise ValueError(
            f"max_length={max_length} is too small to fit the header ({len(header)} chars) "
            "plus at least one game line"
        )
    lines = [_truncate(format_game_line(gw), line_budget) for gw in games]

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
