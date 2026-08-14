"""Fingerprint the backend tree so launch can replace a stale daemon."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def backend_code_stamp(repo: Path | None = None) -> str:
    """Stable hash of ``backend/**/*.py`` sizes + mtimes (excludes pycache)."""
    root = (repo or Path(__file__).resolve().parents[2]) / "backend"
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:20]


def pid_path(cache: Path) -> Path:
    return cache / "daemon.pid"


def write_running_stamp(cache: Path, repo: Path | None = None, *, pid: int | None = None) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "daemon.code").write_text(backend_code_stamp(repo), encoding="utf-8")
    if pid is None:
        pid = os.getpid()
    pid_path(cache).write_text(str(pid), encoding="utf-8")


def running_stamp(cache: Path) -> str:
    try:
        return (cache / "daemon.code").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
