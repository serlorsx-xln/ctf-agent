"""Shared Cursor SDK bridge client for concurrent solvers/coordinators."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from cursor_sdk import AsyncClient

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_client: AsyncClient | None = None  # owns the bridge subprocess
_agent_client: AsyncClient | None = None  # timeout-tuned view for agents
_refs = 0
_token_patch_applied = False

# ObserveRun streams must outlive long bash tools (crypto factoring, memory
# scans). SDK default stream timeout is 600s — that kills productive turns.
_DEFAULT_UNARY_TIMEOUT_S = 300.0
# None = disable stream timeout (httpx: wait indefinitely for stream chunks).
_DEFAULT_STREAM_TIMEOUT_S: float | None = None


def _env_timeout(name: str, default: float | None) -> float | None:
    """Parse timeout seconds from env. Empty / 'none' / '0' → no timeout."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in ("", "none", "null", "inf", "infinite", "0"):
        return None
    try:
        value = float(text)
    except ValueError:
        logger.warning("Invalid %s=%r — using default %s", name, raw, default)
        return default
    if value <= 0:
        return None
    return value


def _patch_sdk_auth_tokens() -> None:
    """Avoid bridge argv parse failures when token_urlsafe starts with '-'."""
    # cursor-sdk bug: bridge takeValue rejects values that start with "-".
    # https://forum.cursor.com/t/163586
    global _token_patch_applied
    if _token_patch_applied:
        return

    def _safe_token() -> str:
        token = secrets.token_urlsafe(32)
        while token.startswith("-"):
            token = secrets.token_urlsafe(32)
        return token

    import cursor_sdk._store_callback as store_callback
    import cursor_sdk._tool_callback as tool_callback

    # Monkeypatch private SDK helpers (argv rejects tokens starting with "-").
    def _install(mod: Any) -> None:
        mod._new_auth_token = _safe_token

    _install(tool_callback)
    _install(store_callback)
    _token_patch_applied = True


def _timeout_view(owner: AsyncClient) -> AsyncClient:
    """Return a client view with CTF-friendly stream/unary timeouts."""
    unary = _env_timeout("CURSOR_UNARY_TIMEOUT_SECONDS", _DEFAULT_UNARY_TIMEOUT_S)
    stream = _env_timeout("CURSOR_STREAM_TIMEOUT_SECONDS", _DEFAULT_STREAM_TIMEOUT_S)
    logger.info(
        "Cursor bridge timeouts: unary=%s stream=%s",
        unary if unary is not None else "none",
        stream if stream is not None else "none",
    )
    return owner.with_options(unary_timeout=unary, stream_timeout=stream)


async def _launch_owner(cwd: str) -> AsyncClient:
    _patch_sdk_auth_tokens()
    logger.info("Launching Cursor SDK bridge (workspace=%s)", cwd)
    # Discovery timeout only; HTTP timeouts come from the with_options view.
    return await AsyncClient.launch_bridge(workspace=cwd, timeout=60)


async def acquire_client(workspace: str | None = None) -> AsyncClient:
    """Return a process-wide AsyncClient view, launching the bridge on first use."""
    global _client, _agent_client, _refs
    async with _lock:
        if _client is None:
            cwd = workspace or str(Path.cwd())
            _client = await _launch_owner(cwd)
            _agent_client = _timeout_view(_client)
        _refs += 1
        assert _agent_client is not None
        return _agent_client


async def current_client() -> AsyncClient | None:
    """Return the live agent client view without changing refcount."""
    async with _lock:
        return _agent_client


async def force_recreate_client(workspace: str | None = None) -> AsyncClient:
    """Kill and relaunch the shared bridge (poisoned session / dead process).

    Callers that already hold a ref keep their ref count; only the underlying
    bridge process is replaced.
    """
    global _client, _agent_client
    async with _lock:
        if _client is not None:
            logger.warning("Force-recreating Cursor SDK bridge")
            try:
                await _client.aclose()
            except Exception as e:
                logger.warning("Error closing old bridge during recreate: %s", e)
            _client = None
            _agent_client = None
        cwd = workspace or str(Path.cwd())
        _client = await _launch_owner(cwd)
        _agent_client = _timeout_view(_client)
        assert _agent_client is not None
        return _agent_client


async def release_client() -> None:
    """Drop a reference; close the bridge when the last user exits."""
    global _client, _agent_client, _refs
    async with _lock:
        _refs = max(0, _refs - 1)
        if _refs == 0 and _client is not None:
            logger.info("Closing Cursor SDK bridge")
            try:
                await _client.aclose()
            except Exception as e:
                logger.warning("Error closing Cursor SDK bridge: %s", e)
            _client = None
            _agent_client = None


def resolve_api_key(settings: Any) -> str:
    """Resolve CURSOR_API_KEY from settings or environment."""
    key = (getattr(settings, "cursor_api_key", "") or "").strip()
    if key:
        return key
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if key:
        return key
    raise RuntimeError(
        "CURSOR_API_KEY is required for the Cursor backend. "
        "Set it in .env or the environment (Cursor Dashboard → Integrations)."
    )


def is_infra_error_message(message: str | None) -> bool:
    """True when the failure is Cursor bridge/transport, not the challenge."""
    if not message:
        return False
    err = message.strip().lower()
    # Cursor often finishes a turn as status=error with no detail ("error").
    # Treat those as session/transport poison so swarm recovers instead of
    # counting toward the consecutive-ERROR give-up limit.
    if err in {"error", "run error", "unknown error", "failed"}:
        return True
    needles = (
        "bridge request timed out",
        "readtimeout",
        "writetimeout",
        "connecttimeout",
        "pooltimeout",
        "internal: internal error",
        "internal error",
        "apitimeouterror",
        "network error",
        "bridge request failed",
        "connection reset",
        "broken pipe",
        "server disconnected",
        "remoteprotocolerror",
        "connecterror",
    )
    return any(n in err for n in needles)
