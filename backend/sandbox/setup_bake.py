"""Phase 3: customer ``artemis setup`` — warm L0 + common pack caches.

Does not solve challenges. Builds missing donor images and extracts pack trees
into the host pack cache so the first real solve skips cold docker/materialize
for common packs. When a sample Flutter APK is available, also prebuilds the
blutter Dart VM into the shared pack-state cache.
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

# First-run / TUI Install gate: Docker + L0 only. Packs attach on demand.
GATE_REQUIRED_PACKS: tuple[str, ...] = ()

# ``artemis setup`` default — cheap Jeopardy packs (no Sage / pwn / mobile).
LITE_BAKE_PACKS: tuple[str, ...] = (
    "web",
    "steg",
    "forensics",
)

# ``artemis setup --full`` — previous default Jeopardy set (still skips ml).
FULL_BAKE_PACKS: tuple[str, ...] = (
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

# Alias kept for older imports / docs; default bake is the lite set.
DEFAULT_BAKE_PACKS: tuple[str, ...] = LITE_BAKE_PACKS

# Donor images used only as extract sources (not L0 runtimes). Safe to drop
# after pack cache ``.ready`` — keep core / pwn / mobile / warm-*.
EXTRACT_ONLY_DONOR_IMAGES: tuple[str, ...] = (
    "ctf-sandbox-crypto",
    "ctf-sandbox-ghidra",
    "ctf-sandbox-crypto-tools",
    "ctf-sandbox-linux",
    "ctf-sandbox-steg",
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


def find_blutter_warm_apk() -> Path | None:
    """Locate a Flutter APK to prebuild the shared blutter Dart VM.

    Order: ``ARTEMIS_BLUTTER_WARM_APK``, repo ``challenges/**/*.apk``, then
    ``~/.cache/artemis/challenges/**/*.apk``.
    """
    from backend.sandbox.donor_build import repo_root

    env = (os.environ.get("ARTEMIS_BLUTTER_WARM_APK") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
        # Explicit path that is missing — do not silently fall through.
        return None

    roots: list[Path] = []
    try:
        roots.append(repo_root() / "challenges")
    except Exception:
        pass
    from backend.cache import cache_dir

    roots.append(cache_dir() / "challenges")
    for root in roots:
        if not root.is_dir():
            continue
        # Prefer known Flutter CTF names, then any apk.
        preferred = sorted(root.rglob("*PWNKnight*.apk")) + sorted(
            root.rglob("*pwnknight*.apk")
        )
        for apk in preferred:
            if apk.is_file() and apk.stat().st_size > 1_000_000:
                return apk
        for apk in sorted(root.rglob("*.apk")):
            if apk.is_file() and apk.stat().st_size > 500_000:
                return apk
    return None


async def _compile_blutter_vm_from_apk(
    apk: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Run blutter once in a short-lived mobile sandbox to populate shared cache."""
    from backend.config import Settings
    from backend.sandbox.container import DockerSandbox
    from backend.sandbox.docker_client import _docker_cli
    from backend.tool_router import PREFETCH_RUNTIME_IMAGES, _blutter_vm_present, pack_state_dir

    def _emit(text: str) -> None:
        if on_progress:
            on_progress(text)

    home = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id=None)
    if _blutter_vm_present(home):
        return True, f"Blutter Dart VM already cached at {home}"

    image = PREFETCH_RUNTIME_IMAGES.get("mobile") or "ctf-sandbox-mobile"
    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "artemis-blutter-warm"
    tmp.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    settings.force_packs = ["mobile"]
    settings.sandbox_image = image
    sb = DockerSandbox(
        image=image,
        challenge_dir=str(tmp),
        settings=settings,
        session_id="_blutter_warm",
    )
    try:
        _emit(
            f"Blutter Dart VM: warming from {apk.name} "
            "(first compile can take 10–30+ min)…"
        )
        await sb.start()
        await sb.ensure_pack("mobile", refresh_tools=False)
        await sb.write_file("/challenge/workspace/warm_apk/.keep", b"")
        cid = sb.container_id
        if not cid:
            return False, "Blutter warm: no container id"
        rc, _, err = await _docker_cli(
            "cp",
            str(apk.resolve()),
            f"{cid}:/challenge/workspace/warm_apk/app.apk",
            timeout_s=120,
        )
        if rc != 0:
            return False, f"Blutter warm: docker cp APK failed: {(err or '').strip()}"
        extract = (
            "mkdir -p /challenge/workspace/warm_libs /challenge/workspace/warm_out && "
            "cd /challenge/workspace && "
            "unzip -qo warm_apk/app.apk 'lib/arm64-v8a/*' -d warm_extract && "
            "cp warm_extract/lib/arm64-v8a/libapp.so "
            "warm_extract/lib/arm64-v8a/libflutter.so warm_libs/ && "
            "ls -la warm_libs/"
        )
        res = await sb.exec(extract, timeout_s=120)
        if res.exit_code != 0 or "libapp.so" not in (res.stdout or ""):
            return (
                False,
                "Blutter warm: APK has no lib/arm64-v8a (need Flutter arm64 sample)",
            )
        _emit("Blutter Dart VM: compiling (ninja) — leave this running…")
        dump = await sb.exec(
            "blutter /challenge/workspace/warm_libs /challenge/workspace/warm_out",
            timeout_s=1800,
        )
        if _blutter_vm_present(home):
            return True, f"Blutter Dart VM warmed into {home}"
        if dump.exit_code != 0:
            return (
                False,
                f"Blutter warm compile failed (exit {dump.exit_code}): "
                f"{(dump.stderr or dump.stdout or '')[:400]}",
            )
        return True, f"Blutter warm finished (cache at {home})"
    except Exception as e:
        logger.exception("blutter VM warm failed")
        return False, f"Blutter warm failed: {e}"
    finally:
        try:
            await sb.stop()
        except Exception:
            logger.debug("blutter warm stop failed", exc_info=True)


