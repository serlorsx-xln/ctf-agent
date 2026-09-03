"""Host hygiene for Docker builds on macOS/ExFAT (AppleDouble / xattr)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("ctf.sandbox.docker_hygiene")


def scrub_appledouble(root: Path, *, limit: int = 50_000) -> int:
    """Delete AppleDouble ``._*`` sidecars under ``root``.

    Docker BuildKit on ExFAT/USB often fails with
    ``failed to xattr .../._FILE: operation not permitted`` even when
    ``.dockerignore`` lists them — removing the files is the reliable fix.
    """
    removed = 0
    try:
        root = root.resolve()
    except OSError:
        return 0
    if not root.is_dir():
        return 0
    for path in root.rglob("._*"):
        if removed >= limit:
            break
        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    # Finder metadata
    for path in root.rglob(".DS_Store"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
    if removed:
        logger.info("Removed %s AppleDouble sidecar(s) under %s", removed, root)
    return removed


def sandbox_build_context(repo: Path) -> Path:
    """Source tree for Artemis images — only ``sandbox/`` is needed."""
    return repo / "sandbox"


def _ignore_apple_cruft(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.startswith("._") or n in (".DS_Store", ".AppleDouble")}


def prepare_sandbox_build_context(repo: Path) -> Path:
    """Return a BuildKit-safe context directory for ``docker build``.

    On ExFAT / USB volumes, Docker Desktop fails reading xattrs on AppleDouble
    files. We scrub the source, then copy ``sandbox/`` into the Artemis cache
    (typically APFS) excluding ``._*`` so the build context is clean.

    Each call gets a unique destination so concurrent donor/core builds cannot
    ``rmtree`` a context another ``docker build`` is still reading.
    """
    import os
    import uuid

    from backend.cache import cache_dir

    # macOS: do not create AppleDouble sidecars while copying.
    os.environ.setdefault("COPYFILE_DISABLE", "1")

    src = sandbox_build_context(repo)
    if not src.is_dir():
        raise FileNotFoundError(f"sandbox context missing: {src}")
    # Only scrub sandbox/ — walking the whole repo (chassis/node_modules) on
    # ExFAT is extremely slow and unnecessary for the build context.
    scrub_appledouble(src)

    dest = cache_dir() / "docker-ctx" / f"sandbox-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=_ignore_apple_cruft)
    scrub_appledouble(dest)
    logger.info("Docker build context ready at %s", dest)
    return dest


def cleanup_sandbox_build_context(ctx: Path | None) -> None:
    """Best-effort remove a unique build context after ``docker build`` finishes."""
    if ctx is None:
        return
    try:
        if ctx.is_dir() and "docker-ctx" in ctx.parts:
            shutil.rmtree(ctx, ignore_errors=True)
    except OSError:
        logger.debug("cleanup_sandbox_build_context failed for %s", ctx, exc_info=True)
