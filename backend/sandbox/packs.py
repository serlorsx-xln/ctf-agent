"""Pack cache flock / readiness helpers (materialize lives on DockerSandbox)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from backend.file_lock import acquire as _file_lock_acquire
from backend.file_lock import release as _file_lock_release

logger = logging.getLogger("ctf.sandbox")

_pack_cache_locks: dict[str, asyncio.Lock] = {}
_pack_cache_locks_mu = asyncio.Lock()


async def _pack_cache_lock(pack_id: str) -> asyncio.Lock:
    async with _pack_cache_locks_mu:
        lock = _pack_cache_locks.get(pack_id)
        if lock is None:
            lock = asyncio.Lock()
            _pack_cache_locks[pack_id] = lock
        return lock


def _pack_cache_lock_path(pack_id: str) -> Path:
    """Cross-process lock file: one extract per pack_id globally."""
    from backend.tool_router import pack_cache_root

    d = pack_cache_root() / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d / ".extract.lock"


def _acquire_pack_flock(pack_id: str) -> int:
    """Block until this process owns exclusive extract rights for ``pack_id``."""
    path = _pack_cache_lock_path(pack_id)
    logger.info("Pack %s: waiting for cross-process extract lock (%s)", pack_id, path)
    fd = _file_lock_acquire(path)
    logger.info("Pack %s: acquired cross-process extract lock", pack_id)
    return fd


def _release_pack_flock(fd: int) -> None:
    _file_lock_release(fd)


def _pack_cache_is_ready(pack_id: str) -> bool:
    from backend.sandbox.setup_bake import pack_cache_incomplete, pack_cache_stale
    from backend.tool_router import PACK_SPECS, pack_cache_dir

    spec = PACK_SPECS.get(pack_id)
    if not spec:
        return False
    cache = pack_cache_dir(pack_id)
    marker = cache / ".ready"
    if not marker.is_file():
        return False
    if pack_cache_stale(pack_id):
        return False
    if pack_cache_incomplete(pack_id):
        return False
    return all((cache / p.lstrip("/")).exists() for p in spec.paths)
