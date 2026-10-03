"""Classify, score, and sort game-day weather by how "messy" it is."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from messy_weather_nfl_bot.alerts import SEVERITY_SCORE_BONUS, WeatherAlert, most_severe
from messy_weather_nfl_bot.schedule import Game
from messy_weather_nfl_bot.weather import WeatherReport

HIGH_WIND_MPH = 20.0
# Extreme cold and heat are judged on how it feels (WeatherReport.feels_like_f), not the
# air temperature. Wind chill drops a few degrees below the air temperature with almost
# any breeze, so the cold bar sits below freezing to keep a still 35°F November day from
# counting. The heat bar is where NWS's heat index chart turns from "Extreme Caution"
# to "Danger" - a humid 90°F afternoon reads as ~100°F, which is every early-September
# game in Florida.
EXTREME_COLD_F = 25
EXTREME_HEAT_F = 103
COMFORTABLE_LOW_F = 40
COMFORTABLE_HIGH_F = 80


class Condition(Enum):
    ICE = "ice"
    SNOW = "snow"
    THUNDERSTORM = "thunderstorm"
    RAIN = "rain"
    FOG = "fog"
    WIND = "wind"
    EXTREME_COLD = "extreme_cold"
    EXTREME_HEAT = "extreme_heat"
    CLEAR = "clear"


EMOJI: dict[Condition, str] = {
    Condition.ICE: "🧊",
    Condition.SNOW: "❄️",
    Condition.THUNDERSTORM: "⛈️",
    Condition.RAIN: "🌧️",
    Condition.FOG: "🌫️",
    Condition.WIND: "💨",
    Condition.EXTREME_COLD: "🥶",
    Condition.EXTREME_HEAT: "🥵",
    Condition.CLEAR: "☀️",
}

# Snow and ice carry most of the weight: a near-certain snow game should outrank almost
# anything else, but a precipitation bonus is scaled by its chance (see
# messiness_score), so a few possible flurries don't outrank a likely, windy storm.
_CONDITION_SCORE_BONUS: dict[Condition, float] = {
    Condition.ICE: 110.0,
    Condition.SNOW: 100.0,
    Condition.THUNDERSTORM: 40.0,
    Condition.RAIN: 15.0,
    Condition.FOG: 10.0,
    Condition.WIND: 10.0,
    Condition.EXTREME_COLD: 20.0,
    Condition.EXTREME_HEAT: 15.0,
    Condition.CLEAR: 0.0,
}

# The chance of precipitation (percent) a period needs before ice, snow, thunderstorms,
# or rain in its forecast text counts. NWS words 15-24% as "Slight Chance" and 30-50%
# as "Chance", and a slight chance of an afternoon shower is most Southern game days in
# September - without a floor, nearly every game reads as rainy. Ice and snow are rare
# enough to be worth flagging from a "Chance" up; rain and storms need to be at least a
# coin flip.
MIN_PRECIP_PROBABILITY: dict[Condition, int] = {
    Condition.ICE: 30,
    Condition.SNOW: 30,
    Condition.THUNDERSTORM: 50,
    Condition.RAIN: 50,
}

# Extra (chance-scaled) bonus for precipitation the forecast calls out as heavy or
# wind-driven - "Heavy Rain" and "Blowing Snow" are messier than plain rain and snow.
INTENSE_PRECIP_BONUS = 25.0
_INTENSE_PRECIP_KEYWORDS = ("heavy", "blowing", "blizzard")

# Checked in this order, so a mixed forecast takes its messiest part: "Rain And Sleet"
# is ICE, "Rain And Snow" is SNOW, "Showers And Thunderstorms" is THUNDERSTORM. Ice comes
# before rain because "freezing rain" contains "rain". Plain "freezing" isn't an ice
# keyword - "Freezing Fog" is fog. Haze isn't fog: it rarely affects a game.
_ICE_KEYWORDS = ("freezing rain", "freezing drizzle", "sleet", "ice pellets")
_SNOW_KEYWORDS = ("snow", "blizzard", "flurries", "wintry mix")
_THUNDERSTORM_KEYWORDS = ("thunderstorm",)
_RAIN_KEYWORDS = ("rain", "showers", "drizzle")
_FOG_KEYWORDS = ("fog", "mist")

_PRECIP_KEYWORDS: tuple[tuple[Condition, tuple[str, ...]], ...] = (
    (Condition.ICE, _ICE_KEYWORDS),
    (Condition.SNOW, _SNOW_KEYWORDS),
    (Condition.THUNDERSTORM, _THUNDERSTORM_KEYWORDS),
    (Condition.RAIN, _RAIN_KEYWORDS),
)


@dataclass(frozen=True)
class GameWeather:
    game: Game
    weather: WeatherReport
    condition: Condition
    score: float
    alert: WeatherAlert | None
    """The most severe active NWS alert (Warning > Watch > Advisory) overlapping the
    game, or None if there isn't one."""

    @property
    def is_messy(self) -> bool:
        """Whether this game is worth telling followers to watch. Only the forecast
        decides this - an alert alone (a Small Craft Advisory, a Rip Current Statement)
        doesn't make a sunny game messy, though it still raises a messy game's rank."""
        return self.condition is not Condition.CLEAR


