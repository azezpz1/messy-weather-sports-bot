"""Builds a college football venue table (`data/cfb_venues.json`) from ESPN's scoreboards.

ESPN reports each game's venue (id, name, city, state, whether it's indoors) but not where
it is, so the generator harvests the venues FBS teams actually played at over whole
seasons - every home field, plus the bowl, neutral-site and rivalry venues - and finds
coordinates for each:

1. A venue that's also an NFL stadium copies the NFL table's coordinates and roof status.
2. Otherwise it is geocoded with Photon (OpenStreetMap data), accepting only a result in
   the venue's state whose name matches and whose OSM class is a stadium or sports ground.
3. `scripts/data/cfb_venue_overrides.json` has the last word, so a hand-corrected
   coordinate survives regeneration.

A venue that can't be resolved is reported, never guessed. The output is deterministic
so a regeneration's diff is exactly what changed. Network and file access sit at the
edges (`harvest`, `PhotonGeocoder`, the CLI script); everything else is plain functions
over data so it can be tested.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Protocol

import httpx

from messy_weather_sports_bot.espn import CFB_SCOREBOARD_URL
from messy_weather_sports_bot.retry import request_with_retry
from messy_weather_sports_bot.venue_table import COORDINATE_DECIMALS, VenueRow, id_order
from messy_weather_sports_bot.venues import VenueCatalog, is_confirmed_international

PHOTON_URL = "https://photon.komoot.io/api/"

SEASON_START_MONTH_DAY = (8, 20)
SEASON_END_MONTH_DAY = (1, 25)
"""A season's scoreboards run from just before the first game to just after the title
game, so every home field and every bowl or playoff venue is seen."""

MIN_GAMES_FOR_COVERAGE = 6
"""A team seen in at least this many games is an FBS team playing a real schedule, not
an FCS opponent that turned up for a single guarantee game."""

US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "PR": "Puerto Rico",
}

# (south, north, west, east), a little generous: the check is "this point is in that
# state, not another one", which is what a wrong-city geocode would break.
STATE_BOXES: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.1, 35.1, -88.6, -84.8),
    "AK": (51.0, 71.5, -180.0, -129.9),
    "AZ": (31.2, 37.1, -114.9, -109.0),
    "AR": (33.0, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.1),
    "CO": (36.9, 41.1, -109.1, -102.0),
    "CT": (40.9, 42.1, -73.8, -71.7),
    "DE": (38.4, 39.9, -75.8, -75.0),
    "DC": (38.78, 39.0, -77.15, -76.9),
    "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.3, 35.1, -85.7, -80.7),
    "HI": (18.8, 22.3, -160.3, -154.7),
    "ID": (41.9, 49.1, -117.3, -111.0),
    "IL": (36.9, 42.6, -91.6, -87.0),
    "IN": (37.7, 41.8, -88.2, -84.7),
    "IA": (40.3, 43.6, -96.7, -90.1),
    "KS": (36.9, 40.1, -102.1, -94.5),
    "KY": (36.4, 39.2, -89.6, -81.9),
    "LA": (28.9, 33.1, -94.1, -88.7),
    "ME": (42.9, 47.5, -71.2, -66.9),
    "MD": (37.8, 39.8, -79.5, -75.0),
    "MA": (41.2, 42.9, -73.6, -69.9),
    "MI": (41.6, 48.4, -90.5, -82.1),
    "MN": (43.4, 49.5, -97.3, -89.4),
    "MS": (30.1, 35.1, -91.7, -88.0),
    "MO": (35.9, 40.7, -95.8, -89.0),
    "MT": (44.3, 49.1, -116.1, -104.0),
    "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (35.0, 42.1, -120.1, -114.0),
    "NH": (42.6, 45.4, -72.6, -70.6),
    "NJ": (38.9, 41.4, -75.6, -73.8),
    "NM": (31.3, 37.1, -109.1, -103.0),
    "NY": (40.4, 45.1, -79.8, -71.8),
    "NC": (33.8, 36.7, -84.4, -75.4),
    "ND": (45.9, 49.1, -104.1, -96.5),
    "OH": (38.3, 42.0, -84.9, -80.5),
    "OK": (33.6, 37.1, -103.1, -94.4),
    "OR": (41.9, 46.3, -124.7, -116.4),
    "PA": (39.7, 42.3, -80.6, -74.6),
    "RI": (41.1, 42.1, -71.9, -71.1),
    "SC": (32.0, 35.3, -83.4, -78.5),
    "SD": (42.4, 45.95, -104.1, -96.4),
    "TN": (34.9, 36.7, -90.4, -81.6),
    "TX": (25.8, 36.6, -106.7, -93.5),
    "UT": (36.9, 42.1, -114.1, -109.0),
    "VT": (42.7, 45.1, -73.5, -71.4),
    "VA": (36.5, 39.5, -83.7, -75.2),
    "WA": (45.5, 49.1, -124.9, -116.9),
    "WV": (37.1, 40.7, -82.7, -77.7),
    "WI": (42.4, 47.4, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.1, -104.0),
    "PR": (17.8, 18.6, -67.4, -65.2),
}


def row_problems(row: VenueRow) -> list[str]:
    """Why `row` shouldn't go in the table (empty if it's fine): a blank name, city or
    state - which the table's own parser would refuse to read back - an unknown state, or
    coordinates outside the row's state, such as an override with a dropped minus sign."""
    problems = [
        f"{label} is blank"
        for label, value in (("name", row.name), ("city", row.city), ("state", row.state))
        if not value.strip()
    ]
    box = STATE_BOXES.get(row.state)
    if row.state.strip() and box is None:
        problems.append(f"unknown state {row.state!r}")
    elif box is not None:
        south, north, west, east = box
        if not (south <= row.latitude <= north and west <= row.longitude <= east):
            where = f"{row.latitude:.5f}, {row.longitude:.5f}"
            problems.append(f"coordinates {where} are outside {row.state}")
    return problems


