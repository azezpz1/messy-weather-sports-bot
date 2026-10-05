import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from messy_weather_sports_bot.alerts import AlertSeverity, WeatherAlert
from messy_weather_sports_bot.cfb import CFB
from messy_weather_sports_bot.formatting import build_post_texts, format_game_line, format_header
from messy_weather_sports_bot.messiness import evaluate_game
from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import Game
from messy_weather_sports_bot.stadiums import stadium_for_team
from messy_weather_sports_bot.weather import WeatherReport

GAME_DATE = dt.date(2026, 1, 18)


def make_game_weather(
    home: str,
    away: str,
    short_forecast: str,
    wind_speed_mph: float = 5.0,
    temperature_f: int | None = 28,
    alerts: list[WeatherAlert] | None = None,
):
    game = Game(
        home_team=home,
        away_team=away,
        kickoff=dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC),
        stadium=stadium_for_team(home),
    )
    weather = WeatherReport(
        short_forecast=short_forecast,
        temperature_f=temperature_f,
        wind_speed_mph=wind_speed_mph,
        precipitation_probability=60,
    )
    return evaluate_game(game, [weather], alerts)


def test_format_game_line_includes_teams_emoji_and_temperature() -> None:
    gw = make_game_weather("GB", "CHI", "Snow")
    line = format_game_line(gw, NFL)
    assert "CHI @ GB" in line
    assert "28°F" in line
    assert "❄️" in line  # snowflake emoji


def test_format_game_line_omits_wind_when_calm() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny", wind_speed_mph=0)
    assert "mph wind" not in format_game_line(gw, NFL)


def test_format_game_line_shows_the_wind_chill_when_it_differs() -> None:
    # 28°F at 15 mph feels like ~16°F.
    gw = make_game_weather("GB", "CHI", "Snow", wind_speed_mph=15)
    assert "28°F (feels 16°F)" in format_game_line(gw, NFL)


def test_format_game_line_omits_feels_like_when_close_to_the_air_temperature() -> None:
    gw = make_game_weather("GB", "CHI", "Snow", temperature_f=40, wind_speed_mph=4)
    assert "feels" not in format_game_line(gw, NFL)


def test_format_game_line_omits_temperature_when_missing() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny", temperature_f=None)
    assert "°F" not in format_game_line(gw, NFL)


def test_format_game_line_includes_kickoff_time_in_eastern() -> None:
    gw = make_game_weather("GB", "CHI", "Snow")  # kickoff is 18:00 UTC == 1:00pm ET
    assert "1:00pm ET" in format_game_line(gw, NFL)


def test_build_post_texts_empty_games_returns_no_posts() -> None:
    assert build_post_texts([], GAME_DATE, NFL) == []


def test_build_post_texts_rejects_max_length_too_small_for_header() -> None:
    games = [make_game_weather("GB", "CHI", "Snow")]
    with pytest.raises(ValueError, match="too small"):
        build_post_texts(games, GAME_DATE, NFL, max_length=5)


def test_build_post_texts_single_game_fits_in_one_post() -> None:
    games = [make_game_weather("GB", "CHI", "Snow")]
    texts = build_post_texts(games, GAME_DATE, NFL)
    assert len(texts) == 1
    assert "GB" in texts[0] and "CHI" in texts[0]
    assert len(texts[0]) <= 280


def test_build_post_texts_splits_into_thread_when_too_long() -> None:
    games = [
        make_game_weather(home, "MIA", "Heavy Thunderstorms with Damaging Wind Gusts Expected")
        for home in ["GB", "CHI", "KC", "DEN", "BUF", "NE", "SEA", "TB", "CAR", "PIT", "CIN", "BAL"]
    ]
    texts = build_post_texts(games, GAME_DATE, NFL)
    assert len(texts) > 1
    for text in texts:
        assert len(text) <= 280

    # every game's line shows up somewhere across the thread
    combined = "\n".join(texts)
    for gw in games:
        assert gw.game.home_team in combined


def test_build_post_texts_truncates_an_oversized_single_line_and_keeps_header() -> None:
    absurdly_long_forecast = "Chance Of Rain " * 40  # far longer than the post limit alone
    games = [make_game_weather("GB", "CHI", absurdly_long_forecast)]

    texts = build_post_texts(games, GAME_DATE, NFL)

    assert len(texts) == 1
    assert len(texts[0]) <= 280
    # the header (not just the truncated game line) is still present - never a header-only post
    assert "games to watch" in texts[0]
    assert "GB" in texts[0]


