"""Static NFL stadium data: location and whether the field is shielded from weather.

Covered stadiums (fixed domes, fixed/skylight roofs, and retractable roofs) are all
treated as indoor, since roof-open/closed state on a retractable roof isn't reliably
knowable ahead of a game from free data sources.

Stadiums are looked up by ESPN's `competition.venue.id` first (stable across a venue
rename), then by name - either the current `name` or a historical `aliases` entry -
before falling back to the home team's usual venue. `venue_id` is left unset (`None`)
until we've confirmed the real ESPN id for that venue; until then, matching falls
through to name/alias matching for that stadium.
"""

from dataclasses import dataclass, field


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


# Keyed by ESPN team abbreviation.
STADIUMS: dict[str, StadiumInfo] = {
    "ARI": StadiumInfo("State Farm Stadium", 33.5276, -112.2626, is_covered=True),
    "ATL": StadiumInfo("Mercedes-Benz Stadium", 33.7554, -84.4008, is_covered=True),
    "BAL": StadiumInfo("M&T Bank Stadium", 39.2780, -76.6227, is_covered=False),
    # New Highmark Stadium (opened June 2026), across the street from the old site.
    "BUF": StadiumInfo("Highmark Stadium", 42.77306, -78.79222, is_covered=False),
    "CAR": StadiumInfo("Bank of America Stadium", 35.2258, -80.8528, is_covered=False),
    "CHI": StadiumInfo("Soldier Field", 41.8623, -87.6167, is_covered=False),
    "CIN": StadiumInfo(
        "Paycor Stadium", 39.0955, -84.5160, is_covered=False, aliases=("Paul Brown Stadium",)
    ),
    "CLE": StadiumInfo(
        "Huntington Bank Field",
        41.5061,
        -81.6995,
        is_covered=False,
        aliases=("FirstEnergy Stadium", "Cleveland Browns Stadium"),
    ),
    "DAL": StadiumInfo("AT&T Stadium", 32.7473, -97.0945, is_covered=True),
    "DEN": StadiumInfo(
        "Empower Field at Mile High",
        39.7439,
        -105.0201,
        is_covered=False,
        aliases=("Sports Authority Field at Mile High", "INVESCO Field at Mile High"),
    ),
    "DET": StadiumInfo("Ford Field", 42.3400, -83.0456, is_covered=True),
    "GB": StadiumInfo("Lambeau Field", 44.5013, -88.0622, is_covered=False),
    "HOU": StadiumInfo("NRG Stadium", 29.6847, -95.4107, is_covered=True),
    "IND": StadiumInfo("Lucas Oil Stadium", 39.7601, -86.1639, is_covered=True),
    "JAX": StadiumInfo(
        "EverBank Stadium",
        30.3239,
        -81.6373,
        is_covered=False,
        aliases=("TIAA Bank Field", "EverBank Field"),
    ),
    "KC": StadiumInfo(
        "GEHA Field at Arrowhead Stadium",
        39.0489,
        -94.4839,
        is_covered=False,
        aliases=("Arrowhead Stadium",),
    ),
    "LAC": StadiumInfo("SoFi Stadium", 33.9535, -118.3392, is_covered=True),
    "LAR": StadiumInfo("SoFi Stadium", 33.9535, -118.3392, is_covered=True),
    "LV": StadiumInfo("Allegiant Stadium", 36.0909, -115.1833, is_covered=True),
    "MIA": StadiumInfo(
        "Hard Rock Stadium",
        25.9580,
        -80.2389,
        is_covered=False,
        aliases=("Sun Life Stadium", "Joe Robbie Stadium"),
    ),
    "MIN": StadiumInfo("U.S. Bank Stadium", 44.9737, -93.2577, is_covered=True),
    "NE": StadiumInfo("Gillette Stadium", 42.0909, -71.2643, is_covered=False),
    "NO": StadiumInfo("Caesars Superdome", 29.9511, -90.0812, is_covered=True),
    "NYG": StadiumInfo("MetLife Stadium", 40.8135, -74.0745, is_covered=False),
    "NYJ": StadiumInfo("MetLife Stadium", 40.8135, -74.0745, is_covered=False),
    "PHI": StadiumInfo("Lincoln Financial Field", 39.9008, -75.1675, is_covered=False),
    "PIT": StadiumInfo(
        "Acrisure Stadium", 40.4468, -80.0158, is_covered=False, aliases=("Heinz Field",)
    ),
    "SEA": StadiumInfo("Lumen Field", 47.5952, -122.3316, is_covered=False),
    "SF": StadiumInfo("Levi's Stadium", 37.4033, -121.9694, is_covered=False),
    "TB": StadiumInfo("Raymond James Stadium", 27.9759, -82.5033, is_covered=False),
    "TEN": StadiumInfo(
        "Nissan Stadium", 36.1665, -86.7713, is_covered=False, aliases=("LP Field",)
    ),
    "WSH": StadiumInfo(
        "Northwest Stadium", 38.9076, -76.8645, is_covered=False, aliases=("FedExField",)
    ),
}


def stadium_for_team(team_abbreviation: str) -> StadiumInfo:
    """Look up stadium info for an ESPN team abbreviation.

    Raises KeyError if the abbreviation isn't recognized.
    """
    return STADIUMS[team_abbreviation]


def stadium_for_venue(venue_id: str, venue_name: str) -> StadiumInfo | None:
    """Look up stadium info by ESPN venue id or name/alias, across *all* known
    stadiums - not just a particular team's usual one. Used to resolve a game
    played at a known NFL venue other than the home team's own (a relocated game),
    and to resolve a home team's own venue even after ESPN renames it, as long as
    the id matches or the new name has been added as an alias.

    Returns None if neither the id nor the name/alias matches any known stadium.
    """
    if venue_id:
        for stadium in STADIUMS.values():
            if stadium.venue_id == venue_id:
                return stadium
    if venue_name:
        for stadium in STADIUMS.values():
            if venue_name == stadium.name or venue_name in stadium.aliases:
                return stadium
    return None
