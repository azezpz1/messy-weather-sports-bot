"""Fetch today's NFL games from ESPN's public scoreboard endpoint."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx

from messy_weather_nfl_bot.retry import request_with_retry
from messy_weather_nfl_bot.stadiums import StadiumInfo, stadium_for_team, stadium_for_venue

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# NFL scheduling (and this bot's cron) revolves around US Eastern game days.
GAME_DAY_TIMEZONE = ZoneInfo("America/New_York")

# Country names ESPN uses for US venues.
US_COUNTRY_NAMES = frozenset({"USA", "United States", "United States of America"})


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
    """True when `unresolved_reason` signals `stadiums.py` is out of date (an unrecognized
    US venue) rather than an expected, ignorable skip - see `venue_drift()`."""


def todays_local_date(now: dt.datetime | None = None) -> dt.date:
    """Today's calendar date in the NFL's Eastern game-day timezone."""
    current = now or dt.datetime.now(tz=GAME_DAY_TIMEZONE)
    return current.astimezone(GAME_DAY_TIMEZONE).date()


def _is_confirmed_international(venue_address: dict) -> bool:
    """True only when ESPN reports a non-US country. A *missing* country is
    deliberately not treated as international - it's ambiguous, and treating it as
    international would silently exclude an unrecognized US venue from
    `venue_drift()`, the exact silent-drop failure mode this module exists to avoid.
    """
    country = venue_address.get("country")
    return bool(country) and country not in US_COUNTRY_NAMES


def _resolve_stadium(
    home_team: str,
    venue: dict,
    venue_present: bool,
) -> tuple[StadiumInfo | None, str | None, bool]:
    """Resolve a game's venue against *all* known stadiums (not just the home team's),
    so a relocated game or a renamed venue still resolves. Returns
    `(stadium, reason, is_drift)`, where `reason` explains why `stadium` is None and
    `is_drift` flags an unrecognized US venue - see `Game.is_venue_drift`.

    `venue_present` distinguishes ESPN omitting the `venue` key (or sending it as
    `null`) from ESPN sending an explicit but empty `{}` - `venue` alone can't tell
    those apart, since both end up as `{}` by the time it gets here.
    """
    venue_id = str(venue.get("id") or "")
    venue_name = venue.get("fullName", "")
    stadium = stadium_for_venue(venue_id, venue_name)
    if stadium is None:
        if not venue_present:
            # ESPN gave us no venue data at all - fall back to the home team's usual
            # stadium rather than dropping the game outright. A *present* venue with
            # no id/name (but e.g. an address, or literally `{}`) is not this case -
            # it's an unrecognized venue, handled below, not a reason to guess the
            # home team's stadium instead of the actual (possibly different) one.
            try:
                stadium = stadium_for_team(home_team)
            except KeyError:
                return None, f"unrecognized home team {home_team!r}", False
        elif _is_confirmed_international(venue.get("address") or {}):
            return None, f'international venue "{venue_name}"', False
        else:
            # A US (or unconfirmed-country) venue we don't have on file - likely a
            # stadium rename or relocation `stadiums.py` hasn't caught up with yet.
            reason = f'unrecognized venue "{venue_name}" (espn venue id {venue_id!r})'
            return None, reason, True
    if venue.get("indoor") and not stadium.is_covered:
        # ESPN says this particular game is indoors even though the matched stadium is
        # open-air (e.g. relocated to a covered neutral site) - treat as covered.
        covered = StadiumInfo(
            venue_name or stadium.name, stadium.latitude, stadium.longitude, is_covered=True
        )
        return covered, None, False
    return stadium, None, False


def get_todays_games(date: dt.date | None = None, client: httpx.Client | None = None) -> list[Game]:
    """Fetch NFL games scheduled for `date` (default: today, Eastern time)."""
    target_date = date or todays_local_date()
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)

    def _get() -> httpx.Response:
        response = http_client.get(SCOREBOARD_URL, params={"dates": target_date.strftime("%Y%m%d")})
        response.raise_for_status()
        return response

    try:
        response = request_with_retry(_get)
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(payload, dict):
        kind = type(payload).__name__
        raise ValueError(f"Unexpected ESPN scoreboard response shape: expected object, got {kind}")
    events = payload.get("events", [])
    if not isinstance(events, list):
        kind = type(events).__name__
        raise ValueError(f"Unexpected ESPN scoreboard 'events' shape: expected a list, got {kind}")

    games: list[Game] = []
    for event in events:
        if not isinstance(event, dict):
            kind = type(event).__name__
            raise ValueError(f"Unexpected ESPN scoreboard event shape: expected object, got {kind}")
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]

        kickoff = dt.datetime.fromisoformat(competition["date"].replace("Z", "+00:00"))
        if kickoff.astimezone(GAME_DAY_TIMEZONE).date() != target_date:
            continue

        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue

        home_team = home["team"]["abbreviation"]
        away_team = away["team"]["abbreviation"]

        raw_venue = competition.get("venue")
        venue = raw_venue or {}
        stadium, unresolved_reason, is_venue_drift = _resolve_stadium(
            home_team, venue, venue_present=raw_venue is not None
        )

        games.append(
            Game(
                home_team=home_team,
                away_team=away_team,
                kickoff=kickoff,
                stadium=stadium,
                venue_name=venue.get("fullName", ""),
                venue_id=str(venue.get("id") or ""),
                unresolved_reason=unresolved_reason,
                is_venue_drift=is_venue_drift,
            )
        )

    return games


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
    """Reasons for games whose US venue isn't recognized - i.e. `stadiums.py` has
    drifted out of date (a rename, a relocation to a new venue) rather than an
    expected, ignorable skip (an international game, or an unrecognized home team).
    Used by the scheduled drift-check job so this is caught mid-week instead of
    silently dropping a game.
    """
    return [g.unresolved_reason for g in games if g.is_venue_drift and g.unresolved_reason]
