import datetime as dt
import json
from pathlib import Path

import pytest

from messy_weather_nfl_bot import state
from messy_weather_nfl_bot.poster.base import PostRef


def test_default_state_dir_uses_override_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(state.STATE_DIR_ENV_VAR, "/custom/state/dir")
    assert state.default_state_dir() == Path("/custom/state/dir")


def test_default_state_dir_falls_back_to_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(state.STATE_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert state.default_state_dir() == Path("/xdg/state/messy-weather-bot")


def test_default_state_dir_falls_back_to_home_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(state.STATE_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/pi"))
    assert state.default_state_dir() == Path("/home/pi/.local/state/messy-weather-bot")


def test_state_file_path_is_named_after_the_date(tmp_path: Path) -> None:
    path = state.state_file_path(dt.date(2026, 9, 27), state_dir=tmp_path)
    assert path == tmp_path / "2026-09-27.json"


def test_load_missing_file_returns_empty_state(tmp_path: Path) -> None:
    day = state.DayState.load(tmp_path / "does-not-exist.json")
    assert day.platforms == {}
    assert day.for_platform("BlueskyPoster").posts == []
    assert day.for_platform("BlueskyPoster").completed is False


def test_load_corrupt_file_is_ignored_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-27.json"
    path.write_text("not json{{{")

    day = state.DayState.load(path)

    assert day.platforms == {}


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # top-level list instead of an object
        '{"platforms": []}',  # "platforms" is a list, not an object
        '{"platforms": {"BlueskyPoster": []}}',  # a platform value that isn't an object
        '{"platforms": {"BlueskyPoster": {"posts": [{"id": "root"}]}}}',  # post missing root_id
        '{"platforms": {"BlueskyPoster": {"posts": "not-a-list"}}}',
        # id/root_id/cid of the wrong type - would otherwise reach resume_from() and
        # blow up there (e.g. an unhashable list used as a strong-ref cache key).
        '{"platforms": {"BlueskyPoster": {"posts": [{"id": [], "root_id": "root"}]}}}',
        '{"platforms": {"BlueskyPoster": {"posts": [{"id": "root", "root_id": 1}]}}}',
        '{"platforms": {"BlueskyPoster": '
        '{"posts": [{"id": "root", "root_id": "root", "cid": 1}]}}}',
    ],
)
def test_load_malformed_but_valid_json_is_ignored_rather_than_raising(
    tmp_path: Path, raw: str
) -> None:
    path = tmp_path / "2026-09-27.json"
    path.write_text(raw)

    day = state.DayState.load(path)

    assert day.platforms == {}


def test_record_then_load_round_trips_completed_thread(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-27.json"
    day = state.DayState(path)
    refs = [
        PostRef(id="at://root", root_id="at://root", cid="cid-root"),
        PostRef(id="at://reply1", root_id="at://root", cid="cid-reply1"),
    ]

    day.record("BlueskyPoster", refs, completed=True)
    reloaded = state.DayState.load(path)

    platform_state = reloaded.for_platform("BlueskyPoster")
    assert platform_state.completed is True
    assert platform_state.posts == refs


def test_record_partial_thread_persists_completed_false(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-27.json"
    day = state.DayState(path)
    refs = [PostRef(id="at://root", root_id="at://root", cid="cid-root")]

    day.record("BlueskyPoster", refs, completed=False)
    reloaded = state.DayState.load(path)

    platform_state = reloaded.for_platform("BlueskyPoster")
    assert platform_state.completed is False
    assert platform_state.posts == refs


def test_record_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "2026-09-27.json"
    day = state.DayState(path)

    day.record("ConsolePoster", [PostRef(id="1", root_id="1")], completed=True)

    assert path.exists()


def test_record_does_not_clobber_other_platforms(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-27.json"
    day = state.DayState(path)
    bluesky_refs = [PostRef(id="at://root", root_id="at://root", cid="cid-root")]
    day.record("BlueskyPoster", bluesky_refs, completed=True)

    day.record("MastodonPoster", [PostRef(id="1", root_id="1")], completed=True)

    reloaded = state.DayState.load(path)
    assert reloaded.for_platform("BlueskyPoster").posts == bluesky_refs
    assert reloaded.for_platform("MastodonPoster").completed is True


def test_record_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-27.json"
    day = state.DayState(path)

    day.record("ConsolePoster", [PostRef(id="1", root_id="1")], completed=True)

    assert not (tmp_path / "2026-09-27.json.tmp").exists()
    assert json.loads(path.read_text())  # is valid JSON
