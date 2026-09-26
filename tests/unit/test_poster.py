import pytest

from messy_weather_nfl_bot.poster.base import PartialThreadError, PostRef, SocialMediaPoster
from messy_weather_nfl_bot.poster.console import ConsolePoster


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # post_thread() retries a couple of times before giving up - keep the tests that
    # exercise a failing post()/reply() fast rather than actually sleeping.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda seconds: None)


def test_post_thread_empty_returns_no_refs() -> None:
    poster = ConsolePoster()
    assert poster.post_thread([]) == []


def test_post_thread_single_text_posts_once() -> None:
    poster = ConsolePoster()
    refs = poster.post_thread(["hello"])
    assert len(refs) == 1
    assert refs[0].root_id == refs[0].id
    assert poster.posted == ["hello"]


def test_post_thread_multiple_texts_chain_replies_to_same_root() -> None:
    poster = ConsolePoster()
    refs = poster.post_thread(["root", "reply 1", "reply 2"])

    assert len(refs) == 3
    assert poster.posted == ["root", "reply 1", "reply 2"]

    root_id = refs[0].id
    # Every post in the thread traces back to the same root, not just its immediate parent.
    assert all(ref.root_id == root_id for ref in refs)
    assert refs[0].id != refs[1].id != refs[2].id


class _FailsAfterFirstPost(SocialMediaPoster):
    """A poster whose root post succeeds but every reply fails."""

    def post(self, text: str) -> PostRef:
        return PostRef(id="root", root_id="root")

    def reply(self, text: str, parent: PostRef) -> PostRef:
        raise RuntimeError("platform outage")


class _FailsOnFirstPost(SocialMediaPoster):
    def post(self, text: str) -> PostRef:
        raise RuntimeError("platform outage")

    def reply(self, text: str, parent: PostRef) -> PostRef:
        raise AssertionError("unreachable")


def test_post_thread_raises_partial_thread_error_if_a_reply_fails_after_the_root_posted() -> None:
    poster = _FailsAfterFirstPost()

    with pytest.raises(PartialThreadError) as exc_info:
        poster.post_thread(["root", "reply"])

    # The root did publish - callers need that to avoid reporting "nothing posted".
    assert len(exc_info.value.posted) == 1
    assert exc_info.value.posted[0].id == "root"


def test_post_thread_reraises_plainly_if_nothing_posted_at_all() -> None:
    poster = _FailsOnFirstPost()

    with pytest.raises(RuntimeError, match="platform outage"):
        poster.post_thread(["root", "reply"])


def test_post_thread_resumes_by_replying_to_the_last_posted_ref() -> None:
    poster = ConsolePoster()
    already_posted = [PostRef(id="root", root_id="root")]

    refs = poster.post_thread(["root", "reply 1", "reply 2"], resume=already_posted)

    # Only the not-yet-posted texts were published - the root wasn't repeated.
    assert poster.posted == ["reply 1", "reply 2"]
    assert len(refs) == 3
    assert refs[0] == already_posted[0]
    assert all(ref.root_id == "root" for ref in refs)


def test_post_thread_resume_covering_every_text_posts_nothing_new() -> None:
    poster = ConsolePoster()
    already_posted = [
        PostRef(id="root", root_id="root"),
        PostRef(id="reply1", root_id="root"),
    ]

    refs = poster.post_thread(["root", "reply 1"], resume=already_posted)

    assert poster.posted == []
    assert refs == already_posted


def test_post_thread_calls_resume_from_hook_with_the_resumed_refs() -> None:
    seeded: list[PostRef] = []

    class SeedsOnResume(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            return PostRef(id="reply", root_id=parent.root_id)

        def resume_from(self, posted: list[PostRef]) -> None:
            seeded.extend(posted)

    already_posted = [PostRef(id="root", root_id="root", cid="cid-root")]
    SeedsOnResume().post_thread(["root", "reply"], resume=already_posted)

    assert seeded == already_posted


def test_post_thread_retries_a_flaky_reply_before_succeeding() -> None:
    attempts = {"count": 0}

    class FlakyOnce(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient network blip")
            return PostRef(id="reply", root_id=parent.root_id)

    refs = FlakyOnce().post_thread(["root", "reply"])

    assert attempts["count"] == 2
    assert len(refs) == 2


def test_post_thread_gives_up_after_a_couple_of_retries() -> None:
    attempts = {"count": 0}

    class AlwaysFlaky(SocialMediaPoster):
        def post(self, text: str) -> PostRef:
            return PostRef(id="root", root_id="root")

        def reply(self, text: str, parent: PostRef) -> PostRef:
            attempts["count"] += 1
            raise RuntimeError("platform outage")

    with pytest.raises(PartialThreadError):
        AlwaysFlaky().post_thread(["root", "reply"])

    assert attempts["count"] > 1