# --------------------------------------------------------------------------- harvest


def _most_common(counter: Counter[str]) -> str:
    """The value seen most often (ties go to the alphabetically first), or "" if none -
    so the answer never depends on the order games arrived in."""
    return min(counter, key=lambda value: (-counter[value], value)) if counter else ""


@dataclass
class VenueObservation:
    """What ESPN's scoreboards reported about one venue id, over every game seen. A name,
    city, state or country is the one reported most often; a blank is never counted, so
    a game that omits a field can't blank it out."""

    venue_id: str
    names: Counter[str] = field(default_factory=Counter)
    cities: Counter[str] = field(default_factory=Counter)
    states: Counter[str] = field(default_factory=Counter)
    countries: Counter[str] = field(default_factory=Counter)
    indoor: bool = False
    games: int = 0

    @property
    def name(self) -> str:
        return _most_common(self.names)

    @property
    def other_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name in self.names if name != self.name))

    @property
    def city(self) -> str:
        return _most_common(self.cities)

    @property
    def state(self) -> str:
        return _most_common(self.states)

    @property
    def country(self) -> str:
        return _most_common(self.countries)

    @property
    def address(self) -> dict:
        return {"city": self.city, "state": self.state, "country": self.country}

    def describe(self) -> str:
        return (
            f"{self.name or '(unnamed)'}, {self.city or '?'}, {self.state or '?'} "
            f"({self.games} game(s))"
        )


@dataclass
class TeamObservation:
    team_id: str
    name: str
    games: int = 0
    home_games: int = 0
    """Games it hosted at a non-neutral site - where its real home field shows up. (ESPN's
    own `team.venue.id` is left stale for teams that have moved, so isn't used.)"""
    home_venue_ids: Counter[str] = field(default_factory=Counter)


@dataclass
class Harvest:
    venues: dict[str, VenueObservation] = field(default_factory=dict)
    teams: dict[str, TeamObservation] = field(default_factory=dict)
    days: int = 0
    events: int = 0
    events_without_venue_id: int = 0


def observe_scoreboard(payload: object, harvest: Harvest) -> None:
    """Fold one scoreboard response into `harvest`. Tolerant: an event it can't make
    sense of is skipped (and counted, if it has no venue id) rather than failing the run."""
    if not isinstance(payload, dict):
        return
    for event in payload.get("events") or []:
        competitions = event.get("competitions") if isinstance(event, dict) else None
        if not competitions or not isinstance(competitions[0], dict):
            continue
        competition = competitions[0]
        harvest.events += 1
        neutral = bool(competition.get("neutralSite"))
        venue = competition.get("venue") or {}
        venue_id = str(venue.get("id") or "")
        if not venue_id:
            harvest.events_without_venue_id += 1
        else:
            observation = harvest.venues.setdefault(venue_id, VenueObservation(venue_id))
            address = venue.get("address") or {}
            for counter, value in (
                (observation.names, venue.get("fullName")),
                (observation.cities, address.get("city")),
                (observation.states, address.get("state")),
                (observation.countries, address.get("country")),
            ):
                text = str(value or "").strip()
                if text:
                    counter[text] += 1
            observation.indoor = observation.indoor or bool(venue.get("indoor"))
            observation.games += 1
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            team_id = str(team.get("id") or "")
            if not team_id:
                continue
            record = harvest.teams.setdefault(
                team_id, TeamObservation(team_id, str(team.get("displayName") or team_id))
            )
            record.games += 1
            if competitor.get("homeAway") == "home" and not neutral and venue_id:
                record.home_games += 1
                record.home_venue_ids[venue_id] += 1


