"""Fetch a day's games from ESPN's public scoreboard endpoint."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace

import httpx

from messy_weather_sports_bot.retry import request_with_retry
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.sport import Sport
from messy_weather_sports_bot.venues import VenueCatalog, resolve_venue

logger = logging.getLogger(__name__)

NFL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
CFB_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
)

MAX_RANK = 25
"""The last place in the poll; ESPN gives an unranked team 99."""


class EspnScoreboard:
    """A sport's ESPN scoreboard. `client` is optional - pass one to reuse a connection
    (or, in tests, to control transport); otherwise each fetch uses its own."""

    def __init__(self, sport: Sport, client: httpx.Client | None = None) -> None:
        self._sport = sport
        self._client = client

    def fetch(self, game_day: dt.date) -> list[Game]:
        """The sport's games scheduled for `game_day` (a date in its game-day timezone)."""
        events = self._events(self._get_payload(game_day))
        limit = dict(self._sport.scoreboard_params).get("limit")
        if limit and limit.isdigit() and len(events) >= int(limit):
            logger.warning(
                "ESPN returned %d events, the most it was asked for: games may be missing from %s",
                len(events),
                game_day.isoformat(),
            )
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
                self._sport.scoreboard_url,
                params={
                    "dates": game_day.strftime("%Y%m%d"),
                    **dict(self._sport.scoreboard_params),
                },
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
        label = event.get("shortName") or event.get("name") or event.get("id")

        if competition.get("timeValid") is False:
            # ESPN's placeholder for a kickoff it hasn't set: the time is a guess, so is
            # any forecast window built from it.
            logger.info("Skipping ESPN event %r: its kickoff time is not set yet", label)
            return None

        kickoff = dt.datetime.fromisoformat(competition["date"].replace("Z", "+00:00"))
        try:
            event_day = self._sport.game_day_for(kickoff)
        except ValueError:
            # A kickoff with no UTC offset is rejected rather than assumed to be in this
            # machine's timezone, which could put the game on the wrong day. Skip just
            # this event (and say which), so one odd entry doesn't cost the whole slate.
            logger.warning(
                "Skipping ESPN event %r: its kickoff %r has no timezone", label, competition["date"]
            )
            return None
        if event_day != game_day:
            logger.info(
                "Skipping ESPN event %r: it falls on %s, not %s", label, event_day, game_day
            )
            return None

        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            return None

        home_team = self._team_label(home)
        away_team = self._team_label(away)
        if home_team is None or away_team is None:
            fields = ", ".join(self._sport.team_label_fields)
            logger.warning("Skipping ESPN event %r: a team has none of %s", label, fields)
            return None

        raw_venue = competition.get("venue")
        venue = raw_venue or {}
        stadium, unresolved_reason, is_venue_drift = resolve_venue(
            catalog, home_team, venue, venue_present=raw_venue is not None
        )

        game = Game(
            home_team=home_team,
            away_team=away_team,
            kickoff=kickoff,
            stadium=stadium,
            venue_name=venue.get("fullName", ""),
            venue_id=str(venue.get("id") or ""),
            unresolved_reason=unresolved_reason,
            is_venue_drift=is_venue_drift,
        )
        if self._sport.parse_neutral_site:
            game = replace(game, neutral_site=competition.get("neutralSite") is True)
        if self._sport.parse_rankings:
            home_rank, home_reported = self._rank(home)
            away_rank, away_reported = self._rank(away)
            game = replace(
                game,
                home_rank=home_rank,
                away_rank=away_rank,
                rankings_reported=home_reported or away_reported,
            )
        return game

    def _team_label(self, competitor: dict) -> str | None:
        """The first non-blank of the sport's `team_label_fields` on the competitor's team."""
        team = competitor.get("team")
        if not isinstance(team, dict):
            return None
        for name in self._sport.team_label_fields:
            value = team.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _rank(competitor: dict) -> tuple[int | None, bool]:
        """(the team's ranking if it's in the poll, whether ESPN sent a ranking at all).
        ESPN gives an unranked team 99, so only a real integer 1-25 counts as ranked; a
        bool is an int in Python but never a ranking."""
        curated = competitor.get("curatedRank")
        current = curated.get("current") if isinstance(curated, dict) else None
        if isinstance(current, bool) or not isinstance(current, int):
            return None, False
        return (current if 1 <= current <= MAX_RANK else None), True
