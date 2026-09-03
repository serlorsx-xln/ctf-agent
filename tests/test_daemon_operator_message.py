"""Daemon handler: mid-solve operator messages."""

from __future__ import annotations

import json

import pytest

from backend.daemon.handlers import Handlers
from backend.daemon.state import DaemonState
from backend.operator_inbox import operator_inbox_path


class _RunningSup:
    def __init__(
        self,
        roster: list[str] | None = None,
        models: list[str] | None = None,
    ):
        self._roster = list(roster or [])
        self._models = list(models or [])

    def is_running(self, session):  # noqa: ANN001
        return True

    def last_roster(self, session):  # noqa: ANN001
        return list(self._roster)

    def last_models(self, session):  # noqa: ANN001
        return list(self._models)


class _IdleSup:
    def is_running(self, session):  # noqa: ANN001
        return False

    def last_roster(self, session):  # noqa: ANN001
        return []

    def last_models(self, session):  # noqa: ANN001
        return []


@pytest.mark.asyncio
async def test_swarm_operator_message_requires_running():
    h = Handlers(DaemonState(), _IdleSup())  # type: ignore[arg-type]
    res = await h._h_swarm_operator_message({"text": "hello"}, session="s")
    assert res.get("ok") is False
    assert "swarm" in (res.get("error") or "").lower()


@pytest.mark.asyncio
async def test_swarm_operator_message_writes_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    h = Handlers(DaemonState(), _RunningSup(["default"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message({"text": "focus on XSS"}, session="abc")
    assert out.get("ok") is True
    body = operator_inbox_path("abc").read_text(encoding="utf-8")
    assert "focus on XSS" in body
    assert '"delivery": "steer"' in body
    assert '"target": "default"' in body


@pytest.mark.asyncio
async def test_swarm_operator_message_fanout_all(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(state, _RunningSup(["default#1", "default#2"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message(
        {"text": "try local storage", "delivery": "steer"}, session="abc"
    )
    assert out.get("ok") is True
    assert out.get("fanout") == 2
    crumbs = [str(m.get("text") or "") for m in seen if "you (steer" in str(m.get("text") or "")]
    assert any("→default#1" in c for c in crumbs)
    assert any("→default#2" in c for c in crumbs)
    assert not any("→all" in c for c in crumbs)
    lines = [
        json.loads(ln)
        for ln in operator_inbox_path("abc").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert {ln.get("target") for ln in lines} == {"default#1", "default#2"}


@pytest.mark.asyncio
async def test_swarm_operator_message_targeted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    h = Handlers(DaemonState(), _RunningSup(["default#1", "default#2"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message(
        {"text": "only two", "delivery": "queue", "target": "default#2"},
        session="abc",
    )
    assert out.get("ok") is True
    assert out.get("target") == "default#2"
    assert out.get("fanout") == 1
    lines = [
        json.loads(ln)
        for ln in operator_inbox_path("abc").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["target"] == "default#2"
    assert lines[0]["delivery"] == "queue"


@pytest.mark.asyncio
async def test_hold_no_fanout_uses_sole_roster_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(state, _RunningSup(["default"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message(
        {"text": "hold note", "delivery": "steer", "no_fanout": True},
        session="abc",
    )
    assert out.get("ok") is True
    assert out.get("fanout") == 1
    lines = [
        json.loads(ln)
        for ln in operator_inbox_path("abc").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0].get("target") == "default"
    crumbs = [str(m.get("text") or "") for m in seen if "you (steer" in str(m.get("text") or "")]
    assert any("→default" in c for c in crumbs)
    assert not any("→all" in c for c in crumbs)
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("Hold · delivering" in t for t in follow)
    assert not any("interrupting" in t for t in follow)


@pytest.mark.asyncio
async def test_hold_no_fanout_multi_without_target_errors(tmp_path, monkeypatch):
    """Hold without winner key on multi roster must not write an unscoped row."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(state, _RunningSup(["default#1", "default#2"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message(
        {"text": "hold q", "delivery": "steer", "no_fanout": True},
        session="abc",
    )
    assert out.get("ok") is False
    assert "Hold target unknown" in str(out.get("error") or "")
    assert not operator_inbox_path("abc").exists() or not operator_inbox_path("abc").read_text(
        encoding="utf-8"
    ).strip()
    crumbs = [str(m.get("text") or "") for m in seen if "you (steer" in str(m.get("text") or "")]
    assert crumbs == []


@pytest.mark.asyncio
async def test_swarm_operator_message_queue_broadcast(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(state, _RunningSup(["solo"], ["cursor/solo"]))  # type: ignore[arg-type]
    out = await h._h_swarm_operator_message(
        {"text": "later", "delivery": "queue"}, session="abc"
    )
    assert out.get("ok") is True
    assert out.get("delivery") == "queue"
    assert any("later" in str(m.get("text") or "") for m in seen)
    assert any("followup" in str(m.get("text") or "").lower() for m in seen)
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("queued until idle" in t for t in follow)
    body = operator_inbox_path("abc").read_text(encoding="utf-8")
    assert '"delivery": "queue"' in body


@pytest.mark.asyncio
async def test_soft_swarm_steer_crumb_matches_hash_roster(tmp_path, monkeypatch):
    """Target ``opus#2`` must resolve via roster keys, not only raw specs."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(
        state,
        _RunningSup(
            ["opus#1", "opus#2"],
            ["claude-sdk/opus", "claude-sdk/opus"],
        ),  # type: ignore[arg-type]
    )
    out = await h._h_swarm_operator_message(
        {"text": "only two", "delivery": "steer", "target": "opus#2"},
        session="abc",
    )
    assert out.get("ok") is True
    assert out.get("target") == "opus#2"
    assert out.get("fanout") == 1
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("interrupting opus#2" in t for t in follow)
    lines = [
        json.loads(ln)
        for ln in operator_inbox_path("abc").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["target"] == "opus#2"


@pytest.mark.asyncio
async def test_soft_swarm_steer_crumb_interrupts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(
        state,
        _RunningSup(["opus"], ["claude-sdk/opus"]),  # type: ignore[arg-type]
    )
    out = await h._h_swarm_operator_message(
        {"text": "try IDOR", "delivery": "steer"}, session="abc"
    )
    assert out.get("ok") is True
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("interrupting" in t for t in follow)
    assert not any("will inject on next tool check" in t for t in follow)


@pytest.mark.asyncio
async def test_soft_swarm_queue_crumb_uses_turn_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(
        state,
        _RunningSup(["opus"], ["claude-sdk/opus"]),  # type: ignore[arg-type]
    )
    out = await h._h_swarm_operator_message(
        {"text": "later", "delivery": "queue"}, session="abc"
    )
    assert out.get("ok") is True
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("queued until next turn boundary" in t for t in follow)


@pytest.mark.asyncio
async def test_cursor_swarm_steer_crumb_still_interrupts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )
    state = DaemonState()
    seen: list[dict] = []
    state.broadcast = lambda msg: seen.append(msg)  # type: ignore[method-assign]
    h = Handlers(
        state,
        _RunningSup(["auto"], ["cursor/auto"]),  # type: ignore[arg-type]
    )
    out = await h._h_swarm_operator_message(
        {"text": "try IDOR", "delivery": "steer"}, session="abc"
    )
    assert out.get("ok") is True
    follow = [str(m.get("text") or "") for m in seen if "followup" in str(m.get("text") or "")]
    assert any("interrupting" in t for t in follow)