def season_ranges(today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """The date ranges to harvest: the last full season, and the current one so far."""
    start_month, start_day = SEASON_START_MONTH_DAY
    end_month, end_day = SEASON_END_MONTH_DAY
    ranges = [
        (
            dt.date(today.year - 1, start_month, start_day),
            dt.date(today.year, end_month, end_day),
        )
    ]
    this_season_start = dt.date(today.year, start_month, start_day)
    if today >= this_season_start:
        ranges.append((this_season_start, today))
    return [(start, min(end, today)) for start, end in ranges if start <= today]


def iter_days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def harvest(
    client: httpx.Client,
    ranges: Iterable[tuple[dt.date, dt.date]],
    *,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = 0.2,
) -> Harvest:
    """Fetch FBS scoreboards for every day in `ranges`, one date per request (the only
    query shape the scoreboard is known to support reliably). `client` should use the
    default User-Agent: ESPN's edge refuses unfamiliar ones."""
    result = Harvest()
    for start, end in ranges:
        for day in iter_days(start, end):

            def _get(day: dt.date = day) -> httpx.Response:
                response = client.get(
                    CFB_SCOREBOARD_URL,
                    params={"dates": day.strftime("%Y%m%d"), "groups": "80", "limit": "400"},
                )
                response.raise_for_status()
                return response

            observe_scoreboard(request_with_retry(_get).json(), result)
            result.days += 1
            sleep(pause)
    return result


# ----------------------------------------------------------------------- geocoding


STADIUM_CLASSES = frozenset({("leisure", "stadium"), ("building", "stadium")})
SPORTS_GROUND_CLASSES = frozenset(
    {("leisure", "pitch"), ("leisure", "sports_centre"), ("leisure", "track")}
)

_GENERIC_TOKENS = frozenset({"the", "at", "of", "and"})


def _tokens(text: str) -> tuple[str, ...]:
    """The comparable words of a venue or place name: lower-case, accents folded, any
    punctuation (an en dash included) a separator, and initials normalized - a run of
    them is one word ("H. A. Chapman" matches OSM's "HA Chapman") and a lone middle
    initial is dropped ("Warren P. McGuirk" matches "Warren McGuirk")."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", plain.casefold().replace("&", " and "))
        if word not in _GENERIC_TOKENS
    ]
    tokens: list[str] = []
    initials: list[str] = []
    for word in [*words, ""]:  # the empty word flushes a trailing run
        if len(word) == 1 and word.isalpha():
            initials.append(word)
            continue
        if len(initials) > 1:
            tokens.append("".join(initials))
        initials = []
        if word:
            tokens.append(word)
    return tuple(tokens)


def _clean_name(name: str) -> str:
    """ESPN's venue name without a trailing disambiguation like "(Norman, OK)", which
    would only confuse a search and a name comparison."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or name


def _query_names(name: str) -> list[str]:
    """The names to search for: ESPN's, then - for "Zoeller Field at Dix Stadium" - just
    the part OpenStreetMap is likelier to know, "Dix Stadium"."""
    cleaned = _clean_name(name)
    names = [cleaned]
    _, separator, after = cleaned.partition(" at ")
    if separator and after.strip():
        names.append(after.strip())
    return names


@dataclass(frozen=True)
class Geocode:
    latitude: float
    longitude: float
    name: str
    osm_type: str
    osm_id: int
    osm_key: str
    osm_value: str
    places: tuple[str, ...]
    """The city, district and locality OSM gives, for comparing with ESPN's city."""
    state: str

    @property
    def source(self) -> str:
        return f"photon:{self.osm_type}{self.osm_id}"

    @property
    def precision(self) -> str:
        return "stadium" if (self.osm_key, self.osm_value) in STADIUM_CLASSES else "other"

    @property
    def osm_url(self) -> str:
        kind = {"N": "node", "W": "way", "R": "relation"}.get(self.osm_type, "way")
        return f"https://www.openstreetmap.org/{kind}/{self.osm_id}"


def parse_photon_feature(feature: object) -> Geocode | None:
    """A Photon GeoJSON feature as a `Geocode`, or None if it's missing what we need."""
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates")
    if not (isinstance(coordinates, list) and len(coordinates) >= 2):
        return None
    longitude, latitude = coordinates[0], coordinates[1]
    if not all(isinstance(value, int | float) for value in (latitude, longitude)):
        return None
    if properties.get("countrycode") != "US" or not properties.get("name"):
        return None
    places = tuple(
        str(properties[key]) for key in ("city", "district", "locality") if properties.get(key)
    )
    return Geocode(
        latitude=float(latitude),
        longitude=float(longitude),
        name=str(properties["name"]),
        osm_type=str(properties.get("osm_type") or ""),
        osm_id=int(properties.get("osm_id") or 0),
        osm_key=str(properties.get("osm_key") or ""),
        osm_value=str(properties.get("osm_value") or ""),
        places=places,
        state=str(properties.get("state") or ""),
    )


@dataclass(frozen=True)
class GeocodeOutcome:
    result: Geocode | None
    notes: tuple[str, ...] = ()
    """Things a reviewer should look at (a different city, a sports ground not a stadium)."""
    candidates: tuple[str, ...] = ()
    """What was found but rejected, when nothing was accepted."""


class Geocoder(Protocol):
    def geocode(self, name: str, city: str, state: str) -> GeocodeOutcome: ...


class RateLimiter:
    """Spaces calls at least `min_interval` seconds apart."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class JsonCache:
    """Raw geocoder responses saved to a JSON file, so a rerun (or a tweak to the
    acceptance rules) doesn't query the service again. With no path it's in-memory."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._data: dict[str, list] = {}
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                self._data = loaded

    def get(self, key: str) -> list | None:
        return self._data.get(key)

    def put(self, key: str, features: list) -> None:
        self._data[key] = features
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, sort_keys=True))


