"""Phase 3: customer ``artemis setup`` — warm L0 + common pack caches.

Does not solve challenges. Builds missing donor images and extracts pack trees
into the host pack cache so the first real solve skips cold docker/materialize
for common packs. Blutter Dart VMs still compile on first use of a Dart version
(then shared across sessions via ``shared_state``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("ctf.setup")

# Default warm set — covers most Jeopardy without pulling ml/linux/containers.
DEFAULT_BAKE_PACKS: tuple[str, ...] = (
    "mobile",
    "pwn",
    "ghidra",
    "crypto",
    "crypto-tools",
    "steg",
    "forensics",
    "web",
    "linux",
)

# Host-cache paths that must exist after a successful donor extract. Top-level
# dirs alone are not enough — a partial delete during disk pressure can leave
# ``.ready`` while ``opt/sagemath/bin/sage`` is missing.
_PACK_CACHE_SENTINELS: dict[str, tuple[str, ...]] = {
    "mobile": ("opt/jadx/bin/jadx",),
    "crypto": ("opt/sagemath/bin/sage", "opt/sagemath/bin/python3"),
    "crypto-tools": ("opt/RsaCtfTool",),
    "ghidra": ("opt/ghidra/support/analyzeHeadless",),
    "steg": ("opt/stegseek/bin/stegseek",),
    "pwn": ("root/.gdbinit-gef.py",),
    "linux": ("opt/linux-tools",),
}


def pack_cache_incomplete(pack_id: str) -> bool:
    """True when ``.ready`` exists but extracted trees look truncated."""
    from backend.tool_router import pack_cache_dir

    sentinels = _PACK_CACHE_SENTINELS.get(pack_id)
    if not sentinels:
        return False
    cache = pack_cache_dir(pack_id)
    return any(not (cache / rel).exists() for rel in sentinels)


def invalidate_pack_cache(pack_id: str) -> None:
    """Drop ready/prepared markers so the next materialize re-extracts."""
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir(pack_id)
    for name in (".ready", ".prepared"):
        try:
            (cache / name).unlink(missing_ok=True)
        except OSError:
            pass


def probe_docker_env() -> list[str]:
    """Return advisory lines about Docker / Colima / DOCKER_HOST (never fails setup)."""
    lines: list[str] = []
    host = (os.environ.get("DOCKER_HOST") or "").strip()
    if host:
        lines.append(f"DOCKER_HOST={host}")
        return lines

    if sys.platform == "win32":
        lines.append("Docker: npipe:////./pipe/docker_engine (Docker Desktop)")
        return lines

    candidates: tuple[Path, ...] = (
            Path.home() / ".colima" / "default" / "docker.sock",
            Path.home() / ".docker" / "run" / "docker.sock",
            Path("/var/run/docker.sock"),
        )
    found = next((p for p in candidates if p.exists()), None)
    if found is not None:
        if "colima" in str(found):
            lines.append(
                f"Docker socket: {found} (Colima). "
                f"If `docker` fails, export DOCKER_HOST=unix://{found}"
            )
        else:
            lines.append(f"Docker socket: {found}")
    else:
        lines.append(
            "No Docker socket found. Start Colima (`colima start`) or Docker Desktop, "
            "then re-run setup. On Colima: "
            f"export DOCKER_HOST=unix://{Path.home()}/.colima/default/docker.sock"
        )
    return lines


def dockerfile_digest(dockerfile: Path) -> str:
    """Short content hash of a Dockerfile (for stale-cache detection)."""
    try:
        data = dockerfile.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


def pack_source_digest(pack_id: str) -> str:
    """Digest covering donor Dockerfile + pack recipe (apt/pip/gems/wrapper).

    Warm / ``.ready`` markers store this so recipe-only edits invalidate caches
    even when the Dockerfile is unchanged.
    """
    from backend.sandbox.donor_build import donor_dockerfile_for
    from backend.tool_router import pack_recipe_fingerprint

    parts: list[str] = []
    path = donor_dockerfile_for(pack_id)
    if path is not None and path.is_file():
        parts.append(dockerfile_digest(path))
    recipe = pack_recipe_fingerprint(pack_id)
    if recipe:
        parts.append(recipe)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def pack_cache_stale(pack_id: str) -> bool:
    """True when ``.ready`` exists but ``pack_source_digest`` no longer matches.

    Digest covers donor Dockerfile + PackSpec/bootstrap recipe fingerprint.
    Legacy markers that are just ``ok`` (no digest) are treated as **stale**
    when a digest is available, so the next materialize stamps a real digest.
    Only a *matching* digest keeps the cache.
    """
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir(pack_id)
    marker = cache / ".ready"
    if not marker.is_file():
        return False
    want = pack_source_digest(pack_id)
    if not want:
        return False
    try:
        have = marker.read_text(encoding="utf-8").strip().split()[-1]
    except OSError:
        return True
    # Legacy bare ``ok`` must rematerialize once so the digest is stamped —
    # upgrading the marker without extract would hide a stale tree forever.
    if have in ("ok", ""):
        return True
    return have != want


def write_pack_ready_marker(cache: Path, pack_id: str) -> None:
    """Write ``.ready`` with optional ``pack_source_digest`` for later staleness checks."""
    digest = pack_source_digest(pack_id)
    cache.mkdir(parents=True, exist_ok=True)
    payload = f"ok {digest}\n" if digest else "ok\n"
    (cache / ".ready").write_text(payload, encoding="utf-8")


async def ensure_core_image(
    image: str = "ctf-sandbox-core",
    *,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Build L0 core when missing (same donor path as packs)."""
    from backend.sandbox.docker_client import _docker_cli
    from backend.sandbox.docker_hygiene import (
        cleanup_sandbox_build_context,
        prepare_sandbox_build_context,
    )
    from backend.sandbox.donor_build import repo_root

    def _emit(text: str) -> None:
        if on_progress:
            on_progress(text)

    rc, _, _ = await _docker_cli("image", "inspect", image, timeout_s=30)
    if rc == 0:
        return True, f"L0 image ready: {image}"
    root = repo_root()
    ctx = await asyncio.to_thread(prepare_sandbox_build_context, root)
    try:
        dockerfile = ctx / "Dockerfile.core"
        if not dockerfile.is_file():
            return False, f"Missing {dockerfile}"
        logger.info("Building L0 %s from %s …", image, dockerfile)
        _emit(f"Building L0 image {image} (docker build — often 5–20+ min)…")

        async def _heartbeat() -> None:
            elapsed = 0
            while True:
                await asyncio.sleep(15)
                elapsed += 15
                mins, secs = divmod(elapsed, 60)
                _emit(f"Still building L0… {mins}m{secs:02d}s elapsed (Docker is working)")

        beat = asyncio.create_task(_heartbeat())
        try:
            rc, out, err = await _docker_cli(
                "build",
                "-t",
                image,
                "-f",
                str(dockerfile),
                str(ctx),
                timeout_s=int(os.environ.get("CTF_CORE_BUILD_TIMEOUT_S", "1800") or 1800),
            )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        if rc != 0:
            return False, (err or out or f"docker build {image} failed").strip()
        _emit(f"L0 image built: {image}")
        return True, f"Built L0 image: {image}"
    finally:
        await asyncio.to_thread(cleanup_sandbox_build_context, ctx)


