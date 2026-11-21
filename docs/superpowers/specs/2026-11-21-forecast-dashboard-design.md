# Forecast dashboard design

[Issue #127](https://github.com/azezpz1/messy-weather-sports-bot/issues/127) builds on the shared forecast history and the [Phase 2 collector](https://github.com/azezpz1/messy-weather-sports-bot/issues/126).

## Purpose and scope

Give one or two people a private way to find upcoming NFL and Top 25 college football games worth watching for the weather. Use the same evaluation as the Bluesky bots and look ahead through the collector's rolling seven-day window. The first version runs on the Raspberry Pi 4 and opens in desktop browsers and iPhone Safari on the home network. Remote access and Tailscale are deferred. Publishing stays on its existing cron schedule, data path, and recommendation rules.

## Approaches

- **Flask, Jinja, Waitress, and htmx** are the selected stack. Server-rendered pages fit the filtering, browsing, and forms. htmx refreshes the job panel while work is in progress. Use responsive CSS and package the assets with the Python app.
- **FastAPI and a static Svelte build** could also run as one web process on the Pi, with the frontend built off-device. A reactive UI and a typed API would pay off for synchronized chart comparisons or browser state shared across many views. This dashboard's main flow is scanning and filtering a list, so the extra frontend build and API contract add maintenance to the first version.
- **FastAPI with Jinja and htmx** keeps the Python deployment and offers typed request parsing. The proposed form and query inputs are small, and Flask can share validation with the existing schedule services and render errors directly in the form.

## Browse upcoming games

- Give **NFL** and **Top 25 CFB** separate tabs. Use a desktop table and a compact list on iPhone. Show matchup, kickoff with timezone, venue, the selected game-window forecast, the saved score and qualifying condition, any recorded alert, and when the forecast was collected. Use the saved condition, forecast, and alert to explain the score in the first version.
- Default to score descending with kickoff as a tiebreaker and unscored games after scored games. Offer kickoff ordering, a date filter, a precipitation filter, and **Messy only**. Store filters and sort in the URL so links, bookmarks, and browser Back preserve the view. The default includes pleasant games and games waiting for data.
- Use the collector's persisted score, condition, and messy flag from the shared evaluator. For the precipitation filter, match categories mentioned in any saved game-window forecast period independently of whether they met the messiness threshold. Support rain, snow, ice or wintry mix, and thunderstorms. Recognize mixed conditions and treat freezing rain as ice. A forecast with slight-chance showers can match Rain without becoming a recommendation. Unknown forecasts have no precipitation tags.
- Distinguish evaluated pleasant weather, covered venues, venue or forecast lookup failures, and games beyond the current forecast horizon. Show partial coverage or failed alert lookup alongside any available score. Display snapshot age and mark it stale after two configured collection intervals, which is 12 hours with the current defaults. A failed latest forecast attempt shows its own state and links to older snapshots on the detail page.
- Use the latest trustworthy independent collection for each sport and date to determine the current slate. An authoritative empty slate clears that date. A failed schedule fetch or unavailable CFB rankings retains the last verified slate and its saved forecasts with a notice. Keep showing the original forecast age in that case. Deduplicate by the existing game identity and use the newest verified kickoff and venue for a rescheduled game. Use publishing observations for historical details when available.
- Game detail shows the saved hourly conditions and a dated list of earlier observations. Selecting a snapshot shows its recorded kickoff and venue, score, outlook, and available hourly periods. The first version uses tables and summaries for history.

## Admin and raw data

- **Tasks** shows NFL and CFB collection intervals, enabled state, next run, last successful run, and recent executions. Use the Phase 2 schedule rules for editing the interval, pausing, and resuming. Forms validate schedule edits and report conflicts when a form was opened before another change. Paused tasks display their paused status and still accept manual refreshes.
- **Refresh now** durably queues an allowlisted sport for the existing collector. Repeated taps return the status of that sport's pending or running manual request. The collector claims requests under its existing lock and records execution progress from saved collection days. Manual refreshes leave the recurring cadence in place. Show queued, running, complete, partial, failed, and interrupted states with dates processed, time stamps, and safe error summaries. Poll only while work is queued or running.
- **Data** is a read-only SQLite table browser. Start with `games`, `game_observations`, `forecast_periods`, `collection_runs`, `collection_schedules`, `collection_executions`, and the new refresh request table. Show stored column names, IDs, values, and `NULL` distinctly. Paginate rows and link related records through their stored IDs. Restrict the table and sort fields to fixed allowlists. Render stored text as escaped text. The page offers browsing and row details through predefined queries.

## Runtime and LAN access

- Run Waitress on a permissioned Unix socket shared with Caddy in a standalone systemd service using the same native `uv` deployment as the collector. Run Caddy as a native system service. Use the existing SQLite file on local storage. Dashboard reads use a read-only connection and short queries so ordinary browsing and polling avoid the current write-locking status path. Use a new Alembic revision for the durable request queue. Keep migrations and network work out of web requests.
- Use the Pi's LAN address or hostname behind an authenticated HTTPS reverse proxy. The proposed default is Caddy with a local certificate authority and a password-protected site. Every authorized household user can browse games and use Admin. Document the one-time certificate trust setup on the iPhone and desktop. Limit access to the home network and store the proxy settings, credentials, and Flask secret outside the repo. Protect schedule and refresh forms with CSRF tokens. The dashboard and collector services run without Bluesky credentials. [Caddy local HTTPS](https://caddyserver.com/docs/automatic-https) · [Caddy authentication](https://caddyserver.com/docs/caddyfile/directives/basic_auth) · [iPhone certificate trust](https://support.apple.com/en-ie/102390)
- Keep public deployment instructions reusable. Use placeholders for hostnames, IP addresses, usernames, and file paths. Give the operator a private walkthrough for their devices and keep their filled-in configuration, certificate files, and credentials outside the repository.
- The existing collector owns schedule execution, upstream calls, and recovery. Closing the browser or restarting the dashboard leaves collection and publishing operational. If the worker stops, saved data stays visible and pending refreshes remain queued for its return.

## Acceptance checks

- Verify both tabs, score and kickoff ordering, precipitation and messy filters, bookmarks, and history using fixture data. Compare fully covered dashboard results with the shared publishing evaluator, including cases where an alert changes the score and where the forecast remains pleasant.
- Exercise empty, stale, missing, covered, partial, mixed-precipitation, and rescheduled games. Verify the raw explorer displays the matching saved rows and rejects unrecognized tables and sort fields.
- Verify authentication and CSRF, invalid and concurrent schedule edits, request deduplication, worker restart, interrupted collection, and dashboard restart during a run. Check the migration on a fresh database and a populated Phase 2 database, and confirm publishing remains best-effort on persistence failure.
- Smoke-test the deployed dashboard from a desktop and an iPhone on the home network, including the private URL, a filter bookmark, a refresh, and opening a forecast row in Data.
