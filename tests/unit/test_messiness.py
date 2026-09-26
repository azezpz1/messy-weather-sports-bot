import datetime as dt

import pytest

from messy_weather_nfl_bot.alerts import AlertSeverity, WeatherAlert
from messy_weather_nfl_bot.messiness import (
    Condition,
    classify_condition,
    evaluate_game,
    messiness_score,
    sort_by_messiness,
)
from messy_weather_nfl_bot.schedule import Game
from messy_weather_nfl_bot.stadiums import stadium_for_team
from messy_weather_nfl_bot.weather import WeatherReport


def make_game(home: str = "BUF", away: str = "MIA") -> Game:
    return Game(
        home_team=home,
        away_team=away,
        kickoff=dt.datetime(2026, 1, 18, 18, 0, tzinfo=dt.UTC),
        stadium=stadium_for_team(home),
    )


def make_weather(
    short_forecast: str = "Sunny",
    temperature_f: int | None = 65,
    wind_speed_mph: float = 5.0,
    precipitation_probability: int | None = 0,
) -> WeatherReport:
    return WeatherReport(
        short_forecast=short_forecast,
        temperature_f=temperature_f,
        wind_speed_mph=wind_speed_mph,
        precipitation_probability=precipitation_probability,
    )


@pytest.mark.parametrize(
    ("short_forecast", "expected"),
    [
        ("Snow likely", Condition.SNOW),
        ("Chance of Flurries", Condition.SNOW),
        ("Wintry Mix", Condition.SNOW),
        ("Thunderstorms Likely", Condition.THUNDERSTORM),
        ("Chance Rain Showers", Condition.RAIN),
        ("Patchy Fog", Condition.FOG),
        ("Sunny", Condition.CLEAR),
    ],
)
def test_classify_condition_from_text(short_forecast: str, expected: Condition) -> None:
    weather = make_weather(short_forecast=short_forecast)
    assert classify_condition(weather) == expected


def test_classify_condition_high_wind_without_precip_text() -> None:
    weather = make_weather(short_forecast="Sunny", wind_speed_mph=30.0)
    assert classify_condition(weather) == Condition.WIND


def test_classify_condition_extreme_cold() -> None:
    weather = make_weather(short_forecast="Sunny", temperature_f=10)
    assert classify_condition(weather) == Condition.EXTREME_COLD


def test_classify_condition_extreme_heat() -> None:
    weather = make_weather(short_forecast="Sunny", temperature_f=100)
    assert classify_condition(weather) == Condition.EXTREME_HEAT


def test_snow_keyword_takes_priority_over_wind_and_temperature() -> None:
    weather = make_weather(short_forecast="Snow", wind_speed_mph=40.0, temperature_f=5)
    assert classify_condition(weather) == Condition.SNOW


def test_messiness_score_increases_with_precip_wind_and_temp_extremity() -> None:
    calm = make_weather(wind_speed_mph=0, temperature_f=65, precipitation_probability=0)
    messy = make_weather(wind_speed_mph=30, temperature_f=20, precipitation_probability=80)
    assert messiness_score(messy, classify_condition(messy)) > messiness_score(
        calm, classify_condition(calm)
    )


def test_sort_by_messiness_snow_always_first_even_with_lower_score() -> None:
    # A mild snow game should still rank above a severe (but snow-less) storm.
    snow = evaluate_game(
        make_game("GB"),
        [make_weather(short_forecast="Light Snow", temperature_f=30, wind_speed_mph=2)],
    )
    storm = evaluate_game(
        make_game("KC"),
        [
            make_weather(
                short_forecast="Thunderstorms",
                wind_speed_mph=45,
                temperature_f=95,
                precipitation_probability=100,
            )
        ],
    )
    assert storm.score > snow.score

    ranked = sort_by_messiness([storm, snow])
    assert ranked[0].condition == Condition.SNOW
    assert ranked[1].condition == Condition.THUNDERSTORM


def test_sort_by_messiness_orders_non_snow_games_by_score_descending() -> None:
    mild = evaluate_game(make_game("GB"), [make_weather(short_forecast="Sunny")])
    windy = evaluate_game(
        make_game("KC"), [make_weather(short_forecast="Windy", wind_speed_mph=35)]
    )

    ranked = sort_by_messiness([mild, windy])
    assert [gw.condition for gw in ranked] == [Condition.WIND, Condition.CLEAR]


