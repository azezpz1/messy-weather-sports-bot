# messy-weather-sports-bot

A bot to post messy NFL weather games to various social media sites

On the morning of NFL game days, this bot checks the forecast for every outdoor
stadium hosting a game that day, ranks them by how messy the weather looks
(snow first, then by a combined score of wind, precipitation odds, and
temperature extremes), and posts a weather report — currently to
[Bluesky](https://bsky.app), with a clean abstraction to add more platforms
later.

Games in domed, fixed-roof, or retractable-roof stadiums are skipped, since
roof status isn't reliably knowable ahead of time. If there are no outdoor
games that day, the bot posts nothing.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

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

Every run logs why each game found for the day was included or skipped (a
covered stadium, an international/neutral-site venue, or a forecast that
couldn't be fetched), followed by a one-line summary. Logs go to stderr with
timestamps, so they're readable from cron/journald logs.

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
bot's own exit codes. Days with no outdoor games still ping success, so they
don't look like a missed run. A failed ping is logged but never affects the
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
0  9   *   *   0,1,4        cd /path/to/messy-weather-nfl-bot && uv run messy-weather-nfl-bot
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
cd /path/to/messy-weather-nfl-bot
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
0  0   *   *   6            cd /path/to/messy-weather-nfl-bot && ./scripts/update-to-latest-release.sh >> update-to-latest-release.log 2>&1
0  9   *   *   0,1,4        cd /path/to/messy-weather-nfl-bot && uv run messy-weather-nfl-bot
```

## Development

```sh
uv run ruff check .      # lint
uv run ty check          # type check
uv run pytest            # unit + integration tests, with coverage
uv run pytest -m "not integration"  # unit tests only (no network)
```

Integration tests hit the real ESPN and NWS APIs (no credentials needed) but
never post to Bluesky — they use a console-printing poster instead. They run
in CI on every push and pull request via GitHub Actions.

### Pre-commit hooks

Optionally, install [pre-commit](https://pre-commit.com/) to run ruff (lint
and format) and ty automatically before each commit:

```sh
uv tool install pre-commit --with pre-commit-uv
pre-commit install
```

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
