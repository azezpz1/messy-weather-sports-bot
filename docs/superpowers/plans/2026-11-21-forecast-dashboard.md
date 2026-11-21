# Forecast Dashboard Implementation Plan

> Use test-driven development and execute the tasks in order. Confirm each regression reproduces before changing production code.

**Goal** — Build the LAN dashboard from the [approved design](../specs/2026-11-21-forecast-dashboard-design.md).

**Architecture** — A Flask application reads the shared SQLite history and exposes validated schedule and refresh forms. The existing collector claims durable refresh requests. A separate native systemd service runs Waitress behind Caddy.

**Stack** — Python, uv, Flask, Jinja, Waitress, htmx, SQLAlchemy, Alembic, and SQLite.

## Constraints

- Keep publishing on its current cron schedule and shared evaluator. Keep machine-specific settings outside the public repository.
- Use the newest verified seven-day collector slate and preserve history. Dashboard reads use read-only transactions.
- Keep write requests authenticated and protected against CSRF. Only NFL and Top 25 CFB collection are remotely controllable.

## Tasks

1. **Durable refresh requests.** Add `0003_dashboard_refresh_requests.py`. In `scheduler_store.py`, add `request_refresh(sport, now)` and `claim_requested(now)` and allow `configure()` to check an expected schedule revision atomically. Update `scheduler.run_once()` so the worker claims requests and due schedules under its existing lock. Test request deduplication, pause behavior, interruption with saved dates, cadence, legacy observations, and fresh upgrades with `uv run pytest tests/unit/test_scheduler.py`.
2. **Read model.** Add `dashboard_read.py` for read-only SQLite access, current-slate selection, history, tasks, and allowlisted raw tables. Verify a successful empty slate removes old games, failed slate retains its snapshot, rescheduled games appear once, absent scores remain unscored, rain can match a wind-driven result, and read queries coexist with a writer. Test using a populated migrated database.
3. **Web app.** Add `dashboard.py`, templates, and local static assets. Expose the NFL/CFB tabs, filters, game detail, tasks and raw data. Use protected POST forms, a loopback-only Waitress CLI, and a small htmx status fragment. Verify URL filters, history, forbidden tables, CSRF, invalid and stale schedule edits, and dashboard restart during a queued run using Flask's test client.
4. **Deployment and final checks.** Add generic native service and Caddy instructions to the README. Verify the packaged templates and static assets, run `scripts/check.sh -m 'not integration'` and the full suite where networking permits, and smoke-test the web routes. Verify physical devices during the guided LAN deployment.

5. **Browser tests.** Run the desktop Chromium and iPhone WebKit flows on CI using the optional uv test group. Verify filters, Back navigation, schedule updates, a refresh that survives a transient status error, and raw data.

## Review focus

- Preserve a manual request across web restarts, and report a collector crash as interrupted.
- Keep date-level partial weather failures distinct from failures to verify the slate.
- Escape stored forecast text and restrict table and sort identifiers.
- Protect existing cron publishing when storage fails and avoid write locks for browser polling.
- Keep the web process loopback-only and document authentication at the HTTPS proxy.