async def _ensure_pack_prepared(pack_id: str, cache: Path) -> None:
    """Finish host-side finalize (``.prepared``) when ``.ready`` already exists."""
    if (cache / ".prepared").is_file():
        return
    from backend.config import Settings
    from backend.sandbox.container import DockerSandbox

    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "artemis-setup"
    tmp.mkdir(parents=True, exist_ok=True)
    sb = DockerSandbox(
        image="ctf-sandbox-core",
        challenge_dir=str(tmp),
        settings=Settings(),
        session_id="_setup",
    )
    await sb._finish_ready_pack_cache(pack_id)


async def materialize_pack(pack_id: str) -> tuple[bool, str]:
    """Extract one pack into the host cache (builds donor if needed).

    Uses the same cross-process pack flock as solve-time materialize. Rebuilds
    when ``pack_source_digest`` no longer matches ``.ready``.
    """
    from backend.config import Settings
    from backend.sandbox.container import DockerSandbox
    from backend.sandbox.packs import _acquire_pack_flock, _release_pack_flock
    from backend.tool_router import PACK_SPECS, pack_cache_dir

    if pack_id not in PACK_SPECS:
        return False, f"Unknown pack: {pack_id}"
    cache = pack_cache_dir(pack_id)
    if (cache / ".ready").is_file() and not pack_cache_stale(pack_id):
        if pack_cache_incomplete(pack_id):
            logger.warning(
                "Pack %s: cache incomplete (partial delete?) — rematerializing",
                pack_id,
            )
            invalidate_pack_cache(pack_id)
        else:
            # Sage/pip finalize must serialize with solve-time ensure_pack.
            fd = -1
            try:
                fd = await asyncio.to_thread(_acquire_pack_flock, pack_id)
                if (cache / ".ready").is_file() and not pack_cache_stale(pack_id):
                    if pack_cache_incomplete(pack_id):
                        invalidate_pack_cache(pack_id)
                    else:
                        await _ensure_pack_prepared(pack_id, cache)
                        if pack_id == "crypto" and not (cache / ".prepared").is_file():
                            return False, (
                                f"Pack {pack_id}: cache at {cache} but Sage finalize "
                                "failed — retry setup"
                            )
                        return True, f"Pack {pack_id}: cache already ready at {cache}"
            finally:
                if fd >= 0:
                    await asyncio.to_thread(_release_pack_flock, fd)

    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "artemis-setup"
    tmp.mkdir(parents=True, exist_ok=True)
    sb = DockerSandbox(
        image="ctf-sandbox-core",
        challenge_dir=str(tmp),
        settings=Settings(),
        session_id="_setup",
    )
    fd = -1
    try:
        # Cross-process: two `artemis setup` runs bake the same pack serially.
        fd = await asyncio.to_thread(_acquire_pack_flock, pack_id)
        if (cache / ".ready").is_file() and not pack_cache_stale(pack_id):
            if pack_cache_incomplete(pack_id):
                logger.warning(
                    "Pack %s: cache incomplete under flock — rematerializing",
                    pack_id,
                )
                invalidate_pack_cache(pack_id)
            else:
                await _ensure_pack_prepared(pack_id, cache)
                if pack_id == "crypto" and not (cache / ".prepared").is_file():
                    return False, (
                        f"Pack {pack_id}: cache at {cache} but Sage finalize "
                        "failed — retry setup"
                    )
                return True, f"Pack {pack_id}: cache already ready at {cache}"
        if pack_cache_stale(pack_id):
            logger.info("Pack %s: Dockerfile changed — rematerializing cache", pack_id)
            try:
                (cache / ".ready").unlink(missing_ok=True)
                (cache / ".prepared").unlink(missing_ok=True)
            except OSError:
                pass
        path = await sb._materialize_pack_cache_unlocked(pack_id)
        write_pack_ready_marker(cache, pack_id)
        await sb._finalize_pack_cache(pack_id, cache)
        if pack_id == "crypto" and not (cache / ".prepared").is_file():
            return False, (
                f"Pack {pack_id}: extracted at {path} but Sage finalize failed — "
                "retry `artemis setup` or the next solve will retry under flock"
            )
        return True, f"Pack {pack_id}: materialized at {path}"
    except Exception as e:
        logger.exception("materialize %s failed", pack_id)
        return False, f"Pack {pack_id}: {e}"
    finally:
        if fd >= 0:
            await asyncio.to_thread(_release_pack_flock, fd)


