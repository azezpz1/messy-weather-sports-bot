# messy-weather-sports-bot

A recommendation engine for NFL games worth watching because the weather is
going to make them messy — snow games, sideways rain, wind that turns every
field goal into an adventure — posted to social media. A second bot does the
same for college football's [Top 25](#college-football-top-25).

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
`messy-weather-sports-bot` (the repo's name), while the commands it installs are
`messy-weather-nfl-bot` and `messy-weather-cfb-bot`. The installed package (and so the version line that
starts every run's log) also uses the repo's name, `messy-weather-sports-bot`,
not the command's - match on that if you grep logs for the version line. Paths
in crontab entries below need the directory name. A `cd` into a nonexistent
`messy-weather-nfl-bot` (or `-cfb-bot`) directory fails before the command after it runs, and a
`>> log 2>&1` at the end of the entry only captures that later command - so the
failure never reaches the log, and an update job pointed at the wrong directory
goes unnoticed while the checkout your posts run from never updates. The update
entry below groups the whole command under one redirect, to a log outside the
checkout, so a failed `cd` is logged too.

## Configuration

The schedule (ESPN) and weather (National Weather Service) APIs are free and
require no credentials. Posting to Bluesky needs an
[app password](https://bsky.app/settings/app-passwords), set via environment
variables. The state and history settings apply to both bots.

| Env var                 | Description                                   |
| ------------------------ | ---------------------------------------------- |
| `BLUESKY_HANDLE`         | Your Bluesky handle, e.g. `example.bsky.social` |
| `BLUESKY_APP_PASSWORD`   | An app password (not your account password)     |
| `HEALTHCHECK_URL`        | Optional [Healthchecks.io](#failure-alerting-with-healthchecksio) ping URL |
| `MESSY_WEATHER_STATE_DIR` | Optional override for [where post state is saved](#avoiding-duplicate-posts) |
| `MESSY_WEATHER_DB_PATH`   | Optional path to the [SQLite forecast history](#forecast-history) shared by both bots |

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
writes this state. (The [college football bot](#college-football-top-25) keeps its
own, beside it, as `<date>.cfb.json`.)

### Forecast history

The first normal run creates `forecast-history.sqlite3` automatically in the
state directory described above. Both bots append to that same file on their
existing schedules. The account running the bots needs write access to the
database and its directory. To use a different location, give both bots the
same setting in the environment their cron jobs use. For example, add this
line to their crontab with a directory writable by the cron user.

```cron
MESSY_WEATHER_DB_PATH=/var/lib/messy-weather/forecast-history.sqlite3
```

Dry runs, including previews for other game days, leave the database untouched.

The history includes supported games, venue and kickoff details, and the hourly
forecast periods used to score each game. It records clear-weather evaluations,
unavailable forecasts, alert lookup failures, and schedule failures. Reruns
keep their own timestamped observations, even when the day's thread is already
posted. The bot applies pending Alembic revisions automatically. If it cannot
save the history, it logs a warning and continues the run.

Schema revisions live in `src/messy_weather_sports_bot/migrations/versions/`.
Use `uv run alembic revision -m "describe the schema change"` to create a revision.
To apply pending revisions manually, run `uv run alembic upgrade head` with the
same database settings as the bots.

### Independent forecast collection

`messy-weather-collector` refreshes upcoming NFL and Top 25 college football
games and saves forecasts to the shared SQLite history. It looks ahead seven
days, records available hourly periods for eligible outdoor games, and keeps
earlier snapshots when forecasts or kickoffs change. Empty slates and games
beyond the available forecast horizon are expected. Failed lookups and
incomplete game forecasts are recorded separately. Collection never posts,
reads posting credentials, or updates publication state.

The worker runs year-round, every six hours per sport by default. It keeps
schedule settings in SQLite. After downtime, it runs each overdue schedule
once and skips older slots. One collection runs at a time, including manual
refreshes. A restart marks an abandoned run as interrupted and retains any
dates already saved. Schedule edits apply while the worker runs.

Run these commands as the collector user with the same database setting as
the service:

```sh
uv run messy-weather-collector schedule status
uv run messy-weather-collector schedule set-interval nfl 4
uv run messy-weather-collector schedule pause cfb
uv run messy-weather-collector schedule resume cfb
uv run messy-weather-collector collect --sport nfl
```

Changing an interval schedules the next run that many hours from the edit.
Resuming a paused sport makes it due immediately. A manual refresh leaves
the recurring schedule on its existing cadence.

On a Raspberry Pi with systemd, create a local
`/etc/systemd/system/messy-weather-collector.service` using this example.
Replace the user and paths with those on your Pi. Use the same app version
and database path for the collector and both publishing bots. The user must
be able to write to the database and its parent directory.

```ini
[Unit]
Description=Messy weather forecast collector
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=collector
WorkingDirectory=/path/to/messy-weather-sports-bot
Environment=MESSY_WEATHER_DB_PATH=/var/lib/messy-weather/forecast-history.sqlite3
ExecStart=/path/to/uv run --locked messy-weather-collector worker
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now messy-weather-collector.service
systemctl status messy-weather-collector.service
journalctl -u messy-weather-collector.service -n 50
```

If a run fails, check the journal and `schedule status` for the recorded
outcome. After resolving the problem, use `collect --sport nfl` or
`collect --sport cfb` to refresh immediately. Keep the publishing cron entries
and their existing Healthchecks in place. Store the service configuration and
runtime database outside the repository.

### Private forecast dashboard

The dashboard shows the collector's next seven days on separate NFL and Top 25
CFB tabs. Sort games by messiness or kickoff, filter by date or precipitation
type, and open a matchup for its hourly forecast and saved observations. It uses
the same scoring rules as the publishing bots. **Tasks** manages the two
collection schedules and manual refreshes. **Data** browses the stored forecast
and scheduler tables in read-only mode.

Run the [independent collector](#independent-forecast-collection) first to apply
the database migrations and populate schedules. The dashboard, collector, and
publishing jobs use the same release and local database directory. Their users
need the appropriate permissions on that directory and its database and lock
files. Give the dashboard and collector their own service configuration.

#### Run the dashboard as a systemd service

Install Caddy with your OS package manager before creating the dashboard
service. Create a dashboard environment file outside the checkout. Adapt it to
your installation.

    MESSY_WEATHER_DB_PATH=/path/to/forecast-history.sqlite3
    MESSY_WEATHER_DASHBOARD_HOST=dashboard.home.arpa
    MESSY_WEATHER_DASHBOARD_SECRET=<random secret>

Generate the session secret with `openssl rand -hex 32` and save it in the
environment file. Restrict access to the file. Set the host to the exact
hostname or LAN IP used in the browser, without an HTTPS scheme or port.

Create a local `messy-weather-dashboard.service` using your service account,
paths, and environment file.

    [Unit]
    Description=Messy weather forecast dashboard
    After=network.target

    [Service]
    Type=simple
    User=<service-user>
    Group=caddy
    RuntimeDirectory=messy-weather-dashboard
    RuntimeDirectoryMode=0750
    WorkingDirectory=/path/to/messy-weather-sports-bot
    EnvironmentFile=/path/to/dashboard.env
    ExecStart=/path/to/uv run --locked messy-weather-dashboard
    Restart=on-failure
    RestartSec=5

    [Install]
    WantedBy=multi-user.target

The proxy account needs the socket group only. If a separate group controls
the shared database, add that group to the dashboard unit's
`SupplementaryGroups=`.

Enable the service after reloading systemd.

    sudo systemctl daemon-reload
    sudo systemctl enable --now messy-weather-dashboard

Waitress serves through `/run/messy-weather-dashboard/dashboard.sock`. The
dashboard and Caddy service accounts can open the socket. Publishing and
collection continue while the dashboard service is stopped.

#### Serve the dashboard on your home network

Choose a LAN hostname that resolves on both devices or use a reserved LAN IP.
Run `caddy hash-password` and enter your dashboard password when prompted. Keep
the password hash and the actual Caddyfile on the server. Adapt this example
site block to your host, interface, and credentials.

    https://dashboard.home.arpa {
        bind <pi-lan-ip>
        tls internal
        route {
            @browser_icons path /favicon.ico /apple-touch-icon*.png
            respond @browser_icons 404

            @dashboard not path /static/*
            basic_auth @dashboard {
                <username> <password-hash>
            }
            reverse_proxy unix//run/messy-weather-dashboard/dashboard.sock {
                header_up Host {hostport}
                header_up -Authorization
            }
        }
    }

Caddy 2.6 and 2.7 use `basicauth` for the same directive. Caddy 2.8 and newer
use `basic_auth`. Restrict the HTTPS port to the trusted home network in your
host firewall. If the dashboard is already deployed, update its site block in
`/etc/caddy/Caddyfile` with the routing above, keeping your host, bind address,
and credentials. Validate the Caddyfile and reload the Caddy service.

    sudo caddy validate --config /etc/caddy/Caddyfile
    sudo systemctl reload caddy

Caddy requires sign-in for dashboard pages, data, and actions. Browsers can
request the bundled CSS and JavaScript without credentials. Favicon and Apple
touch icon requests get a plain `404`, so background icon lookups do not trigger
another sign-in prompt. Caddy's local CA signs the dashboard certificate.

- Transfer the CA's public root certificate from Caddy's
  [data directory](https://caddyserver.com/docs/automatic-https#local-https)
  to your devices. Keep the private key on the server.
- On iPhone, install the certificate, then enable TLS trust under **Settings
  → General → About → Certificate Trust Settings**.
  [Apple certificate trust instructions](https://support.apple.com/en-ie/102390)
- On desktop, add the certificate to your OS or browser trust store.
- Open the HTTPS URL on both devices and sign in. Verify the NFL and CFB tabs
  load, queue a refresh from **Tasks**, and open a row in **Data**.

The existing release updater installs the dashboard's Python dependencies
through uv. Use the [coordinated update procedure](#updating-to-the-latest-release)
when the dashboard and collector share the checkout. Caddy updates through the
OS package manager. The bundled htmx asset ships with a normal app release.

The browser tests run in CI with Chromium and an iPhone-sized WebKit viewport.
CI also verifies the documented Caddy configuration against Waitress over HTTPS.
To reproduce these checks, install Caddy on your development machine, install
the browser engines, and run the optional test group.

    uv run --locked --group e2e playwright install chromium webkit
    uv run --locked --group e2e pytest --no-cov tests/e2e

Playwright's `install --with-deps` flag also installs system libraries on Linux.

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

## College football (Top 25)

`messy-weather-cfb-bot` is the same bot for college football: the same
forecast check, the same messiness thresholds, the same threaded posts - and
the same rule that a game with nice weather never appears. It covers only games
where **one or both teams are in the Top 25** (ESPN's ranking: the AP poll
during the season, the CFP rankings later), since a full Saturday is 60-80
games and the ranked ones are the ones followers tune in for. Posts read
"Messy Top 25 college football games to watch", ranked teams carry their
number, and a neutral-site game says "vs" rather than "@":

```text
🌩️ Messy Top 25 college football games to watch — Sat Oct 10
🏈 South Carolina @ #16 Florida (12:45pm ET): ⛈️ Showers And Thunderstorms Likely, 82°F (feels 90°F), 13mph wind
🏈 #2 Georgia @ #6 Alabama (7:30pm ET): 🌧️ Rain Showers Likely, 74°F, 20mph wind
```

```sh
uv run messy-weather-cfb-bot --dry-run                  # preview today
uv run messy-weather-cfb-bot --dry-run --date 2026-10-10  # preview another Saturday
uv run messy-weather-cfb-bot                            # post for real
```

It takes the same options as the NFL bot, and posts to **its own accounts**:
nothing is shared with the NFL bot, and nothing falls back to its settings (a
fallback could post college games to the NFL account).

| Env var                    | Description                                          |
| -------------------------- | ---------------------------------------------------- |
| `CFB_BLUESKY_HANDLE`       | The college football account's Bluesky handle        |
| `CFB_BLUESKY_APP_PASSWORD` | An app password for that account                     |
| `CFB_HEALTHCHECK_URL`      | Optional ping URL for its own Healthchecks.io check  |

Its post state is kept beside the NFL's as `<date>.cfb.json` (the NFL's stays
`<date>.json`), so the two never mistake each other's thread for their own. Give
it its own Healthchecks check, with the schedule and timezone of its own crontab
entry.

Notes:

- **Venues** come from the generated college table (see
  [College football venue data](#college-football-venue-data)), looked up by ESPN's
  venue id. A game at a venue that isn't in it is skipped and logged as
  unrecognized, and the weekly [venue drift check](#stadium-data) flags it.
- **Rankings guard:** ESPN gives every team a ranking field (99 when unranked). If a
  day has eight or more games and none carries one, ESPN's response has changed;
  the bot logs a warning and exits `2` instead of quietly never posting.
- **Late kickoffs:** ESPN files every game under its Eastern date, so a 10:30pm
  ET kickoff belongs to that day's run, and a game whose kickoff time isn't set
  yet is skipped.
- An unranked game costs no weather lookups, and a day with no messy ranked game
  posts nothing and exits cleanly, like the NFL's.

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

The college football bot needs its own entry. Bowl games and the playoff land on
arbitrary weekdays, and it is a no-op on any day without a messy ranked game, so
run it daily through the season (August to January):

```cron
# m h  dom mon dow          command
0  9   *   8-12,1   *        cd /path/to/messy-weather-sports-bot && uv run messy-weather-cfb-bot
```

Cron does not load your shell profile or `.env` files automatically, so
`BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`, and `HEALTHCHECK_URL` (and the `CFB_`
versions the college bot reads) won't be set
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

If the checkout is also used by a running collector or dashboard, coordinate
updates outside the publishing window. Remove the updater from any publishing
wrapper and remove any standalone updater cron entry. Keep the publishing
cron commands. Stop the collector and dashboard, run the updater, then start
the collector first so it can migrate the database. Start the dashboard
after the collector is ready.

For a publishing-only checkout, you can run the updater before your scheduled
job, such as from the wrapper your crontab invokes. If you hit GitHub's
unauthenticated API rate limit, set a `GITHUB_TOKEN` env var (no special
permissions needed) and the script will use it.

For a publishing-only checkout, you can also schedule the updater separately —
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
runs weekly during each season - one job for the NFL, one for college football -
and fails if any upcoming US game's venue isn't recognized, so a stadium rename
or relocation is caught before it quietly drops a team's home games. (The
college job checks every game, not only the ranked ones, since any venue can
host a ranked team next week. For a venue it doesn't know, rerun the
[generator](#college-football-venue-data) with `--range` covering that game's date
- its default range stops at today, so it can't see a venue that hasn't hosted
a game yet - or add an override.) It also runs on pull requests that
touch a venue table (`stadiums.py`, `data/cfb_venues.json`), the code the check
runs through (`venues.py`, `venue_table.py`, `espn.py`, `schedule.py`,
`sport.py`, `sports.py`, `nfl.py`, `cfb.py`, `drift.py`), the check script, or
the workflow file itself. Run it locally with:

```sh
uv run python scripts/check_venue_drift.py            # the NFL
uv run python scripts/check_venue_drift.py --sport cfb  # college football
```

### College football venue data

`src/messy_weather_sports_bot/data/cfb_venues.json` lists the venues FBS teams
play at: ESPN's venue id, coordinates, and whether the field is covered. It is
keyed by venue id, never by team, because college teams play at neutral sites,
bowls and rivalry venues too often for "the home team's stadium" to be a safe
guess. A venue is also found by its id alone, never by name: there are many
"Memorial Stadium"s, and a name-only match could report another town's weather.
(The names and aliases in the file are for people reading it.) A game at an id
that isn't in the table is therefore an unrecognized venue, not a guess. The
[college football bot](#college-football-top-25) reads it.

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
committing: each venue added or moved by a geocode comes with an OpenStreetMap link
to check it against (rows copied from the NFL table or placed by an override have no
link), and it lists what changed field by field, any venue that couldn't be
placed, any override that matched no venue, and any FBS team with no home field
in the table. A warning before it says if some scoreboard responses or games couldn't
be read: a venue played only there is missing from the table.

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
