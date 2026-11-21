import datetime as dt
import logging

import httpx
import pytest
import respx

from messy_weather_sports_bot import collector, dashboard_read, scheduler, scheduler_store
from messy_weather_sports_bot.cfb import CFB, cfb_catalog
from messy_weather_sports_bot.collection import forecast_db_transaction
from messy_weather_sports_bot.collector_cli import main
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.stadiums import stadium_for_team
from tests.support.espn import MISSING, cfb_event, espn_event
from tests.support.nws import forecast_response, mock_hourly_forecast


def test_rolling_collection_continues_after_one_failed_date(monkeypatch):
    now = dt.datetime(2026, 11, 21, 18, tzinfo=dt.UTC)
    day = NFL.game_day_for(now)

    def fetch(self, date):
        if date == day + dt.timedelta(days=1):
            raise httpx.ConnectError("offline")
        return []

    monkeypatch.setattr(collector.EspnScoreboard, "fetch", fetch)
    results = list(collector.collect_window(NFL, now))
    assert [result.game_day for result in results] == [day + dt.timedelta(days=i) for i in range(8)]
    assert results[1].outcome == "schedule_unavailable"
    assert results[1].error_type == "ConnectError"
    assert results[2].outcome == "complete"
    assert results[2].games_found == 0


def test_manual_collection_persists_boundary_games_and_reschedule(monkeypatch, tmp_path):

    now = dt.datetime(2026, 1, 18, 18, tzinfo=dt.UTC)
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("MESSY_WEATHER_STATE_DIR", str(tmp_path / "publication"))
    monkeypatch.setattr(
        scheduler, "collect_window", lambda sport, _: collector.collect_window(sport, now)
    )
    stadium = stadium_for_team("GB")

    def event(kickoff, identifier):
        return dict(espn_event("GB", "CHI", kickoff=kickoff), id=identifier)

    with respx.mock:
        scoreboard = respx.get(NFL.scoreboard_url)

        def slate(request):
            games = events.get(request.url.params["dates"], [])
            return httpx.Response(200, json={"events": games})

        events = {
            "20260118": [
                event("2026-01-18T17:59Z", "past"),
                event("2026-01-18T18:00Z", "rescheduled"),
            ],
            "20260125": [
                event("2026-01-25T17:59Z", "within"),
                event("2026-01-25T18:00Z", "outside"),
            ],
        }
        scoreboard.mock(side_effect=slate)
        mock_hourly_forecast(
            stadium.latitude,
            stadium.longitude,
            response=forecast_response("Sunny", 45, "5 mph", 0),
        )
        assert main(["collect", "--sport", "nfl"]) == 0
        initial_due = {row["sport"]: row["next_due"] for row in scheduler_store.status()}
        events["20260118"] = [event("2026-01-18T19:00Z", "rescheduled")]
        assert main(["collect", "--sport", "nfl"]) == 0
        assert {row["sport"]: row["next_due"] for row in scheduler_store.status()} == initial_due

    assert not (tmp_path / "publication").exists()
    with forecast_db_transaction() as db:
        observations = db.exec_driver_sql(
            """
            SELECT g.espn_event_id, o.kickoff, o.detail, o.is_messy
            FROM game_observations AS o JOIN games AS g ON o.game_id = g.id
            ORDER BY o.id
            """
        ).all()
    assert observations == [
        ("rescheduled", "2026-01-18T18:00:00+00:00", None, 0),
        ("within", "2026-01-25T17:59:00+00:00", "forecast_horizon", None),
        ("rescheduled", "2026-01-18T19:00:00+00:00", None, 0),
        ("within", "2026-01-25T17:59:00+00:00", "forecast_horizon", None),
    ]


def test_ranked_college_game_keeps_partial_hourly_forecast():

    now = dt.datetime(2026, 1, 18, 17, tzinfo=dt.UTC)
    stadium = cfb_catalog().for_venue("3861", "")
    assert stadium is not None
    response = forecast_response("Rain", 42, "5 mph", 90)
    payload = response.json()
    payload["properties"]["periods"][0]["endTime"] = "2026-01-18T19:00:00Z"
    with respx.mock:
        respx.get(CFB.scoreboard_url).respond(
            json={
                "events": [
                    cfb_event("Ohio State", "Michigan", home_rank=3),
                    cfb_event("Ohio", "Toledo"),
                ]
            }
        )
        mock_hourly_forecast(
            stadium.latitude, stadium.longitude, response=httpx.Response(200, json=payload)
        )
        day = next(collector.collect_window(CFB, now))
    assert day.outcome == "partial"
    (game,) = day.observations
    assert game.detail == "partial_forecast_coverage"
    assert len(game.forecasts) == 1
    assert game.result is not None and game.result.is_messy


