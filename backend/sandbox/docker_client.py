"""aiodocker client, start semaphore, orphan cleanup, docker CLI helper."""

from __future__ import annotations

import asyncio
import logging
import os

import aiodocker

logger = logging.getLogger("ctf.sandbox")

CONTAINER_LABEL = "ctf-agent"
OWNER_PID_LABEL = "ctf-agent.owner-pid"

# Concurrency control
_start_semaphore: asyncio.Semaphore | None = None
_active_count: int = 0
_count_lock = asyncio.Lock()

_WARN_THRESHOLDS = {100, 200, 500}


def _docker_client() -> aiodocker.Docker:
    """Connect like the Docker CLI: DOCKER_HOST wins over ~/.docker currentContext.

    aiodocker prefers currentContext (e.g. desktop-linux) over DOCKER_HOST, which
    breaks Colima setups where the CLI sees images but the agent does not.
    """
    host = os.environ.get("DOCKER_HOST")
    if host:
        return aiodocker.Docker(url=host)
    return aiodocker.Docker()


def configure_semaphore(max_concurrent: int = 50) -> None:
    """Set the max concurrent container starts. Call once at startup."""
    global _start_semaphore
    _start_semaphore = asyncio.Semaphore(max_concurrent)


async def _track_start() -> None:
    global _active_count
    async with _count_lock:
        _active_count += 1
        if _active_count in _WARN_THRESHOLDS:
            logger.warning("Active containers: %d", _active_count)


async def _track_stop() -> None:
    global _active_count
    async with _count_lock:
        _active_count = max(0, _active_count - 1)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def cleanup_orphan_containers() -> None:
    """Remove leftover ctf-agent containers without killing concurrent solvers.

    Containers labeled with a live owner PID are left alone so two ``ctf-solve``
    processes can run at once. Unlabeled running containers are also kept
    (legacy / in-flight). Only true orphans (dead owner, or exited unlabeled)
    are force-deleted.
    """
    try:
        docker = _docker_client()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [CONTAINER_LABEL]},
            )
            removed = 0
            skipped = 0
            for c in containers:
                try:
                    info = await c.show()
                    labels = (info.get("Config") or {}).get("Labels") or {}
                    owner = (labels.get(OWNER_PID_LABEL) or "").strip()
                    status = ((info.get("State") or {}).get("Status") or "").lower()
                    if owner.isdigit() and _pid_alive(int(owner)):
                        skipped += 1
                        continue
                    if not owner and status in {"running", "created", "restarting"}:
                        # No owner label but still live — likely a concurrent run
                        # started before owner labeling; do not steal it.
                        skipped += 1
                        continue
                    await c.delete(force=True)
                    removed += 1
                except Exception:
                    pass
            if removed:
                logger.info(
                    "Cleaned up %d orphan container(s) (kept %d live)",
                    removed,
                    skipped,
                )
        finally:
            await docker.close()
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)


async def _docker_cli(*args: str, timeout_s: float = 600) -> tuple[int, str, str]:
    """Run the host `docker` CLI (respects DOCKER_HOST)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"docker {' '.join(args)} timed out after {timeout_s}s"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )
