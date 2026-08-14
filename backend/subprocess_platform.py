"""Cross-platform asyncio / Popen helpers for Artemis daemons and swarms."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

# (resolved_path_str, mtime_ns) -> ok. Avoids 20–40ms ``python -c`` on every spawn.
_smoke_cache: dict[tuple[str, int], bool] = {}


def detached_subprocess_kwargs() -> dict[str, object]:
    """Isolate swarm/daemon child processes from parent signals / console."""
    if sys.platform == "win32":
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
        # Avoid flashing consoles when the TUI bootstrap starts services.
        no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        return {"creationflags": flags | no_window}
    return {"start_new_session": True}


def background_popen_kwargs(
    *,
    log_file: IO[Any] | None = None,
) -> dict[str, object]:
    """kwargs for fire-and-forget service processes (daemon, stub).

    Always sets stdin/stdout/stderr explicitly so children never inherit a pipe
    that dies with the bootstrap parent (``eval "$(tui_bootstrap)"`` / creds
    redirect on Windows).
    """
    kwargs: dict[str, object] = {
        **detached_subprocess_kwargs(),
        "stdin": subprocess.DEVNULL,
    }
    if log_file is not None:
        kwargs["stdout"] = log_file
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return kwargs


def swarm_subprocess_kwargs() -> dict[str, object]:
    """kwargs for daemon → swarm spawn (capture stdout, never inherit stdin)."""
    import asyncio

    return {
        **detached_subprocess_kwargs(),
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }


def resolve_venv_python(repo: str | Path | None = None) -> Path | None:
    """Return a runnable project venv interpreter, or None."""
    root = Path(repo) if repo else Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        candidates = (
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python",
        )
    else:
        candidates = (root / ".venv" / "bin" / "python",)

    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        exe = Path(sys.executable)

    for cand in candidates:
        if not cand.is_file() or not os.access(cand, os.X_OK):
            continue
        try:
            if cand.resolve() == exe:
                return cand
        except OSError:
            pass
        if _python_smoke_ok(cand):
            return cand
    return None


def _python_smoke_ok(py: Path) -> bool:
    """True when ``py -c`` starts (filters broken USB/moved venv symlinks)."""
    try:
        key_path = str(py.resolve())
        mtime = py.stat().st_mtime_ns
    except OSError:
        return False
    cache_key = (key_path, mtime)
    cached = _smoke_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        r = subprocess.run(
            [str(py), "-c", "import sys; sys.exit(0)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _smoke_cache[cache_key] = ok
    # Bound cache size (USB re-copy creates new mtimes anyway).
    if len(_smoke_cache) > 32:
        _smoke_cache.clear()
        _smoke_cache[cache_key] = ok
    return ok


def swarm_command(repo: str | Path, swarm_args: list[str]) -> list[str]:
    """Build argv for a swarm child — prefer venv python over ``uv run``.

    ``uv run`` adds an extra process and can rematerialize envs on USB / slow
    disks. The daemon already runs inside the project venv, so invoke
    ``python -m backend.cli swarm …`` directly when possible.
    """
    root = Path(repo)
    py = resolve_venv_python(root)
    if py is not None:
        return [str(py), "-m", "backend.cli", "swarm", *swarm_args]
    uv_name = "uv.exe" if sys.platform == "win32" else "uv"
    uv = Path.home() / ".local" / "bin" / uv_name
    if uv.is_file():
        return [str(uv), "run", "--directory", str(root), "artemis", "swarm", *swarm_args]
    return ["uv", "run", "--directory", str(root), "artemis", "swarm", *swarm_args]


def windows_system_root() -> Path:
    return Path(os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows")


def windows_system_exe(name: str) -> str:
    """Absolute System32/PowerShell path so stripped PATH still finds the tool."""
    if sys.platform != "win32":
        return name
    root = windows_system_root()
    sys32 = root / "System32"
    key = name.lower().removesuffix(".exe")
    if key == "cmd":
        comspec = (os.environ.get("COMSPEC") or "").strip()
        if comspec and Path(comspec).is_file():
            return comspec
        return str(sys32 / "cmd.exe")
    if key == "powershell":
        pshome = (os.environ.get("PSHOME") or "").strip()
        if pshome:
            cand = Path(pshome) / "powershell.exe"
            if cand.is_file():
                return str(cand)
        return str(sys32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    cand = sys32 / f"{key}.exe"
    return str(cand) if cand.is_file() else name


def resolve_docker_exe() -> str:
    import shutil

    found = shutil.which("docker")
    if found:
        return found
    if sys.platform == "win32":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        cand = Path(pf) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        if cand.is_file():
            return str(cand)
    return "docker"


def ensure_windows_system_path(env: dict[str, str]) -> dict[str, str]:
    """Prepend System32 so child processes find cmd/taskkill on stripped PATH."""
    if sys.platform != "win32":
        return env
    root = windows_system_root()
    need = (
        root / "System32",
        root / "System32" / "Wbem",
        root / "System32" / "WindowsPowerShell" / "v1.0",
    )
    raw = env.get("PATH") or env.get("Path") or ""
    parts = [p.strip().rstrip("\\") for p in raw.split(";") if p.strip()]
    have = {p.lower() for p in parts}
    prefix = [str(d) for d in need if d.is_dir() and str(d).rstrip("\\").lower() not in have]
    if prefix:
        env["PATH"] = ";".join([*prefix, raw] if raw else prefix)
        env["Path"] = env["PATH"]
    return env


def sanitize_child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy ``base``/``os.environ`` without USB-hostile interpreter overrides."""
    env = dict(base if base is not None else os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return ensure_windows_system_path(env)
