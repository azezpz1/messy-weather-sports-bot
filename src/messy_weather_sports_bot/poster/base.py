"""Abstraction over social media backends, so new platforms can be added later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

# A couple of retries for a single post()/reply() call - transient network blips
# shouldn't turn one flaky call into a broken thread that needs a manual resume.
PUBLISH_MAX_ATTEMPTS = 3
PUBLISH_BASE_DELAY = 0.5


@dataclass(frozen=True)
class PostRef:
    """An opaque reference to a published post, usable as a `reply()` parent.

    `root_id` tracks the original root post of the thread (equal to `id` for a root
    post itself), since some backends (e.g. Bluesky) need the thread root, not just
    the immediate parent, to build a valid reply.
    """

    id: str
    root_id: str
    cid: str | None = None
    """Bluesky's content-addressed revision id for this post, alongside `id` (its AT
    URI) - needed to build a valid reply's strong ref. None for backends (e.g. the
    console poster) with no equivalent concept."""


class PartialThreadError(Exception):
    """Raised by `post_thread` when some, but not all, posts in the thread went out -
    e.g. the root published but a later reply failed. Carries the refs that did
    succeed, so callers can tell a partially-posted thread from a totally failed one."""

    def __init__(self, posted: list[PostRef], cause: BaseException) -> None:
        super().__init__(f"posted {len(posted)} of the thread before failing: {cause}")
        self.posted = posted


class SocialMediaPoster(ABC):
    @abstractmethod
    def post(self, text: str) -> PostRef:
        """Publish a new top-level post and return a reference to it."""

    @abstractmethod
    def reply(self, text: str, parent: PostRef) -> PostRef:
        """Publish a reply to `parent` and return a reference to it."""

    def resume_from(self, posted: list[PostRef]) -> None:  # noqa: B027
        """Restore whatever per-post bookkeeping `reply()` needs (e.g. Bluesky's strong
        refs) from `posted` - refs for posts published in an earlier, since-restarted
        process, most likely loaded back from persisted state. A no-op by default;
        backends that keep such bookkeeping in memory override this."""

    def _is_retryable(self, exc: BaseException) -> bool:
        """Whether `exc` is safe to retry - i.e. it proves `post()`/`reply()` never
        reached the point of publishing. True for everything by default; backends
        whose failures can be ambiguous (the publish may have gone through despite
        the exception, e.g. a client-side timeout with no response) should override
        this, since blindly retrying a non-idempotent publish call risks creating
        the very duplicate post this retry exists to route around."""
        return True

    def _retry_delay(self, exc: BaseException) -> float | None:
        """An explicit wait before the next attempt, when `exc` itself says how long
        (e.g. a rate limit's reset time) - overrides the default exponential backoff.
        None (the default) means use that backoff instead."""
        return None

    def _retrying(self) -> Retrying:
        exponential_wait = wait_exponential_jitter(initial=PUBLISH_BASE_DELAY)

        def _wait(retry_state: RetryCallState) -> float:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            delay = self._retry_delay(exc) if exc is not None else None
            return exponential_wait(retry_state) if delay is None else delay

        return Retrying(
            stop=stop_after_attempt(PUBLISH_MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception(self._is_retryable),
            reraise=True,
        )

    def post_thread(self, texts: list[str], resume: list[PostRef] | None = None) -> list[PostRef]:
        """Post `texts` as a thread: the first as a root post, the rest as chained replies.

        `resume` is a prefix of `texts` already published in an earlier, interrupted
        run (e.g. loaded from persisted state); those are skipped and posting
        continues by replying to the last of them, rather than starting the thread
        over from a new root.

        Raises `PartialThreadError` (wrapping whatever posted successfully, `resume`
        included) if a later post in the thread fails after an earlier one already
        went out.
        """
        if not texts:
            return []
        refs: list[PostRef] = list(resume) if resume else []
        if refs:
            self.resume_from(refs)
        remaining = texts[len(refs) :]
        if not remaining:
            return refs
        try:
            if not refs:
                refs.append(self._retrying()(self.post, remaining[0]))
                remaining = remaining[1:]
            for text in remaining:
                refs.append(self._retrying()(self.reply, text, refs[-1]))
        except Exception as exc:
            if refs:
                raise PartialThreadError(refs, exc) from exc
            raise
        return refs
