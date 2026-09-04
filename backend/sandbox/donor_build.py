"""Auto-build pack donor images when missing (Strix-style first-use setup).

Keeps the core+packs model: donors stay optional until needed, then we build
them once instead of telling the operator to run docker by hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from backend.file_lock import acquire as _file_lock_acquire
from backend.file_lock import release as _file_lock_release

logger = logging.getLogger("ctf.sandbox.donor")

# pack_id → (Dockerfile relative to repo root, image tag)
DONOR_BUILD_SPECS: dict[str, tuple[str, str]] = {
    "crypto": ("sandbox/Dockerfile.crypto", "ctf-sandbox-crypto"),
    "crypto-tools": ("sandbox/Dockerfile.crypto-tools", "ctf-sandbox-crypto-tools"),
    "steg": ("sandbox/Dockerfile.steg", "ctf-sandbox-steg"),
    "linux": ("sandbox/Dockerfile.linux", "ctf-sandbox-linux"),
    "mobile": ("sandbox/Dockerfile.mobile", "ctf-sandbox-mobile"),
    "pwn": ("sandbox/Dockerfile.pwn", "ctf-sandbox-pwn"),
    "ghidra": ("sandbox/Dockerfile.ghidra", "ctf-sandbox-ghidra"),
}

# Cap so a hung build cannot freeze the agent forever.
_DEFAULT_BUILD_TIMEOUT_S = 1800

_build_locks: dict[str, asyncio.Lock] = {}
_donor_functional_cache: dict[str, bool] = {}


def repo_root() -> Path:
    env = (os.environ.get("ARTEMIS_REPO_ROOT") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def donor_image_for(pack_id: str) -> str | None:
    spec = DONOR_BUILD_SPECS.get(pack_id)
    return spec[1] if spec else None


def donor_dockerfile_for(pack_id: str) -> Path | None:
    spec = DONOR_BUILD_SPECS.get(pack_id)
    if not spec:
        return None
    return repo_root() / spec[0]


def _lock_for(pack_id: str) -> asyncio.Lock:
    lock = _build_locks.get(pack_id)
    if lock is None:
        lock = asyncio.Lock()
        _build_locks[pack_id] = lock
    return lock


def _donor_flock_path(pack_id: str) -> Path:
    from backend.tool_router import pack_cache_root

    d = pack_cache_root() / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d / ".donor-build.lock"


def _acquire_donor_flock(pack_id: str) -> int:
    path = _donor_flock_path(pack_id)
    logger.info("Donor %s: waiting for cross-process build lock (%s)", pack_id, path)
    fd = _file_lock_acquire(path)
    logger.info("Donor %s: acquired cross-process build lock", pack_id)
    return fd


def _release_donor_flock(fd: int) -> None:
    _file_lock_release(fd)


async def _donor_guest_libs_ok(image: str) -> bool:
    from backend.sandbox.docker_client import _docker_cli
    from backend.sandbox.guest_libs import GUEST_LIB_PATHS

    rc, _, _ = await _docker_cli(
        "run",
        "--rm",
        "--entrypoint",
        "python3",
        image,
        "-c",
        "import os,sys; need="
        + repr(list(GUEST_LIB_PATHS))
        + "; sys.exit(0 if all(os.path.exists(p) for p in need) else 1)",
        timeout_s=60,
    )
    return rc == 0


async def _donor_image_functional(pack_id: str, image: str) -> bool:
    """True when a runtime-capable donor image has the baked tools we expect."""
    if pack_id not in ("pwn", "mobile"):
        return True
    cache_key = f"{pack_id}:{image}"
    cached = _donor_functional_cache.get(cache_key)
    if cached is not None:
        return cached
    from backend.sandbox.docker_client import _docker_cli

    ok = True
    if pack_id == "pwn":
        rc, _, _ = await _docker_cli(
            "run",
            "--rm",
            "--entrypoint",
            "python3",
            image,
            "-c",
            "import pwn, keystone, capstone",
            timeout_s=120,
        )
        ok = rc == 0
    if ok:
        ok = await _donor_guest_libs_ok(image)
    _donor_functional_cache[cache_key] = ok
    return ok


async def ensure_donor_image(pack_id: str) -> tuple[bool, str]:
    """Ensure the donor Docker image for ``pack_id`` exists.

    Returns ``(ok, message)``. When the image is already present, ok=True with
    a short note. When missing, attempts ``docker build`` once (serialized per
    pack, cross-process). Failures leave ok=False with an operator-facing message.
    """
    spec = DONOR_BUILD_SPECS.get(pack_id)
    if not spec:
        return True, f"pack {pack_id} has no donor image"

    dockerfile_rel, image = spec
    root = repo_root()
    dockerfile = root / dockerfile_rel
    if not dockerfile.is_file():
        return False, f"Dockerfile missing: {dockerfile}"

    if pack_id == "pwn":
        from backend.sandbox.setup_bake import ensure_core_image

        ok, msg = await ensure_core_image()
        if not ok:
            return False, msg

    from backend.sandbox.docker_client import _docker_cli

    # Fast path: already built and (for pwn) has the baked runtime stack.
    rc, _, _ = await _docker_cli("image", "inspect", image, timeout_s=30)
    if rc == 0:
        if await _donor_image_functional(pack_id, image):
            return True, f"donor {image} already present"
        logger.info(
            "Donor %s exists but lacks baked runtime / guest libs — rebuilding",
            image,
        )
        await _docker_cli("rmi", "-f", image, timeout_s=120)

    async with _lock_for(pack_id):
        fd = await asyncio.to_thread(_acquire_donor_flock, pack_id)
        try:
            # Another waiter / process may have finished the build.
            rc, _, _ = await _docker_cli("image", "inspect", image, timeout_s=30)
            if rc == 0:
                if await _donor_image_functional(pack_id, image):
                    return True, f"donor {image} already present"
                logger.info(
                    "Donor %s exists but lacks baked runtime / guest libs — rebuilding",
                    image,
                )
                await _docker_cli("rmi", "-f", image, timeout_s=120)

            from backend.sandbox.docker_hygiene import (
                cleanup_sandbox_build_context,
                prepare_sandbox_build_context,
            )

            timeout_s = int(
                os.environ.get("ARTEMIS_DONOR_BUILD_TIMEOUT_S", str(_DEFAULT_BUILD_TIMEOUT_S))
            )
            ctx = await asyncio.to_thread(prepare_sandbox_build_context, root)
            try:
                dockerfile_name = Path(dockerfile_rel).name
                logger.info(
                    "Building missing donor image %s from %s (context=%s timeout=%ss)",
                    image,
                    dockerfile_name,
                    ctx,
                    timeout_s,
                )
                # Surface progress into the agent bash stream via stderr logger.
                build_args: list[str] = [
                    "build",
                    "-f",
                    str(ctx / dockerfile_name),
                    "-t",
                    image,
                ]
                if pack_id == "crypto-tools":
                    jobs = (os.environ.get("ARTEMIS_DONOR_BUILD_JOBS") or "2").strip() or "2"
                    build_args.extend(["--build-arg", f"BUILD_JOBS={jobs}"])
                rc, out, err = await _docker_cli(
                    *build_args,
                    str(ctx),
                    timeout_s=timeout_s,
                )
                if rc != 0:
                    detail = (err or out or "").strip()[-800:]
                    logger.warning("Donor build failed for %s: %s", image, detail)
                    return (
                        False,
                        f"Auto-build of {image} failed (exit {rc}). "
                        f"Build manually after scrubbing AppleDouble (._*) files, or: "
                        f"docker build -f {dockerfile_name} -t {image} <clean-sandbox-copy>. "
                        f"Detail: {detail or 'no output'}",
                    )
                logger.info("Donor image ready: %s", image)
                return True, f"Built donor {image}"
            finally:
                await asyncio.to_thread(cleanup_sandbox_build_context, ctx)
        finally:
            await asyncio.to_thread(_release_donor_flock, fd)
