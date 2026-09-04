"""Commit pack-bootstrapped L0 images so solve-time skips cold apt/pip.

``artemis setup`` materializes host RO trees, then optionally warms a
``ctf-sandbox-warm-<pack>`` image (core + bootstrap). Solve prefers that tag
over plain core when the marker is present — same idea as baked ``pwn`` /
``mobile`` donors, without merging every pack into one Kali image.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("ctf.setup")

# Apt/pip-heavy packs that are not already full L0 donors (pwn/mobile).
WARM_BAKE_PACKS: tuple[str, ...] = (
    "ghidra",
    "steg",
    "forensics",
    "web",
    "crypto-tools",
    "linux",
)


def warm_runtime_tag(pack_id: str) -> str:
    return f"ctf-sandbox-warm-{pack_id}"


def warm_marker_path(pack_id: str) -> Path:
    from backend.tool_router import pack_cache_dir

    return pack_cache_dir(pack_id) / ".warm_runtime"


def read_warm_runtime(pack_id: str, *, require_image: bool = False) -> str | None:
    """Return warm image tag when marker is present and digest still matches.

    When ``require_image`` is True (launch assess), also require the Docker image
    to exist so a pruned image does not look "warm ready".
    """
    from backend.sandbox.setup_bake import pack_source_digest

    path = warm_marker_path(pack_id)
    if not path.is_file():
        return None
    try:
        body = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not body:
        return None
    parts = body.split()
    tag = parts[0].strip()
    if not tag.startswith("ctf-sandbox-warm-"):
        return None
    want = pack_source_digest(pack_id)
    if want:
        if len(parts) < 2:
            return None
        have = parts[-1]
        if have != want:
            return None
    if require_image and not _image_exists_sync(tag):
        return None
    return tag


def write_warm_runtime_marker(pack_id: str, tag: str | None = None) -> None:
    from backend.sandbox.setup_bake import pack_source_digest

    path = warm_marker_path(pack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = tag or warm_runtime_tag(pack_id)
    digest = pack_source_digest(pack_id)
    payload = f"{image} {digest}\n" if digest else f"{image}\n"
    path.write_text(payload, encoding="utf-8")


def clear_warm_runtime_marker(pack_id: str) -> None:
    try:
        warm_marker_path(pack_id).unlink(missing_ok=True)
    except OSError:
        pass


def is_warm_runtime_image(image: str | None) -> bool:
    return bool(image) and str(image).startswith("ctf-sandbox-warm-")


def _image_exists_sync(tag: str) -> bool:
    """Same existence check as the TUI setup gate (inspect + ``images -q``)."""
    from backend.sandbox.setup_ready import _docker_image_exists

    return _docker_image_exists(tag)


async def _image_exists(tag: str) -> bool:
    from backend.sandbox.docker_client import _docker_cli
    from backend.sandbox.setup_ready import _image_refs

    for ref in _image_refs(tag):
        rc, _, _ = await _docker_cli("image", "inspect", ref, timeout_s=30)
        if rc == 0:
            return True
        rc, out, _ = await _docker_cli("images", "-q", ref, timeout_s=30)
        if rc == 0 and (out or "").strip():
            return True
    return False


async def warm_pack_runtime(pack_id: str) -> tuple[bool, str]:
    """Bootstrap ``pack_id`` into a committed warm L0 image (idempotent)."""
    from backend.sandbox.setup_bake import materialize_pack
    from backend.tool_router import PACK_SPECS, PREFETCH_RUNTIME_IMAGES

    if pack_id not in PACK_SPECS:
        return False, f"Unknown pack: {pack_id}"
    if pack_id in PREFETCH_RUNTIME_IMAGES:
        donor = PREFETCH_RUNTIME_IMAGES[pack_id]
        return True, f"Pack {pack_id}: using donor runtime {donor} (no warm commit)"

    tag = warm_runtime_tag(pack_id)
    existing = read_warm_runtime(pack_id)
    if existing == tag and await _image_exists(tag):
        return True, f"Pack {pack_id}: warm runtime already ready ({tag})"

    ok, msg = await materialize_pack(pack_id)
    if not ok:
        return False, msg

    from backend.config import Settings
    from backend.sandbox.container import DockerSandbox
    from backend.sandbox.docker_client import _docker_cli

    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "artemis-warm" / pack_id
    tmp.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    settings.force_packs = [pack_id]
    settings.sandbox_image = "ctf-sandbox-core"
    sb = DockerSandbox(
        image="ctf-sandbox-core",
        challenge_dir=str(tmp),
        settings=settings,
        session_id="_warm",
    )
    try:
        logger.info("Warming runtime %s (cold apt/pip once during setup)…", tag)
        await sb.start()
        cid = sb.container_id
        if not cid:
            return False, f"Pack {pack_id}: warm start produced no container id"
        rc, out, err = await _docker_cli("commit", cid, tag, timeout_s=600)
        if rc != 0:
            clear_warm_runtime_marker(pack_id)
            return False, f"Pack {pack_id}: docker commit failed: {(err or out).strip()}"
        write_warm_runtime_marker(pack_id, tag)
        return True, f"Pack {pack_id}: warm runtime committed as {tag}"
    except Exception as e:
        logger.exception("warm_pack_runtime %s failed", pack_id)
        clear_warm_runtime_marker(pack_id)
        return False, f"Pack {pack_id}: warm failed: {e}"
    finally:
        try:
            await sb.stop()
        except Exception:
            logger.debug("warm stop failed for %s", pack_id, exc_info=True)