class PhotonGeocoder:
    """Finds a venue with Photon, preferring a result tagged as a stadium and falling
    back to other sports grounds (a complex that OSM maps as a pitch). Only accepts a
    result that's in the venue's state and whose name matches ESPN's; a partial name
    match must also be in ESPN's city, since many towns have a "Memorial Stadium"."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        limiter: RateLimiter | None = None,
        cache: JsonCache | None = None,
    ) -> None:
        self._client = client
        self._limiter = limiter or RateLimiter(1.1)
        self._cache = cache or JsonCache()

    def _search(self, query: str, osm_tag: str | None) -> list:
        key = f"{query}|{osm_tag or ''}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        params = {"q": query, "limit": "8", "lang": "en"}
        if osm_tag:
            params["osm_tag"] = osm_tag

        def _get() -> httpx.Response:
            self._limiter.wait()
            response = self._client.get(PHOTON_URL, params=params)
            response.raise_for_status()
            return response

        features = request_with_retry(_get).json().get("features") or []
        self._cache.put(key, features)
        return features

    def geocode(self, name: str, city: str, state: str) -> GeocodeOutcome:
        if not name.strip():
            return GeocodeOutcome(None, notes=("ESPN gave no venue name",))
        state_name = US_STATES.get(state)
        if state_name is None:
            return GeocodeOutcome(None, notes=(f"unknown state code {state!r}",))

        # Search by stadium tag first, then more widely, under ESPN's name and then its
        # shorter form - stopping as soon as something in the right city turns up, since a
        # match in the wrong town (every state has a "Memorial Stadium") is the failure to
        # avoid, and only worth settling for when nothing in the right town is found.
        accepted: list[tuple[bool, bool, Geocode]] = []
        rejected: list[str] = []
        for osm_tag, classes in (
            ("leisure:stadium", STADIUM_CLASSES),
            (None, STADIUM_CLASSES | SPORTS_GROUND_CLASSES),
        ):
            for query_name in _query_names(name):
                for feature in self._search(f"{query_name}, {city}, {state_name}", osm_tag):
                    candidate = parse_photon_feature(feature)
                    if candidate is None:
                        continue
                    verdict = _judge(candidate, name, city, state_name, classes)
                    if verdict is None:
                        rejected.append(_describe(candidate))
                    else:
                        accepted.append((verdict, _city_matches(candidate, city), candidate))
                if any(city_ok for _, city_ok, _ in accepted):
                    break
            if any(city_ok for _, city_ok, _ in accepted):
                break
        if not accepted:
            return GeocodeOutcome(None, candidates=tuple(dict.fromkeys(rejected)))

        # The right city beats a different one - "Gies Memorial Stadium" in Champaign is
        # the Illinois field, an exact "Memorial Stadium" in another town isn't - then an
        # exact name beats a partial one; ties keep Photon's own ranking.
        accepted.sort(key=lambda item: (not item[1], not item[0]))
        _, city_ok, best = accepted[0]
        notes = []
        if not city_ok:
            notes.append(f"city differs: OSM says {', '.join(best.places) or 'nothing'}")
        if best.precision != "stadium":
            notes.append(f"mapped as {best.osm_key}={best.osm_value}, not a stadium")
        return GeocodeOutcome(best, tuple(notes))


def _city_matches(candidate: Geocode, city: str) -> bool:
    """Whether OSM places the venue in ESPN's city - allowing "Oxford" for "Oxford
    Township", where one name's words are all within the other's."""
    wanted = set(_tokens(city))
    if not wanted:
        return False
    for place in candidate.places:
        found = set(_tokens(place))
        if found and (wanted <= found or found <= wanted):
            return True
    return False


