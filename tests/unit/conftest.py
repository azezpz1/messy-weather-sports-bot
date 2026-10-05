import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the unit suite fast - backoff timing is covered by test_retry.py. Not applied
    # to tests/integration, which should retry real APIs with their real delays.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _restore_package_logger() -> Iterator[None]:
    # `run_cli` replaces the package logger's handlers with ones writing to the current
    # stderr (and an in-memory buffer). Left in place, a later test's log records would go
    # to a stream pytest has since closed, and fail in whichever test happens to run next.
    package_logger = logging.getLogger("messy_weather_sports_bot")
    handlers, level = list(package_logger.handlers), package_logger.level
    yield
    package_logger.handlers[:] = handlers
    package_logger.setLevel(level)
