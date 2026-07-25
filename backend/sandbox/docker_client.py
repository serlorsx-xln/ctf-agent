"""aiodocker client, start semaphore, orphan cleanup, docker CLI helper."""

from __future__ import annotations

import asyncio
import logging
import os

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
