"""Cross-platform temp dirs for daemon integration tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def daemon_cache_dir(prefix: str) -> Path:
    """Short-lived cache dir — avoids hardcoded ``/tmp`` (missing on Windows)."""
    d = Path(tempfile.gettempdir()) / prefix / str(os.getpid())
    d.mkdir(parents=True, exist_ok=True)
    return d