async def warm_shared_blutter_state() -> str:
    """Ensure the shared blutter state dir exists and adopt any session leftovers."""
    from backend.tool_router import _blutter_vm_present, pack_state_dir

    home = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id=None)
    if _blutter_vm_present(home):
        return f"Blutter shared cache ready: {home}"
    return (
        f"Blutter shared cache prepared at {home} "
        "(Dart VM still builds on first Flutter APK for each Dart version, then reuses)"
    )


async def run_setup(
    *,
    packs: list[str] | None = None,
    skip_core: bool = False,
    skip_warm_runtime: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Warm L0 + selected packs (+ optional committed warm runtimes).

    Host cache materialize first; then ``ctf-sandbox-warm-<pack>`` commits so
    the first real solve skips cold apt/pip bootstrap for those packs.
    """
    from backend.sandbox.warm_runtime import WARM_BAKE_PACKS, warm_pack_runtime

    def _emit(text: str) -> None:
        if on_progress:
            on_progress(text)

    lines: list[str] = []
    for tip in probe_docker_env():
        line = f"INFO {tip}"
        lines.append(line)
        _emit(line)

    if not skip_core:
        _emit("Checking / building L0 core image…")
        ok, msg = await ensure_core_image(on_progress=on_progress)
        line = ("OK  " if ok else "FAIL") + " " + msg
        lines.append(line)
        _emit(line)
        if not ok:
            return lines

    chosen = list(packs) if packs else list(DEFAULT_BAKE_PACKS)
    total = len(chosen)
    for i, pack_id in enumerate(chosen, start=1):
        _emit(f"Pack {i}/{total}: {pack_id}…")
        ok, msg = await materialize_pack(pack_id)
        line = ("OK  " if ok else "FAIL") + " " + msg
        lines.append(line)
        _emit(line)

    blutter = await warm_shared_blutter_state()
    line = "OK  " + blutter
    lines.append(line)
    _emit(line)

    if not skip_warm_runtime:
        warm_ids = [p for p in chosen if p in WARM_BAKE_PACKS]
        # Always include default warm set when using DEFAULT_BAKE_PACKS so
        # ``artemis setup`` alone kills cold apt on first solve.
        if packs is None:
            warm_ids = list(WARM_BAKE_PACKS)
        for pack_id in warm_ids:
            _emit(f"Warm runtime: {pack_id}…")
            ok, msg = await warm_pack_runtime(pack_id)
            line = ("OK  " if ok else "FAIL") + " " + msg
            lines.append(line)
            _emit(line)
    else:
        _emit("Skipping warm runtime commits (use `artemis setup` later for faster solves).")

    _emit("Install steps finished.")
    return lines
