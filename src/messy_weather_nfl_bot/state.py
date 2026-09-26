"""Per-day, per-platform post state.

Without this, re-running the bot on the same day (a manual retry, an overlapping
crontab entry, a scheduler catch-up) reposts the whole thread from scratch, and a
thread that failed partway through can only be "fixed" by posting the root again
and leaving the broken partial thread on the timeline. This module tracks, per
platform, the refs of whatever posted successfully today, so a re-run can skip a
platform that already finished and resume one that didn't - instead of restarting.

`--dry-run` must never read or write any of this - callers should only ever
construct a `DayState` when actually posting for real.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

from messy_weather_nfl_bot.poster.base import PostRef

logger = logging.getLogger(__name__)

STATE_DIR_ENV_VAR = "MESSY_WEATHER_STATE_DIR"
_APP_DIR_NAME = "messy-weather-bot"


def default_state_dir() -> Path:
    """Where per-day state files live: `MESSY_WEATHER_STATE_DIR` if set, else
    `$XDG_STATE_HOME/messy-weather-bot`, falling back to `~/.local/state` per the XDG
    base directory spec when `XDG_STATE_HOME` isn't set either."""
    override = os.environ.get(STATE_DIR_ENV_VAR)
    if override:
        return Path(override)
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / _APP_DIR_NAME


def state_file_path(date: dt.date, state_dir: Path | None = None) -> Path:
    return (state_dir or default_state_dir()) / f"{date.isoformat()}.json"


class PlatformState:
    """One platform's progress on today's thread: the refs published so far, and
    whether the thread finished completely."""

    def __init__(self, posts: list[PostRef] | None = None, completed: bool = False) -> None:
        self.posts = posts or []
        self.completed = completed

    def to_json(self) -> dict:
        return {
            "completed": self.completed,
            "posts": [{"id": r.id, "root_id": r.root_id, "cid": r.cid} for r in self.posts],
        }

    @staticmethod
    def _post_ref_from_json(p: dict) -> PostRef:
        post_id = p["id"]
        root_id = p["root_id"]
        cid = p.get("cid")
        if not isinstance(post_id, str) or not isinstance(root_id, str):
            raise TypeError("post reference id/root_id must be strings")
        if cid is not None and not isinstance(cid, str):
            raise TypeError("post reference cid must be a string or None")
        return PostRef(id=post_id, root_id=root_id, cid=cid)

    @classmethod
    def from_json(cls, data: dict) -> PlatformState:
        """Raises `(AttributeError, KeyError, TypeError)` if `data` isn't shaped like
        something `to_json()` would have produced - callers (`DayState.load`) treat
        that the same as corrupt JSON: log a warning and fall back to empty state."""
        posts = [cls._post_ref_from_json(p) for p in data.get("posts", [])]
        return cls(posts=posts, completed=bool(data.get("completed", False)))


class DayState:
    """A single day's per-platform post progress, backed by a JSON file at `path`."""

    def __init__(self, path: Path, platforms: dict[str, PlatformState] | None = None) -> None:
        self.path = path
        self.platforms = platforms if platforms is not None else {}

    @classmethod
    def load(cls, path: Path) -> DayState:
        try:
            raw = json.loads(path.read_text())
            platforms = {
                name: PlatformState.from_json(value)
                for name, value in raw.get("platforms", {}).items()
            }
        except FileNotFoundError:
            return cls(path)
        except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
            # Unreadable, corrupt, or valid JSON in an unexpected shape (e.g. a list
            # where an object was expected) - none of it should take the whole run
            # down. Worst case, a platform that already finished today gets reposted.
            logger.warning("Ignoring unreadable state file %s: %s", path, exc)
            return cls(path)
        return cls(path, platforms)

    def for_platform(self, name: str) -> PlatformState:
        return self.platforms.get(name, PlatformState())

    def record(self, name: str, posts: list[PostRef], *, completed: bool) -> None:
        """Persist `posts` (everything published so far) for `name` immediately - a
        crash right after a post must not lose track of what already went out."""
        self.platforms[name] = PlatformState(posts=posts, completed=completed)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"platforms": {n: s.to_json() for n, s in self.platforms.items()}}
        # Write-then-rename so a crash mid-write can't leave a half-written, unreadable
        # state file behind.
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(self.path)
