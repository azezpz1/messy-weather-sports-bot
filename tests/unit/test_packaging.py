"""The package/distribution rename touches names that nothing else exercises: the
console-script key cron jobs call, its import target, and the distribution name the
version log line looks up. These tests fail loudly if they drift apart."""

import importlib
import tomllib
from pathlib import Path

from messy_weather_sports_bot.main import DISTRIBUTION_NAME

PYPROJECT = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())


def test_distribution_name_matches_pyproject() -> None:
    # running_version() looks the version up by this name; a mismatch would silently
    # log "(unknown version)" on every run.
    assert PYPROJECT["project"]["name"] == DISTRIBUTION_NAME


def test_nfl_console_script_is_kept_and_resolves_to_a_callable() -> None:
    # Existing crontab entries run `uv run messy-weather-nfl-bot`; it must keep working.
    target = PYPROJECT["project"]["scripts"]["messy-weather-nfl-bot"]
    module_name, _, attribute = target.partition(":")

    assert callable(getattr(importlib.import_module(module_name), attribute))


def test_every_console_script_target_is_importable() -> None:
    for name, target in PYPROJECT["project"]["scripts"].items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{name} -> {target} is not callable"


def test_package_logger_name_matches_the_package() -> None:
    # Other modules log via getLogger(__name__) and rely on propagating up to this
    # logger to reach the healthcheck ping body - the name must be the package's.
    import messy_weather_sports_bot
    from messy_weather_sports_bot.main import logger

    assert logger.name == messy_weather_sports_bot.__name__
