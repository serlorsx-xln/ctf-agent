"""POSIX container path helpers (Windows host safe)."""

from __future__ import annotations

from pathlib import PurePosixPath


def container_path_parts(path: str) -> tuple[str, str]:
    """Return (parent, basename) as POSIX strings for Docker archive APIs."""
    p = PurePosixPath(path)
    parent = p.parent
    parent_str = "/" if str(parent) == "." else str(parent)
    return parent_str, p.name
