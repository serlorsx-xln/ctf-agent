"""Tests for stdio repair and swarm spawn argv helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.process_hygiene import (
    bridge_belongs_to_session,
    cursor_bridge_session_prefix,
    is_artemis_cursor_bridge_command,
    is_artemis_swarm_command,
    should_kill_cursor_bridge,
)
from backend.stdio_platform import fd_is_valid
from backend.subprocess_platform import (
    resolve_docker_exe,
    resolve_venv_python,
    sanitize_child_env,
    swarm_command,
    windows_system_exe,
)


def test_ensure_standard_streams_repairs_closed_stdin(tmp_path: Path) -> None:
    """Closed fd 0 must be reopened so children can inherit a valid stdin."""
    report = tmp_path / "ok.txt"
    # Run in a child so we do not break the pytest process stdio.
    code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backend.stdio_platform import ensure_standard_streams, fd_is_valid
null = os.open(os.devnull, os.O_RDWR)
os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)
if null > 2:
    os.close(null)
os.close(0)
assert not fd_is_valid(0)
ensure_standard_streams()
assert fd_is_valid(0)
assert fd_is_valid(1)
assert fd_is_valid(2)
Path(sys.argv[2]).write_text("ok", encoding="utf-8")
"""
    import subprocess

    repo = str(Path(__file__).resolve().parents[1])
    r = subprocess.run(
        [sys.executable, "-c", code, repo, str(report)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0, r.stderr
    assert report.read_text(encoding="utf-8") == "ok"


def test_fd_is_valid_negative() -> None:
    assert fd_is_valid(-1) is False


def test_swarm_command_prefers_venv_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "proj"
    if sys.platform == "win32":
        py = repo / ".venv" / "Scripts" / "python.exe"
    else:
        py = repo / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    py.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(
        "backend.subprocess_platform._python_smoke_ok",
        lambda p: True,
    )
    cmd = swarm_command(repo, ["--challenge", "c", "--models", "cursor/x"])
    assert cmd[0] == str(py)
    assert cmd[1:4] == ["-m", "backend.cli", "swarm"]
    assert "--challenge" in cmd


def test_swarm_command_falls_back_to_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cmd = swarm_command(tmp_path, ["--challenge", "c"])
    assert cmd[:2] == ["uv", "run"]
    assert "artemis" in cmd
    assert "swarm" in cmd


def test_windows_system_exe_passthrough_on_unix() -> None:
    if sys.platform == "win32":
        exe = windows_system_exe("cmd")
        assert exe.lower().endswith("cmd.exe")
        assert Path(exe).is_absolute()
    else:
        assert windows_system_exe("cmd") == "cmd"
    assert resolve_docker_exe()


def test_resolve_venv_python_none_without_venv(tmp_path: Path) -> None:
    assert resolve_venv_python(tmp_path) is None


@pytest.mark.parametrize(
    ("cmd", "expect"),
    [
        (
            "node .../cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-abc123 --tool-callback-url http://127.0.0.1:1/",
            True,
        ),
        (
            "node .../cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-bridge-ses_abc-xyz --tool-callback-url http://127.0.0.1:1/",
            True,
        ),
        (
            "node .../cursor-sdk-bridge.js --workspace /Users/me/my-app",
            False,
        ),
        ("vim README.md", False),
        ("", False),
    ],
)
def test_is_artemis_cursor_bridge_command(cmd: str, expect: bool) -> None:
    assert is_artemis_cursor_bridge_command(cmd) is expect


@pytest.mark.parametrize(
    ("cmd", "expect"),
    [
        ("uv run artemis swarm --challenge ./c", True),
        ("/venv/bin/python -m backend.cli swarm --challenge ./c", True),
        ("python -m backend.cli race --challenge ./c", True),
        ("python -m backend.cli --help", False),
        ("python -m backend.cli setup", False),
        ("vim README.md", False),
        ("", False),
    ],
)
def test_is_artemis_swarm_command(cmd: str, expect: bool) -> None:
    assert is_artemis_swarm_command(cmd) is expect


def test_sanitize_child_env_drops_pythonhome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONHOME", "/broken")
    monkeypatch.setenv("PYTHONPATH", "/also-broken")
    monkeypatch.setenv("KEEP_ME", "1")
    env = sanitize_child_env()
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["KEEP_ME"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_windows_empty_cmdline_not_treated_as_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.shell.bridge as bridge

    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge, "_pid_command", lambda pid: "")
    monkeypatch.setattr(bridge, "_windows_executable_looks_like_python", lambda pid: False)
    monkeypatch.setattr(bridge.os, "kill", lambda *a, **k: None)
    assert bridge._is_artemis_race_pid(12345) is False


def test_windows_empty_cmdline_python_image_treated_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.shell.bridge as bridge

    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge, "_pid_command", lambda pid: "")
    monkeypatch.setattr(bridge, "_windows_executable_looks_like_python", lambda pid: True)
    monkeypatch.setattr(bridge.os, "kill", lambda *a, **k: None)
    assert bridge._is_artemis_race_pid(12345) is True


def test_unix_empty_cmdline_still_treated_live(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.shell.bridge as bridge

    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    monkeypatch.setattr(bridge, "_pid_command", lambda pid: "")
    monkeypatch.setattr(bridge.os, "kill", lambda *a, **k: None)
    assert bridge._is_artemis_race_pid(12345) is True


def test_clear_stale_skipped_when_daemon_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bootstrap must not unlink endpoints while a daemon is reachable."""
    import backend.daemon.transport as transport
    import backend.process_hygiene as hygiene
    import scripts.tui_bootstrap as boot

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    calls: list[str] = []

    monkeypatch.setattr(hygiene, "cleanup_orphan_cursor_bridges", lambda: calls.append("sweep"))
    monkeypatch.setattr(boot, "_refresh_install_path", lambda: None)
    monkeypatch.setattr(transport, "daemon_alive", lambda timeout=0.25: True)
    monkeypatch.setattr(
        transport,
        "clear_stale_endpoint_files",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(boot, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(boot, "daemon_code_stale", lambda: False)
    monkeypatch.setattr(boot, "spawn_background", lambda *a, **k: calls.append("spawn"))

    boot.ensure_background_services()
    assert "clear" not in calls
    assert "spawn" not in calls
    assert "sweep" not in calls


def test_services_already_up_false_without_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import scripts.tui_bootstrap as boot

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    monkeypatch.delenv("ARTEMIS_DAEMON_TCP", raising=False)
    monkeypatch.setattr(boot.sys, "platform", "darwin")
    assert boot.services_already_up() is False


def test_main_hot_path_skips_full_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.tui_bootstrap as boot

    calls: list[str] = []
    monkeypatch.setattr(boot, "services_already_up", lambda: True)
    monkeypatch.setattr(boot, "daemon_code_stale", lambda: False)
    monkeypatch.setattr(boot, "ensure_background_services", lambda: calls.append("full"))
    monkeypatch.setattr(boot, "emit_credentials", lambda mode: calls.append(f"emit:{mode}"))
    monkeypatch.setattr(boot.sys, "argv", ["tui_bootstrap.py", "--emit", "export"])
    boot.main()
    assert calls == ["emit:export"]


def test_main_stale_daemon_runs_full_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.tui_bootstrap as boot

    calls: list[str] = []
    monkeypatch.setattr(boot, "services_already_up", lambda: True)
    monkeypatch.setattr(boot, "daemon_code_stale", lambda: True)
    monkeypatch.setattr(boot, "ensure_background_services", lambda: calls.append("full"))
    monkeypatch.setattr(boot, "emit_credentials", lambda mode: calls.append(f"emit:{mode}"))
    monkeypatch.setattr(boot.sys, "argv", ["tui_bootstrap.py", "--emit", "export"])
    boot.main()
    assert calls == ["full", "emit:export"]


def test_stale_idle_daemon_is_restarted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import backend.daemon.transport as transport
    import scripts.tui_bootstrap as boot

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    calls: list[str] = []
    alive = {"v": True}

    def stop() -> None:
        calls.append("stop")
        alive["v"] = False

    monkeypatch.setattr(transport, "daemon_alive", lambda timeout=0.25: alive["v"])
    monkeypatch.setattr(boot, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(boot, "daemon_code_stale", lambda: True)
    monkeypatch.setattr(boot, "_live_swarm", lambda: False)
    monkeypatch.setattr(boot, "_stop_stale_daemon", stop)
    monkeypatch.setattr(boot, "spawn_background", lambda *a, **k: calls.append("spawn"))
    monkeypatch.setattr(boot, "_wait_until", lambda *a, **k: True)
    monkeypatch.setattr(boot, "_refresh_install_path", lambda: None)

    boot.ensure_background_services()
    assert calls[0] == "stop"
    assert "spawn" in calls


def test_stale_daemon_kept_while_swarm_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import backend.daemon.transport as transport
    import scripts.tui_bootstrap as boot

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(transport, "daemon_alive", lambda timeout=0.25: True)
    monkeypatch.setattr(boot, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(boot, "daemon_code_stale", lambda: True)
    monkeypatch.setattr(boot, "_live_swarm", lambda: True)
    monkeypatch.setattr(boot, "_stop_stale_daemon", lambda: calls.append("stop"))
    monkeypatch.setattr(boot, "spawn_background", lambda *a, **k: calls.append("spawn"))
    monkeypatch.setattr(boot, "_refresh_install_path", lambda: None)

    boot.ensure_background_services()
    assert calls == []


def test_supervisor_stop_runs_bridge_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Manual stop must sweep Cursor bridges even when stream generation advances."""
    import asyncio

    from backend.daemon.state import DaemonState
    from backend.daemon.supervisor import SwarmSupervisor

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
    seen: list[object] = []

    async def _noop_containers(session_id=None):
        return None

    monkeypatch.setattr(
        "backend.sandbox.cleanup_orphan_containers",
        _noop_containers,
    )
    monkeypatch.setattr(
        "backend.process_hygiene.cleanup_orphan_cursor_bridges",
        lambda *a, **k: seen.append(a[0] if a else k.get("session_id")) or [],
    )

    state = DaemonState()
    sup = SwarmSupervisor(state)

    async def run() -> None:
        await sup.stop(session_id="s1")

    asyncio.run(run())
    assert "s1" in seen


def test_child_python_starts_after_stdio_repair(tmp_path: Path) -> None:
    """Parent with repaired stdio can spawn Python with PIPE (the daemon path)."""
    import subprocess

    repo = str(Path(__file__).resolve().parents[1])
    marker = tmp_path / "child.txt"
    worker = rf"""
import asyncio, os, sys
sys.path.insert(0, {repo!r})
from backend.stdio_platform import ensure_standard_streams
from backend.subprocess_platform import swarm_subprocess_kwargs

null = os.open(os.devnull, os.O_RDWR)
os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)
if null > 2:
    os.close(null)
os.close(0)
os.close(1)
os.close(2)
ensure_standard_streams()

async def main():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdout.write('child-ok\\n'); sys.stdout.flush()",
        **swarm_subprocess_kwargs(),
    )
    out, _ = await proc.communicate()
    open({str(marker)!r}, "w", encoding="utf-8").write(f"{{proc.returncode}}:{{out!r}}")

asyncio.run(main())
"""
    r = subprocess.run([sys.executable, "-c", worker], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    body = marker.read_text(encoding="utf-8")
    assert body.startswith("0:")
    assert "child-ok" in body


_BRIDGE_A = (
    "node cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-bridge-ses_a-xyz "
    "--tool-callback-url http://127.0.0.1:1/"
)
_BRIDGE_B = (
    "node cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-bridge-ses_b-xyz "
    "--tool-callback-url http://127.0.0.1:1/"
)
_BRIDGE_LEGACY = (
    "node cursor-sdk-bridge.js --workspace /tmp/ctf-cursor-bridge-abc123 "
    "--tool-callback-url http://127.0.0.1:1/"
)


def test_session_bridge_prefix_is_stable() -> None:
    assert cursor_bridge_session_prefix("ses_a") == "ctf-cursor-bridge-ses_a-"
    assert cursor_bridge_session_prefix(None) == "ctf-cursor-bridge-_default-"
    assert bridge_belongs_to_session(_BRIDGE_A, "ses_a")
    assert not bridge_belongs_to_session(_BRIDGE_A, "ses_b")
    assert not bridge_belongs_to_session(_BRIDGE_LEGACY, "ses_a")


def test_finished_session_does_not_kill_other_window_bridge() -> None:
    """Window A finishing must not reap window B's still-running Cursor bridge."""
    assert not should_kill_cursor_bridge(
        200,
        _BRIDGE_B,
        session_id="ses_a",
        live_swarm_pids={50},
        ppid_of={200: 50},
        me=1,
    )
    assert should_kill_cursor_bridge(
        100,
        _BRIDGE_A,
        session_id="ses_a",
        live_swarm_pids={50},
        ppid_of={100: 1},
        me=1,
    )


def test_live_swarm_child_bridge_is_kept_even_on_global_sweep() -> None:
    assert not should_kill_cursor_bridge(
        200,
        _BRIDGE_B,
        session_id=None,
        live_swarm_pids={50},
        ppid_of={200: 50},
        me=1,
    )
    # Detached/unknown owner while another swarm is live — do not guess.
    assert not should_kill_cursor_bridge(
        300,
        _BRIDGE_LEGACY,
        session_id=None,
        live_swarm_pids={50},
        ppid_of={300: 1},
        me=1,
    )
    # Last swarm gone: leftover bridges can be reaped.
    assert should_kill_cursor_bridge(
        300,
        _BRIDGE_LEGACY,
        session_id=None,
        live_swarm_pids=set(),
        ppid_of={300: 1},
        me=1,
    )