def test_evaluate_game_picks_the_messiest_period_not_the_first() -> None:
    # Kickoff is calm, but the game turns messy later - evaluate_game must surface that
    # worst moment rather than just reporting conditions at kickoff.
    calm_at_kickoff = make_weather(short_forecast="Sunny", wind_speed_mph=2, temperature_f=65)
    snow_later = make_weather(
        short_forecast="Snow", wind_speed_mph=20, temperature_f=25, precipitation_probability=90
    )

    result = evaluate_game(make_game("GB"), [calm_at_kickoff, snow_later])

    assert result.condition == Condition.SNOW
    assert result.weather == snow_later


def test_evaluate_game_flags_has_snow_even_when_a_later_period_scores_higher() -> None:
    # Mild snow early, then a much stormier period that outscores it - the displayed
    # report should be the messier storm, but has_snow must still reflect the snow.
    mild_snow = make_weather(short_forecast="Light Snow", temperature_f=30, wind_speed_mph=2)
    bigger_storm = make_weather(
        short_forecast="Thunderstorms",
        wind_speed_mph=45,
        temperature_f=95,
        precipitation_probability=100,
    )

    result = evaluate_game(make_game("GB"), [mild_snow, bigger_storm])

    assert result.condition == Condition.THUNDERSTORM
    assert result.has_snow is True


def test_classify_condition_missing_temperature_is_not_extreme() -> None:
    # A missing temperature must not be treated as a measured 0°F (extreme cold).
    weather = make_weather(short_forecast="Sunny", temperature_f=None)
    assert classify_condition(weather) == Condition.CLEAR


def test_messiness_score_excludes_missing_temperature_from_extremity() -> None:
    missing = make_weather(short_forecast="Sunny", temperature_f=None, wind_speed_mph=0)
    calm_at_comfortable_temp = make_weather(
        short_forecast="Sunny", temperature_f=65, wind_speed_mph=0
    )
    assert messiness_score(missing, classify_condition(missing)) == messiness_score(
        calm_at_comfortable_temp, classify_condition(calm_at_comfortable_temp)
    )


def test_sort_by_messiness_keeps_snow_first_when_a_stormier_period_scores_higher() -> None:
    # The game has snow at some point, but a later thunderstorm period scores higher and
    # becomes the displayed condition - it must still rank ahead of a snow-less game.
    mixed_snow_and_storm = evaluate_game(
        make_game("GB"),
        [
            make_weather(short_forecast="Light Snow", temperature_f=30, wind_speed_mph=2),
            make_weather(
                short_forecast="Thunderstorms",
                wind_speed_mph=45,
                temperature_f=95,
                precipitation_probability=100,
            ),
        ],
    )
    clear = evaluate_game(make_game("KC"), [make_weather(short_forecast="Sunny")])

    ranked = sort_by_messiness([clear, mixed_snow_and_storm])

    assert ranked[0] is mixed_snow_and_storm


def test_evaluate_game_with_no_alerts_matches_the_no_alerts_argument_case() -> None:
    weather = make_weather()
    assert (
        evaluate_game(make_game("GB"), [weather]).score
        == evaluate_game(make_game("GB"), [weather], []).score
    )


def test_evaluate_game_adds_a_score_bonus_for_an_active_alert() -> None:
    weather = make_weather()
    plain = evaluate_game(make_game("GB"), [weather])
    warned = evaluate_game(
        make_game("GB"),
        [weather],
        [WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
    )

    assert warned.score > plain.score
    assert warned.alert is not None
    assert warned.alert.event == "Winter Storm Warning"


def test_evaluate_game_warning_bonus_exceeds_watch_which_exceeds_advisory() -> None:
    weather = make_weather()
    warning = evaluate_game(
        make_game("GB"),
        [weather],
        [WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None)],
    )
    watch = evaluate_game(
        make_game("GB"),
        [weather],
        [WeatherAlert("Severe Thunderstorm Watch", AlertSeverity.WATCH, None, None)],
    )
    advisory = evaluate_game(
        make_game("GB"),
        [weather],
        [WeatherAlert("Heat Advisory", AlertSeverity.ADVISORY, None, None)],
    )

    assert warning.score > watch.score > advisory.score


def test_evaluate_game_picks_the_most_severe_of_several_active_alerts() -> None:
    weather = make_weather()
    result = evaluate_game(
        make_game("GB"),
        [weather],
        [
            WeatherAlert("Heat Advisory", AlertSeverity.ADVISORY, None, None),
            WeatherAlert("Winter Storm Warning", AlertSeverity.WARNING, None, None),
        ],
    )

    assert result.alert is not None
    assert result.alert.event == "Winter Storm Warning"


def test_evaluate_game_with_no_active_alerts_has_no_alert() -> None:
    result = evaluate_game(make_game("GB"), [make_weather()], [])
    assert result.alert is None
