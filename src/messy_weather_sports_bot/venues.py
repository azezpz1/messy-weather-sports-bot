"""Venues a game can be played at, and how an ESPN `competition.venue` resolves to one.

A `VenueCatalog` holds a sport's known stadiums - location, and whether the field is
shielded from weather - and looks them up by ESPN's `venue.id` first (stable across a
venue rename), then by name (the current `name` or a historical `aliases` entry).
Covered stadiums (fixed domes, fixed/skylight roofs, and retractable roofs) are all
treated as indoor, since roof-open/closed state on a retractable roof isn't reliably
knowable ahead of a game from free data sources.

`venue_id` is left unset (`None`) on a stadium until we've confirmed the real ESPN id
for that venue (pulled from a live scoreboard response); until then, matching falls
through to name/alias matching for that stadium.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# Country names ESPN uses for US venues.
US_COUNTRY_NAMES = frozenset({"USA", "United States", "United States of America"})


@dataclass(frozen=True)
class StadiumInfo:
    name: str
    latitude: float
    longitude: float
    is_covered: bool
    venue_id: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    """Past names ESPN may still report for this venue (e.g. a sponsor rename),
    so a schedule fetched before `name` here is updated still resolves correctly."""


class VenueCatalog:
    """A sport's known stadiums, searchable by ESPN venue id or name/alias across *all*
    of them - not just a particular team's usual one - so a game played at a known
    venue other than the home team's own (a relocated game) still resolves, and so does
    a home team's own venue after ESPN renames it, as long as the id matches or the new
    name has been added as an alias.

    Where two stadiums share an id or a name (two teams listing the same building),
    the one that comes first in `stadiums` wins. `by_team` optionally maps a team
    abbreviation to its usual stadium, for the fallback when ESPN omits a game's venue
    entirely; it is built into the catalog, so changing the mapping later has no effect.

    `match_names=False` makes the catalog id-only, for a table too big to trust a name in:
    many towns have a "Memorial Stadium", so a name-only match could report another
    town's weather. An unknown id is then an unrecognized venue, which is reported
    as drift instead.
    """

    def __init__(
        self,
        stadiums: Iterable[StadiumInfo],
        *,
        by_team: Mapping[str, StadiumInfo] | None = None,
        match_names: bool = True,
    ) -> None:
        self._by_id: dict[str, StadiumInfo] = {}
        self._by_name: dict[str, StadiumInfo] = {}
        for stadium in stadiums:
            if stadium.venue_id:
                self._by_id.setdefault(stadium.venue_id, stadium)
            if match_names:
                self._by_name.setdefault(stadium.name, stadium)
                for alias in stadium.aliases:
                    self._by_name.setdefault(alias, stadium)
        self._by_team = dict(by_team or {})

    @property
    def has_team_stadiums(self) -> bool:
        """Whether teams have a usual stadium to fall back on (the NFL's do; a college
        catalog has none, since teams play at neutral sites too often to guess)."""
        return bool(self._by_team)

    def for_team(self, team_abbreviation: str) -> StadiumInfo:
        """The team's usual stadium. Raises KeyError if it isn't recognized."""
        return self._by_team[team_abbreviation]

    def for_venue(self, venue_id: str, venue_name: str) -> StadiumInfo | None:
        """Look up a stadium by ESPN venue id, then by name/alias. Returns None if
        neither matches any known stadium (a blank id or name never matches)."""
        if venue_id and venue_id in self._by_id:
            return self._by_id[venue_id]
        if venue_name:
            return self._by_name.get(venue_name)
        return None


def is_confirmed_international(venue_address: dict) -> bool:
    """True only when ESPN reports a non-US country. A *missing* country is
    deliberately not treated as international - it's ambiguous, and treating it as
    international would silently exclude an unrecognized US venue from
    `venue_drift()`, the exact silent-drop failure mode the venue table exists to avoid.
    """
    country = venue_address.get("country")
    return bool(country) and country not in US_COUNTRY_NAMES


def resolve_venue(
    catalog: VenueCatalog,
    home_team: str,
    venue: dict,
    venue_present: bool,
) -> tuple[StadiumInfo | None, str | None, bool]:
    """Resolve a game's venue against *all* of `catalog`'s stadiums (not just the home
    team's), so a relocated game or a renamed venue still resolves. Returns
    `(stadium, reason, is_drift)`, where `reason` explains why `stadium` is None and
    `is_drift` flags an unrecognized US venue - see `Game.is_venue_drift`.

    `venue_present` distinguishes ESPN omitting the `venue` key (or sending it as
    `null`) from ESPN sending an explicit but empty `{}` - `venue` alone can't tell
    those apart, since both end up as `{}` by the time it gets here.
    """
    venue_id = str(venue.get("id") or "")
    venue_name = venue.get("fullName", "")
    stadium = catalog.for_venue(venue_id, venue_name)
    if stadium is None:
        if not venue_present:
            # ESPN gave us no venue data at all - fall back to the home team's usual
            # stadium rather than dropping the game outright. A *present* venue with
            # no id/name (but e.g. an address, or literally `{}`) is not this case -
            # it's an unrecognized venue, handled below, not a reason to guess the
            # home team's stadium instead of the actual (possibly different) one.
            try:
                stadium = catalog.for_team(home_team)
            except KeyError:
                if not catalog.has_team_stadiums:
                    return None, f"no venue reported for {home_team!r}", False
                return None, f"unrecognized home team {home_team!r}", False
        elif is_confirmed_international(venue.get("address") or {}):
            return None, f'international venue "{venue_name}"', False
        else:
            # A US (or unconfirmed-country) venue we don't have on file - likely a
            # stadium rename or relocation the venue table hasn't caught up with yet.
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
