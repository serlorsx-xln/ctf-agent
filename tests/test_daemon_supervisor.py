"""Supervisor + disk-log tee tests for the Artemis daemon.

Uses a fake swarm subprocess that emits live_log lines (which tee to the disk
log via ``ARTEMIS_SWARM_LOG``) so we can verify spawn → disk log → replay
without spinning a real Docker swarm.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from backend.daemon.state import DaemonState
from backend.daemon.supervisor import SwarmSupervisor, swarm_log_path

FAKE_SWARM = """
import os, sys, time
sys.path.insert(0, os.environ["ARTEMIS_REPO_ROOT"])
from backend.agents.live_log import live, close_disk_tee
for i in range(6):
    live("fake", f"line {i}")
    time.sleep(0.05)
close_disk_tee()
"""


@pytest.fixture
def repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


@pytest.fixture
def fake_swarm_script(tmp_path: Path, repo_root: str) -> str:
    p = tmp_path / "fake_swarm.py"
    p.write_text(FAKE_SWARM)
    return str(p)


@pytest.fixture(autouse=True)
def _patch_subprocess_exec(monkeypatch: pytest.MonkeyPatch, fake_swarm_script: str) -> None:
    """Replace the swarm CLI spawn with the fake swarm script."""
    import backend.daemon.supervisor as sup_mod
    from backend.subprocess_platform import swarm_subprocess_kwargs

    orig = sup_mod.asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kw):
        return await orig(
            sys.executable,
            fake_swarm_script,
            env=kw.get("env", os.environ.copy()),
            **swarm_subprocess_kwargs(),
        )

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)


def test_spawn_writes_disk_log_and_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_root: str
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", repo_root)

    state = DaemonState()
    sup = SwarmSupervisor(state)

    asyncio.run(
        sup.spawn(
            challenge="fake-chal",
            models=["cursor/x"],
            flags_required=1,
            session_id="s1",
        )
    )
    # wait for swarm to finish + stream task to drain
    asyncio.run(asyncio.sleep(1.0))

    log = swarm_log_path("fake-chal", "s1")
    assert log.is_file()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert lines[0] == "[fake] line 0"

    # Replay returns the tail.
    replay = sup.replay_tail("fake-chal", "s1")
    assert replay == lines


def test_supervisor_is_running_false_when_idle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    state = DaemonState()
    sup = SwarmSupervisor(state)
    assert sup.is_running() is False


def test_supervisor_has_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    state = DaemonState()
    sup = SwarmSupervisor(state)
    assert hasattr(sup, "_lock")
    assert isinstance(sup._lock, asyncio.Lock)


def test_stop_gen_guards_swarm_exit_broadcast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_root: str
) -> None:
    """stop() must broadcast swarm_exit with gen so a concurrent spawn is safe."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", repo_root)

    state = DaemonState()
    seen: list[dict] = []
    monkeypatch.setattr(state, "broadcast", lambda ev: seen.append(dict(ev)))

    sup = SwarmSupervisor(state)

    async def run() -> None:
        await sup.spawn(
            challenge="fake-chal",
            models=["cursor/x"],
            flags_required=1,
            session_id="s1",
        )
        # Let the fake swarm emit a couple lines, then stop.
        await asyncio.sleep(0.3)
        msg = await sup.stop(session_id="s1")
        assert "Stopped" in msg or "No active" in msg
        # Drain any cancelled stream task so pytest doesn't warn on loop close.
        task = sup._get_slot("s1").stream_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass

    asyncio.run(run())
    exits = [e for e in seen if e.get("type") == "swarm_exit"]
    assert exits, "expected swarm_exit from stop"
    assert all(e.get("session") == "s1" for e in exits)


