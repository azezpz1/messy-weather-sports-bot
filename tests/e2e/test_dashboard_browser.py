import datetime as dt
from importlib import import_module
from threading import Thread

import pytest
from werkzeug.serving import make_server

from messy_weather_sports_bot import scheduler, scheduler_store
from messy_weather_sports_bot.collection import GameObservation
from messy_weather_sports_bot.collector import CollectedDay
from messy_weather_sports_bot.dashboard import create_app
from messy_weather_sports_bot.messiness import evaluate_game
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.sports import SPORTS
from messy_weather_sports_bot.venues import StadiumInfo
from messy_weather_sports_bot.weather import WeatherReport


@pytest.fixture
def dashboard_url(monkeypatch, tmp_path):
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))
    now = dt.datetime.now(dt.UTC)
    scheduler_store.initialize(now)
    kickoff = now + dt.timedelta(hours=12)
    field = StadiumInfo("Fixture Park", 44.5, -88, False)
    fixtures = {
        "nfl": [
            ("Snow matchup", WeatherReport("Snow", 28, 20, 85)),
            ("Rain matchup", WeatherReport("Rain", 50, 22, 70)),
        ],
        "cfb": [("Ranked matchup", WeatherReport("Rain", 48, 18, 65))],
    }
    for sport, games in fixtures.items():
        scheduler_store.configure(sport, now, enabled=False)
        observations = []
        for name, weather in games:
            game = Game(name, "Away", kickoff, field, espn_event_id=name, home_rank=12)
            observations.append(
                GameObservation(
                    game,
                    "evaluated",
                    forecasts=(weather,),
                    result=evaluate_game(game, [weather], []),
                    observed_at=now,
                )
            )
        execution = scheduler_store.claim_manual(sport, now)
        scheduler_store.save_day(
            execution,
            CollectedDay(
                SPORTS[sport].game_day_for(kickoff),
                now,
                "complete",
                len(observations),
                tuple(observations),
            ),
        )
        scheduler_store.finish(execution, now, "complete")

    def collect(sport, started):
        yield CollectedDay(sport.game_day_for(started), started, "complete", 0)

    monkeypatch.setattr(scheduler, "collect_window", collect)
    app = create_app("browser-fixture")
    # The test listener uses loopback HTTP. Production terminates HTTPS at Caddy.
    app.config["SESSION_COOKIE_SECURE"] = False
    # Browsers may open idle speculative connections while making other requests.
    with make_server("127.0.0.1", 0, app, threaded=True) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    ("engine", "device"), [("chromium", "Desktop Chrome"), ("webkit", "iPhone 15")]
)
def test_filter_games_and_manage_a_refresh(dashboard_url, engine, device):
    playwright_api = import_module("playwright.sync_api")
    expect = playwright_api.expect
    with playwright_api.sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        try:
            page = browser.new_page(**playwright.devices[device])
            page.goto(dashboard_url)
            expect(page.get_by_role("heading", name="NFL games")).to_be_visible()
            expect(page.get_by_role("link", name="Away at Rain matchup")).to_be_visible()
            page.get_by_label("Precipitation").select_option("snow")
            page.get_by_role("button", name="Apply filters").click()
            expect(page.get_by_role("link", name="Away at Rain matchup")).to_have_count(0)
            page.get_by_role("link", name="Away at Snow matchup").click()
            expect(page.get_by_role("heading", name="Hourly forecast")).to_be_visible()
            page.go_back()
            expect(page.get_by_label("Precipitation")).to_have_value("snow")
            page.get_by_role("link", name="Top 25 CFB").click()
            expect(page.get_by_role("link", name="Away at Ranked matchup")).to_be_visible()

            page.get_by_role("link", name="Tasks").click()
            schedule = page.locator("#cfb-schedule")
            schedule.get_by_label("Interval in hours").fill("5")
            schedule.get_by_role("button", name="Save schedule").click()
            expect(schedule.get_by_label("Interval in hours")).to_have_value("5")

            status_requests = 0

            def fail_first_status_request(route):
                nonlocal status_requests
                status_requests += 1
                if status_requests == 1:
                    route.fulfill(status=503, body="Temporary restart")
                else:
                    route.continue_()

            page.route("**/admin/tasks/status", fail_first_status_request)
            schedule.get_by_role("button", name="Refresh now").click()
            status = page.locator("#run-status")
            expect(status.get_by_text("Queued", exact=True)).to_be_visible()
            details = status.locator("details").first
            summary = details.locator("summary")
            summary.click()
            summary.focus()
            expect(details).to_have_attribute("open", "")
            opened_id = details.get_attribute("id")
            assert scheduler.run_once() == "complete"
            expect(status.get_by_text("Complete", exact=True)).to_be_visible(timeout=15_000)
            assert status_requests >= 2
            assert status.get_attribute("hx-trigger") is None
            # Preserve the exact execution opened before the refresh changed the list order.
            opened = status.locator("details[open]")
            expect(opened).to_have_count(1)
            assert opened.get_attribute("id") == opened_id
            expect(opened.locator("summary")).to_be_focused()

            publishing = page.locator("#nfl-publication")
            expect(publishing.get_by_label("Time (America/New_York)")).to_have_value("10:00")
            publishing.get_by_label("Time (America/New_York)").fill("09:15")
            publishing.get_by_role("button", name="Save publishing schedule").click()
            expect(publishing.get_by_label("Time (America/New_York)")).to_have_value("09:15")
            publishing.get_by_text("Publish this game day", exact=True).click()
            publishing.get_by_role("checkbox", name="Confirm NFL publishing", exact=False).check()
            publishing.get_by_role("button", name="Queue publishing").click()
            expect(page.locator("#run-status").get_by_text("Queued", exact=True)).to_be_visible()

            page.get_by_role("link", name="Data", exact=True).click()
            page.get_by_role("link", name="Hourly forecast periods").click()
            page.get_by_role("link", name="Open row").first.click()
            expect(
                page.get_by_role("heading", name="Hourly forecast periods record")
            ).to_be_visible()
            expect(page.get_by_role("row", name="short_forecast Snow")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        finally:
            browser.close()
