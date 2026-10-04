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

# --------------------------------------------------------------------------- harvest


@dataclass
class VenueObservation:
    """What ESPN's scoreboards reported about one venue id, over every game seen."""

    venue_id: str
    names: Counter[str] = field(default_factory=Counter)
    city: str = ""
    state: str = ""
    country: str = ""
    indoor: bool = False
    games: int = 0
    neutral_games: int = 0

    @property
    def name(self) -> str:
        """The name most often reported (ties go to the alphabetically first)."""
        return min(self.names, key=lambda n: (-self.names[n], n))

    @property
    def other_names(self) -> tuple[str, ...]:
        return tuple(sorted(n for n in self.names if n != self.name))

    @property
    def address(self) -> dict:
        return {"city": self.city, "state": self.state, "country": self.country}


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
            observation.names[str(venue.get("fullName") or "").strip()] += 1
            observation.city = str(address.get("city") or observation.city)
            observation.state = str(address.get("state") or observation.state)
            observation.country = str(address.get("country") or observation.country)
            observation.indoor = observation.indoor or bool(venue.get("indoor"))
            observation.games += 1
            observation.neutral_games += neutral
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


def _norm(text: str) -> str:
    return " ".join(_tokens(text))


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
        state_name = US_STATES.get(state)
        if state_name is None:
            return GeocodeOutcome(None, candidates=(f"unknown state code {state!r}",))
        rejected: list[str] = []
        for osm_tag, classes in (
            ("leisure:stadium", STADIUM_CLASSES),
            (None, STADIUM_CLASSES | SPORTS_GROUND_CLASSES),
        ):
            accepted: list[tuple[bool, bool, Geocode]] = []
            for query_name in _query_names(name):
                query = f"{query_name}, {city}, {state_name}"
                for feature in self._search(query, osm_tag):
                    candidate = parse_photon_feature(feature)
                    if candidate is None:
                        continue
                    verdict = _judge(candidate, name, city, state_name, classes)
                    if verdict is None:
                        rejected.append(_describe(candidate))
                    else:
                        accepted.append((verdict, _city_matches(candidate, city), candidate))
                if accepted:
                    break  # the full name found something; no need to try the short one
            if accepted:
                # The right city beats a different one - "Gies Memorial Stadium" in
                # Champaign is the Illinois field, an exact "Memorial Stadium" in another
                # town isn't - then an exact name beats a partial one; ties keep Photon's
                # own ranking.
                accepted.sort(key=lambda item: (not item[1], not item[0]))
                _, city_ok, best = accepted[0]
                notes = []
                if not city_ok:
                    notes.append(f"city differs: OSM says {', '.join(best.places) or 'nothing'}")
                if best.precision != "stadium":
                    notes.append(f"mapped as {best.osm_key}={best.osm_value}, not a stadium")
                return GeocodeOutcome(best, tuple(notes))
        return GeocodeOutcome(None, candidates=tuple(dict.fromkeys(rejected)))


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


