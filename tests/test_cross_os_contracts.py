"""Cross-OS smoke contracts for spawn / bootstrap / hygiene helpers."""

from __future__ import annotations

import sys

import pytest

from backend.process_hygiene import (
    is_artemis_cursor_bridge_command,
    is_artemis_swarm_command,
)
from backend.stdio_platform import ensure_standard_streams, fd_is_valid
from backend.subprocess_platform import (
    background_popen_kwargs,
    detached_subprocess_kwargs,
    sanitize_child_env,
    swarm_command,
    swarm_subprocess_kwargs,
)


def test_detached_kwargs_platform_shape() -> None:
    kw = detached_subprocess_kwargs()
    if sys.platform == "win32":
        assert "creationflags" in kw
        assert "start_new_session" not in kw
    else:
        assert kw.get("start_new_session") is True
        assert "creationflags" not in kw


def test_swarm_and_background_kwargs_always_set_stdin() -> None:
    bg = background_popen_kwargs()
    assert bg["stdin"] is not None
    sw = swarm_subprocess_kwargs()
    assert "stdin" in sw


def test_swarm_command_shape_is_os_agnostic(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.subprocess_platform.resolve_venv_python",
        lambda repo=None: None,
    )
    cmd = swarm_command(tmp_path, ["--challenge", "c"])
    head = cmd[0].lower()
    assert head in ("uv",) or head.endswith("uv") or head.endswith("uv.exe") or "python" in head
    assert "swarm" in cmd


def test_sanitize_env_is_idempotent() -> None:
    a = sanitize_child_env()
    b = sanitize_child_env(a)
    assert "PYTHONHOME" not in b
    assert b["PYTHONUNBUFFERED"] == "1"


def test_ensure_stdio_idempotent() -> None:
    ensure_standard_streams()
    assert fd_is_valid(0) and fd_is_valid(1) and fd_is_valid(2)
    ensure_standard_streams()
    assert fd_is_valid(0)


@pytest.mark.parametrize(
    "cmd",
    [
        r"C:\venv\Scripts\python.exe -m backend.cli swarm --challenge c",
        "/Volumes/USB/tui/.venv/bin/python -m backend.cli swarm --challenge c",
        "uv run --directory /repo artemis swarm --challenge c",
    ],
)
def test_swarm_cmd_detection_cross_path_styles(cmd: str) -> None:
    assert is_artemis_swarm_command(cmd) is True


def test_bridge_detection_requires_ctf_workspace() -> None:
    assert is_artemis_cursor_bridge_command(
        "node cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-abc"
    )
    assert not is_artemis_cursor_bridge_command(
        "node cursor-sdk-bridge.js --workspace /Users/me/app"
    )
