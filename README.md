# messy-weather-sports-bot

A recommendation engine for NFL games worth watching because the weather is
going to make them messy — snow games, sideways rain, wind that turns every
field goal into an adventure — posted to social media.

**This is not a sports weather report.** Every post should tell followers
*"you should watch this game, because it's going to be messy."* A game with
nice weather isn't a recommendation, so it never appears in a post, and a day
with no messy games gets no post at all. Silence on a sunny Sunday is the bot
working as intended. When changing what gets posted, the test is: would a
follower tune in to this game for the weather? If not, it doesn't belong.

On the morning of NFL game days, the bot checks the forecast — and any active
National Weather Service alerts (Winter Storm Warning, Wind Advisory, etc.) —
for every outdoor stadium hosting a game that day, picks out the messy ones,
and posts them — currently to [Bluesky](https://bsky.app), with a clean
abstraction to add more platforms later.

A game counts as messy if, at any hour between kickoff and the final whistle,
the forecast shows:

| Condition     | Threshold                                                        |
| ------------- | ---------------------------------------------------------------- |
| Ice 🧊        | freezing rain, freezing drizzle, sleet, or ice pellets with a ≥30% chance |
| Snow ❄️       | snow, flurries, wintry mix, etc. with a ≥30% chance              |
| Thunderstorms | ≥50% chance                                                      |
| Rain          | rain, showers, or drizzle with a ≥50% chance                     |
| Fog           | fog or mist in the forecast (haze doesn't count)                 |
| Wind          | ≥20 mph sustained                                                |
| Cold / heat   | feels like ≤30°F (wind chill) or ≥103°F (heat index)             |

So a "Slight Chance Rain Showers" (NWS's wording for 15–24%) sunny afternoon
doesn't make the cut. An NWS alert on its own doesn't either — alerts for a
stadium's location include things like Small Craft Advisories — but an active
alert does push an already-messy game up the ranking and is shown on its line.
If NWS reports no precipitation odds for an hour at all, the forecast text is
taken at its word - "Rain" with no reported odds still counts as rain.

Cold and heat are judged on how it feels, using the NWS wind chill and heat
index formulas: 30°F with a 15 mph wind feels like 19°F, and a humid 93°F
feels hotter than a dry 97°F. Posts show the feels-like temperature next to
the air temperature when they differ by 5°F or more.

Messy games are ranked by a combined score of precipitation odds, wind,
feels-like temperature extremes, alert severity, and a bonus for the
condition. Ice and snow carry by far the biggest bonuses, and heavy or blowing
precipitation earns extra - but a precipitation bonus is scaled by its chance,
so a likely snow game tops the list while a 30% chance of flurries ranks below
a likely thunderstorm with 30 mph winds. The thresholds and weights live at
the top of `src/messy_weather_sports_bot/messiness.py`.

Games in domed, fixed-roof, or retractable-roof stadiums are skipped, since
roof status isn't reliably knowable ahead of time. If there are no outdoor
games that day, the bot posts nothing.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/azezpz1/messy-weather-sports-bot.git
cd messy-weather-sports-bot
uv sync
```

Note the two different names: the checkout directory is
`messy-weather-sports-bot` (the repo's name), while the command it installs is
`messy-weather-nfl-bot`. The installed package (and so the version line that
starts every run's log) also uses the repo's name, `messy-weather-sports-bot`,
not the command's - match on that if you grep logs for the version line. Paths
in crontab entries below need the directory name. A `cd` into a nonexistent
`messy-weather-nfl-bot` directory fails before the command after it runs, and a
`>> log 2>&1` at the end of the entry only captures that later command - so the
failure never reaches the log, and an update job pointed at the wrong directory
goes unnoticed while the checkout your posts run from never updates. The update
entry below groups the whole command under one redirect, to a log outside the
checkout, so a failed `cd` is logged too.

## Configuration

The schedule (ESPN) and weather (National Weather Service) APIs are free and
require no credentials. Posting to Bluesky needs an
[app password](https://bsky.app/settings/app-passwords), set via environment
variables:

| Env var                 | Description                                   |
| ------------------------ | ---------------------------------------------- |
| `BLUESKY_HANDLE`         | Your Bluesky handle, e.g. `example.bsky.social` |
| `BLUESKY_APP_PASSWORD`   | An app password (not your account password)     |
| `HEALTHCHECK_URL`        | Optional [Healthchecks.io](#failure-alerting-with-healthchecksio) ping URL |
| `MESSY_WEATHER_STATE_DIR` | Optional override for [where post state is saved](#avoiding-duplicate-posts) |

## Running

```sh
# Preview what would be posted today, without posting anywhere:
uv run messy-weather-nfl-bot --dry-run

# Preview another game day (always a dry run; forecasts only reach ~7 days out):
uv run messy-weather-nfl-bot --dry-run --date 2026-10-04

# Post for real (requires BLUESKY_HANDLE / BLUESKY_APP_PASSWORD):
uv run messy-weather-nfl-bot

# Post to multiple platforms at once (as more are added):
uv run messy-weather-nfl-bot --platforms bluesky

# More or less log detail:
uv run messy-weather-nfl-bot --verbose   # DEBUG-level detail too
uv run messy-weather-nfl-bot --quiet     # only warnings and errors

# Repost today's thread even though it already finished:
uv run messy-weather-nfl-bot --force
```

Every run starts by logging the installed version (e.g.
`messy-weather-sports-bot 2.0.1 starting`), so a log - or a Healthchecks.io ping
body - always says which release produced it. It then logs why each game found
for the day was included or skipped (a covered stadium, an international venue,
an unrecognized venue, a forecast that couldn't be fetched, or weather that
isn't messy), followed by a one-line summary. Logs go to stderr
with timestamps, so they're readable from cron/journald logs.

### Avoiding duplicate posts

The bot keeps a small per-day, per-platform state file recording the posts it
has published successfully, so it's safe to re-run — a manual retry, an
overlapping crontab entry, or a scheduler catch-up won't repost a thread
that already finished today. Pass `--force` to repost anyway.

If a thread only partly posted (e.g. reply 2 of 3 hit a network error), a
re-run resumes it by replying to the last post that succeeded, instead of
starting over — the root is never posted twice. Individual `reply()` calls
are also retried a couple of times before the thread is given up on as
partially posted.

State files live at `$XDG_STATE_HOME/messy-weather-bot/<date>.json` (or
`~/.local/state/messy-weather-bot/<date>.json` if `XDG_STATE_HOME` isn't
set), overridable via `MESSY_WEATHER_STATE_DIR`. `--dry-run` never reads or
writes this state.

### Failure alerting with Healthchecks.io

The bot's own logs only help if something reads them. Since it normally runs
unattended from cron, a silent failure — bad credentials, an API change, the
Pi being off — can go unnoticed for weeks. [Healthchecks.io](https://healthchecks.io)
is a free "dead man's switch": the bot pings it on every run, and *you* get
alerted if an expected ping doesn't show up, not just when the bot itself
detects a problem.

To set it up:

1. Create a free Healthchecks.io account (or [self-host it](https://github.com/healthchecks/healthchecks)
   somewhere other than the Pi it's watching).
2. Add a check using the **same cron schedule and timezone as your crontab**
   (e.g. `0 9 * * 0,1,4` in `America/New_York`), with a grace period (e.g. 60
   minutes) to allow for a slow run.
3. Connect an alert channel (email, Discord, Slack, ntfy, Pushover, etc.).
4. Copy the check's ping URL (`https://hc-ping.com/<uuid>`) into
   `HEALTHCHECK_URL`, in your crontab or `.env`.

With `HEALTHCHECK_URL` set, the bot pings `{url}/start` when a run begins and
`{url}/{exit_code}` when it ends (carrying the run summary and log tail as
the ping body) — `0` means success, anything else is a failure, reusing this
bot's own exit codes. Days with no outdoor games, or no messy ones, still
ping success, so they don't look like a missed run. The exception is a day
where a game's forecast couldn't be fetched: if none of the games that could be
checked are messy, it exits `2` (partial), since the missing game might have
been the messy one - and if every outdoor game's forecast failed, it exits `1`
(nothing posted). A failed ping is logged but never affects the
run's outcome — the bot doesn't need `HEALTHCHECK_URL` set at all, and
nothing is pinged if it's unset.

## Running on a schedule (e.g. a Raspberry Pi)

This script doesn't schedule itself — run it via cron (or any scheduler) on
game mornings. It's a no-op (exits cleanly, posts nothing) on days with no
outdoor NFL games, so it's safe to run daily if you'd rather not maintain a
precise NFL schedule in your crontab. A typical crontab entry, run at 9am on
Thursdays, Sundays, and Mondays:

```cron
# m h  dom mon dow          command
0  9   *   *   0,1,4        cd /path/to/messy-weather-sports-bot && uv run messy-weather-nfl-bot
```

Cron does not load your shell profile or `.env` files automatically, so
`BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`, and `HEALTHCHECK_URL` won't be set
unless you provide them explicitly: set them directly in the crontab, or in
a wrapper script that exports them before invoking `uv`. If you're using
Healthchecks.io, match the check's schedule and timezone to whatever cron
expression you use here.

### Updating to the latest release

Releases are cut in two steps, since `main` is protected against direct
pushes:

1. Run the "Release (prepare)" workflow manually from the Actions tab,
   choosing a `patch`/`minor`/`major` bump. It bumps the version and opens a
   PR (`release/vX.Y.Z`) with the change.
2. Merge that PR. Merging triggers the "Release (publish)" workflow, which
   tags the merge commit (`vX.Y.Z`) and publishes a GitHub release with
   auto-generated notes.

Rather than tracking `main` directly, point a deployment (e.g. a Raspberry
Pi) at the latest tag instead, using `scripts/update-to-latest-release.sh`:

```sh
cd /path/to/messy-weather-sports-bot
./scripts/update-to-latest-release.sh
```

This asks GitHub for the latest *published* release rather than just the
newest tag, since a repo could in principle have tags that were never turned
into a release, then checks out that tag and runs `uv sync`.

Run it before your scheduled job (e.g. as the first line of the wrapper
script your crontab invokes) to stay on the latest release without ever
checking out unreleased commits from `main`. If you're hitting GitHub's
unauthenticated API rate limit, set a `GITHUB_TOKEN` env var (no special
permissions needed) and the script will use it.

Rather than running it inline before every scheduled post, you can instead
give it its own crontab entry so the checkout is refreshed ahead of time —
for example, Saturday at midnight so it's ready before the Sunday morning
run:

```cron
# m h  dom mon dow          command
0  0   *   *   6            { cd /path/to/messy-weather-sports-bot && ./scripts/update-to-latest-release.sh; } >> $HOME/messy-weather-update.log 2>&1
0  9   *   *   0,1,4        cd /path/to/messy-weather-sports-bot && uv run messy-weather-nfl-bot
```

## Development

```sh
uv run ruff check .      # lint
uv run ty check          # type check
uv run pytest            # unit + integration tests, with coverage
uv run pytest -m "not integration"  # unit tests only (no network)
```

Integration tests hit the real ESPN and NWS APIs (no credentials needed) but
never post to Bluesky — they use a console-printing poster instead. They're
contract tests: they catch an upstream API change that the unit tests, which
mock those APIs, can't. CI runs them on every push and pull request, in an
`integration` job separate from the `test` job (lint, types, unit tests), so
an upstream outage shows up as its own red check without failing `test`.
Only `test` should be a required status check for merging.

### Releasing

The "Release (prepare)" workflow (manually triggered) bumps the version,
commits, and opens a `release/vX.Y.Z` pull request; merging that PR triggers
"Release (publish)", which tags the merge commit and creates the GitHub
release.

**Known gap:** the release PR's CI run needs a manual approval. "Release
(prepare)" opens the PR using the default `GITHUB_TOKEN`. GitHub puts the
resulting `pull_request`-triggered CI run into an approval-required state — a user
with write access has to click "Approve and run" in the PR's merge box
before it (and the integration tests) actually execute. This is distinct
from the separate approval gate for PRs from forks or first-time
contributors. The prepare job runs lint, type checks, and unit tests
*before* opening the PR as a partial substitute, but if the release PR is
merged before anyone approves the run, it merges without CI ever having
executed against it. Fixing this so CI runs automatically requires
authenticating `gh pr create` with a GitHub App token or a fine-grained PAT
(owned by a non-Actions identity) instead of `GITHUB_TOKEN`, which needs a
repository secret to be provisioned by a maintainer.

### Stadium data

`src/messy_weather_sports_bot/stadiums.py` has each team's usual venue -
coordinates and whether it's covered. Games are matched (in
`src/messy_weather_sports_bot/venues.py`) against *all* known stadiums (by
ESPN's venue id, current name, or a listed alias), not just the home team's, so
a relocated game or a renamed venue still resolves; a venue alias should be
added in `stadiums.py` when a stadium gets a new sponsor name. A US venue that
doesn't match anything is skipped and logged as unrecognized rather than
silently dropped.

The "Venue drift check" workflow (`.github/workflows/venue-drift-check.yml`)
runs weekly during the season and fails if any upcoming US game's venue
isn't recognized, so a stadium rename or relocation is caught before it
quietly drops a team's home games. It also runs on pull requests that touch
`stadiums.py`, the code the check runs through (`venues.py`, `espn.py`,
`schedule.py`, `sport.py`, `nfl.py`), the check script, or the workflow file
itself. Run it locally with:

```sh
uv run python scripts/check_venue_drift.py
```

### College football venue data

`src/messy_weather_sports_bot/data/cfb_venues.json` lists the venues FBS teams
play at: ESPN's venue id, coordinates, and whether the field is covered. It is
keyed by venue id, never by team, because college teams play at neutral sites,
bowls and rivalry venues too often for "the home team's stadium" to be a safe
guess. A venue is also found by its id alone, never by name: there are many
"Memorial Stadium"s, and a name-only match could report another town's weather.
(The names and aliases in the file are for people reading it.) A game at an id
that isn't in the table is therefore an unrecognized venue, not a guess.

The file is generated, not edited by hand:

```sh
uv run python scripts/generate_cfb_venues.py --cache .cache/venue_geocode.json
```

It harvests the venues FBS teams played at over the last full season and this
one so far from ESPN's scoreboards, then places each one: a venue that is also an
NFL stadium copies the NFL table; otherwise it is geocoded with
[Photon](https://photon.komoot.io) (OpenStreetMap data), accepting only a result
in the venue's state whose name matches ESPN's (a partial match must also be in
the right city) and whose OSM class is a stadium or sports ground. A venue
ESPN reports as indoors is covered. A row is only written if the table's own
reader would accept it, so a blank name or coordinates outside the venue's state
(say, an override with a dropped minus sign) is refused and reported instead.

A rerun only adds what's new: rows already in the table are kept as they are, and
a row is never dropped unless you pass `--refresh` and the venue turns out to be
outside the US, or an override excludes it - and either way it is listed under
"Removed". `--refresh` recomputes every row; if it can't place a venue again, the
old row stays and the venue is reported. Read the printed report before
committing: each venue added from a geocode comes with an OpenStreetMap link to
check it against (rows copied from the NFL table or placed by an override have no
link), and it lists what changed field by field, any venue that couldn't be
placed, any override that matched no venue, and any FBS team with no home field
in the table.

The script exits 0 when everything was placed, 2 when something needs a person
(the table is still written), and 1 on an error - the network, an unreadable or
malformed input file, an ESPN reply that isn't a scoreboard, or a harvest that
found no games - in which case nothing is written. A `--overrides` path that
doesn't exist is an error; the default file is optional.

Corrections go in `scripts/data/cfb_venue_overrides.json` - coordinates, a roof
that ESPN reports as open, aliases, an excluded venue, or a whole venue the
scoreboards haven't shown yet - each with a `note` saying why, so a regeneration
keeps them. The file is parsed strictly (a typo in a field name or type is an
error, not a silently ignored override). Don't edit the table JSON by hand:
`tests/unit/test_cfb_venue_table.py` fails if the file isn't exactly the form the
generator writes, if an override isn't reflected in the table, or if a row's
coordinates leave its state, but it can't tell a hand-moved coordinate that stays
well-formed from a generated one, so make corrections as overrides. Pinned there
too: the set of covered venues and a list of famous open-air stadiums. Rerun the
generator at the start of each season for new stadiums and renames.

### Pre-commit hooks

Optionally, install [pre-commit](https://pre-commit.com/) to run ruff (lint
and format) and ty automatically before each commit, and the full
`scripts/check.sh` (lint, format, types, unit tests) before each push:

```sh
uv tool install pre-commit --with pre-commit-uv
pre-commit install                    # lint/format/types on commit
pre-commit install --hook-type pre-push  # full check before push
```

`scripts/check.sh` runs the same checks as CI and can also be run directly
at any time (`./scripts/check.sh` for the full suite, or
`./scripts/check.sh -m "not integration"` to skip network-dependent tests).

### Code coverage

`pytest` runs with [`pytest-cov`](https://pytest-cov.readthedocs.io/) enabled
by default (see `[tool.pytest.ini_options]` / `[tool.coverage.*]` in
`pyproject.toml`), printing a per-file report and failing if total coverage
drops below 80%. CI also writes `coverage.xml` and uploads it as a build
artifact. For a browsable HTML report locally:

```sh
uv run pytest --cov-report=html
open htmlcov/index.html
```
