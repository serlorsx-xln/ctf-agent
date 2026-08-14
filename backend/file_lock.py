"""Cross-process exclusive file locks (Unix fcntl + Windows msvcrt)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _lock_byte(fd: int, *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(fd, mode, 1)
        return

    import fcntl

    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(fd, flags)


def _unlock_byte(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def acquire(path: Path) -> int:
    """Block until this process owns an exclusive lock on ``path``."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    _lock_byte(fd)
    return fd


def try_acquire(path: Path) -> int | None:
    """Non-blocking exclusive lock. ``None`` if another process holds it."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _lock_byte(fd, blocking=False)
    except OSError:
        os.close(fd)
        return None
    return fd


def release(fd: int) -> None:
    try:
        _unlock_byte(fd)
    finally:
        os.close(fd)
