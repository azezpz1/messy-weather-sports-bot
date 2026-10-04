"""Fetch a day's games from ESPN's public scoreboard endpoint."""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from messy_weather_sports_bot.retry import request_with_retry
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.venues import VenueCatalog, resolve_venue

logger = logging.getLogger(__name__)

NFL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


class EspnScoreboard:
    """A sport's ESPN scoreboard. `client` is optional - pass one to reuse a connection
    (or, in tests, to control transport); otherwise each fetch uses its own."""

    def __init__(self, sport: Sport, client: httpx.Client | None = None) -> None:
        self._sport = sport
        self._client = client

    def fetch(self, game_day: dt.date) -> list[Game]:
        """The sport's games scheduled for `game_day` (a date in its game-day timezone)."""
        events = self._events(self._get_payload(game_day))
        catalog = self._sport.venue_catalog()

        games: list[Game] = []
        for event in events:
            if not isinstance(event, dict):
                kind = type(event).__name__
                raise ValueError(
                    f"Unexpected ESPN scoreboard event shape: expected object, got {kind}"
                )
            game = self._parse_event(event, game_day, catalog)
            if game is not None:
                games.append(game)
        return games

    def _get_payload(self, game_day: dt.date) -> object:
        owns_client = self._client is None
        http_client = self._client or httpx.Client(timeout=10.0)

        def _get() -> httpx.Response:
            response = http_client.get(
                self._sport.scoreboard_url, params={"dates": game_day.strftime("%Y%m%d")}
            )
            response.raise_for_status()
            return response

        try:
            return request_with_retry(_get).json()
        finally:
            if owns_client:
                http_client.close()

    @staticmethod
    def _events(payload: object) -> list:
        if not isinstance(payload, dict):
            kind = type(payload).__name__
            raise ValueError(
                f"Unexpected ESPN scoreboard response shape: expected object, got {kind}"
            )
        events = payload.get("events", [])
        if not isinstance(events, list):
            kind = type(events).__name__
            raise ValueError(
                f"Unexpected ESPN scoreboard 'events' shape: expected a list, got {kind}"
            )
        return events

    def _parse_event(self, event: dict, game_day: dt.date, catalog: VenueCatalog) -> Game | None:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        competition = competitions[0]

        kickoff = dt.datetime.fromisoformat(competition["date"].replace("Z", "+00:00"))
        try:
            event_day = self._sport.game_day_for(kickoff)
        except ValueError:
            # A kickoff with no UTC offset is rejected rather than assumed to be in this
            # machine's timezone, which could put the game on the wrong day. Skip just
            # this event (and say which), so one odd entry doesn't cost the whole slate.
            label = event.get("shortName") or event.get("name") or event.get("id")
            logger.warning(
                "Skipping ESPN event %r: its kickoff %r has no timezone", label, competition["date"]
            )
            return None
        if event_day != game_day:
            return None

        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            return None

        home_team = home["team"]["abbreviation"]
        away_team = away["team"]["abbreviation"]

        raw_venue = competition.get("venue")
        venue = raw_venue or {}
        stadium, unresolved_reason, is_venue_drift = resolve_venue(
            catalog, home_team, venue, venue_present=raw_venue is not None
        )

        return Game(
            home_team=home_team,
            away_team=away_team,
            kickoff=kickoff,
            stadium=stadium,
            venue_name=venue.get("fullName", ""),
            venue_id=str(venue.get("id") or ""),
            unresolved_reason=unresolved_reason,
            is_venue_drift=is_venue_drift,
        )
