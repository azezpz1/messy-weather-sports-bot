import typing as t

import pytest
from atproto import Client
from atproto.exceptions import InvokeTimeoutError, NetworkError, RateLimitExceededError
from atproto_client.request import Response

from messy_weather_sports_bot.poster.base import PartialThreadError, PostRef
from messy_weather_sports_bot.poster.bluesky import MAX_RATE_LIMIT_WAIT_SECONDS, BlueskyPoster


def _rate_limit_error(headers: dict[str, str]) -> RateLimitExceededError:
    response = Response(success=False, status_code=429, content=None, headers=headers)
    return RateLimitExceededError(response=response)


def _network_error_with_response(status_code: int) -> NetworkError:
    response = Response(success=False, status_code=status_code, content=None, headers={})
    return NetworkError(response=response)


class _DummyClient:
    """Stands in for atproto's Client - BlueskyPoster only calls login() when no
    client is injected, so passing one skips that entirely."""


def _poster() -> BlueskyPoster:
    return BlueskyPoster(client=t.cast(Client, _DummyClient()))


def test_invoke_timeout_error_is_not_retryable() -> None:
    poster = _poster()
    assert poster._is_retryable(InvokeTimeoutError()) is False


def test_network_error_without_a_response_is_not_retryable() -> None:
    # A dropped connection: atproto wraps a raw httpx.NetworkError into this with no
    # response ever arriving - same ambiguity as a timeout, so not safe to retry.
    poster = _poster()
    assert poster._is_retryable(NetworkError()) is False


def test_network_error_with_a_response_is_retryable() -> None:
    # 409/413/502 - statuses the API itself calls safe to retry - carry a response.
    poster = _poster()
    assert poster._is_retryable(_network_error_with_response(502)) is True


def test_non_atproto_errors_are_retryable() -> None:
    poster = _poster()
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


class _LoginRecorder:
    def __init__(self) -> None:
        self.logins: list[tuple[str, str]] = []

    def login(self, handle: str, password: str) -> None:
        self.logins.append((handle, password))


@pytest.fixture
def login_recorder(monkeypatch: pytest.MonkeyPatch) -> _LoginRecorder:
    recorder = _LoginRecorder()
    monkeypatch.setattr("messy_weather_sports_bot.poster.bluesky.Client", lambda: recorder)
    for name in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD", "CFB_BLUESKY_HANDLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CFB_BLUESKY_APP_PASSWORD", raising=False)
    return recorder


def test_from_env_with_no_prefix_reads_the_original_variable_names(
    monkeypatch: pytest.MonkeyPatch, login_recorder: _LoginRecorder
) -> None:
    monkeypatch.setenv("BLUESKY_HANDLE", "first.example")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "placeholder-one")

    BlueskyPoster.from_env()

    assert login_recorder.logins == [("first.example", "placeholder-one")]


def test_from_env_reads_only_the_prefixed_variables(
    monkeypatch: pytest.MonkeyPatch, login_recorder: _LoginRecorder
) -> None:
    monkeypatch.setenv("BLUESKY_HANDLE", "first.example")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "placeholder-one")
    monkeypatch.setenv("CFB_BLUESKY_HANDLE", "second.example")
    monkeypatch.setenv("CFB_BLUESKY_APP_PASSWORD", "placeholder-two")

    BlueskyPoster.from_env("CFB_")

    assert login_recorder.logins == [("second.example", "placeholder-two")]


def test_from_env_never_falls_back_to_the_unprefixed_credentials(
    monkeypatch: pytest.MonkeyPatch, login_recorder: _LoginRecorder
) -> None:
    # Posting one sport's games to another sport's account would be worse than failing.
    monkeypatch.setenv("BLUESKY_HANDLE", "first.example")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "placeholder-one")

    with pytest.raises(KeyError, match="CFB_BLUESKY_HANDLE"):
        BlueskyPoster.from_env("CFB_")

    assert login_recorder.logins == []