async def warm_shared_blutter_state(
    *,
    skip_vm_compile: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Ensure shared blutter cache exists; optionally precompile a Dart VM."""
    from backend.tool_router import _blutter_vm_present, pack_state_dir

    home = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id=None)
    if _blutter_vm_present(home):
        return f"Blutter shared cache ready (Dart VM present): {home}"

    if skip_vm_compile:
        return (
            f"Blutter shared cache prepared at {home} "
            "(Dart VM still builds on first Flutter APK for each Dart version, then reuses)"
        )

    apk = find_blutter_warm_apk()
    if apk is None:
        return (
            f"Blutter shared cache prepared at {home} "
            "(no sample APK for VM warm — set ARTEMIS_BLUTTER_WARM_APK or place an APK "
            "under challenges/; first Flutter solve still compiles once)"
        )

    ok, msg = await _compile_blutter_vm_from_apk(apk, on_progress=on_progress)
    if ok:
        return msg
    return (
        f"Blutter shared cache at {home} — VM warm skipped: {msg}. "
        "First Flutter solve may still compile the Dart VM once."
    )


async def run_setup(
    *,
    packs: list[str] | None = None,
    skip_core: bool = False,
    skip_warm_runtime: bool = False,
    skip_blutter_vm: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Warm L0 + selected packs (+ optional committed warm runtimes / blutter VM).

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

    chosen = list(LITE_BAKE_PACKS) if packs is None else list(packs)
    total = len(chosen)
    for i, pack_id in enumerate(chosen, start=1):
        _emit(f"Pack {i}/{total}: {pack_id}…")
        ok, msg = await materialize_pack(pack_id)
        line = ("OK  " if ok else "FAIL") + " " + msg
        lines.append(line)
        _emit(line)

    # Blutter VM warm needs the mobile pack tree; skip when mobile was not baked.
    want_blutter = "mobile" in chosen and not skip_blutter_vm
    _emit("Blutter shared cache…")
    blutter = await warm_shared_blutter_state(
        skip_vm_compile=not want_blutter,
        on_progress=on_progress,
    )
    # Soft failure still leaves usable cache dir.
    line = "OK  " + blutter
    lines.append(line)
    _emit(line)

    if not skip_warm_runtime:
        warm_ids = [p for p in chosen if p in WARM_BAKE_PACKS]
        for pack_id in warm_ids:
            _emit(f"Warm runtime: {pack_id}…")
            ok, msg = await warm_pack_runtime(pack_id)
            line = ("OK  " if ok else "FAIL") + " " + msg
            lines.append(line)
            _emit(line)
    else:
        _emit("Skipping warm runtime commits (use `artemis setup` later for faster solves).")

    for line in await prune_extract_only_donors(
        pack_ids=chosen, on_progress=on_progress
    ):
        lines.append(line)
        _emit(line)

    _emit("Install steps finished.")
    return lines


def _keep_extract_donors() -> bool:
    return (os.environ.get("ARTEMIS_KEEP_EXTRACT_DONORS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def prune_extract_only_donors(
    *,
    pack_ids: list[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """``docker rmi`` extract-only donors after pack cache is ready.

    Keeps ``ctf-sandbox-core`` / ``ctf-sandbox-pwn`` / ``ctf-sandbox-mobile``
    and ``ctf-sandbox-warm-*``. When ``pack_ids`` is set, only donors for those
    packs are removed (L0-only gate therefore prunes nothing). Set
    ``ARTEMIS_KEEP_EXTRACT_DONORS=1`` to skip.
    """
    if _keep_extract_donors():
        return ["INFO keep extract-only donors (ARTEMIS_KEEP_EXTRACT_DONORS=1)"]

    from backend.sandbox.docker_client import _docker_cli
    from backend.tool_router import PACK_SPECS

    if pack_ids is None:
        images = EXTRACT_ONLY_DONOR_IMAGES
    else:
        want = {
            spec.image
            for pid in pack_ids
            if (spec := PACK_SPECS.get(pid)) is not None
            and spec.image in EXTRACT_ONLY_DONOR_IMAGES
        }
        images = tuple(img for img in EXTRACT_ONLY_DONOR_IMAGES if img in want)
        if not images:
            return []

    lines: list[str] = []
    for image in images:
        rc, _, err = await _docker_cli("rmi", image, timeout_s=90)
        if rc == 0:
            line = f"OK  pruned extract-only donor {image}"
        elif err and "No such image" in err:
            line = f"INFO extract-only donor {image} already absent"
        else:
            # In-use or inspect miss — not a setup failure.
            detail = (err or "").strip().splitlines()[-1] if err else f"exit {rc}"
            line = f"INFO skip prune {image}: {detail}"
        lines.append(line)
        if on_progress:
            on_progress(line)
    return lines