def _judge(
    candidate: Geocode,
    name: str,
    city: str,
    state_name: str,
    classes: frozenset[tuple[str, str]],
) -> bool | None:
    """None to reject `candidate`; otherwise whether its name matches ESPN's exactly."""
    if candidate.state.casefold() != state_name.casefold():
        return None
    if (candidate.osm_key, candidate.osm_value) not in classes:
        return None
    espn_tokens, osm_tokens = set(_tokens(_clean_name(name))), set(_tokens(candidate.name))
    if not espn_tokens or not osm_tokens:
        return None
    if espn_tokens == osm_tokens:
        return True
    # One name contains the other ("Gies Memorial Stadium" / "Memorial Stadium"): only
    # trust it in the right city.
    if (espn_tokens <= osm_tokens or osm_tokens <= espn_tokens) and _city_matches(candidate, city):
        return False
    return None


def _describe(candidate: Geocode) -> str:
    where = ", ".join((*candidate.places, candidate.state)) or "?"
    return f"{candidate.name} ({candidate.osm_key}={candidate.osm_value}, {where})"


# ---------------------------------------------------------------------- overrides


@dataclass(frozen=True)
class Override:
    """A hand-made correction for one venue id. Every override says why."""

    note: str
    exclude: bool = False
    name: str | None = None
    aliases: tuple[str, ...] | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_covered: bool | None = None
    city: str | None = None
    state: str | None = None


def _optional_text(raw: dict, key: str, venue_id: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"override {venue_id!r}: {key!r} must be a non-blank string")
    return value


def _optional_bool(raw: dict, key: str, venue_id: str) -> bool | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"override {venue_id!r}: {key!r} must be true or false, got {value!r}")
    return value


def _optional_coordinate(raw: dict, key: str, venue_id: str, limit: float) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"override {venue_id!r}: {key!r} must be a number")
    if not -limit <= value <= limit:
        raise ValueError(f"override {venue_id!r}: {key!r} out of range: {value!r}")
    return float(value)