def test_format_game_line_shows_the_most_severe_active_alert() -> None:
    gw = make_game_weather(
        "GB",
        "CHI",
        "Snow",
        alerts=[WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
    )
    assert "Winter Storm Warning" in format_game_line(gw, NFL)


def test_format_game_line_omits_alert_text_when_there_is_no_active_alert() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny")
    line = format_game_line(gw, NFL)
    assert "Warning" not in line
    assert "Watch" not in line
    assert "Advisory" not in line


def test_format_header_pitches_games_to_watch_not_a_weather_report() -> None:
    header = format_header(GAME_DATE, NFL)
    assert "Messy NFL games to watch" in header
    assert "Weather Report" not in header


def test_format_header_omits_alert_mention_by_default() -> None:
    assert "Alerts" not in format_header(GAME_DATE, NFL)


def test_format_header_mentions_alerts_when_requested() -> None:
    assert "Alerts" in format_header(GAME_DATE, NFL, has_warning=True)


def test_build_post_texts_mentions_alerts_in_header_when_a_game_has_a_warning() -> None:
    games = [
        make_game_weather(
            "GB",
            "CHI",
            "Snow",
            alerts=[WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
        )
    ]
    texts = build_post_texts(games, GAME_DATE, NFL)
    assert "Alerts" in texts[0]


def test_build_post_texts_does_not_mention_alerts_for_a_watch_or_advisory_only() -> None:
    games = [
        make_game_weather(
            "GB",
            "CHI",
            "Sunny",
            alerts=[WeatherAlert("Heat Advisory", AlertSeverity.ADVISORY, None, None)],
        )
    ]
    texts = build_post_texts(games, GAME_DATE, NFL)
    assert "Alerts in effect" not in texts[0]


WEST_COAST = dataclasses.replace(
    NFL,
    name="west coast football",
    game_day_timezone=ZoneInfo("America/Los_Angeles"),
    timezone_label="PT",
    header_emoji="\U0001f3df️",
    header_title="Messy west coast games to watch",
    game_emoji="\U0001f3c9",
)


def test_header_and_game_line_use_the_sports_own_wording_and_timezone() -> None:
    gw = make_game_weather("GB", "CHI", "Snow")  # kickoff is 18:00 UTC == 10:00am PT

    assert format_header(GAME_DATE, WEST_COAST).startswith(
        "\U0001f3df️ Messy west coast games to watch"
    )
    line = format_game_line(gw, WEST_COAST)
    assert line.startswith("\U0001f3c9 CHI @ GB (10:00am PT)")


def test_build_post_texts_leads_with_the_sports_header() -> None:
    texts = build_post_texts([make_game_weather("GB", "CHI", "Snow")], GAME_DATE, WEST_COAST)

    assert "Messy west coast games to watch" in texts[0]
    assert "NFL" not in texts[0]


# ------------------------------------------------------------ college football lines


def make_college_weather(
    home: str,
    away: str,
    *,
    home_rank: int | None = None,
    away_rank: int | None = None,
    neutral_site: bool = False,
    short_forecast: str = "Rain",
    wind_speed_mph: float = 12.0,
    temperature_f: int = 45,
    alerts: list[WeatherAlert] | None = None,
):
    game = Game(
        home_team=home,
        away_team=away,
        kickoff=dt.datetime(2026, 1, 18, 23, 30, tzinfo=dt.UTC),
        stadium=stadium_for_team("GB"),
        home_rank=home_rank,
        away_rank=away_rank,
        neutral_site=neutral_site,
    )
    weather = WeatherReport(
        short_forecast=short_forecast,
        temperature_f=temperature_f,
        wind_speed_mph=wind_speed_mph,
        precipitation_probability=80,
    )
    return evaluate_game(game, [weather], alerts)


def test_a_college_line_numbers_each_ranked_team() -> None:
    gw = make_college_weather("Penn State", "Ohio State", home_rank=12, away_rank=4)

    assert format_game_line(gw, CFB) == (
        "🏈 #4 Ohio State @ #12 Penn State (6:30pm ET): 🌧️ Rain, 45°F (feels 39°F), 12mph wind"
    )


def test_only_the_ranked_team_gets_a_number() -> None:
    line = format_game_line(make_college_weather("Pitt", "Notre Dame", away_rank=21), CFB)

    assert line.startswith("🏈 #21 Notre Dame @ Pitt (")


def test_a_neutral_site_game_says_vs_because_neither_team_is_the_host() -> None:
    gw = make_college_weather("Texas", "Oklahoma", away_rank=6, neutral_site=True)

    assert format_game_line(gw, CFB).startswith("🏈 #6 Oklahoma vs Texas (")


def test_a_game_with_no_ranks_and_no_neutral_site_reads_exactly_like_an_nfl_line() -> None:
    gw = make_game_weather("GB", "CHI", "Snow")

    assert format_game_line(gw, NFL).startswith("🏈 CHI @ GB (1:00pm ET): ❄️ Snow")


def test_the_college_header_is_a_recommendation_of_top_25_games() -> None:
    assert (
        format_header(GAME_DATE, CFB)
        == "🌩️ Messy Top 25 college football games to watch — Sun Jan 18"
    )


def test_the_longest_realistic_college_line_stays_well_inside_a_post() -> None:
    alert = WeatherAlert(
        event="Severe Thunderstorm Warning",
        severity=AlertSeverity.WARNING,
        onset=dt.datetime(2026, 1, 18, 22, 0, tzinfo=dt.UTC),
        ends=dt.datetime(2026, 1, 19, 2, 0, tzinfo=dt.UTC),
    )
    gw = make_college_weather(
        "North Carolina",
        "Louisiana-Monroe",
        home_rank=25,
        away_rank=23,
        neutral_site=True,
        short_forecast="Showers And Thunderstorms Likely",
        temperature_f=92,
        alerts=[alert],
    )

    line = format_game_line(gw, CFB)

    assert len(line) < 200
    assert all(len(text) <= 280 for text in build_post_texts([gw] * 12, GAME_DATE, CFB))
