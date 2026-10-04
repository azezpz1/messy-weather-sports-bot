import datetime as dt

import httpx
import pytest
import respx

from messy_weather_sports_bot.weather import (
    WeatherReport,
    _parse_wind_speed_mph,
    _periods_in_window,
    get_forecast,
    heat_index_f,
    wind_chill_f,
)

LAT, LON = 44.5013, -88.0622
EASTERN = dt.timezone(dt.timedelta(hours=-5))  # EST, matches the fixture period offsets below


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test suite fast - backoff timing is covered by tests/unit/test_retry.py.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


@pytest.mark.parametrize(
    ("wind_speed", "expected"),
    [
        ("10 mph", 10.0),
        ("5 to 10 mph", 10.0),
        ("0 mph", 0.0),
        ("", 0.0),
    ],
)
def test_parse_wind_speed_mph(wind_speed: str, expected: float) -> None:
    assert _parse_wind_speed_mph(wind_speed) == expected


def _mock_forecast(periods: list[dict]) -> None:
    points_payload = {
        "properties": {
            "forecastHourly": "https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly"
        }
    }
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(200, json=points_payload)
    )
    respx.get("https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly").mock(
        return_value=httpx.Response(200, json={"properties": {"periods": periods}})
    )


def _hourly_period(
    hour_start: str,
    short_forecast: str,
    temperature: int,
    wind: str,
    precip: int | None,
    is_daytime: bool = True,
) -> dict:
    start = dt.datetime.fromisoformat(hour_start)
    end = start + dt.timedelta(hours=1)
    return {
        "startTime": start.isoformat(),
        "endTime": end.isoformat(),
        "isDaytime": is_daytime,
        "shortForecast": short_forecast,
        "temperature": temperature,
        "windSpeed": wind,
        "probabilityOfPrecipitation": {"value": precip},
    }


@respx.mock
def test_get_forecast_covers_the_whole_game_not_just_kickoff() -> None:
    # Kickoff is clear, but snow rolls in an hour later - well within the game's length.
    # The bot must catch that, not just report conditions at the opening whistle.
    periods = [
        _hourly_period("2026-01-18T17:00:00-05:00", "Sunny", 35, "5 mph", 0),
        _hourly_period("2026-01-18T18:00:00-05:00", "Sunny", 34, "5 mph", 0),
        _hourly_period("2026-01-18T19:00:00-05:00", "Snow", 28, "10 to 15 mph", 80),
        _hourly_period("2026-01-18T20:00:00-05:00", "Snow", 26, "15 mph", 90),
        _hourly_period("2026-01-18T21:00:00-05:00", "Snow", 25, "15 mph", 90),
        _hourly_period("2026-01-18T22:00:00-05:00", "Clear", 24, "5 mph", 0),
    ]
    _mock_forecast(periods)
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)  # 6:00pm ET

    reports = get_forecast(LAT, LON, kickoff, game_duration=dt.timedelta(hours=3, minutes=30))

    # Covers 18:00 through 21:30 -> the 18:00, 19:00, 20:00, and 21:00 periods.
    assert [r.short_forecast for r in reports] == ["Sunny", "Snow", "Snow", "Snow"]
    assert any(r.precipitation_probability == 90 for r in reports)


@respx.mock
def test_get_forecast_handles_null_temperature_and_wind_speed() -> None:
    # NWS's hourly endpoint can report null temperature/windSpeed for a period with
    # missing data - that shouldn't blow up parsing or suppress every other report.
    periods = [
        _hourly_period("2026-01-18T18:00:00-05:00", "Sunny", 35, "5 mph", 0),
        {
            "startTime": "2026-01-18T19:00:00-05:00",
            "endTime": "2026-01-18T20:00:00-05:00",
            "isDaytime": True,
            "shortForecast": "Data Unavailable",
            "temperature": None,
            "windSpeed": None,
            "probabilityOfPrecipitation": {"value": None},
        },
    ]
    _mock_forecast(periods)
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    reports = get_forecast(LAT, LON, kickoff, game_duration=dt.timedelta(hours=2))

    assert len(reports) == 2
    incomplete = reports[1]
    assert incomplete.temperature_f is None  # not a fabricated 0°F
    assert incomplete.wind_speed_mph == 0.0
    assert incomplete.precipitation_probability is None
    assert incomplete.relative_humidity is None


@respx.mock
def test_get_forecast_reads_relative_humidity() -> None:
    period = _hourly_period("2026-01-18T18:00:00-05:00", "Sunny", 35, "5 mph", 0)
    period["relativeHumidity"] = {"unitCode": "wmoUnit:percent", "value": 72}
    _mock_forecast([period])
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    reports = get_forecast(LAT, LON, kickoff, game_duration=dt.timedelta(hours=1))

    assert reports[0].relative_humidity == 72


