"""Cross-platform helpers for Artemis runtime."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("ctf.platform")


def ensure_docker_bind_dir(path: str | Path) -> None:
    """Ensure a host directory can be bind-mounted by Docker Desktop on Windows.

    ``tempfile.mkdtemp`` from SSH / non-interactive sessions often omits inherited
    user ACLs; Docker then returns ``Access is denied`` for new workspace binds.
    """
    if sys.platform != "win32":
        return
    p = str(Path(path).resolve())
    try:
        proc = subprocess.run(
            ["icacls", p, "/inheritance:e"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "Could not enable ACL inheritance on %s: %s",
                p,
                (proc.stderr or proc.stdout or "").strip()[:200],
            )
    except OSError as e:
        logger.warning("Could not fix bind dir ACL on %s: %s", p, e)


def docker_volume_path(path: str | Path) -> str:
    """Host path for ``docker run -v`` / aiodocker binds (Windows-safe)."""
    p = Path(path).resolve()
    if sys.platform == "win32":
        # Docker Desktop accepts forward slashes on Windows hosts.
        return str(p).replace("\\", "/")
    return str(p)
