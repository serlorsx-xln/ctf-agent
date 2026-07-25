"""DaemonState hydration + push tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.daemon.state import DaemonState


def test_hydrate_loads_session_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "session.json").write_text(
        json.dumps({"challenge_dir": "/x", "flags_required": 2})
    )
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    state.hydrate()
    assert state.session["challenge_dir"] == "/x"
    assert state.session["flags_required"] == 2
    # Legacy top-level session.json was migrated into sessions/_default/.
    assert (cache / "sessions" / "_default" / "session.json").is_file()


def test_hydrate_all_loads_multiple_slots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    (cache / "sessions" / "a").mkdir(parents=True)
    (cache / "sessions" / "b").mkdir(parents=True)
    (cache / "sessions" / "a" / "session.json").write_text(
        json.dumps({"challenge_dir": "/a"})
    )
    (cache / "sessions" / "b" / "session.json").write_text(
        json.dumps({"challenge_dir": "/b"})
    )
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    state.hydrate_all()
    assert state.get_session("a")["challenge_dir"] == "/a"
    assert state.get_session("b")["challenge_dir"] == "/b"


def test_update_session_broadcasts(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    q = state.subscribe()

    state.update_session(challenge_dir="/y", flags_required=3)

    assert state.session["challenge_dir"] == "/y"
    event = asyncio.run(q.get())
    assert event["type"] == "session_update"
    assert event["session"] == "_default"
    assert event["session_state"]["challenge_dir"] == "/y"


def test_broadcast_isolates_subscribers(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    qa = state.subscribe("win-a")
    qb = state.subscribe("win-b")
    state.broadcast({"type": "boot", "session": "win-a", "text": "only-a"})
    event = asyncio.run(qa.get())
    assert event["text"] == "only-a"
    assert qb.empty()


def test_set_usage_mirrors_reported_cost_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    q = state.subscribe()
    # Provider has not reported cost yet.
    state.set_usage({"tokens": 100, "cost_usd": None})
    event = asyncio.run(q.get())
    assert event["type"] == "usage_update"
    assert event["session"] == "_default"
    assert event["tokens"] == 100
    assert event["cost_usd"] is None


def test_clear_session_resets(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    state.update_session(challenge_dir="/z")
    state.set_usage({"tokens": 5})
    state.clear_session()
    assert state.session == {}
    assert state.usage == {}


def test_clear_session_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    state = DaemonState()
    state.update_session(session_id="a", challenge_dir="/a")
    state.update_session(session_id="b", challenge_dir="/b")
    state.clear_session("a")
    assert state.get_session("a") == {}
    assert state.get_session("b")["challenge_dir"] == "/b"