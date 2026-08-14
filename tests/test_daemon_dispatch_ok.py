"""Regression: handler results that include ``ok`` must not TypeError in dispatch."""

from __future__ import annotations

import pytest

from backend.daemon.handlers import Handlers
from backend.daemon.state import DaemonState
from backend.daemon.supervisor import SwarmSupervisor


@pytest.mark.asyncio
async def test_dispatch_ok_field_does_not_typeerror(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    state = DaemonState()
    supervisor = SwarmSupervisor(state)
    handlers = Handlers(state, supervisor)

    # Simulate swarm_start-style result with ok=True
    async def _fake(_payload, *, session=None):
        return {"ok": True, "swarm_id": "abc"}

    handlers._h_swarm_start = _fake  # type: ignore[method-assign]
    resp = await handlers.dispatch({"v": 1, "id": "1", "type": "swarm_start", "session": None})
    assert resp is not None
    assert resp.get("ok") is True
    assert resp.get("swarm_id") == "abc"
    assert resp.get("type") == "swarm_start"


@pytest.mark.asyncio
async def test_dispatch_ok_false_preserved(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    state = DaemonState()
    supervisor = SwarmSupervisor(state)
    handlers = Handlers(state, supervisor)

    async def _fake(_payload, *, session=None):
        return {"ok": False, "error": "load a challenge first"}

    handlers._h_swarm_start = _fake  # type: ignore[method-assign]
    resp = await handlers.dispatch({"v": 1, "id": "2", "type": "swarm_start", "session": None})
    assert resp is not None
    assert resp.get("ok") is False
    assert "challenge" in str(resp.get("error", "")).lower() or resp.get("error")


@pytest.mark.asyncio
async def test_swarm_replay_when_not_running(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    state = DaemonState()
    supervisor = SwarmSupervisor(state)
    handlers = Handlers(state, supervisor)

    resp = await handlers.dispatch({"v": 1, "id": "3", "type": "swarm_replay", "session": "s1"})
    assert resp is not None
    assert resp.get("ok") is True
    assert resp.get("running") is False
