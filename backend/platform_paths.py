"""Cross-platform helpers for Artemis runtime."""

from __future__ import annotations

import sys
from pathlib import Path


def docker_volume_path(path: str | Path) -> str:
    """Host path for ``docker run -v`` / aiodocker binds (Windows-safe)."""
    p = Path(path).resolve()
    if sys.platform == "win32":
        # Docker Desktop accepts forward slashes on Windows hosts.
        return str(p).replace("\\", "/")
    return str(p)
