"""Best-effort cleanup of leftover Artemis / Cursor SDK helper processes.

Orphan ``cursor-sdk-bridge`` workers (from crashed swarms) spin at high CPU and
can leave the machine in a bad state for new Python children. Sweep them on
daemon boot and after swarm stop — only match CTF-scoped workspaces, never
arbitrary Cursor IDE bridges.
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
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
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
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
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
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
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
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
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


def cleanup_orphan_cursor_bridges() -> list[int]:
    """Kill leftover Artemis Cursor SDK bridge processes. Returns killed pids."""
    killed: list[int] = []
    me = os.getpid()
    for pid, cmd in _iter_bridge_candidates():
        if pid == me:
            continue
        if not is_artemis_cursor_bridge_command(cmd):
            continue
        logger.info("killing orphan Cursor SDK bridge pid=%s", pid)
        _kill_pid(pid)
        killed.append(pid)
    if killed:
        logger.info("cleaned %d orphan Cursor SDK bridge process(es)", len(killed))
    return killed
