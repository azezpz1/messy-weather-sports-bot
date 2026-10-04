import pytest


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the unit suite fast - backoff timing is covered by test_retry.py. Not applied
    # to tests/integration, which should retry real APIs with their real delays.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)
