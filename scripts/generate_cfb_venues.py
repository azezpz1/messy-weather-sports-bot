#!/usr/bin/env python
"""Regenerates the college football venue table, `data/cfb_venues.json`.

Harvests the venues FBS teams played at over the last full season and this one so far
(ESPN scoreboards), finds coordinates for each (the NFL table, else Photon/OpenStreetMap),
applies the hand-made corrections in `scripts/data/cfb_venue_overrides.json`, and writes
the table. Rows already in the table are kept, so a rerun only adds what's new; pass
`--refresh` to recompute them. Review the printed report before committing: each added
venue comes with a map link to check it against.

Exits 0 when every venue was placed, 2 when something needs a human (a venue it
couldn't place, an override that matched nothing, or an FBS team with no home field in
the table; the table is still written), 1 on an error (the network, an unreadable or
malformed input file, a harvest that found no games), in which case nothing is written.
Run it from a checkout with network access to ESPN and photon.komoot.io:

    uv run python scripts/generate_cfb_venues.py --cache .cache/venue_geocode.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import httpx

from messy_weather_sports_bot import venue_generation as generation
from messy_weather_sports_bot.stadiums import NFL_CATALOG
from messy_weather_sports_bot.venue_table import dump_table, parse_table
from messy_weather_sports_bot.weather import USER_AGENT

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "src" / "messy_weather_sports_bot" / "data" / "cfb_venues.json"
DEFAULT_OVERRIDES = REPO_ROOT / "scripts" / "data" / "cfb_venue_overrides.json"


def _date_range(text: str) -> tuple[dt.date, dt.date]:
    try:
        start, end = (dt.date.fromisoformat(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid range {text!r}: expected YYYY-MM-DD:YYYY-MM-DD"
        ) from None
    if end < start:
        raise argparse.ArgumentTypeError(f"range {text!r} ends before it starts")
    return start, end


def _fail(problem: object) -> int:
    """Report an error and return the exit code for one. Control characters are escaped,
    since the text can include something ESPN or a geocoder said."""
    if isinstance(problem, BaseException):
        problem = f"{type(problem).__name__}: {problem}"
    print(f"error: {generation.printable(problem)}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--overrides",
        type=Path,
        help="Hand-made corrections (default: scripts/data/cfb_venue_overrides.json).",
    )
    parser.add_argument(
        "--range",
        dest="ranges",
        type=_date_range,
        action="append",
        metavar="START:END",
        help="A date range to harvest (repeatable). Default: last season and this one so far.",
    )
    parser.add_argument(
        "--cache", type=Path, help="Save geocoder responses here so a rerun doesn't repeat them."
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Recompute rows already in the table."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the report without writing the table."
    )
    args = parser.parse_args(argv)

    ranges = args.ranges or generation.season_ranges(dt.date.today())
    try:
        existing = parse_table(args.output.read_text("utf-8")) if args.output.exists() else []
        overrides_path = args.overrides or DEFAULT_OVERRIDES
        if args.overrides is None and not overrides_path.exists():
            overrides = {}
        else:
            overrides = generation.parse_overrides(overrides_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(exc)

    try:
        # ESPN's edge refuses unfamiliar User-Agents, so its client keeps httpx's default;
        # the geocoder identifies this project, as OpenStreetMap's usage policy asks.
        with (
            httpx.Client(timeout=30.0) as espn_client,
            httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as geocode_client,
        ):
            harvested = generation.harvest(espn_client, ranges)
            print(
                f"Harvested {harvested.events} game(s) over {harvested.days} day(s): "
                f"{len(harvested.venues)} venue(s), {len(harvested.teams)} team(s); "
                f"{harvested.events_without_venue_id} game(s) had no venue id.",
                file=sys.stderr,
            )
            if harvested.unreadable:
                print(
                    f"warning: {harvested.unreadable} scoreboard response(s) or game(s) could "
                    "not be read and were skipped; a venue played only there is missing.",
                    file=sys.stderr,
                )
            if not harvested.events:
                return _fail(
                    "ESPN returned no games for the date range, so there is nothing to build"
                )
            geocoder = generation.PhotonGeocoder(
                geocode_client, cache=generation.JsonCache(args.cache)
            )
            result = generation.build_table(
                harvested,
                existing=existing,
                overrides=overrides,
                nfl_catalog=NFL_CATALOG,
                geocoder=geocoder,
                refresh=args.refresh,
            )
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return _fail(exc)

    print(generation.format_report(result, venues_seen=len(harvested.venues)))
    if not args.dry_run:
        text = dump_table(result.rows)
        try:
            # Never write a table the runtime couldn't read back.
            parse_table(text)
            generation.write_text_atomic(args.output, text)
        except (OSError, ValueError) as exc:
            return _fail(exc)
        print(f"\nWrote {len(result.rows)} venue(s) to {args.output}.")
    return 2 if result.report.needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
