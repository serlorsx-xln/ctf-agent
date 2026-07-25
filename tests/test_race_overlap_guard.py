"""Swarm bridge: one swarm at a time; force replaces previous."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_swarm_force_false_rejects_when_pid_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    from backend.shell.sandbox_session import swarm_pid_path

    pid_path = swarm_pid_path("_default")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    from backend.shell import bridge

    with (
        patch("backend.shell.sandbox_session.load_session_state", return_value={"challenge_dir": "/chal"}),
        patch("backend.models.normalize_race_specs", return_value=["cursor/default"]),
        patch("backend.models.missing_race_credentials", return_value=[]),
        patch.object(bridge, "_is_artemis_race_pid", return_value=True),
        patch.object(bridge, "_daemon_available", return_value=False),
    ):
        out = await bridge._swarm(
            {"models": ["cursor/default"], "challenge_dir": "/chal", "force": False}
        )
    assert "already running" in out
    assert str(os.getpid()) in out


@pytest.mark.asyncio
async def test_swarm_force_true_stops_previous_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    from backend.shell.sandbox_session import swarm_pid_path

    pid_path = swarm_pid_path("_default")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("424242", encoding="utf-8")

    from backend.shell import bridge

    class FakeStdout:
        def __init__(self) -> None:
            self._lines = [b"swarm go\n", b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class FakeProc:
        pid = 12345
        returncode = 0
        stdout = FakeStdout()

        async def wait(self):
            return 0

    with (
        patch(
            "backend.shell.sandbox_session.load_session_state",
            return_value={"challenge_dir": "/chal", "flags_required": 1},
        ),
        patch("backend.models.normalize_race_specs", return_value=["cursor/default"]),
        patch("backend.models.missing_race_credentials", return_value=[]),
        patch.object(bridge, "_is_artemis_race_pid", return_value=True),
        patch.object(bridge, "_daemon_available", return_value=False),
        patch.object(bridge, "_stop_race", new=AsyncMock(return_value="Stopped")),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=FakeProc())),
    ):
        out = await bridge._swarm(
            {"models": ["cursor/default"], "challenge_dir": "/chal", "force": True}
        )
    assert "swarm exit 0" in out
    assert not pid_path.exists()


@pytest.mark.asyncio
async def test_swarm_migrates_legacy_race_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    legacy = tmp_path / "race.pid"
    legacy.write_text("999999999", encoding="utf-8")

    from backend.shell import bridge
    from backend.shell.sandbox_session import swarm_pid_path

    class FakeStdout:
        def __init__(self) -> None:
            self._lines = [b"line one\n", b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class FakeProc:
        pid = 12345
        returncode = 0
        stdout = FakeStdout()

        async def wait(self):
            return 0

    with (
        patch(
            "backend.shell.sandbox_session.load_session_state",
            return_value={"challenge_dir": "/chal", "flags_required": 1},
        ),
        patch("backend.models.normalize_race_specs", return_value=["cursor/default"]),
        patch("backend.models.missing_race_credentials", return_value=[]),
        patch.object(bridge, "_is_artemis_race_pid", return_value=False),
        patch.object(bridge, "_daemon_available", return_value=False),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=FakeProc())),
    ):
        out = await bridge._swarm({"models": ["cursor/default"], "challenge_dir": "/chal"})
    assert "swarm exit 0" in out
    assert not legacy.exists()
    assert not swarm_pid_path("_default").exists()
    assert not (tmp_path / "swarm.pid").exists()


@pytest.mark.asyncio
async def test_swarm_clears_stale_pid_and_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    from backend.shell.sandbox_session import swarm_pid_path

    pid_path = swarm_pid_path("_default")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999999", encoding="utf-8")

    from backend.shell import bridge

    class FakeStdout:
        def __init__(self) -> None:
            self._lines = [b"line one\n", b"line two\n", b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class FakeProc:
        pid = 12345
        returncode = 0
        stdout = FakeStdout()

        async def wait(self):
            return 0

    with (
        patch(
            "backend.shell.sandbox_session.load_session_state",
            return_value={"challenge_dir": "/chal", "flags_required": 1},
        ),
        patch("backend.models.normalize_race_specs", return_value=["cursor/default"]),
        patch("backend.models.missing_race_credentials", return_value=[]),
        patch.object(bridge, "_is_artemis_race_pid", return_value=False),
        patch.object(bridge, "_daemon_available", return_value=False),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=FakeProc())),
    ):
        out = await bridge._swarm({"models": ["cursor/default"], "challenge_dir": "/chal"})
    assert "swarm exit 0" in out
    assert not pid_path.exists()