def test_respawn_does_not_emit_intermediate_swarm_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_root: str
) -> None:
    """Stopping inside spawn (restart) must not broadcast swarm_exit mid-run."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", repo_root)

    state = DaemonState()
    seen: list[dict] = []
    monkeypatch.setattr(state, "broadcast", lambda ev: seen.append(dict(ev)))

    async def _noop_cleanup() -> None:
        return None

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", _noop_cleanup)

    # Long-lived fake so the first spawn is still running when we respawn.
    long_script = tmp_path / "long_swarm.py"
    long_script.write_text(
        "import time\n"
        "import os, sys\n"
        "sys.path.insert(0, os.environ['ARTEMIS_REPO_ROOT'])\n"
        "from backend.agents.live_log import live, close_disk_tee\n"
        "live('fake', 'boot')\n"
        "time.sleep(8)\n"
        "close_disk_tee()\n",
        encoding="utf-8",
    )
    import backend.daemon.supervisor as sup_mod
    from backend.subprocess_platform import swarm_subprocess_kwargs

    orig = sup_mod.asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kw):
        return await orig(
            sys.executable,
            str(long_script),
            env=kw.get("env", os.environ.copy()),
            **swarm_subprocess_kwargs(),
        )

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)

    sup = SwarmSupervisor(state)

    async def run() -> None:
        await sup.spawn(
            challenge="fake-chal",
            models=["cursor/x"],
            flags_required=1,
            session_id="s1",
        )
        await asyncio.sleep(0.2)
        before = len([e for e in seen if e.get("type") == "swarm_exit"])
        await sup.spawn(
            challenge="fake-chal",
            models=["cursor/y"],
            flags_required=1,
            session_id="s1",
        )
        await asyncio.sleep(0.2)
        after = [e for e in seen if e.get("type") == "swarm_exit"]
        assert len(after) == before, f"respawn must not emit swarm_exit, got {after}"
        await sup.stop(session_id="s1")

    asyncio.run(run())


def test_two_sessions_spawn_concurrently_stop_a_leaves_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_root: str
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", repo_root)

    long_script = tmp_path / "long_swarm.py"
    long_script.write_text(
        "import time\n"
        "import os, sys\n"
        "sys.path.insert(0, os.environ['ARTEMIS_REPO_ROOT'])\n"
        "from backend.agents.live_log import live, close_disk_tee\n"
        "live('fake', 'boot')\n"
        "time.sleep(8)\n"
        "close_disk_tee()\n",
        encoding="utf-8",
    )
    import backend.daemon.supervisor as sup_mod
    from backend.subprocess_platform import swarm_subprocess_kwargs

    orig = sup_mod.asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kw):
        return await orig(
            sys.executable,
            str(long_script),
            env=kw.get("env", os.environ.copy()),
            **swarm_subprocess_kwargs(),
        )

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)

    async def _noop_cleanup() -> None:
        return None

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", _noop_cleanup)

    state = DaemonState()
    sup = SwarmSupervisor(state)

    async def run() -> None:
        await sup.spawn(
            challenge="chal-a",
            models=["cursor/x"],
            flags_required=1,
            session_id="A",
        )
        await sup.spawn(
            challenge="chal-b",
            models=["cursor/y"],
            flags_required=1,
            session_id="B",
        )
        assert sup.is_running("A")
        assert sup.is_running("B")
        await sup.stop(session_id="A")
        assert not sup.is_running("A")
        assert sup.is_running("B")
        await sup.stop(session_id="B")

    asyncio.run(run())


def test_daemon_shutdown_stops_live_swarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_root: str
) -> None:
    """Daemon.shutdown must kill the detached swarm (exit must not leave it running)."""
    from backend.daemon.server import Daemon

    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", repo_root)
    # Avoid real Docker cleanup in unit tests.
    async def _noop_cleanup() -> None:
        return None

    monkeypatch.setattr(
        "backend.sandbox.cleanup_orphan_containers",
        _noop_cleanup,
    )

    async def run() -> None:
        d = Daemon()
        await d.supervisor.spawn(
            challenge="fake-chal",
            models=["cursor/x"],
            flags_required=1,
            session_id="s1",
        )
        assert d.supervisor.is_running("s1")
        await asyncio.sleep(0.2)
        await d.shutdown()
        assert not d.supervisor.is_running()
        # Drain stream task.
        task = d.supervisor._get_slot("s1").stream_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass

    asyncio.run(run())