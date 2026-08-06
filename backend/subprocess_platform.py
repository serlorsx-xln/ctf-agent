"""Cross-platform asyncio subprocess helpers."""

from __future__ import annotations

import subprocess
import sys


def detached_subprocess_kwargs() -> dict[str, object]:
    """Isolate swarm/daemon child processes from parent signals."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
