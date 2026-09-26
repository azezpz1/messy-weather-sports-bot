import typing as t

import pytest
from atproto import Client
from atproto.exceptions import InvokeTimeoutError, NetworkError, RateLimitExceededError
from atproto_client.request import Response

from messy_weather_nfl_bot.poster.base import PartialThreadError, PostRef
from messy_weather_nfl_bot.poster.bluesky import MAX_RATE_LIMIT_WAIT_SECONDS, BlueskyPoster


def _rate_limit_error(headers: dict[str, str]) -> RateLimitExceededError:
    response = Response(success=False, status_code=429, content=None, headers=headers)
    return RateLimitExceededError(response=response)


class _DummyClient:
    """Stands in for atproto's Client - BlueskyPoster only calls login() when no
    client is injected, so passing one skips that entirely."""


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


def _poster() -> BlueskyPoster:
    return BlueskyPoster(client=t.cast(Client, _DummyClient()))


def test_invoke_timeout_error_is_not_retryable() -> None:
    poster = _poster()
    assert poster._is_retryable(InvokeTimeoutError()) is False


def test_other_atproto_errors_are_retryable() -> None:
    poster = _poster()
    assert poster._is_retryable(NetworkError()) is True
    assert poster._is_retryable(RuntimeError("unrelated")) is True


class _TimesOutOnReply(BlueskyPoster):
    """A root post that succeeds, but every reply times out with no response - the
    ambiguous case where the reply may have actually gone through."""

    def __init__(self) -> None:
        super().__init__(client=t.cast(Client, _DummyClient()))
        self.reply_attempts = 0

    def post(self, text: str) -> PostRef:
        return PostRef(id="root", root_id="root")

    def reply(self, text: str, parent: PostRef) -> PostRef:
        self.reply_attempts += 1
        raise InvokeTimeoutError()


def test_post_thread_does_not_retry_a_reply_that_timed_out() -> None:
    # A timeout means no response ever arrived - the reply may have already gone
    # through server-side, so retrying it risks posting it twice. It should fail
    # immediately (stalling the thread as partial) rather than being retried.
    poster = _TimesOutOnReply()

    with pytest.raises(PartialThreadError):
        poster.post_thread(["root post", "reply"])

    assert poster.reply_attempts == 1


def test_retry_delay_honors_retry_after_header() -> None:
    poster = _poster()
    exc = _rate_limit_error({"retry-after": "12"})
    assert poster._retry_delay(exc) == 12.0


def test_retry_delay_caps_a_long_retry_after_at_the_bound() -> None:
    poster = _poster()
    exc = _rate_limit_error({"retry-after": "9999"})
    assert poster._retry_delay(exc) == MAX_RATE_LIMIT_WAIT_SECONDS


def test_retry_delay_is_none_for_a_429_with_no_timing_headers() -> None:
    poster = _poster()
    exc = _rate_limit_error({})
    assert poster._retry_delay(exc) is None


def test_retry_delay_is_none_for_non_rate_limit_errors() -> None:
    poster = _poster()
    assert poster._retry_delay(NetworkError()) is None
    assert poster._retry_delay(RuntimeError("unrelated")) is None
