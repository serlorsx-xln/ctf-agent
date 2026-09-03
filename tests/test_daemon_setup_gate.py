"""Daemon setup_status / install gate handlers."""

from __future__ import annotations

import pytest

from backend.daemon.handlers import Handlers
from backend.daemon.state import DaemonState
from backend.sandbox.setup_ready import SetupStatus


class _IdleSup:
    def is_running(self, session):  # noqa: ANN001
        return False


@pytest.mark.asyncio
async def test_setup_status_returns_dict(monkeypatch):
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: SetupStatus(
            ready=False,
            docker_ok=True,
            core_image=False,
            packs_ready=[],
            packs_missing=["web"],
            message="Missing ctf-sandbox-core image.",
        ),
    )
    h = Handlers(DaemonState(), _IdleSup())  # type: ignore[arg-type]
    out = await h._h_setup_status({}, session="s")
    assert out["ready"] is False
    assert out["core_image"] is False
    assert out["installing"] is False
    assert "Missing" in out["message"]


@pytest.mark.asyncio
async def test_setup_status_reports_installing(monkeypatch):
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: SetupStatus(
            ready=False,
            docker_ok=True,
            core_image=False,
            message="baking",
        ),
    )
    h = Handlers(DaemonState(), _IdleSup())  # type: ignore[arg-type]

    class _Running:
        def done(self) -> bool:
            return False

    h._setup_task = _Running()  # type: ignore[assignment]
    out = await h._h_setup_status({}, session="s")
    assert out["installing"] is True


@pytest.mark.asyncio
async def test_load_blocked_when_setup_not_ready(monkeypatch):
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: SetupStatus(
            ready=False,
            docker_ok=False,
            core_image=False,
            message="Docker is not reachable.",
        ),
    )
    h = Handlers(DaemonState(), _IdleSup())  # type: ignore[arg-type]
    out = await h._h_load({"path": "/tmp"}, session="s")
    assert str(out.get("text", "")).startswith("ERROR:")
    assert out.get("setup", {}).get("ready") is False


@pytest.mark.asyncio
async def test_swarm_start_blocked_when_setup_not_ready(monkeypatch):
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: SetupStatus(
            ready=False,
            docker_ok=True,
            core_image=False,
            message="Missing image",
        ),
    )
    h = Handlers(DaemonState(), _IdleSup())  # type: ignore[arg-type]
    out = await h._h_swarm_start({"models": ["cursor/composer-2.5"]}, session="s")
    assert out.get("ok") is False
    assert "not installed" in str(out.get("error", "")).lower()


class _RunningSup:
    def is_running(self, session):  # noqa: ANN001
        return True


@pytest.mark.asyncio
async def test_load_blocked_when_swarm_running(monkeypatch):
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: SetupStatus(ready=True, docker_ok=True, core_image=True, message="ok"),
    )
    h = Handlers(DaemonState(), _RunningSup())  # type: ignore[arg-type]
    out = await h._h_load({"path": "/tmp"}, session="s")
    assert "swarm still running" in str(out.get("text", "")).lower()


@pytest.mark.asyncio
async def test_clear_session_blocked_when_swarm_running():
    h = Handlers(DaemonState(), _RunningSup())  # type: ignore[arg-type]
    out = await h._h_clear_session({}, session="s")
    assert out.get("ok") is False
    assert "swarm still running" in str(out.get("error", "")).lower()