def test_bad_forecast_keeps_other_games_and_later_dates(monkeypatch, tmp_path):
    now = dt.datetime(2026, 1, 18, 17, tzinfo=dt.UTC)
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setattr(
        scheduler, "collect_window", lambda sport, _: collector.collect_window(sport, now)
    )
    with respx.mock:
        respx.get(NFL.scoreboard_url).mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "events": (
                        [espn_event("GB", "CHI"), espn_event("BUF", "NE")]
                        if request.url.params["dates"] == "20260118"
                        else []
                    )
                },
            )
        )
        good = stadium_for_team("GB")
        mock_hourly_forecast(
            good.latitude, good.longitude, response=forecast_response("Sunny", 45, "5 mph", 0)
        )
        bad = stadium_for_team("BUF")
        payload = forecast_response("Rain", 45, "5 mph", 90).json()
        payload["properties"]["periods"][0]["windSpeed"] = 20
        mock_hourly_forecast(
            bad.latitude, bad.longitude, response=httpx.Response(200, json=payload)
        )

        assert main(["collect", "--sport", "nfl"]) == 2

    with forecast_db_transaction() as db:
        observations = db.exec_driver_sql(
            "SELECT outcome, detail FROM game_observations ORDER BY id"
        ).all()
        dates = db.exec_driver_sql(
            "SELECT game_day, outcome FROM collection_runs ORDER BY game_day"
        ).all()
    assert observations == [("evaluated", None), ("forecast_unavailable", "ValueError")]
    assert dates[0] == ("2026-01-18", "partial")
    assert dates[-1] == ("2026-01-25", "complete")


def test_collector_enables_application_info_without_enabling_httpx_info(monkeypatch, tmp_path):
    root = logging.getLogger()
    app = logging.getLogger("messy_weather_sports_bot")
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(app, "level", logging.WARNING)
    monkeypatch.setattr(app, "handlers", [])
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))

    assert main(["schedule", "status"]) == 0
    assert app.isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_a_scoreboard_without_events_does_not_replace_the_slate_with_an_empty_day():
    now = dt.datetime(2026, 11, 21, 18, tzinfo=dt.UTC)
    with respx.mock:
        respx.get(NFL.scoreboard_url).mock(
            side_effect=[httpx.Response(200, json={}), httpx.Response(200, json={"events": []})]
        )
        failed, empty = (next(collector.collect_window(NFL, now)) for _ in range(2))

    assert failed.outcome == "schedule_unavailable"
    assert failed.games_found is None
    assert empty.outcome == "complete"
    assert empty.games_found == 0


@pytest.mark.parametrize(
    ("replacement", "notice"),
    [
        pytest.param({}, "schedule_unavailable", id="missing-competition"),
        pytest.param(
            cfb_event("Ohio State", "Michigan", kickoff="2026-01-18T18:00"),
            "schedule_unavailable",
            id="timezone-naive-kickoff",
        ),
        pytest.param(
            {"competitions": [{"date": "2026-01-18T18:00Z", "competitors": []}]},
            "schedule_unavailable",
            id="missing-competitors",
        ),
        pytest.param(
            {
                "competitions": [
                    {
                        "date": "2026-01-18T18:00Z",
                        "competitors": [{"homeAway": "home"}, {"homeAway": "away"}],
                    }
                ]
            },
            "schedule_unavailable",
            id="missing-team-labels",
        ),
        pytest.param(
            cfb_event("Ohio State", "Michigan", home_curated=MISSING, away_curated=MISSING),
            "rankings_unavailable",
            id="both-rankings-missing",
        ),
        pytest.param(
            cfb_event("Ohio State", "Michigan", home_curated=MISSING),
            "rankings_unavailable",
            id="one-ranking-missing",
        ),
    ],
)
def test_incomplete_scoreboard_retains_previously_verified_slate(
    monkeypatch, tmp_path, replacement, notice
):
    now = dt.datetime(2026, 1, 18, 17, tzinfo=dt.UTC)
    path = tmp_path / "history.sqlite3"
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(path))
    scheduler_store.initialize(now)
    stadium = cfb_catalog().for_venue("3861", "")
    assert stadium is not None

    events = [
        cfb_event("Ohio State", "Michigan", home_rank=3),
        replacement,
        cfb_event("Ohio State", "Michigan"),
    ]
    with respx.mock:
        respx.get(CFB.scoreboard_url).mock(
            side_effect=[httpx.Response(200, json={"events": [event]}) for event in events]
        )
        mock_hourly_forecast(
            stadium.latitude,
            stadium.longitude,
            response=forecast_response("Rain", 45, "5 mph", 90),
        )

        def refresh():
            execution = scheduler_store.claim_manual("cfb", now)
            scheduler_store.save_day(execution, next(collector.collect_window(CFB, now)))
            with dashboard_read.read_database(path) as db:
                return dashboard_read.browse_games(db, "cfb", now)

        (verified,) = refresh()["games"]
        unverified = refresh()
        unranked = refresh()

    assert [game["id"] for game in unverified["games"]] == [verified["id"]]
    assert unverified["notices"] == [
        {"game_day": "2026-01-18", "outcome": notice, "retained": True}
    ]
    assert unranked["games"] == []
    assert unranked["notices"] == []
