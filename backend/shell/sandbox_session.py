"""Persistent Docker sandbox + per-session Artemis progress on disk.

Session files live under ``~/.cache/artemis/sessions/<sid>/``. Missing/null
session ids map to ``_default``. Legacy top-level ``session.json`` is migrated
into ``_default`` on first load.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()
_SANDBOXES: dict[str, Any] = {}
_REFS: dict[str, int] = {}
_PINNED: set[str] = set()
_ORPHAN_CLEANUP_AT: dict[str, float] = {}
_ORPHAN_CLEANUP_TTL_S = 45.0

_SAFE_SID = re.compile(r"[^A-Za-z0-9._-]+")


def _state_dir() -> Path:
    from backend.cache import cache_dir

    return cache_dir()


def resolve_session_id(session_id: str | None = None) -> str:
    """Resolve session id from arg, else ``ARTEMIS_SESSION_ID``, else ``_default``."""
    from backend.daemon.session_id import normalize_session_id

    if session_id is not None and str(session_id).strip():
        return normalize_session_id(session_id)
    return normalize_session_id(os.environ.get("ARTEMIS_SESSION_ID"))


def _fs_sid(session_id: str | None = None) -> str:
    from backend.daemon.session_id import DEFAULT_SESSION_ID

    sid = resolve_session_id(session_id)
    if sid == DEFAULT_SESSION_ID:
        return DEFAULT_SESSION_ID
    # Sanitize for filesystem; keep leading/trailing underscores (OpenCode ids).
    safe = _SAFE_SID.sub("_", sid).strip(".")[:200]
    return safe or DEFAULT_SESSION_ID


def session_dir(session_id: str | None = None) -> Path:
    d = _state_dir() / "sessions" / _fs_sid(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(challenge_dir: str) -> str:
    return str(Path(challenge_dir).expanduser().resolve())


def _sandbox_cache_key(challenge_dir: str, session_id: str | None = None) -> str:
    """Cache key: one sandbox per (session, challenge) so windows do not share."""
    return f"{resolve_session_id(session_id)}::{_key(challenge_dir)}"


async def get_sandbox(
    challenge_dir: str,
    session_id: str | None = None,
    settings: Any | None = None,
    *,
    pin: bool = True,
):
    """Return a started DockerSandbox for challenge_dir (cached per session).

    ``pin=True`` (TUI / bridge) keeps the box until ``stop_sandbox``. Solver
    ``acquire_sandbox`` uses ``pin=False`` plus a refcount so one solver
    ``stop()`` does not kill siblings on the same challenge.
    """
    from backend.config import Settings
    from backend.sandbox import DockerSandbox, cleanup_orphan_containers
    from backend.sandbox.docker_client import ensure_start_semaphore

    sid = resolve_session_id(session_id)
    chal = _key(challenge_dir)
    key = _sandbox_cache_key(chal, sid)
    async with _LOCK:
        existing = _SANDBOXES.get(key)
        if existing is not None:
            try:
                if getattr(existing, "container_id", None):
                    if pin:
                        _PINNED.add(key)
                    return existing
            except Exception:
                pass
            try:
                await existing.stop()
            except Exception:
                pass
            _SANDBOXES.pop(key, None)
            _REFS.pop(key, None)
            _PINNED.discard(key)

        ensure_start_semaphore(50)
        now = time.monotonic()
        last = _ORPHAN_CLEANUP_AT.get(sid, 0.0)
        if now - last >= _ORPHAN_CLEANUP_TTL_S:
            await cleanup_orphan_containers(session_id=sid)
            _ORPHAN_CLEANUP_AT[sid] = now
        cfg = settings if settings is not None else Settings()
        sandbox = DockerSandbox(
            image=getattr(cfg, "sandbox_image", None) or "ctf-sandbox-core",
            challenge_dir=chal,
            memory_limit=getattr(cfg, "container_memory_limit", None) or "4g",
            settings=cfg,
            session_id=sid,
        )
        await sandbox.start()
        _SANDBOXES[key] = sandbox
        if pin:
            _PINNED.add(key)
        _write_meta(chal, getattr(sandbox, "container_id", None), session_id=sid)
        return sandbox


async def acquire_sandbox(
    challenge_dir: str,
    settings: Any | None = None,
    session_id: str | None = None,
):
    """Get or create the shared box and bump the solver refcount."""
    sandbox = await get_sandbox(
        challenge_dir, session_id=session_id, settings=settings, pin=False
    )
    sid = resolve_session_id(session_id)
    key = _sandbox_cache_key(challenge_dir, sid)
    async with _LOCK:
        _REFS[key] = _REFS.get(key, 0) + 1
    return sandbox


async def release_sandbox(
    challenge_dir: str, session_id: str | None = None
) -> str:
    """Drop one solver ref; stop the box when unpinned and refs hit zero."""
    sid = resolve_session_id(session_id)
    key = _sandbox_cache_key(challenge_dir, sid)
    async with _LOCK:
        n = _REFS.get(key, 0) - 1
        if n > 0:
            _REFS[key] = n
            return f"Released sandbox for {key} (refs={n})"
        _REFS.pop(key, None)
        if key in _PINNED:
            return f"Released sandbox for {key} (pinned)"
        sb = _SANDBOXES.pop(key, None)
        if sb is not None:
            await sb.stop()
            return f"Stopped sandbox for {key}"
        return f"No sandbox for {key}"


def reset_sandbox_cache_for_tests() -> None:
    _SANDBOXES.clear()
    _REFS.clear()
    _PINNED.clear()
    _ORPHAN_CLEANUP_AT.clear()


async def stop_sandbox(
    challenge_dir: str | None = None, session_id: str | None = None
) -> str:
    sid = resolve_session_id(session_id)
    async with _LOCK:
        if challenge_dir:
            key = _sandbox_cache_key(challenge_dir, sid)
            _PINNED.discard(key)
            _REFS.pop(key, None)
            sb = _SANDBOXES.pop(key, None)
            if sb is not None:
                await sb.stop()
                return f"Stopped sandbox for {key}"
            return f"No sandbox for {key}"
        # No challenge_dir: stop every sandbox for this session only.
        prefix = f"{sid}::"
        keys = [k for k in _SANDBOXES if k.startswith(prefix)]
        for key in keys:
            _PINNED.discard(key)
            _REFS.pop(key, None)
            sb = _SANDBOXES.pop(key)
            try:
                await sb.stop()
            except Exception as e:
                logger.warning("stop %s: %s", key, e)
        return f"Stopped {len(keys)} sandbox(s) for session {sid}"


def session_state_path(session_id: str | None = None) -> Path:
    d = session_dir(session_id)
    return d / "session.json"


def _legacy_session_path() -> Path:
    return _state_dir() / "session.json"


def _migrate_legacy_default(session_id: str | None = None) -> None:
    """Copy top-level session.json into sessions/_default/ once."""
    sid = resolve_session_id(session_id)
    if sid != "_default":
        return
    dest = session_state_path("_default")
    if dest.is_file():
        return
    legacy = _legacy_session_path()
    if not legacy.is_file():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        logger.debug("legacy session migrate failed", exc_info=True)


def load_session_state(session_id: str | None = None) -> dict:
    _migrate_legacy_default(session_id)
    p = session_state_path(session_id)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session_state(session_id: str | None = None, **kwargs) -> dict:
    sid = resolve_session_id(session_id)
    cur = load_session_state(sid)
    cur.update({k: v for k, v in kwargs.items() if v is not None})
    cur["updated_at"] = time.time()
    p = session_state_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def sync_accepted_flags(
    flags: list[str] | tuple[str, ...],
    *,
    flags_required: int | None = None,
    session_id: str | None = None,
) -> None:
    """Update TUI sidebar progress after a solver confirms flag(s)."""
    try:
        cleaned = [f.strip() for f in flags if f and str(f).strip()]
        if not cleaned:
            return
        sid = resolve_session_id(session_id)
        st = load_session_state(sid)
        prior = [f for f in (st.get("accepted_flags") or []) if f]
        merged = list(prior)
        for f in cleaned:
            if f not in merged:
                merged.append(f)
        kwargs: dict = {"accepted_flags": merged}
        if flags_required is not None:
            kwargs["flags_required"] = int(flags_required)
        save_session_state(sid, **kwargs)
        _notify_daemon_session_refresh(sid)
    except Exception:
        logger.debug("sync_accepted_flags failed", exc_info=True)


def _notify_daemon_session_refresh(session_id: str | None = None) -> None:
    """Best-effort: tell the daemon to rehydrate session.json → TUI push."""
    import json
    import uuid

    from backend.daemon.auth import with_hello_token
    from backend.daemon.transport import daemon_configured_in_env, sync_connect

    if not daemon_configured_in_env():
        return
    session = resolve_session_id(session_id)
    req_id = uuid.uuid4().hex[:12]
    try:
        s = sync_connect(timeout=0.5)
        s.sendall(
            (
                json.dumps(
                    with_hello_token(
                        {"v": 1, "id": None, "type": "hello", "role": "usage", "session": session}
                    )
                )
                + "\n"
                + json.dumps(
                    {
                        "v": 1,
                        "id": req_id,
                        "type": "session_refresh",
                        "session": session,
                    }
                )
                + "\n"
            ).encode()
        )
        try:
            s.recv(8192)
        except OSError:
            pass
        s.close()
    except OSError:
        pass


def clear_tui_handshakes(session_id: str | None = None) -> None:
    """Remove stale flag-confirm / flags-ask pending+answer files for a session."""
    dirs = [session_dir(session_id)]
    # Also clear legacy top-level handshakes when operating on _default.
    if resolve_session_id(session_id) == "_default":
        dirs.append(_state_dir())
    for cache in dirs:
        if not cache.is_dir():
            continue
        for pattern in (
            "flag-confirm-*.pending.json",
            "flag-confirm-*.answer.json",
            "flags-ask-*.pending.json",
            "flags-ask-*.answer.json",
        ):
            for p in cache.glob(pattern):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


def clear_session_state(session_id: str | None = None) -> None:
    """Wipe Artemis challenge progress for one session."""
    sid = resolve_session_id(session_id)
    p = session_state_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
    except OSError:
        pass
    clear_tui_handshakes(sid)
    try:
        from backend.cost_tracker import clear_published_usage

        clear_published_usage(sid)
    except Exception:
        pass


def reset_accepted_flags(session_id: str | None = None) -> dict:
    """Clear flag progress for a new swarm run (keep challenge + flags_required)."""
    return save_session_state(session_id, accepted_flags=[])


def clear_flags_required(session_id: str | None = None) -> dict:
    """Mark flags_required as unknown (delete the keys)."""
    sid = resolve_session_id(session_id)
    cur = load_session_state(sid)
    cur.pop("flags_required", None)
    cur.pop("flags_explicit", None)
    cur["updated_at"] = time.time()
    p = session_state_path(sid)
    try:
        p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cur


def _write_meta(
    challenge_dir: str,
    container_id: str | None,
    session_id: str | None = None,
) -> None:
    save_session_state(
        resolve_session_id(session_id),
        challenge_dir=challenge_dir,
        container_id=container_id,
    )


def swarm_pid_path(session_id: str | None = None) -> Path:
    return session_dir(session_id) / "swarm.pid"


def swarm_log_path_for_session(challenge_dir: str | None, session_id: str | None = None) -> Path:
    safe = "default"
    if challenge_dir:
        safe = Path(challenge_dir).name or "default"
    return session_dir(session_id) / f"swarm-{safe}.log"