@respx.mock
@pytest.mark.parametrize("value", ["72", True, None])
def test_get_forecast_treats_a_non_numeric_relative_humidity_as_unreported(
    value: object,
) -> None:
    period = _hourly_period("2026-01-18T18:00:00-05:00", "Sunny", 35, "5 mph", 0)
    period["relativeHumidity"] = {"unitCode": "wmoUnit:percent", "value": value}
    _mock_forecast([period])
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    reports = get_forecast(LAT, LON, kickoff, game_duration=dt.timedelta(hours=1))

    assert reports[0].relative_humidity is None


def test_get_forecast_keeps_client_as_the_fourth_positional_argument() -> None:
    # game_duration must be keyword-only so a positional 4th argument still binds to
    # client, matching the pre-existing call signature - not to game_duration.
    import inspect

    params = list(inspect.signature(get_forecast).parameters.values())
    assert params[3].name == "client"
    assert params[3].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    game_duration_param = inspect.signature(get_forecast).parameters["game_duration"]
    assert game_duration_param.kind is inspect.Parameter.KEYWORD_ONLY


@respx.mock
def test_get_forecast_raises_if_the_game_is_past_the_last_forecast_period() -> None:
    periods = [
        _hourly_period("2026-01-18T12:00:00-05:00", "Sunny", 30, "5 mph", 0, is_daytime=True),
    ]
    _mock_forecast(periods)
    # Kickoff is a week after the returned forecast ends.
    kickoff = dt.datetime(2026, 1, 25, 13, 0, tzinfo=EASTERN)

    with pytest.raises(ValueError, match="no forecast period covers"):
        get_forecast(LAT, LON, kickoff)


def test_periods_in_window_raises_a_clear_error_on_an_empty_list() -> None:
    now = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)
    with pytest.raises(ValueError, match="no forecast periods"):
        _periods_in_window([], now, now + dt.timedelta(hours=1))


@respx.mock
def test_get_forecast_retries_a_503_from_nws_and_still_succeeds() -> None:
    # api.weather.gov intermittently returns 503s - a single transient failure shouldn't
    # drop the game from the report.
    points_payload = {
        "properties": {
            "forecastHourly": "https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly"
        }
    }
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(200, json=points_payload)
    )
    periods = [_hourly_period("2026-01-18T18:00:00-05:00", "Sunny", 35, "5 mph", 0)]
    respx.get("https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"properties": {"periods": periods}}),
        ]
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    reports = get_forecast(LAT, LON, kickoff)

    assert len(reports) == 1
    assert reports[0].short_forecast == "Sunny"


@respx.mock
def test_get_forecast_raises_after_persistent_5xx_from_nws() -> None:
    # If every retry is also a 5xx, the caller (main.run) needs a clean, catchable error
    # rather than the retry wrapper swallowing the failure forever.
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(return_value=httpx.Response(500))
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    with pytest.raises(httpx.HTTPStatusError):
        get_forecast(LAT, LON, kickoff)


@respx.mock
def test_get_forecast_raises_a_clean_error_on_a_malformed_points_response() -> None:
    # A 200 with an unexpected shape shouldn't crash with a raw KeyError - main.run's
    # per-game handler only catches httpx.HTTPError and ValueError.
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(200, json={"properties": {}})
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    with pytest.raises(ValueError, match="forecastHourly"):
        get_forecast(LAT, LON, kickoff)


