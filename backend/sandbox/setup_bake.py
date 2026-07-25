"""Phase 3: customer ``artemis setup`` — warm L0 + common pack caches.

Does not solve challenges. Builds missing donor images and extracts pack trees
into the host pack cache so the first real solve skips cold docker/materialize
for common packs. Blutter Dart VMs still compile on first use of a Dart version
(then shared across sessions via ``shared_state``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
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
)


def probe_docker_env() -> list[str]:
    """Return advisory lines about Docker / Colima / DOCKER_HOST (never fails setup)."""
    lines: list[str] = []
    host = (os.environ.get("DOCKER_HOST") or "").strip()
    if host:
        lines.append(f"DOCKER_HOST={host}")
        return lines

    candidates = (
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
    """Digest of the donor Dockerfile for ``pack_id`` (empty when unknown)."""
    from backend.sandbox.donor_build import donor_dockerfile_for

    path = donor_dockerfile_for(pack_id)
    if path is None or not path.is_file():
        return ""
    return dockerfile_digest(path)


def pack_cache_stale(pack_id: str) -> bool:
    """True when ``.ready`` exists but Dockerfile digest no longer matches.

    Legacy markers that are just ``ok`` (no digest) are treated as still valid —
    the next successful materialize upgrades the marker. Only a *mismatched*
    digest forces a rebuild.
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
    if have in ("ok", ""):
        return False
    return have != want


def maybe_upgrade_ready_marker(pack_id: str) -> None:
    """Rewrite legacy ``ok`` markers to include the current Dockerfile digest."""
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir(pack_id)
    marker = cache / ".ready"
    if not marker.is_file():
        return
    want = pack_source_digest(pack_id)
    if not want:
        return
    try:
        body = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if body == "ok" or not body.endswith(want):
        write_pack_ready_marker(cache, pack_id)


def write_pack_ready_marker(cache: Path, pack_id: str) -> None:
    """Write ``.ready`` with optional Dockerfile digest for later staleness checks."""
    digest = pack_source_digest(pack_id)
    cache.mkdir(parents=True, exist_ok=True)
    payload = f"ok {digest}\n" if digest else "ok\n"
    (cache / ".ready").write_text(payload, encoding="utf-8")


async def ensure_core_image(image: str = "ctf-sandbox-core") -> tuple[bool, str]:
    """Build L0 core when missing (same donor path as packs)."""
    from backend.sandbox.docker_client import _docker_cli
    from backend.sandbox.donor_build import repo_root

    rc, _, _ = await _docker_cli("image", "inspect", image, timeout_s=30)
    if rc == 0:
        return True, f"L0 image ready: {image}"
    dockerfile = repo_root() / "sandbox" / "Dockerfile.core"
    if not dockerfile.is_file():
        return False, f"Missing {dockerfile}"
    logger.info("Building L0 %s from %s …", image, dockerfile)
    rc, out, err = await _docker_cli(
        "build",
        "-t",
        image,
        "-f",
        str(dockerfile),
        str(repo_root()),
        timeout_s=int(os.environ.get("CTF_CORE_BUILD_TIMEOUT_S", "1800") or 1800),
    )
    if rc != 0:
        return False, (err or out or f"docker build {image} failed").strip()
    return True, f"Built L0 image: {image}"


async def materialize_pack(pack_id: str) -> tuple[bool, str]:
    """Extract one pack into the host cache (builds donor if needed).

    Uses the same cross-process pack flock as solve-time materialize. Rebuilds
    when the donor Dockerfile digest no longer matches ``.ready``.
    """
    from backend.config import Settings
    from backend.sandbox.container import DockerSandbox
    from backend.sandbox.packs import _acquire_pack_flock, _release_pack_flock
    from backend.tool_router import PACK_SPECS, pack_cache_dir

    if pack_id not in PACK_SPECS:
        return False, f"Unknown pack: {pack_id}"
    cache = pack_cache_dir(pack_id)
    if (cache / ".ready").is_file() and not pack_cache_stale(pack_id):
        maybe_upgrade_ready_marker(pack_id)
        return True, f"Pack {pack_id}: cache already ready at {cache}"

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
            maybe_upgrade_ready_marker(pack_id)
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
) -> list[str]:
    """Warm L0 + selected packs. Returns human-readable status lines."""
    lines: list[str] = []
    for tip in probe_docker_env():
        lines.append(f"INFO {tip}")

    if not skip_core:
        ok, msg = await ensure_core_image()
        lines.append(("OK  " if ok else "FAIL") + " " + msg)
        if not ok:
            return lines

    chosen = list(packs) if packs else list(DEFAULT_BAKE_PACKS)
    for pack_id in chosen:
        ok, msg = await materialize_pack(pack_id)
        lines.append(("OK  " if ok else "FAIL") + " " + msg)

    lines.append("OK  " + await warm_shared_blutter_state())
    return lines
