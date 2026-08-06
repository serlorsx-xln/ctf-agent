"""Sandbox memory / CPU resource policy."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from backend.tool_router import parse_memory_bytes, recommended_memory_limit

logger = logging.getLogger("ctf.sandbox")

# Re-export for callers that prefer the governor surface.
__all__ = [
    "recommended_memory_limit",
    "parse_memory_bytes",
    "sandbox_nano_cpus",
    "apply_live_memory",
]


def sandbox_nano_cpus() -> int:
    """Fixed CPU quota for agent sandboxes (env override only — no per-challenge spoilers)."""
    raw = (os.environ.get("CTF_SANDBOX_NANO_CPUS") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid CTF_SANDBOX_NANO_CPUS=%r; using default 2e9", raw)
    return 2_000_000_000


async def apply_live_memory(
    container_id: str,
    new_limit: str,
    *,
    docker_cli: Callable[..., Awaitable[tuple[int, str, str]]] | None = None,
) -> bool:
    """Raise a running container's Memory/MemorySwap via `docker update`."""
    if docker_cli is None:
        from backend.sandbox.docker_client import _docker_cli

        docker_cli = _docker_cli
    rc, _, err = await docker_cli(
        "update",
        f"--memory={new_limit}",
        f"--memory-swap={new_limit}",
        container_id,
        timeout_s=60,
    )
    if rc != 0:
        logger.warning(
            "Could not raise container memory to %s: %s",
            new_limit,
            (err or "").strip()[:300],
        )
        return False
    return True
