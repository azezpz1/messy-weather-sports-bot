import datetime as dt

import pytest

from messy_weather_nfl_bot.alerts import AlertSeverity, WeatherAlert
from messy_weather_nfl_bot.formatting import build_post_texts, format_game_line, format_header
from messy_weather_nfl_bot.messiness import evaluate_game
from messy_weather_nfl_bot.schedule import Game
from messy_weather_nfl_bot.stadiums import stadium_for_team
from messy_weather_nfl_bot.weather import WeatherReport

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
    line = format_game_line(gw)
    assert "CHI @ GB" in line
    assert "28°F" in line
    assert "❄️" in line  # snowflake emoji


def test_format_game_line_omits_wind_when_calm() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny", wind_speed_mph=0)
    assert "mph wind" not in format_game_line(gw)


def test_format_game_line_omits_temperature_when_missing() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny", temperature_f=None)
    assert "°F" not in format_game_line(gw)


def test_format_game_line_includes_kickoff_time_in_eastern() -> None:
    gw = make_game_weather("GB", "CHI", "Snow")  # kickoff is 18:00 UTC == 1:00pm ET
    assert "1:00pm ET" in format_game_line(gw)


def test_build_post_texts_empty_games_returns_no_posts() -> None:
    assert build_post_texts([], GAME_DATE) == []


def test_build_post_texts_rejects_max_length_too_small_for_header() -> None:
    games = [make_game_weather("GB", "CHI", "Snow")]
    with pytest.raises(ValueError, match="too small"):
        build_post_texts(games, GAME_DATE, max_length=5)


def test_build_post_texts_single_game_fits_in_one_post() -> None:
    games = [make_game_weather("GB", "CHI", "Snow")]
    texts = build_post_texts(games, GAME_DATE)
    assert len(texts) == 1
    assert "GB" in texts[0] and "CHI" in texts[0]
    assert len(texts[0]) <= 280


def test_build_post_texts_splits_into_thread_when_too_long() -> None:
    games = [
        make_game_weather(home, "MIA", "Heavy Thunderstorms with Damaging Wind Gusts Expected")
        for home in ["GB", "CHI", "KC", "DEN", "BUF", "NE", "SEA", "TB", "CAR", "PIT", "CIN", "BAL"]
    ]
    texts = build_post_texts(games, GAME_DATE)
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

    texts = build_post_texts(games, GAME_DATE)

    assert len(texts) == 1
    assert len(texts[0]) <= 280
    # the header (not just the truncated game line) is still present - never a header-only post
    assert "NFL Weather Report" in texts[0]
    assert "GB" in texts[0]


def test_format_game_line_shows_the_most_severe_active_alert() -> None:
    gw = make_game_weather(
        "GB",
        "CHI",
        "Snow",
        alerts=[WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
    )
    assert "Winter Storm Warning" in format_game_line(gw)


def test_format_game_line_omits_alert_text_when_there_is_no_active_alert() -> None:
    gw = make_game_weather("GB", "CHI", "Sunny")
    line = format_game_line(gw)
    assert "Warning" not in line
    assert "Watch" not in line
    assert "Advisory" not in line


def test_format_header_omits_alert_mention_by_default() -> None:
    assert "Alerts" not in format_header(GAME_DATE)


def test_format_header_mentions_alerts_when_requested() -> None:
    assert "Alerts" in format_header(GAME_DATE, has_warning=True)


def test_build_post_texts_mentions_alerts_in_header_when_a_game_has_a_warning() -> None:
    games = [
        make_game_weather(
            "GB",
            "CHI",
            "Snow",
            alerts=[WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
        )
    ]
    texts = build_post_texts(games, GAME_DATE)
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
    texts = build_post_texts(games, GAME_DATE)
    assert "Alerts in effect" not in texts[0]
