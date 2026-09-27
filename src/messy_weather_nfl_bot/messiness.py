"""Classify, score, and sort game-day weather by how "messy" it is."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from messy_weather_nfl_bot.alerts import SEVERITY_SCORE_BONUS, WeatherAlert, most_severe
from messy_weather_nfl_bot.schedule import Game
from messy_weather_nfl_bot.weather import WeatherReport

HIGH_WIND_MPH = 20.0
EXTREME_COLD_F = 32
EXTREME_HEAT_F = 95
COMFORTABLE_LOW_F = 40
COMFORTABLE_HIGH_F = 80


class Condition(Enum):
    SNOW = "snow"
    THUNDERSTORM = "thunderstorm"
    RAIN = "rain"
    FOG = "fog"
    WIND = "wind"
    EXTREME_COLD = "extreme_cold"
    EXTREME_HEAT = "extreme_heat"
    CLEAR = "clear"


EMOJI: dict[Condition, str] = {
    Condition.SNOW: "❄️",
    Condition.THUNDERSTORM: "⛈️",
    Condition.RAIN: "🌧️",
    Condition.FOG: "🌫️",
    Condition.WIND: "💨",
    Condition.EXTREME_COLD: "🥶",
    Condition.EXTREME_HEAT: "🥵",
    Condition.CLEAR: "☀️",
}

_CONDITION_SCORE_BONUS: dict[Condition, float] = {
    Condition.SNOW: 50.0,
    Condition.THUNDERSTORM: 30.0,
    Condition.RAIN: 15.0,
    Condition.FOG: 10.0,
    Condition.WIND: 10.0,
    Condition.EXTREME_COLD: 20.0,
    Condition.EXTREME_HEAT: 15.0,
    Condition.CLEAR: 0.0,
}

# The chance of precipitation (percent) a period needs before snow, thunderstorms, or
# rain in its forecast text counts. NWS words 15-24% as "Slight Chance" and 30-50% as
# "Chance", and a slight chance of an afternoon shower is most Southern game days in
# September - without a floor, nearly every game reads as rainy. Snow is rare enough to
# be worth flagging from a "Chance" up; rain and storms need to be at least a coin flip.
MIN_PRECIP_PROBABILITY: dict[Condition, int] = {
    Condition.SNOW: 30,
    Condition.THUNDERSTORM: 50,
    Condition.RAIN: 50,
}

_SNOW_KEYWORDS = ("snow", "blizzard", "flurries", "sleet", "wintry mix")
_THUNDERSTORM_KEYWORDS = ("thunderstorm",)
_RAIN_KEYWORDS = ("rain", "showers", "drizzle")
_FOG_KEYWORDS = ("fog", "mist", "haze")


@dataclass(frozen=True)
class GameWeather:
    game: Game
    weather: WeatherReport
    condition: Condition
    score: float
    has_snow: bool
    """Whether snow appears in any period across the game, even if a later, higher-scoring
    period is what's shown as `weather`/`condition` - used to keep snow games ranked
    first regardless of which period ends up being the messiest one."""
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
    wind and temperature checks - a 20% shower chance with 25mph wind is a WIND game."""
    forecast = weather.short_forecast.lower()

    for condition, keywords in (
        (Condition.SNOW, _SNOW_KEYWORDS),
        (Condition.THUNDERSTORM, _THUNDERSTORM_KEYWORDS),
        (Condition.RAIN, _RAIN_KEYWORDS),
    ):
        if any(keyword in forecast for keyword in keywords) and _precip_likely(weather, condition):
            return condition
    if any(keyword in forecast for keyword in _FOG_KEYWORDS):
        return Condition.FOG
    if weather.wind_speed_mph >= HIGH_WIND_MPH:
        return Condition.WIND
    if weather.temperature_f is not None and weather.temperature_f <= EXTREME_COLD_F:
        return Condition.EXTREME_COLD
    if weather.temperature_f is not None and weather.temperature_f >= EXTREME_HEAT_F:
        return Condition.EXTREME_HEAT
    return Condition.CLEAR


def messiness_score(weather: WeatherReport, condition: Condition) -> float:
    """Higher is messier. Combines precipitation odds, wind, temperature extremity,
    and a bonus for the classified condition. A missing temperature contributes no
    extremity rather than being scored as if it were a measured 0°F."""
    precip_component = weather.precipitation_probability or 0
    wind_component = weather.wind_speed_mph * 1.5
    temp_extremity = 0.0
    if weather.temperature_f is not None:
        temp_extremity = max(0, COMFORTABLE_LOW_F - weather.temperature_f) + max(
            0, weather.temperature_f - COMFORTABLE_HIGH_F
        )
    return precip_component + wind_component + temp_extremity + _CONDITION_SCORE_BONUS[condition]


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
    has_snow = any(condition is Condition.SNOW for condition, _, _ in scored)

    # weather_reports is always non-empty (see get_forecast), so max() has something.
    condition, weather, score = max(
        scored, key=lambda entry: (entry[0] is not Condition.CLEAR, entry[2])
    )
    return GameWeather(
        game=game,
        weather=weather,
        condition=condition,
        score=score,
        has_snow=has_snow,
        alert=alert,
    )


def sort_by_messiness(games: list[GameWeather]) -> list[GameWeather]:
    """Games with snow at any point sort first, then everything else by messiness score
    descending - even if a later, stormier period outscored the snow for display."""
    return sorted(games, key=lambda gw: (not gw.has_snow, -gw.score))
