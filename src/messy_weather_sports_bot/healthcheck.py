"""Report run start/end to a Healthchecks.io-compatible dead-man's-switch endpoint,
configured via the HEALTHCHECK_URL environment variable (see the README). A missed
check-in - the whole point of the service - catches failures the bot can't report
itself, like the Pi being off or a broken cron environment.

A failed ping is logged but never raises, so a flaky healthcheck endpoint can never
change the run's outcome.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from messy_weather_sports_bot.retry import request_with_retry

logger = logging.getLogger(__name__)

ENV_VAR = "HEALTHCHECK_URL"

PING_TIMEOUT = 10.0
PING_MAX_ATTEMPTS = 2


def new_run_id() -> str:
    """A fresh id pairing a run's start and end pings (the `rid` query param)."""
    return str(uuid.uuid4())


def _redact(url: str) -> str:
    """`url`'s path/UUID doubles as a bearer credential for the check (anyone who has
    it can forge a check-in) - never let it reach the logs."""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{parsed.host}/***"
    except Exception:
        return "<healthcheck URL>"


def _describe_error(exc: BaseException) -> str:
    """A summary of `exc` safe to log - deliberately dropping its own message, since
    e.g. `httpx.HTTPStatusError` embeds the full (secret-bearing) request URL in it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


# Healthchecks.io returns 200 even for a ping it didn't record (e.g. an unrecognized
# check UUID), rather than a 4xx, so raise_for_status() alone can't catch a mistyped
# HEALTHCHECK_URL - only the body says the ping was ignored.
_IGNORED_PING_BODY = "not found"


def _ping(url: str, run_id: str, *, method: str = "GET", body: str | None = None) -> None:
    if not url.lower().startswith("https://"):
        logger.warning("Healthcheck URL %s isn't HTTPS; pinging over plaintext.", _redact(url))

    try:
        with httpx.Client(timeout=PING_TIMEOUT) as client:

            def _send() -> httpx.Response:
                response = client.request(method, url, params={"rid": run_id}, content=body)
                response.raise_for_status()
                return response

            response = request_with_retry(
                _send, max_attempts=PING_MAX_ATTEMPTS, describe_exception=_describe_error
            )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # A malformed HEALTHCHECK_URL (InvalidURL) or any transport/status failure -
        # neither should ever take the whole run down with it.
        logger.warning("Healthcheck ping to %s failed: %s", _redact(url), _describe_error(exc))
        return

    if response.text.strip().lower() == _IGNORED_PING_BODY:
        # A 2xx that isn't the run's problem to report as a failure - but worth
        # surfacing, since a stale/mistyped check UUID would otherwise ping "green"
        # forever without ever actually reaching Healthchecks.
        logger.warning(
            "Healthcheck ping to %s was ignored (check UUID not recognized) - "
            "double check HEALTHCHECK_URL.",
            _redact(url),
        )


def ping_start(base_url: str, run_id: str) -> None:
    """Ping that a run has started. Call this before doing any other work."""
    _ping(f"{base_url.rstrip('/')}/start", run_id)


def ping_end(base_url: str, run_id: str, exit_code: int, body: str) -> None:
    """Ping that a run has finished with `exit_code` (0 is success, anything else is a
    failure), carrying `body` (a run summary plus log tail) as the ping's payload."""
    _ping(f"{base_url.rstrip('/')}/{exit_code}", run_id, method="POST", body=body)
