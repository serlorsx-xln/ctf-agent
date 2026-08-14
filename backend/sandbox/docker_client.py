"""aiodocker client, start semaphore, orphan cleanup, docker CLI helper."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

import aiodocker

logger = logging.getLogger("ctf.sandbox")

CONTAINER_LABEL = "ctf-agent"
OWNER_PID_LABEL = "ctf-agent.owner-pid"
SESSION_ID_LABEL = "ctf-agent.session-id"

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
    import sys

    host = os.environ.get("DOCKER_HOST")
    if host:
        return aiodocker.Docker(url=host)
    if sys.platform == "win32":
        # Docker Desktop 4.x Linux engine (desktop-linux context).
        return aiodocker.Docker(url="npipe:////./pipe/dockerDesktopLinuxEngine")
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


async def cleanup_orphan_containers(session_id: str | None = None) -> None:
    """Remove leftover ctf-agent containers without killing concurrent solvers.

    Containers labeled with a live owner PID are left alone so two ``ctf-solve``
    processes can run at once. Unlabeled running containers are also kept
    (legacy / in-flight). Only true orphans (dead owner, or exited unlabeled)
    are force-deleted.

    When ``session_id`` is set, only containers labeled
    ``ctf-agent.session-id=<sid>`` (or unlabeled session — treated as
    ``_default`` when filtering ``_default``) are considered.

    Falls back to the Docker CLI when aiodocker fails (API quirks / disk pressure)
    so exit cleanup still reclaims sandboxes.
    """
    try:
        await asyncio.wait_for(
            _cleanup_orphans_aiodocker(session_id=session_id),
            timeout=30.0,
        )
    except TimeoutError:
        logger.warning("aiodocker orphan cleanup timed out after 30s")
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)

    # CLI fallback: reclaim by label and by sandbox image when aiodocker missed.
    try:
        await asyncio.wait_for(
            _cleanup_orphans_cli(session_id=session_id),
            timeout=30.0,
        )
    except TimeoutError:
        logger.warning("CLI orphan cleanup timed out after 30s")
    except Exception as e:
        logger.warning("CLI orphan cleanup failed: %s", e)


async def _cleanup_orphans_aiodocker(session_id: str | None = None) -> None:
    docker = _docker_client()
    try:
        label_filters = [f"{CONTAINER_LABEL}=true"]
        if session_id is not None:
            from backend.daemon.session_id import normalize_session_id

            sid = normalize_session_id(session_id)
            label_filters.append(f"{SESSION_ID_LABEL}={sid}")
        containers = await docker.containers.list(
            all=True,
            filters={"label": label_filters},
        )
        removed = 0
        skipped = 0
        for c in containers:
            try:
                info = await asyncio.wait_for(c.show(), timeout=10.0)
                if not isinstance(info, dict):
                    continue
                config = info.get("Config")
                if not isinstance(config, dict):
                    config = {}
                labels = config.get("Labels")
                if not isinstance(labels, dict):
                    labels = {}
                if session_id is not None and not _session_label_matches(
                    labels, session_id
                ):
                    skipped += 1
                    continue
                owner = str(labels.get(OWNER_PID_LABEL) or "").strip()
                state = info.get("State")
                if not isinstance(state, dict):
                    state = {}
                status = str(state.get("Status") or "").lower()
                if owner.isdigit() and _pid_alive(int(owner)):
                    skipped += 1
                    continue
                if not owner and status in {"running", "created", "restarting"}:
                    # No owner label but still live — likely a concurrent run
                    # started before owner labeling; do not steal it.
                    skipped += 1
                    continue
                await asyncio.wait_for(c.delete(force=True), timeout=15.0)
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


def _session_label_matches(labels: dict, session_id: str | None) -> bool:
    if session_id is None:
        return True
    from backend.daemon.session_id import DEFAULT_SESSION_ID, normalize_session_id

    want = normalize_session_id(session_id)
    got = str(labels.get(SESSION_ID_LABEL) or "").strip()
    if not got:
        # Legacy containers without session label belong to _default only.
        return want == DEFAULT_SESSION_ID
    return normalize_session_id(got) == want


async def _cleanup_orphans_cli(session_id: str | None = None) -> None:
    """Best-effort ``docker rm -f`` for dead-owner / exited sandbox containers."""
    ids: list[str] = []
    filter_args: list[list[str]] = [
        ["ps", "-aq", "--filter", f"label={CONTAINER_LABEL}=true"],
    ]
    if session_id is not None:
        from backend.daemon.session_id import normalize_session_id

        filter_args[0].extend(
            ["--filter", f"label={SESSION_ID_LABEL}={normalize_session_id(session_id)}"]
        )
    else:
        filter_args.append(["ps", "-aq", "--filter", "ancestor=ctf-sandbox-core"])
    for args in filter_args:
        code, out, _err = await _docker_cli(*args, timeout_s=30)
        if code != 0 or not out.strip():
            continue
        for line in out.splitlines():
            cid = line.strip()
            if cid and cid not in ids:
                ids.append(cid)
    removed = 0
    for cid in ids:
        code, inspect_out, _ = await _docker_cli(
            "inspect",
            "--format",
            '{{index .Config.Labels "ctf-agent.owner-pid"}}'
            '|{{index .Config.Labels "ctf-agent.session-id"}}|{{.State.Status}}',
            cid,
            timeout_s=15,
        )
        if code != 0:
            continue
        parts = inspect_out.strip().split("|")
        while len(parts) < 3:
            parts.append("")
        owner_raw, sess_raw, status = parts[0], parts[1], parts[2].lower()
        owner = "" if owner_raw in ("", "<no value>", "<no>") else owner_raw.strip()
        sess_label = "" if sess_raw in ("", "<no value>", "<no>") else sess_raw.strip()
        if session_id is not None and not _session_label_matches(
            {SESSION_ID_LABEL: sess_label} if sess_label else {}, session_id
        ):
            from backend.daemon.session_id import DEFAULT_SESSION_ID, normalize_session_id

            want = normalize_session_id(session_id)
            if sess_label:
                if normalize_session_id(sess_label) != want:
                    continue
            elif want != DEFAULT_SESSION_ID:
                continue
        if owner.isdigit() and _pid_alive(int(owner)):
            continue
        if not owner and status in {"running", "created", "restarting"}:
            continue
        rm_code, _, _ = await _docker_cli("rm", "-f", cid, timeout_s=30)
        if rm_code == 0:
            removed += 1
    if removed:
        logger.info("CLI cleaned up %d orphan sandbox container(s)", removed)


def _docker_exe() -> str:
    from backend.subprocess_platform import resolve_docker_exe

    return resolve_docker_exe()


async def _docker_cli(*args: str, timeout_s: float = 600) -> tuple[int, str, str]:
    """Run the host `docker` CLI (respects DOCKER_HOST)."""
    proc = await asyncio.create_subprocess_exec(
        _docker_exe(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
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


_WIN_INVALID_PATH_CHARS = str.maketrans(
    {ord(c): ord("_") for c in '<>:"|?*'}
    | {ord(c): ord("_") for c in "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"}
)


def _sanitize_windows_path(name: str) -> str:
    """Make a POSIX tar member path safe for extraction onto NTFS."""
    parts: list[str] = []
    for part in PurePosixPath(name).parts:
        if part in (".", "..", "/"):
            continue
        parts.append(part.translate(_WIN_INVALID_PATH_CHARS))
    return "/".join(parts)


def _extract_tar_to_dest(data: bytes, extract_root: Path) -> None:
    """Extract a tar stream under ``extract_root``, sanitizing names on Windows."""
    extract_root.mkdir(parents=True, exist_ok=True)
    deferred_links: list[tuple[Path, str]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for member in tf.getmembers():
            safe = _sanitize_windows_path(member.name)
            if not safe:
                continue
            target = extract_root / PurePosixPath(safe)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.isreg():
                payload = tf.extractfile(member)
                if payload is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload.read())
                continue
            if member.issym() or member.islnk():
                deferred_links.append((target, member.linkname))
                continue

    for link_path, linkname in deferred_links:
        rel_parent = PurePosixPath(*link_path.relative_to(extract_root).parts[:-1])
        link_target = PurePosixPath(linkname)
        if link_target.is_absolute():
            source = extract_root / PurePosixPath(str(link_target).lstrip("/"))
        else:
            source = extract_root / rel_parent / link_target
        if not source.is_file():
            continue
        link_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, link_path)


async def docker_cp_from_container(
    container: str,
    src: str,
    dest: Path,
    *,
    timeout_s: float = 600,
) -> None:
    """Copy ``src`` from ``container:`` to host ``dest`` (file or directory tree).

    On Windows, ``docker cp`` cannot represent POSIX names like ``App::Cpan.3``;
    stream tar from the container and extract with sanitized paths instead.
    """
    if sys.platform != "win32":
        rc, _, err = await _docker_cli("cp", f"{container}:{src}", str(dest), timeout_s=timeout_s)
        if rc != 0:
            raise RuntimeError(err.strip() or f"docker cp {src} failed")
        return

    src_path = src.lstrip("/")
    parts = PurePosixPath(src_path).parts
    extract_root = dest.parents[len(parts) - 1] if parts else dest.parent

    rc, _, err = await _docker_cli("start", container, timeout_s=120)
    if rc != 0:
        raise RuntimeError(err.strip() or f"docker start {container} failed")

    proc = await asyncio.create_subprocess_exec(
        _docker_exe(),
        "exec",
        container,
        "tar",
        "-C",
        "/",
        "-cf",
        "-",
        src_path,
        stdin=asyncio.subprocess.DEVNULL,
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
        raise RuntimeError(f"docker exec tar {src} timed out after {timeout_s}s") from None
    if proc.returncode != 0:
        msg = err_b.decode("utf-8", errors="replace").strip()
        raise RuntimeError(msg or f"docker exec tar {src} failed")

    _extract_tar_to_dest(out_b, extract_root)