def parse_overrides(text: str) -> dict[str, Override]:
    """Parse the overrides file as strictly as the table itself is parsed, so a typo can't
    quietly exclude a venue or put a row in the table that can't be read back. Raises
    ValueError, naming the venue, if anything is malformed."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"overrides file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("overrides file must be an object with schema_version 1")
    venues = payload.get("venues", {})
    if not isinstance(venues, dict):
        raise ValueError("overrides 'venues' must be an object keyed by ESPN venue id")

    known = {f.name for f in fields(Override)}
    overrides: dict[str, Override] = {}
    for venue_id, raw in venues.items():
        if not isinstance(raw, dict):
            raise ValueError(f"override {venue_id!r} must be an object")
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"override {venue_id!r} has unknown fields {sorted(unknown)}")
        note = raw.get("note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError(f"override {venue_id!r} needs a 'note' saying why")
        aliases = raw.get("aliases")
        if aliases is not None and not (
            isinstance(aliases, list) and all(isinstance(a, str) and a.strip() for a in aliases)
        ):
            raise ValueError(
                f"override {venue_id!r}: 'aliases' must be a list of non-blank strings"
            )
        latitude = _optional_coordinate(raw, "latitude", venue_id, 90)
        longitude = _optional_coordinate(raw, "longitude", venue_id, 180)
        if (latitude is None) != (longitude is None):
            raise ValueError(f"override {venue_id!r}: give latitude and longitude together")
        state = _optional_text(raw, "state", venue_id)
        if state is not None and state not in US_STATES:
            raise ValueError(f"override {venue_id!r}: unknown state code {state!r}")
        overrides[str(venue_id)] = Override(
            note=note,
            exclude=bool(_optional_bool(raw, "exclude", venue_id)),
            name=_optional_text(raw, "name", venue_id),
            aliases=tuple(aliases) if aliases is not None else None,
            latitude=latitude,
            longitude=longitude,
            is_covered=_optional_bool(raw, "is_covered", venue_id),
            city=_optional_text(raw, "city", venue_id),
            state=state,
        )
    return overrides


def _apply_override(row: VenueRow, override: Override) -> VenueRow:
    changes: dict = {}
    if override.name is not None:
        changes["name"] = override.name
    if override.aliases is not None:
        changes["aliases"] = override.aliases
    if override.is_covered is not None:
        changes["is_covered"] = override.is_covered
    if override.city is not None:
        changes["city"] = override.city
    if override.state is not None:
        changes["state"] = override.state
    if override.latitude is not None and override.longitude is not None:
        changes.update(
            latitude=round(override.latitude, COORDINATE_DECIMALS),
            longitude=round(override.longitude, COORDINATE_DECIMALS),
            precision="manual",
            source="manual",
        )
    return replace(row, **changes) if changes else row


def _row_from_override(venue_id: str, override: Override) -> VenueRow | None:
    """A whole new row from an override that supplies everything - for a venue the
    scoreboards haven't shown (a new stadium, a team that just moved up to FBS)."""
    if (
        override.name is None
        or override.latitude is None
        or override.longitude is None
        or override.is_covered is None
        or override.city is None
        or override.state is None
    ):
        return None
    return VenueRow(
        id=venue_id,
        name=override.name,
        latitude=round(override.latitude, COORDINATE_DECIMALS),
        longitude=round(override.longitude, COORDINATE_DECIMALS),
        is_covered=override.is_covered,
        city=override.city,
        state=override.state,
        precision="manual",
        source="manual",
        aliases=override.aliases or (),
    )


# ------------------------------------------------------------------------- build


@dataclass(frozen=True)
class Unplaced:
    """A venue the generator couldn't give a (good) row to, and why."""

    venue_id: str
    description: str
    reasons: tuple[str, ...]


@dataclass
class Report:
    added: list[tuple[VenueRow, tuple[str, ...]]] = field(default_factory=list)
    """New rows, with the notes a reviewer should read."""
    changed: list[tuple[VenueRow, VenueRow]] = field(default_factory=list)
    """(old, new) rows that differ from what was already in the table."""
    kept: int = 0
    removed: list[VenueRow] = field(default_factory=list)
    """Rows that were in the table and aren't in the new one: an exclusion override, or a
    `--refresh` that found the venue to be outside the US."""
    needs_manual: list[Unplaced] = field(default_factory=list)
    unused_overrides: list[str] = field(default_factory=list)
    """Overrides that matched nothing - a mistyped venue id, or one that's gone stale."""
    international: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.needs_manual or self.unused_overrides or self.coverage_gaps)


@dataclass
class BuildResult:
    rows: list[VenueRow]
    report: Report


def _base_row(
    observation: VenueObservation,
    *,
    latitude: float,
    longitude: float,
    is_covered: bool,
    precision: str,
    source: str,
    aliases: tuple[str, ...],
) -> VenueRow:
    return VenueRow(
        id=observation.venue_id,
        name=observation.name,
        latitude=round(latitude, COORDINATE_DECIMALS),
        longitude=round(longitude, COORDINATE_DECIMALS),
        is_covered=is_covered,
        city=observation.city,
        state=observation.state,
        precision=precision,
        source=source,
        aliases=aliases,
    )


def _nfl_row(observation: VenueObservation, nfl_catalog: VenueCatalog) -> VenueRow | None:
    stadium = nfl_catalog.for_venue(observation.venue_id, observation.name)
    if stadium is None:
        return None
    known_names = {*observation.other_names, stadium.name, *stadium.aliases}
    return _base_row(
        observation,
        latitude=stadium.latitude,
        longitude=stadium.longitude,
        is_covered=stadium.is_covered or observation.indoor,
        precision="stadium",
        source="nfl_catalog",
        aliases=tuple(sorted(known_names - {observation.name, ""})),
    )


