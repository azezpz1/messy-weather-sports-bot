"""Bluesky backend for the SocialMediaPoster abstraction, via the atproto SDK."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from atproto import Client, models
from atproto.exceptions import InvokeTimeoutError, RateLimitExceededError

from messy_weather_nfl_bot.poster.base import PostRef, SocialMediaPoster

HANDLE_ENV_VAR = "BLUESKY_HANDLE"
APP_PASSWORD_ENV_VAR = "BLUESKY_APP_PASSWORD"

# This bot runs from cron with no overall run timeout, so a rate limit's reset time
# must not be allowed to stall a run indefinitely - fail fast (and let the thread
# resume on the next run) rather than sleeping past this bound. Mirrors retry.py's
# own MAX_RETRY_AFTER_SECONDS for the same reason.
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0


class BlueskyPoster(SocialMediaPoster):
    def __init__(self, client: Client | None = None) -> None:
        self._client = client or Client()
        self._strong_refs: dict[str, models.ComAtprotoRepoStrongRef.Main] = {}
        if client is None:
            handle = os.environ[HANDLE_ENV_VAR]
            app_password = os.environ[APP_PASSWORD_ENV_VAR]
            self._client.login(handle, app_password)

    def _remember(self, uri: str, cid: str) -> None:
        self._strong_refs[uri] = models.ComAtprotoRepoStrongRef.Main(cid=cid, uri=uri)

    def _is_retryable(self, exc: BaseException) -> bool:
        """`InvokeTimeoutError` means the client gave up waiting - no response ever
        arrived, so the post may already have gone through server-side, and retrying
        risks publishing it twice. Every other atproto request error carries a
        response: either a definite rejection, or (409/413/502) a status the API
        itself calls safe to retry - so it's fine to retry those."""
        return not isinstance(exc, InvokeTimeoutError)

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