def _precip_likely(weather: WeatherReport, condition: Condition) -> bool:
    # With no reported odds, trust the forecast text rather than ignore what it names.
    probability = weather.precipitation_probability
    return probability is None or probability >= MIN_PRECIP_PROBABILITY[condition]


def classify_condition(weather: WeatherReport) -> Condition:
    """The messiest condition in `weather`, or CLEAR if nothing about it is messy.
    Precipitation below its MIN_PRECIP_PROBABILITY is ignored, falling through to the
    wind and temperature checks - a 20% shower chance with 25mph wind is a WIND game.
    Cold and heat are judged on the wind chill / heat index, not the air temperature."""
    forecast = weather.short_forecast.lower()

    for condition, keywords in _PRECIP_KEYWORDS:
        if any(keyword in forecast for keyword in keywords) and _precip_likely(weather, condition):
            return condition
    if any(keyword in forecast for keyword in _FOG_KEYWORDS):
        return Condition.FOG
    if weather.wind_speed_mph >= HIGH_WIND_MPH:
        return Condition.WIND
    feels_like = weather.feels_like_f
    if feels_like is not None and feels_like <= EXTREME_COLD_F:
        return Condition.EXTREME_COLD
    if feels_like is not None and feels_like >= EXTREME_HEAT_F:
        return Condition.EXTREME_HEAT
    return Condition.CLEAR


def messiness_score(weather: WeatherReport, condition: Condition) -> float:
    """Higher is messier. Combines precipitation odds, wind, how far the feels-like
    temperature is from comfortable, and a bonus for the classified condition. A
    precipitation condition's bonus (plus any heavy/blowing bonus) is scaled by its
    chance, so a 30% chance of snow earns 30% of the snow bonus; with no reported
    chance, the forecast text is trusted and the full bonus applies. A missing
    temperature contributes no extremity rather than being scored as if it were a
    measured 0°F."""
    precip_component = weather.precipitation_probability or 0
    wind_component = weather.wind_speed_mph * 1.5
    temp_extremity = 0.0
    feels_like = weather.feels_like_f
    if feels_like is not None:
        temp_extremity = max(0.0, COMFORTABLE_LOW_F - feels_like) + max(
            0.0, feels_like - COMFORTABLE_HIGH_F
        )
    bonus = _CONDITION_SCORE_BONUS[condition]
    if condition in MIN_PRECIP_PROBABILITY:
        forecast = weather.short_forecast.lower()
        if any(keyword in forecast for keyword in _INTENSE_PRECIP_KEYWORDS):
            bonus += INTENSE_PRECIP_BONUS
        if weather.precipitation_probability is not None:
            bonus *= weather.precipitation_probability / 100
    return precip_component + wind_component + temp_extremity + bonus


def evaluate_game(
    game: Game,
    weather_reports: list[WeatherReport],
    alerts: list[WeatherAlert] | None = None,
) -> GameWeather:
    """Evaluate every forecast period spanning the game and report the messiest one -
    weather can turn ugly well after kickoff, so the worst point in the game is what
    matters, not just the conditions at the opening whistle. A messy period always wins
    over a non-messy one, even a higher-scoring one, so a posted game shows the weather
    that got it posted. `alerts` should already be filtered to ones overlapping the
    game; the most severe one adds a flat bonus to the score, on top of whichever period
    turns out messiest."""
    alert = most_severe(alerts or [])
    alert_bonus = SEVERITY_SCORE_BONUS[alert.severity] if alert is not None else 0.0
    scored = [
        (condition, weather, messiness_score(weather, condition) + alert_bonus)
        for weather in weather_reports
        for condition in (classify_condition(weather),)
    ]

    # weather_reports is always non-empty (see get_forecast), so max() has something.
    condition, weather, score = max(
        scored, key=lambda entry: (entry[0] is not Condition.CLEAR, entry[2])
    )
    return GameWeather(
        game=game,
        weather=weather,
        condition=condition,
        score=score,
        alert=alert,
    )


def sort_by_messiness(games: list[GameWeather]) -> list[GameWeather]:
    """Messiest first, by score. Snow and ice don't jump the queue on their own -
    their heavy, chance-scaled weight in the score is what usually puts them on top."""
    return sorted(games, key=lambda gw: -gw.score)
