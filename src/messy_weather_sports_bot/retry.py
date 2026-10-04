"""Retry transient httpx failures (timeouts, connection errors, 429s, and 5xx responses)
with exponential backoff and jitter, via tenacity.

`httpx.HTTPTransport(retries=...)` only retries connection failures, not bad status
codes, and api.weather.gov/ESPN both return intermittent 500s/503s - hence this wrapper.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.25

# This bot runs from cron with no overall run timeout, so a server-requested
# Retry-After (e.g. an hour) must not be allowed to stall a run indefinitely - fail
# fast instead of honoring a wait beyond this bound.
MAX_RETRY_AFTER_SECONDS = 30.0

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(exc: BaseException) -> float | None:
    """The server-requested wait from a 429's `Retry-After` header (seconds or an HTTP
    date), so a rate limit isn't retried again before the server says it's safe to."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return None
    header = exc.response.headers.get("Retry-After")
    if header is None:
        return None
    if header.strip().isdigit():
        return float(header)
    try:
        retry_at = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != 429:
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    # A Retry-After beyond our budget means the server wants a wait this bot can't
    # afford - fail fast rather than sleeping past MAX_RETRY_AFTER_SECONDS.
    retry_after = _retry_after_seconds(exc)
    return retry_after is None or retry_after <= MAX_RETRY_AFTER_SECONDS


def request_with_retry(
    request: Callable[[], httpx.Response],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    describe_exception: Callable[[BaseException], object] = str,
) -> httpx.Response:
    """Call `request()` - which must itself call `raise_for_status()` on its response -
    retrying on timeouts, connection errors, 429s, and 5xx responses. A 429's
    `Retry-After` header takes priority over the usual exponential backoff. Re-raises
    the last `httpx.HTTPError` once every attempt is exhausted, so callers see one
    consistent failure mode.

    `describe_exception` renders the exception logged in the retry-attempt warning -
    override it when the exception's own message could carry something that shouldn't
    reach the logs (e.g. `httpx.HTTPStatusError` embeds the full request URL, which for
    some callers doubles as a bearer credential).
    """
    exponential_wait = wait_exponential_jitter(initial=base_delay)

    def _wait(retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        retry_after = _retry_after_seconds(exc) if exc is not None else None
        if retry_after is None:
            return exponential_wait(retry_state)
        # _is_retryable already rejects a Retry-After beyond the budget, so this is
        # belt-and-suspenders against ever sleeping past it here too.
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)

    def _log_retry(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        described = describe_exception(exc) if exc is not None else None
        logger.warning(
            "Retrying request after %s (attempt %d)", described, retry_state.attempt_number
        )

    retryer = Retrying(
        stop=stop_after_attempt(max_attempts),
        wait=_wait,
        retry=retry_if_exception(_is_retryable),
        before_sleep=_log_retry,
        reraise=True,
    )
    return retryer(request)
