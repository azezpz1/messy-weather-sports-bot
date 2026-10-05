"""Builders for ESPN scoreboard payloads, shared by the unit tests."""

import json
from pathlib import Path
from typing import Any

from messy_weather_sports_bot.stadiums import stadium_for_team

DEFAULT_KICKOFF = "2026-01-18T18:00Z"


def espn_event(
    home: str,
    away: str,
    venue_name: str | None = None,
    *,
    kickoff: str = DEFAULT_KICKOFF,
    venue_id: str = "",
    indoor: bool = False,
    country: str = "USA",
) -> dict:
    """One scoreboard event. `venue_name` defaults to the home team's stadium's name;
    `venue_id` and `country` are left out of the venue when empty, as ESPN sometimes
    omits them."""
    name = stadium_for_team(home).name if venue_name is None else venue_name
    venue: dict = {"fullName": name, "indoor": indoor}
    if venue_id:
        venue["id"] = venue_id
    if country:
        venue["address"] = {"country": country}
    return {
        "date": kickoff,
        "competitions": [
            {
                "date": kickoff,
                "venue": venue,
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": home}},
                    {"homeAway": "away", "team": {"abbreviation": away}},
                ],
            }
        ],
    }


# ------------------------------------------------------------------ college football

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "espn_cfb_scoreboard_sample.json"

MISSING: Any = object()
"""Pass as a `curatedRank` / field value to leave it out of the payload altogether."""


def cfb_sample(date: str) -> dict:
    """A real ESPN college football scoreboard for `date` (YYYYMMDD), trimmed to a handful of
    games - see `tests/fixtures/espn_cfb_scoreboard_sample.json` for which and why."""
    return json.loads(FIXTURE_PATH.read_text("utf-8"))["scoreboards"][date]


def _cfb_competitor(side: str, name: str, rank: object, curated: object) -> dict:
    team = {
        "id": str(sum(map(ord, name))),
        "abbreviation": "".join(word[0] for word in name.split()).upper() or name[:3].upper(),
        "displayName": f"{name} Mascots",
        "shortDisplayName": name,
        "location": name,
    }
    competitor: dict = {"homeAway": side, "team": team}
    if curated is not MISSING:
        competitor["curatedRank"] = curated if curated is not None else {"current": rank}
    return competitor


def cfb_event(
    home: str,
    away: str,
    *,
    venue_id: str = "3861",
    venue_name: str = "Ohio Stadium",
    home_rank: object = 99,
    away_rank: object = 99,
    home_curated: object = None,
    away_curated: object = None,
    kickoff: str = DEFAULT_KICKOFF,
    neutral: bool = False,
    time_valid: bool = True,
    indoor: bool = False,
    country: str = "USA",
) -> dict:
    """One college football scoreboard event, shaped like ESPN's. A team's ranking is
    `curatedRank.current` (99 when unranked); `home_curated`/`away_curated` replace the whole
    `curatedRank` value, and `MISSING` leaves it out."""
    venue: dict = {
        "id": venue_id,
        "fullName": venue_name,
        "indoor": indoor,
        "address": {"city": "Columbus", "state": "OH", "country": country},
    }
    return {
        "id": f"{home}-{away}-{kickoff}",
        "date": kickoff,
        "name": f"{away} at {home}",
        "shortName": f"{away} @ {home}",
        "competitions": [
            {
                "date": kickoff,
                "neutralSite": neutral,
                "timeValid": time_valid,
                "venue": venue,
                "competitors": [
                    _cfb_competitor("home", home, home_rank, home_curated),
                    _cfb_competitor("away", away, away_rank, away_curated),
                ],
            }
        ],
    }
