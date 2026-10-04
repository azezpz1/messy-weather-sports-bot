"""Bluesky backend for the SocialMediaPoster abstraction, via the atproto SDK."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Self

from atproto import Client, models
from atproto.exceptions import RateLimitExceededError, RequestErrorBase

from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster

HANDLE_ENV_VAR = "BLUESKY_HANDLE"
APP_PASSWORD_ENV_VAR = "BLUESKY_APP_PASSWORD"

# This bot runs from cron with no overall run timeout, so a rate limit's reset time
# must not be allowed to stall a run indefinitely - fail fast (and let the thread
# resume on the next run) rather than sleeping past this bound. Mirrors retry.py's
# own MAX_RETRY_AFTER_SECONDS for the same reason.
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0


class BlueskyPoster(SocialMediaPoster):
    def __init__(self, client: Client | None = None, *, env_prefix: str = "") -> None:
        """Logs in with `<env_prefix>BLUESKY_HANDLE` / `<env_prefix>BLUESKY_APP_PASSWORD`
        unless `client` is given. A missing variable raises KeyError naming it."""
        self._client = client or Client()
        self._strong_refs: dict[str, models.ComAtprotoRepoStrongRef.Main] = {}
        if client is None:
            handle = os.environ[f"{env_prefix}{HANDLE_ENV_VAR}"]
            app_password = os.environ[f"{env_prefix}{APP_PASSWORD_ENV_VAR}"]
            self._client.login(handle, app_password)

    @classmethod
    def from_env(cls, env_prefix: str = "") -> Self:
        return cls(env_prefix=env_prefix)

    def _remember(self, uri: str, cid: str) -> None:
        self._strong_refs[uri] = models.ComAtprotoRepoStrongRef.Main(cid=cid, uri=uri)

    def _is_retryable(self, exc: BaseException) -> bool:
        """An atproto request error with no response (a client-side timeout, or a
        dropped connection - both `InvokeTimeoutError` and a response-less
        `NetworkError` per atproto_client.request._handle_request_errors) means the
        failure happened before any response arrived, so the post may already have
        gone through server-side - retrying risks publishing it twice. Every error
        that does carry a response - a definite rejection, or a status (409/413/502)
        the API itself calls safe to retry - is fine to retry."""
        if isinstance(exc, RequestErrorBase):
            return exc.response is not None
        return True

    def _retry_delay(self, exc: BaseException) -> float | None:
        """A 429's own requested wait, when it has one - retrying on the default
        short backoff almost never outlasts an actual rate limit, so those attempts
        just get burned for nothing."""
        if not isinstance(exc, RateLimitExceededError):
            return None
        if exc.retry_after is not None:
            wait = exc.retry_after
        elif exc.reset_at is not None:
            wait = (exc.reset_at - datetime.now(UTC)).total_seconds()
        else:
            return None
        return max(0.0, min(wait, MAX_RATE_LIMIT_WAIT_SECONDS))

    def resume_from(self, posted: list[PostRef]) -> None:
        """Reseed the strong-ref cache `reply()` needs from refs loaded off disk - a
        freshly started process has none of the state `post()`/`reply()` normally
        build up in memory as a thread goes out."""
        for ref in posted:
            if ref.cid is not None:
                self._remember(ref.id, ref.cid)

    def post(self, text: str) -> PostRef:
        response = self._client.send_post(text)
        self._remember(response.uri, response.cid)
        return PostRef(id=response.uri, root_id=response.uri, cid=response.cid)

    def reply(self, text: str, parent: PostRef) -> PostRef:
        parent_ref = self._strong_refs[parent.id]
        root_ref = self._strong_refs[parent.root_id]
        reply_to = models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
        response = self._client.send_post(text, reply_to=reply_to)
        self._remember(response.uri, response.cid)
        return PostRef(id=response.uri, root_id=parent.root_id, cid=response.cid)
