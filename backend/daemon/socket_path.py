"""Resolve daemon paths under the Artemis cache dir."""

from __future__ import annotations

from backend.cache import cache_dir
from backend.daemon.transport import daemon_socket_path

__all__ = ["cache_dir", "daemon_socket_path"]
