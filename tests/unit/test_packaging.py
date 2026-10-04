"""The package/distribution rename touches names that nothing else exercises: the
console-script key cron jobs call, its import target, and the distribution name the
version log line looks up. These tests fail loudly if they drift apart."""

import importlib
import logging
import pkgutil
import tomllib
from pathlib import Path

import messy_weather_sports_bot
from messy_weather_sports_bot.cli import DISTRIBUTION_NAME, logger

PYPROJECT = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())


def test_distribution_name_matches_pyproject() -> None:
    # running_version() looks the version up by this name; a mismatch would silently
    # log "(unknown version)" on every run.
    assert PYPROJECT["project"]["name"] == DISTRIBUTION_NAME


def test_nfl_console_script_is_kept() -> None:
    # Existing crontab entries run `uv run messy-weather-nfl-bot`; it must keep working
    # (test_every_console_script_target_is_importable checks that it resolves).
    assert "messy-weather-nfl-bot" in PYPROJECT["project"]["scripts"]


def test_every_console_script_target_is_importable() -> None:
    for name, target in PYPROJECT["project"]["scripts"].items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{name} -> {target} is not callable"


def test_package_logger_name_matches_the_package() -> None:
    # Other modules log via getLogger(__name__) and rely on propagating up to this
    # logger to reach the healthcheck ping body - the name must be the package's.
    assert logger.name == messy_weather_sports_bot.__name__


def test_every_module_logger_is_under_the_package_logger() -> None:
    # A module whose logger isn't named under the package (a stale hard-coded name, a
    # bare getLogger("x")) would never reach the handlers attached to the package logger,
    # so its lines would silently be missing from the healthcheck ping body.
    package = messy_weather_sports_bot.__name__
    for module_info in pkgutil.walk_packages(messy_weather_sports_bot.__path__, f"{package}."):
        module_logger = getattr(importlib.import_module(module_info.name), "logger", None)
        if isinstance(module_logger, logging.Logger):
            assert module_logger.name.startswith(package), (
                f"{module_info.name}.logger is named {module_logger.name!r}"
            )
