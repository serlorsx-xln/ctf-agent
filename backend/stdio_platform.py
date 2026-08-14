"""Keep stdin/stdout/stderr valid across daemon / detached process lifecycles.

Background launchers often redirect stdio to ``DEVNULL`` or inherit a pipe that
closes when the parent exits. On macOS especially, a closed or half-dead std
fd makes a newly spawned Python die before user code with:

  Fatal Python error: init_sys_streams: can't initialize sys standard streams
  OSError: [Errno 9] Bad file descriptor

Call ``ensure_standard_streams()`` at process entry for long-lived services,
and always pass an explicit ``stdin`` when spawning children from those services.
"""

from __future__ import annotations

import os
import sys


def fd_is_valid(fd: int) -> bool:
    """True when ``fd`` refers to an open file description."""
    if fd < 0:
        return False
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def ensure_standard_streams() -> None:
    """Re-open closed/invalid fds 0/1/2 onto ``os.devnull``.

    Safe to call multiple times. Does not replace healthy streams (TTY, pipes,
    files). Idempotent for services started with ``stdout=DEVNULL``.
    """
    targets: tuple[tuple[int, int], ...] = (
        (0, os.O_RDONLY),
        (1, os.O_WRONLY),
        (2, os.O_WRONLY),
    )
    for fd, flags in targets:
        if fd_is_valid(fd):
            continue
        try:
            null_fd = os.open(os.devnull, flags)
        except OSError:
            continue
        try:
            if null_fd != fd:
                os.dup2(null_fd, fd)
                os.close(null_fd)
        except OSError:
            try:
                if null_fd != fd:
                    os.close(null_fd)
            except OSError:
                pass

    # Keep Python's sys.* objects usable if they were left pointing at a dead fd.
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        stream = getattr(sys, name, None)
        try:
            if stream is None or getattr(stream, "closed", False):
                raise OSError("closed")
            stream.fileno()
        except (OSError, ValueError, AttributeError):
            try:
                setattr(sys, name, open(os.devnull, mode, encoding="utf-8", errors="replace"))
            except OSError:
                pass
