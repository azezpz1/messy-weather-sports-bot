import datetime as dt
from email.utils import format_datetime

import httpx
import pytest

from messy_weather_sports_bot.retry import _retry_after_seconds, request_with_retry

URL = "https://example.test/thing"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test suite fast - backoff timing itself isn't under test here.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", URL))


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    response = _response(status, headers)
    return httpx.HTTPStatusError(str(status), request=response.request, response=response)


def _get_and_raise(response: httpx.Response) -> httpx.Response:
    response.raise_for_status()
    return response


def test_returns_immediately_on_success() -> None:
    response = request_with_retry(lambda: _get_and_raise(_response(200)))
    assert response.status_code == 200


def test_does_not_retry_a_non_retryable_status() -> None:
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _get_and_raise(_response(404))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(request)
    assert attempts == 1


def test_retries_a_5xx_response_then_succeeds() -> None:
    responses = iter([_response(503), _response(200)])

    def request() -> httpx.Response:
        return _get_and_raise(next(responses))

    response = request_with_retry(request)
    assert response.status_code == 200


def test_retries_transient_connection_errors_then_succeeds() -> None:
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.ConnectTimeout("timed out", request=httpx.Request("GET", URL))
        return _get_and_raise(_response(200))

    response = request_with_retry(request)
    assert response.status_code == 200
    assert attempts == 2


def test_reraises_the_last_5xx_after_exhausting_retries() -> None:
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _get_and_raise(_response(500))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        request_with_retry(request, max_attempts=3)
    assert attempts == 3
    assert exc_info.value.response.status_code == 500


def test_does_not_retry_an_unrelated_exception() -> None:
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise ValueError("not a transient failure")

    with pytest.raises(ValueError, match="not a transient failure"):
        request_with_retry(request)
    assert attempts == 1


def test_reraises_the_last_connection_error_after_exhausting_retries() -> None:
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", URL))

    with pytest.raises(httpx.ConnectError):
        request_with_retry(request, max_attempts=3)
    assert attempts == 3


def test_retry_after_seconds_parses_an_integer_delay() -> None:
    exc = _status_error(429, {"Retry-After": "30"})
    assert _retry_after_seconds(exc) == 30.0


def test_retry_after_seconds_parses_an_http_date() -> None:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45)
    exc = _status_error(429, {"Retry-After": format_datetime(future, usegmt=True)})

    seconds = _retry_after_seconds(exc)

    assert seconds is not None
    assert 40 <= seconds <= 46


def test_retry_after_seconds_is_none_without_the_header() -> None:
    assert _retry_after_seconds(_status_error(429)) is None


def test_retry_after_seconds_is_none_for_a_non_429_status() -> None:
    assert _retry_after_seconds(_status_error(500, {"Retry-After": "30"})) is None


def test_retry_after_seconds_is_none_for_an_unrelated_exception() -> None:
    assert _retry_after_seconds(ValueError("nope")) is None


def test_retry_after_seconds_is_none_for_an_unparseable_header() -> None:
    assert _retry_after_seconds(_status_error(429, {"Retry-After": "not a date"})) is None


def test_retry_after_seconds_treats_a_timezone_naive_date_as_utc() -> None:
    # parsedate_to_datetime can return a naive datetime for a header with no offset -
    # treat it as UTC rather than crashing or comparing naive/aware datetimes.
    naive_header = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45)).strftime(
        "%a, %d %b %Y %H:%M:%S"
    )
    exc = _status_error(429, {"Retry-After": naive_header})

    seconds = _retry_after_seconds(exc)

    assert seconds is not None
    assert 40 <= seconds <= 46


def test_request_with_retry_sleeps_for_the_retry_after_header_on_a_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    responses = iter([_response(429, {"Retry-After": "5"}), _response(200)])

    def request() -> httpx.Response:
        return _get_and_raise(next(responses))

    response = request_with_retry(request)

    assert response.status_code == 200
    assert sleeps == [5.0]


def test_does_not_retry_a_429_whose_retry_after_exceeds_the_budget() -> None:
    # This bot runs from cron with no overall timeout - honoring an hour-long
    # Retry-After would stall the run instead of degrading gracefully.
    attempts = 0

    def request() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _get_and_raise(_response(429, {"Retry-After": "3600"}))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        request_with_retry(request)
    assert attempts == 1
    assert exc_info.value.response.status_code == 429
