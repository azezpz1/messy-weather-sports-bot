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
    weather = make_weather(short_forecast=short_forecast, precipitation_probability=80)
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
    weather = make_weather(
        short_forecast="Snow", wind_speed_mph=40.0, temperature_f=5, precipitation_probability=80
    )
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
        [
            make_weather(
                short_forecast="Light Snow",
                temperature_f=30,
                wind_speed_mph=2,
                precipitation_probability=40,
            )
        ],
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
    mild_snow = make_weather(
        short_forecast="Light Snow",
        temperature_f=30,
        wind_speed_mph=2,
        precipitation_probability=40,
    )
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
            make_weather(
                short_forecast="Light Snow",
                temperature_f=30,
                wind_speed_mph=2,
                precipitation_probability=40,
            ),
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


# Regression coverage for 2026-09-27, when every outdoor game got posted - mostly sunny
# and "Slight Chance Rain Showers" games that weren't worth anyone's attention. Each
# forecast below is one that game day actually logged.


@pytest.mark.parametrize(
    "weather",
    [
        pytest.param(make_weather("Sunny", 68, 13.0, 1), id="CAR@CLE sunny, 13mph"),
        pytest.param(make_weather("Mostly Sunny", 84, 3.0, 2), id="KC@MIA mostly sunny, 84F"),
        pytest.param(make_weather("Sunny", 69, 15.0, 0), id="CIN@PIT sunny, 15mph"),
        pytest.param(make_weather("Sunny", 87, 7.0, 3), id="NE@JAX sunny, 87F"),
        pytest.param(
            make_weather("Slight Chance Rain Showers", 64, 13.0, 20),
            id="LAC@BUF slight chance of showers",
        ),
        pytest.param(
            make_weather("Slight Chance Rain Showers", 81, 9.0, 18),
            id="LAR@DEN slight chance of showers",
        ),
    ],
)
def test_nice_weather_game_is_not_messy(weather: WeatherReport) -> None:
    result = evaluate_game(make_game(), [weather])
    assert result.is_messy is False


@pytest.mark.parametrize(
    "weather",
    [
        pytest.param(make_weather("Rain Showers", 66, 16.0, 90), id="TEN@NYG rain showers"),
        pytest.param(make_weather("Light Rain Likely", 62, 17.0, 70), id="SEA@WSH rain likely"),
    ],
)
def test_rainy_game_is_messy(weather: WeatherReport) -> None:
    result = evaluate_game(make_game(), [weather])
    assert result.is_messy is True
    assert result.condition == Condition.RAIN


@pytest.mark.parametrize(
    ("short_forecast", "precipitation_probability"),
    [
        ("Slight Chance Rain Showers", 20),
        ("Chance Rain Showers", 40),
        ("Slight Chance Showers And Thunderstorms", 20),
        ("Slight Chance Snow Showers", 20),
    ],
)
def test_low_odds_precipitation_is_classified_clear(
    short_forecast: str, precipitation_probability: int
) -> None:
    # A 20% shower chance is most Southern afternoons in September - the forecast text
    # naming rain isn't enough on its own to make a game messy.
    weather = make_weather(
        short_forecast=short_forecast, precipitation_probability=precipitation_probability
    )
    assert classify_condition(weather) == Condition.CLEAR


def test_likely_rain_is_classified_rain() -> None:
    weather = make_weather(short_forecast="Chance Rain Showers", precipitation_probability=50)
    assert classify_condition(weather) == Condition.RAIN


def test_likely_thunderstorms_are_classified_thunderstorm() -> None:
    weather = make_weather(short_forecast="Thunderstorms Likely", precipitation_probability=60)
    assert classify_condition(weather) == Condition.THUNDERSTORM


def test_snow_counts_at_lower_odds_than_rain() -> None:
    # Snow games are rare enough to flag at a "Chance" (30%+), where rain needs 50%+.
    snow = make_weather(short_forecast="Chance Snow Showers", precipitation_probability=30)
    rain = make_weather(short_forecast="Chance Rain Showers", precipitation_probability=30)
    assert classify_condition(snow) == Condition.SNOW
    assert classify_condition(rain) == Condition.CLEAR


def test_precipitation_text_with_no_reported_odds_is_trusted() -> None:
    weather = make_weather(short_forecast="Rain", precipitation_probability=None)
    assert classify_condition(weather) == Condition.RAIN


def test_low_odds_rain_still_classifies_as_wind_when_windy() -> None:
    weather = make_weather(
        short_forecast="Slight Chance Rain Showers", wind_speed_mph=25, precipitation_probability=20
    )
    assert classify_condition(weather) == Condition.WIND


@pytest.mark.parametrize(
    "weather",
    [
        pytest.param(make_weather("Sunny", 65, 20.0, 0), id="high wind"),
        pytest.param(make_weather("Sunny", 25, 5.0, 0), id="extreme cold"),
        pytest.param(make_weather("Sunny", 97, 5.0, 0), id="extreme heat"),
        pytest.param(make_weather("Patchy Fog", 50, 3.0, 0), id="fog"),
        pytest.param(make_weather("Snow Likely", 30, 5.0, 60), id="snow"),
    ],
)
def test_other_messy_conditions_are_messy(weather: WeatherReport) -> None:
    assert evaluate_game(make_game(), [weather]).is_messy is True


def test_an_alert_alone_does_not_make_a_nice_weather_game_messy() -> None:
    # Point alerts include things like Small Craft Advisories and Rip Current Statements,
    # which say nothing about the game - the forecast itself has to be messy.
    result = evaluate_game(
        make_game(),
        [make_weather()],
        [WeatherAlert("Small Craft Advisory", AlertSeverity.ADVISORY, None, None)],
    )
    assert result.is_messy is False


def test_evaluate_game_shows_the_messy_period_over_a_higher_scoring_nice_one() -> None:
    # The low-odds shower hour outscores the windy hour on raw numbers, but only the
    # windy hour is actually messy - that's the reason the game is posted, so it's
    # the one the post should show.
    showery = make_weather(
        "Slight Chance Rain Showers",
        temperature_f=94,
        wind_speed_mph=5,
        precipitation_probability=24,
    )
    windy = make_weather("Sunny", temperature_f=65, wind_speed_mph=20, precipitation_probability=0)
    assert messiness_score(showery, classify_condition(showery)) > messiness_score(
        windy, classify_condition(windy)
    )

    result = evaluate_game(make_game(), [showery, windy])

    assert result.is_messy is True
    assert result.weather == windy


def test_has_snow_ignores_low_odds_snow() -> None:
    result = evaluate_game(make_game(), [make_weather("Slight Chance Snow Showers", 33, 5.0, 20)])
    assert result.has_snow is False