def _resolve(
    observation: VenueObservation,
    override: Override | None,
    nfl_catalog: VenueCatalog,
    geocoder: Geocoder,
) -> tuple[VenueRow | None, tuple[str, ...], tuple[str, ...]]:
    """A new row for `observation`, with reviewer notes - or, if it can't be placed,
    None and why not. The NFL table is tried first, then an override's own coordinates
    (no need to geocode what's already been placed by hand), then Photon."""
    nfl_row = _nfl_row(observation, nfl_catalog)
    if nfl_row is not None:
        return nfl_row, (), ()
    if override is not None and override.latitude is not None and override.longitude is not None:
        row = _base_row(
            observation,
            latitude=override.latitude,
            longitude=override.longitude,
            is_covered=observation.indoor,
            precision="manual",
            source="manual",
            aliases=observation.other_names,
        )
        return row, (f"manual: {override.note}",), ()
    outcome = geocoder.geocode(observation.name, observation.city, observation.state)
    if outcome.result is None:
        reasons = list(outcome.notes)
        if outcome.candidates:
            reasons.append(f"rejected: {'; '.join(outcome.candidates)}")
        return None, (), tuple(reasons) or ("no candidates found",)
    found = outcome.result
    row = _base_row(
        observation,
        latitude=found.latitude,
        longitude=found.longitude,
        is_covered=observation.indoor,
        precision=found.precision,
        source=found.source,
        aliases=observation.other_names,
    )
    return row, (*outcome.notes, found.osm_url), ()


def build_table(
    harvested: Harvest,
    *,
    existing: Sequence[VenueRow] = (),
    overrides: Mapping[str, Override] | None = None,
    nfl_catalog: VenueCatalog,
    geocoder: Geocoder,
    refresh: bool = False,
) -> BuildResult:
    """The table for `harvested` venues. A row already in the table is kept as it is
    (unless `refresh`), so a rerun only adds what's new, and it is never dropped by a
    rerun that can't improve on it: if `refresh` can't place a venue again, the old row
    stays and the venue is reported. Overrides are applied to every row; a row that
    would be unreadable (a blank name, coordinates outside its state) is refused and
    reported instead of written."""
    overrides = overrides or {}
    previous = {row.id: row for row in existing}
    report = Report()
    rows: dict[str, VenueRow] = {}
    used: set[str] = set()

    def unplace(venue_id: str, description: str, reasons: tuple[str, ...]) -> None:
        old = previous.get(venue_id)
        if old is not None:
            rows[venue_id] = old
            report.kept += 1
            reasons = (*reasons, "kept the row already in the table")
        report.needs_manual.append(Unplaced(venue_id, description, reasons))

    def settle(row: VenueRow, notes: tuple[str, ...], description: str) -> None:
        """Apply the venue's override, check the row, then file it as added/changed/kept."""
        override = overrides.get(row.id)
        final = row
        if override is not None:
            used.add(row.id)
            final = _apply_override(row, override)
        problems = row_problems(final)
        if problems:
            unplace(row.id, description, tuple(f"row refused: {problem}" for problem in problems))
            return
        old = previous.get(row.id)
        if old is None:
            report.added.append((final, notes))
        elif old != final:
            report.changed.append((old, final))
        else:
            report.kept += 1
        rows[row.id] = final

    for venue_id in sorted(harvested.venues, key=id_order):
        observation = harvested.venues[venue_id]
        override = overrides.get(venue_id)
        description = observation.describe()
        if override is not None and override.exclude:
            used.add(venue_id)
            report.excluded.append(f"{venue_id} {observation.name}: {override.note}")
        elif venue_id in previous and not refresh:
            settle(previous[venue_id], (), description)
        elif is_confirmed_international(observation.address):
            report.international.append(f"{venue_id} {observation.name} ({observation.country})")
        else:
            row, notes, reasons = _resolve(observation, override, nfl_catalog, geocoder)
            if row is None:
                unplace(venue_id, description, reasons)
            else:
                settle(row, notes, description)

    # Rows already in the table for venues the harvest didn't show (one added by hand, or
    # one that simply wasn't hosted this time): keep them, still subject to overrides.
    for venue_id, row in previous.items():
        if venue_id in harvested.venues:
            continue
        override = overrides.get(venue_id)
        if override is not None and override.exclude:
            used.add(venue_id)
            report.excluded.append(f"{venue_id} {row.name}: {override.note}")
        else:
            settle(row, (), f"{row.name}, {row.city}, {row.state}")

    # An override for a venue in neither place can supply a whole row.
    for venue_id, override in overrides.items():
        if venue_id in rows or venue_id in harvested.venues or override.exclude:
            continue
        new_row = _row_from_override(venue_id, override)
        if new_row is not None:
            settle(new_row, (f"manual: {override.note}",), f"{new_row.name} (override only)")

    report.unused_overrides = [
        f"{venue_id}: {overrides[venue_id].note}"
        for venue_id in sorted(set(overrides) - used, key=id_order)
    ]
    report.removed = sorted(
        (row for venue_id, row in previous.items() if venue_id not in rows),
        key=lambda row: id_order(row.id),
    )
    report.coverage_gaps = coverage_gaps(harvested, rows)
    return BuildResult(sorted(rows.values(), key=lambda row: id_order(row.id)), report)