@respx.mock
def test_get_forecast_raises_a_clean_error_on_a_malformed_forecast_response() -> None:
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "forecastHourly": "https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly"
                }
            },
        )
    )
    respx.get("https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly").mock(
        return_value=httpx.Response(200, json={"properties": {}})
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    with pytest.raises(ValueError, match="periods"):
        get_forecast(LAT, LON, kickoff)


@respx.mock
def test_get_forecast_raises_a_clean_error_on_a_period_missing_start_time() -> None:
    # A period that isn't a well-formed mapping with startTime/endTime would otherwise
    # raise a raw KeyError/TypeError from _periods_in_window, which main.run's per-game
    # handler doesn't catch.
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "forecastHourly": "https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly"
                }
            },
        )
    )
    respx.get("https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly").mock(
        return_value=httpx.Response(
            200, json={"properties": {"periods": [{"shortForecast": "Sunny"}]}}
        )
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    with pytest.raises(ValueError, match="startTime"):
        get_forecast(LAT, LON, kickoff)


@respx.mock
def test_get_forecast_raises_a_clean_error_on_a_non_object_period() -> None:
    respx.get(f"https://api.weather.gov/points/{LAT},{LON}").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "forecastHourly": "https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly"
                }
            },
        )
    )
    respx.get("https://api.weather.gov/gridpoints/GRB/1,1/forecast/hourly").mock(
        return_value=httpx.Response(200, json={"properties": {"periods": ["not an object"]}})
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    with pytest.raises(ValueError, match="startTime"):
        get_forecast(LAT, LON, kickoff)


# Highmark Stadium's coordinates, as stored in stadiums.py - NWS only accepts 4 decimal
# places in a point and 301-redirects anything more precise to the rounded point.
BUF_LAT, BUF_LON = 42.77306, -78.79222


@respx.mock
def test_get_forecast_rounds_an_over_precise_point_instead_of_hitting_the_redirect() -> None:
    # Regression for 2026-09-27: NWS started redirecting Buffalo's 5-decimal point, and
    # since the redirect isn't followed, Buffalo's game silently dropped out of the post.
    respx.get(f"https://api.weather.gov/points/{BUF_LAT},{BUF_LON}").mock(
        return_value=httpx.Response(301, headers={"Location": "/points/42.7731,-78.7922"})
    )
    respx.get("https://api.weather.gov/points/42.7731,-78.7922").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "forecastHourly": "https://api.weather.gov/gridpoints/BUF/1,1/forecast/hourly"
                }
            },
        )
    )
    periods = [_hourly_period("2026-01-18T18:00:00-05:00", "Snow", 25, "15 mph", 80)]
    respx.get("https://api.weather.gov/gridpoints/BUF/1,1/forecast/hourly").mock(
        return_value=httpx.Response(200, json={"properties": {"periods": periods}})
    )
    kickoff = dt.datetime(2026, 1, 18, 18, 0, tzinfo=EASTERN)

    reports = get_forecast(BUF_LAT, BUF_LON, kickoff)

    assert [report.short_forecast for report in reports] == ["Snow"]


# Expected values are read off the NWS wind chill and heat index charts.
@pytest.mark.parametrize(
    ("temperature_f", "wind_speed_mph", "expected"),
    [(30, 15, 19), (0, 10, -16), (40, 5, 36), (20, 30, 1)],
)
def test_wind_chill_matches_the_nws_chart(
    temperature_f: float, wind_speed_mph: float, expected: float
) -> None:
    assert round(wind_chill_f(temperature_f, wind_speed_mph)) == expected


@pytest.mark.parametrize(
    ("temperature_f", "relative_humidity", "expected"),
    [(90, 70, 106), (80, 40, 80), (96, 50, 108), (86, 60, 91)],
)
def test_heat_index_matches_the_nws_chart(
    temperature_f: float, relative_humidity: float, expected: float
) -> None:
    assert round(heat_index_f(temperature_f, relative_humidity)) == expected


def test_heat_index_adjusts_down_for_very_dry_air() -> None:
    # Below 13% humidity the NWS subtracts a correction from the regression - a dry
    # 100°F day feels cooler than the air temperature.
    assert heat_index_f(100, 10) < 100


def test_heat_index_adjusts_up_for_very_humid_air() -> None:
    # Above 85% humidity between 80°F and 87°F the NWS adds a correction: here 98.04
    # from the regression, plus ((90 - 85) / 10) * ((87 - 84) / 5) = 0.3.
    assert heat_index_f(84, 90) == pytest.approx(98.34, abs=0.01)


def test_heat_index_uses_the_simple_formula_when_it_is_mild() -> None:
    assert heat_index_f(70, 50) == pytest.approx(0.5 * (70 + 61 + 2 * 1.2 + 50 * 0.094))


def make_report(
    temperature_f: int | None, wind_speed_mph: float = 0.0, relative_humidity: int | None = None
) -> WeatherReport:
    return WeatherReport(
        short_forecast="Sunny",
        temperature_f=temperature_f,
        wind_speed_mph=wind_speed_mph,
        precipitation_probability=0,
        relative_humidity=relative_humidity,
    )


def test_feels_like_is_the_wind_chill_when_cold_and_windy() -> None:
    assert make_report(30, 15.0).feels_like_f == pytest.approx(wind_chill_f(30, 15))


def test_feels_like_is_the_air_temperature_when_cold_but_calm() -> None:
    # The wind chill formula isn't defined below 3 mph.
    assert make_report(30, 2.0).feels_like_f == 30


def test_feels_like_is_the_heat_index_when_hot() -> None:
    assert make_report(90, relative_humidity=70).feels_like_f == pytest.approx(heat_index_f(90, 70))


def test_feels_like_is_the_air_temperature_when_hot_with_unknown_humidity() -> None:
    assert make_report(90).feels_like_f == 90


def test_feels_like_is_the_air_temperature_when_mild() -> None:
    assert make_report(65, 20.0, 90).feels_like_f == 65


def test_feels_like_is_none_without_a_temperature() -> None:
    assert make_report(None, 20.0).feels_like_f is None
