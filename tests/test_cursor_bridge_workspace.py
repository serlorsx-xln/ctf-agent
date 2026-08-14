"""Shared Cursor SDK bridge workspace (not first-solver temp)."""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.agents.cursor_runtime as runtime


def test_shared_bridge_workspace_is_stable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_bridge_workspace", None)
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "bridge"))
    (tmp_path / "bridge").mkdir()
    a = runtime.shared_bridge_workspace()
    b = runtime.shared_bridge_workspace()
    assert a == b
    assert Path(a, "AGENTS.md").is_file()


@pytest.mark.asyncio
async def test_create_on_live_bridge_relaunches_after_connect_error(monkeypatch) -> None:
    calls = {"n": 0, "recreate": 0}

    class _Client:
        pass

    live = _Client()
    monkeypatch.setattr(runtime, "_agent_client", live)

    async def recreate():
        calls["recreate"] += 1
        fresh = _Client()
        runtime._agent_client = fresh
        return fresh

    monkeypatch.setattr(runtime, "force_recreate_client", recreate)

    async def factory(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Bridge request failed: ConnectError: All connection attempts failed")
        return f"ok:{id(client)}"

    out = await runtime.create_on_live_bridge(factory)
    assert calls["n"] == 2
    assert calls["recreate"] == 1
    assert out.startswith("ok:")


@pytest.mark.asyncio
async def test_create_on_live_bridge_does_not_relaunch_other_errors(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_agent_client", object())

    async def recreate():
        raise AssertionError("must not recreate")

    monkeypatch.setattr(runtime, "force_recreate_client", recreate)

    async def factory(_client):
        raise RuntimeError("quota exceeded")

    with pytest.raises(RuntimeError, match="quota"):
        await runtime.create_on_live_bridge(factory)
