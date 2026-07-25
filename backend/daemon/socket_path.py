"""Resolve the daemon Unix socket path under the Artemis cache dir."""

from __future__ import annotations

from pathlib import Path

from backend.cache import cache_dir  # re-export for back-compat

__all__ = ["cache_dir", "daemon_socket_path"]


def daemon_socket_path() -> Path:
    """Path to the daemon Unix socket (``daemon.sock`` under the cache dir)."""
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "daemon.sock"
