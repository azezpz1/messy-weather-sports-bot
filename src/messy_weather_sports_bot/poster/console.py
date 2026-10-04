"""A poster that prints instead of publishing - used for --dry-run and in tests."""

from __future__ import annotations

import itertools

from messy_weather_sports_bot.poster.base import PostRef, SocialMediaPoster


class ConsolePoster(SocialMediaPoster):
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.posted: list[str] = []

    def post(self, text: str) -> PostRef:
        print(f"--- post ---\n{text}\n")
        self.posted.append(text)
        post_id = f"console-{next(self._ids)}"
        return PostRef(id=post_id, root_id=post_id)

    def reply(self, text: str, parent: PostRef) -> PostRef:
        print(f"--- reply to {parent.id} ---\n{text}\n")
        self.posted.append(text)
        post_id = f"console-{next(self._ids)}"
        return PostRef(id=post_id, root_id=parent.root_id)