def parse_overrides(text: str) -> dict[str, Override]:
    """Parse the overrides file; raises ValueError (naming the venue) if it's malformed."""
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
            isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)
        ):
            raise ValueError(f"override {venue_id!r}: 'aliases' must be a list of strings")
        for key in ("latitude", "longitude"):
            value = raw.get(key)
            if value is not None and (
                not isinstance(value, int | float) or isinstance(value, bool)
            ):
                raise ValueError(f"override {venue_id!r}: {key!r} must be a number")
        if (raw.get("latitude") is None) != (raw.get("longitude") is None):
            raise ValueError(f"override {venue_id!r}: give latitude and longitude together")
        overrides[str(venue_id)] = Override(
            note=note,
            exclude=bool(raw.get("exclude", False)),
            name=raw.get("name"),
            aliases=tuple(aliases) if aliases is not None else None,
            latitude=float(raw["latitude"]) if raw.get("latitude") is not None else None,
            longitude=float(raw["longitude"]) if raw.get("longitude") is not None else None,
            is_covered=raw.get("is_covered"),
            city=raw.get("city"),
            state=raw.get("state"),
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


@dataclass
class Report:
    added: list[tuple[VenueRow, tuple[str, ...]]] = field(default_factory=list)
    """New rows, with the notes a reviewer should read."""
    changed: list[tuple[VenueRow, VenueRow]] = field(default_factory=list)
    """(old, new) rows that differ from what was already in the table."""
    kept: int = 0
    needs_manual: list[tuple[VenueObservation, tuple[str, ...]]] = field(default_factory=list)
    international: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.needs_manual or self.coverage_gaps)


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
    None and what was rejected. The NFL table is tried first, then an override's own
    coordinates (no need to geocode what's already been placed by hand), then Photon."""
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
        return None, (), outcome.candidates
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
    """The table for `harvested` venues. Existing rows are kept as they are (unless
    `refresh`), so a rerun only adds what's new; overrides are applied to every row."""
    overrides = overrides or {}
    previous = {row.id: row for row in existing}
    report = Report()
    rows: dict[str, VenueRow] = {}

    def settle(row: VenueRow, notes: tuple[str, ...] = ()) -> None:
        """Apply the venue's override, then file the row as added, changed or kept."""
        override = overrides.get(row.id)
        final = _apply_override(row, override) if override is not None else row
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
        if override is not None and override.exclude:
            report.excluded.append(f"{venue_id} {observation.name}: {override.note}")
        elif is_confirmed_international(observation.address):
            report.international.append(f"{venue_id} {observation.name} ({observation.country})")
        elif venue_id in previous and not refresh:
            settle(previous[venue_id])
        else:
            row, notes, rejected = _resolve(observation, override, nfl_catalog, geocoder)
            if row is None:
                report.needs_manual.append((observation, rejected))
            else:
                settle(row, notes)

    # Rows already in the table for venues the harvest didn't show (one added by hand, or
    # one that simply wasn't hosted this time): keep them, still subject to overrides.
    for venue_id, row in previous.items():
        if venue_id in harvested.venues:
            continue
        override = overrides.get(venue_id)
        if override is not None and override.exclude:
            report.excluded.append(f"{venue_id} {row.name}: {override.note}")
        else:
            settle(row)

    # An override for a venue in neither place can supply a whole row.
    for venue_id, override in overrides.items():
        if venue_id in rows or venue_id in harvested.venues or override.exclude:
            continue
        new_row = _row_from_override(venue_id, override)
        if new_row is not None:
            settle(new_row, (f"manual: {override.note}",))

    report.coverage_gaps = coverage_gaps(harvested, rows)
    return BuildResult(sorted(rows.values(), key=lambda r: id_order(r.id)), report)


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
    changed, and what needs a human."""
    report = result.report
    lines = [
        f"{venues_seen} venue(s) seen, {len(result.rows)} in the table "
        f"({report.kept} kept, {len(report.added)} added, {len(report.changed)} changed).",
    ]
    if report.added:
        lines.append("\nAdded - check each against the map link:")
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
            lines.append(f"  {old.id:>6} {old.name}: {_row_summary(old)} -> {_row_summary(new)}")
    if report.needs_manual:
        lines.append("\nNeeds manual coordinates (add to the overrides file):")
        for observation, candidates in report.needs_manual:
            seen = "; ".join(candidates) if candidates else "no candidates"
            lines.append(
                f"  {observation.venue_id:>6} {observation.name}, {observation.city}, "
                f"{observation.state} ({observation.games} game(s)) - rejected: {seen}"
            )
    if report.coverage_gaps:
        lines.append("\nTeams with no home venue in the table:")
        lines.extend(f"  {gap}" for gap in report.coverage_gaps)
    if report.international:
        lines.append(f"\nSkipped as international: {'; '.join(report.international)}")
    if report.excluded:
        lines.append("\nExcluded by overrides:")
        lines.extend(f"  {entry}" for entry in report.excluded)
    return "\n".join(lines)


def _row_summary(row: VenueRow) -> str:
    covered = "covered" if row.is_covered else "open"
    return f"{row.latitude:.5f},{row.longitude:.5f} {covered} {row.source}"