def coverage_gaps(harvested: Harvest, rows: Mapping[str, VenueRow]) -> list[str]:
    """FBS teams (by games played) none of whose home games were at a venue in the
    table - their usual home field is missing, e.g. a team new to FBS."""
    gaps = []
    for team in sorted(harvested.teams.values(), key=lambda t: t.name):
        if team.games < MIN_GAMES_FOR_COVERAGE:
            continue
        if not any(venue_id in rows for venue_id in team.home_venue_ids):
            hosted = (
                "hosted no non-neutral game"
                if not team.home_games
                else f"hosted {team.home_games} game(s) at venues not in the table"
            )
            gaps.append(f"{team.name}: {hosted}")
    return gaps


def format_report(result: BuildResult, *, venues_seen: int) -> str:
    """The reviewer's report: what's new (with a map link to check each against), what
    changed or was removed, and what needs a human."""
    report = result.report
    lines = [
        f"{venues_seen} venue(s) seen, {len(result.rows)} in the table "
        f"({report.kept} kept, {len(report.added)} added, {len(report.changed)} changed, "
        f"{len(report.removed)} removed).",
    ]
    if report.added:
        lines.append("\nAdded - check each against its map link (if it has one):")
        for row, notes in report.added:
            detail = f" [{'; '.join(notes)}]" if notes else ""
            lines.append(
                f"  {row.id:>6} {row.name}, {row.city}, {row.state}: "
                f"{row.latitude:.5f}, {row.longitude:.5f} ({row.precision}, "
                f"{'covered' if row.is_covered else 'open'}, {row.source}){detail}"
            )
    if report.changed:
        lines.append("\nChanged:")
        for old, new in report.changed:
            lines.append(f"  {old.id:>6} {old.name}: {_describe_change(old, new)}")
    if report.removed:
        lines.append("\nRemoved from the table:")
        for row in report.removed:
            lines.append(f"  {row.id:>6} {row.name}, {row.city}, {row.state}")
    if report.needs_manual:
        lines.append("\nNeeds a person (supply or fix the row in the overrides file):")
        for entry in report.needs_manual:
            lines.append(f"  {entry.venue_id:>6} {entry.description} - {'; '.join(entry.reasons)}")
    if report.unused_overrides:
        lines.append("\nOverrides that matched no venue (a mistyped id, or gone stale?):")
        lines.extend(f"  {entry}" for entry in report.unused_overrides)
    if report.coverage_gaps:
        lines.append("\nTeams with no home venue in the table:")
        lines.extend(f"  {gap}" for gap in report.coverage_gaps)
    if report.international:
        lines.append(f"\nSkipped as international: {'; '.join(report.international)}")
    if report.excluded:
        lines.append("\nExcluded by overrides:")
        lines.extend(f"  {entry}" for entry in report.excluded)
    return "\n".join(lines)


def _describe_change(old: VenueRow, new: VenueRow) -> str:
    """The fields that differ between two rows, so a change to the name or aliases isn't
    invisible in the report. `repr` keeps API-supplied text from smuggling in newlines."""
    return "; ".join(
        f"{f.name} {getattr(old, f.name)!r} -> {getattr(new, f.name)!r}"
        for f in fields(VenueRow)
        if getattr(old, f.name) != getattr(new, f.name)
    )
