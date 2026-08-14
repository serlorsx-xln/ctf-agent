"""Best-effort cleanup of leftover Artemis / Cursor SDK helper processes.

Orphan ``cursor-sdk-bridge`` workers (from crashed swarms) spin at high CPU and
can leave the machine in a bad state for new Python children. Sweep them on
daemon boot and after swarm stop — only match CTF-scoped workspaces, never
arbitrary Cursor IDE bridges. A finished session must not reap another
window's still-running swarm bridge.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_CTF_BRIDGE_RE = re.compile(r"ctf-cursor-[A-Za-z0-9_-]+")
_BRIDGE_MARKERS = ("cursor-sdk-bridge", "cursor_sdk/_vendor/bridge")
_SESSION_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SWARM_NEEDLES = (
    "artemis race",
    "artemis swarm",
    "backend.cli swarm",
    "backend.cli race",
    "backend/cli.py swarm",
    "backend/cli.py race",
    "backend.shell.bridge race",
    "backend.shell.bridge swarm",
    "shell.bridge race",
    "shell.bridge swarm",
)


def is_artemis_cursor_bridge_command(cmd: str) -> bool:
    """True for Cursor SDK bridges launched for Artemis CTF workspaces."""
    if not cmd:
        return False
    lower = cmd.lower().replace("\\", "/")
    if not any(m in lower for m in _BRIDGE_MARKERS):
        return False
    return bool(_CTF_BRIDGE_RE.search(cmd))


def _rows_from_lines(out: str, *, tab: bool) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if tab:
            pid_s, _, cmd = line.partition("\t")
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_s, cmd = parts
        if pid_s.isdigit() and cmd:
            rows.append((int(pid_s), cmd))
    return rows


def _iter_bridge_candidates() -> list[tuple[int, str]]:
    """Fast scan: only processes that might be Artemis Cursor bridges."""
    try:
        if sys.platform == "win32":
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'cursor-sdk-bridge|ctf-cursor-' } | "
                "ForEach-Object { '{0}\t{1}' -f $_.ProcessId, $_.CommandLine }"
            )
            from backend.subprocess_platform import windows_system_exe

            out = subprocess.check_output(
                [windows_system_exe("powershell"), "-NoProfile", "-Command", ps],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            return _rows_from_lines(out, tab=True)

        rows: list[tuple[int, str]] = []
        seen: set[int] = set()
        for pat in ("cursor-sdk-bridge", "ctf-cursor-"):
            try:
                out = subprocess.check_output(
                    ["pgrep", "-fl", pat],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except subprocess.CalledProcessError:
                continue
            for pid, cmd in _rows_from_lines(out, tab=False):
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append((pid, cmd))
        return rows
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return _iter_process_commands_fallback()


def _iter_process_commands_fallback() -> list[tuple[int, str]]:
    """Full process list — only when pgrep/filtered CIM unavailable."""
    if sys.platform == "win32":
        return []
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    return _rows_from_lines(out, tab=False)


def _kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        try:
            from backend.subprocess_platform import windows_system_exe

            subprocess.run(
                [windows_system_exe("taskkill"), "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    for _ in range(5):
        time.sleep(0.01)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def pid_command(pid: int) -> str:
    """Best-effort command line for ``pid`` (Unix ``ps`` + Windows CIM)."""
    if pid <= 0:
        return ""
    if sys.platform == "win32":
        try:
            ps = (
                f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\"; "
                "if ($p) { $p.CommandLine }"
            )
            from backend.subprocess_platform import windows_system_exe

            out = subprocess.check_output(
                [windows_system_exe("powershell"), "-NoProfile", "-Command", ps],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return out.strip()
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            OSError,
            subprocess.TimeoutExpired,
        ):
            return ""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return out.strip()
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        OSError,
        subprocess.TimeoutExpired,
    ):
        return ""


def windows_image_looks_like_python(pid: int) -> bool:
    """True when a Windows PID's image name/path is python or uv.

    Used when Win32_Process.CommandLine is empty (common on some hosts).
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    try:
        ps = (
            f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\"; "
            "if ($p) { $p.Name + '|' + $p.ExecutablePath }"
        )
        from backend.subprocess_platform import windows_system_exe

        out = subprocess.check_output(
            [windows_system_exe("powershell"), "-NoProfile", "-Command", ps],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(tok in out for tok in ("python", "uv.exe", "uv "))


def is_artemis_swarm_command(cmd: str) -> bool:
    """True when cmdline looks like an Artemis swarm / race child."""
    if not cmd:
        return False
    lower = cmd.lower().replace("\\", "/")
    return any(n in lower for n in _SWARM_NEEDLES)


def _session_bridge_token(session_id: str | None) -> str:
    raw = (session_id or "").strip() or "_default"
    return _SESSION_TOKEN_RE.sub("_", raw)[:48]


def cursor_bridge_session_prefix(session_id: str | None = None) -> str:
    """``tempfile.mkdtemp`` prefix for this session's Cursor SDK workspace."""
    return f"ctf-cursor-bridge-{_session_bridge_token(session_id)}-"


def bridge_belongs_to_session(cmd: str, session_id: str | None) -> bool:
    """True when ``cmd`` is tagged for ``session_id``.

    Untagged legacy workspaces (``ctf-cursor-bridge-`` with no session token)
    do not match a specific session — another window may still own them.
    """
    if not cmd or session_id is None:
        return bool(cmd)
    needle = cursor_bridge_session_prefix(session_id)
    return needle in cmd.replace("\\", "/")


def _ppid_map() -> dict[int, int]:
    """Best-effort pid → parent pid. Empty when the process table is unavailable."""
    mapping: dict[int, int] = {}
    try:
        if sys.platform == "win32":
            ps = (
                "Get-CimInstance Win32_Process | "
                "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.ParentProcessId }"
            )
            from backend.subprocess_platform import windows_system_exe

            out = subprocess.check_output(
                [windows_system_exe("powershell"), "-NoProfile", "-Command", ps],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            out = subprocess.check_output(
                ["ps", "-ax", "-o", "pid=,ppid="],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            mapping[int(parts[0])] = int(parts[1])
    return mapping


def _iter_swarm_candidates() -> list[tuple[int, str]]:
    """Processes that might be an Artemis swarm / race child."""
    try:
        if sys.platform == "win32":
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'artemis swarm|artemis race|backend.cli swarm|backend.cli race' } | "
                "ForEach-Object { '{0}\t{1}' -f $_.ProcessId, $_.CommandLine }"
            )
            from backend.subprocess_platform import windows_system_exe

            out = subprocess.check_output(
                [windows_system_exe("powershell"), "-NoProfile", "-Command", ps],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            return _rows_from_lines(out, tab=True)
        rows: list[tuple[int, str]] = []
        seen: set[int] = set()
        for pat in ("backend.cli swarm", "backend.cli race", "artemis swarm", "artemis race"):
            try:
                out = subprocess.check_output(
                    ["pgrep", "-fl", pat],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except subprocess.CalledProcessError:
                continue
            for pid, cmd in _rows_from_lines(out, tab=False):
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append((pid, cmd))
        return rows
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []


def _live_artemis_swarm_pids() -> set[int]:
    return {pid for pid, cmd in _iter_swarm_candidates() if is_artemis_swarm_command(cmd)}


def _bridge_owned_by_live_swarm(
    pid: int,
    live_swarm_pids: set[int],
    ppid_of: dict[int, int],
) -> bool:
    """True when ``pid`` is a descendant of a still-running swarm process."""
    if pid <= 0 or not live_swarm_pids:
        return False
    seen: set[int] = set()
    cur = int(pid)
    for _ in range(32):
        if cur in seen or cur <= 1:
            return False
        seen.add(cur)
        parent = int(ppid_of.get(cur, 0) or 0)
        if parent in live_swarm_pids:
            return True
        cur = parent
    return False


def should_kill_cursor_bridge(
    pid: int,
    cmd: str,
    *,
    session_id: str | None = None,
    live_swarm_pids: set[int] | None = None,
    ppid_of: dict[int, int] | None = None,
    me: int | None = None,
) -> bool:
    """Whether this Artemis Cursor bridge is safe to reap.

    Finishing or quitting one TUI window must not kill another window's live
    grok/Cursor bridge. Session-tagged workspaces are reaped only for that
    session; untagged legacy bridges are left alone while any swarm is live.
    """
    if pid <= 0 or pid == (os.getpid() if me is None else me):
        return False
    if not is_artemis_cursor_bridge_command(cmd):
        return False
    live = live_swarm_pids if live_swarm_pids is not None else set()
    parents = ppid_of if ppid_of is not None else {}
    if _bridge_owned_by_live_swarm(pid, live, parents):
        return False
    if session_id is not None:
        return bridge_belongs_to_session(cmd, session_id)
    # Global sweep (daemon boot / last swarm gone): do not guess at detached
    # bridges while another window's swarm is still running.
    return not live


def cleanup_orphan_cursor_bridges(session_id: str | None = None) -> list[int]:
    """Kill leftover Artemis Cursor SDK bridge processes. Returns killed pids.

    When ``session_id`` is set, only that session's tagged bridges are
    considered. Bridges still parented by a live swarm are never killed.
    """
    killed: list[int] = []
    me = os.getpid()
    live = _live_artemis_swarm_pids()
    parents = _ppid_map()
    for pid, cmd in _iter_bridge_candidates():
        if not should_kill_cursor_bridge(
            pid,
            cmd,
            session_id=session_id,
            live_swarm_pids=live,
            ppid_of=parents,
            me=me,
        ):
            continue
        logger.info(
            "killing orphan Cursor SDK bridge pid=%s session=%s",
            pid,
            session_id or "*",
        )
        _kill_pid(pid)
        killed.append(pid)
    if killed:
        logger.info("cleaned %d orphan Cursor SDK bridge process(es)", len(killed))
    return killed
