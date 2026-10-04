import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from messy_weather_sports_bot.nfl import NFL
from messy_weather_sports_bot.schedule import todays_game_day

PACIFIC = ZoneInfo("America/Los_Angeles")


def test_game_day_follows_the_sports_timezone_not_utc() -> None:
    # 02:30 UTC on Jan 19 is 9:30pm ET on Jan 18 - still the 18th's game day.
    late_game = dt.datetime(2026, 1, 19, 2, 30, tzinfo=dt.UTC)
    assert NFL.game_day_for(late_game) == dt.date(2026, 1, 18)


def test_game_day_uses_the_configured_timezone() -> None:
    west_coast = dataclasses.replace(NFL, game_day_timezone=PACIFIC)
    # 02:30 UTC is 6:30pm PT on the 18th, and 9:30pm ET on the 18th too; 05:30 UTC is
    # 9:30pm PT on the 18th but 12:30am ET on the 19th.
    instant = dt.datetime(2026, 1, 19, 5, 30, tzinfo=dt.UTC)
    assert west_coast.game_day_for(instant) == dt.date(2026, 1, 18)
    assert NFL.game_day_for(instant) == dt.date(2026, 1, 19)


def test_game_day_accepts_any_timezone_aware_datetime() -> None:
    instant = dt.datetime(2026, 1, 18, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    assert NFL.game_day_for(instant) == dt.date(2026, 1, 18)


def test_game_day_rejects_a_timezone_naive_datetime() -> None:
    # Assuming the machine's local timezone would silently put a game on the wrong day.
    with pytest.raises(ValueError, match="timezone-aware"):
        NFL.game_day_for(dt.datetime(2026, 1, 18, 13, 0))


def test_todays_game_day_is_the_date_in_the_sports_timezone() -> None:
    now = dt.datetime(2026, 1, 19, 3, 0, tzinfo=dt.UTC)  # 10pm ET on the 18th
    assert todays_game_day(NFL, now) == dt.date(2026, 1, 18)


def test_todays_game_day_defaults_to_the_current_time() -> None:
    assert isinstance(todays_game_day(NFL), dt.date)


def test_nfl_keeps_the_original_unprefixed_names() -> None:
    # Existing deployments set BLUESKY_HANDLE etc. and have <date>.json state files.
    assert NFL.env_prefix == ""
    assert NFL.state_key is None
