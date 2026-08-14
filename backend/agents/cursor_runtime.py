"""Shared Cursor SDK bridge client for concurrent solvers/coordinators."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from cursor_sdk import AsyncClient

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_client: AsyncClient | None = None  # owns the bridge subprocess
_agent_client: AsyncClient | None = None  # timeout-tuned view for agents
_refs = 0
_token_patch_applied = False
_bridge_workspace: str | None = None

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


def shared_bridge_workspace() -> str:
    """Stable temp tree for the process-wide Cursor SDK bridge.

    Solvers still use their own ``LocalAgentOptions.cwd``. The bridge must not
    bind to the first solver's ephemeral dir (that directory can vanish while
    siblings are still running).
    """
    global _bridge_workspace
    if _bridge_workspace and Path(_bridge_workspace).is_dir():
        return _bridge_workspace
    root = Path(tempfile.mkdtemp(prefix="ctf-cursor-bridge-"))
    (root / "AGENTS.md").write_text(
        "# CTF Solver Workspace\n\n"
        "Use only the custom sandbox tools. Do not use host Shell/Read/Write.\n",
        encoding="utf-8",
    )
    _bridge_workspace = str(root)
    return _bridge_workspace


async def _launch_owner(cwd: str) -> AsyncClient:
    _patch_sdk_auth_tokens()
    logger.info("Launching Cursor SDK bridge (workspace=%s)", cwd)
    # Discovery timeout only; HTTP timeouts come from the with_options view.
    return await AsyncClient.launch_bridge(workspace=cwd, timeout=60)


async def acquire_client() -> AsyncClient:
    """Return a process-wide AsyncClient view, launching the bridge on first use.

    The bridge always uses ``shared_bridge_workspace()``. Each ``CursorSolver``
    still sets its own cwd via ``LocalAgentOptions``.
    """
    global _client, _agent_client, _refs
    async with _lock:
        if _client is None:
            cwd = shared_bridge_workspace()
            _client = await _launch_owner(cwd)
            _agent_client = _timeout_view(_client)
        _refs += 1
        assert _agent_client is not None
        return _agent_client


async def current_client() -> AsyncClient | None:
    """Return the live agent client view without changing refcount."""
    async with _lock:
        return _agent_client


async def force_recreate_client() -> AsyncClient:
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
        cwd = shared_bridge_workspace()
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
        "Add it in the Artemis TUI via /connect (Cursor), or set CURSOR_API_KEY for CI."
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
    if err.startswith("run error (no detail"):
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


def format_cursor_run_error(
    *,
    result_text: str | None = None,
    status_message: str | None = None,
) -> str:
    """Prefer SDK detail over opaque ``run error`` placeholders."""
    placeholders = {"error", "run error", "unknown error", "failed"}
    real: list[str] = []
    seen: set[str] = set()
    for raw in (result_text, status_message):
        text = (raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in placeholders or key in seen:
            continue
        seen.add(key)
        real.append(text)
    if real:
        joined = real[0] if len(real) == 1 else " | ".join(real)
        return humanize_cursor_error(joined)
    for raw in (result_text, status_message):
        text = (raw or "").strip()
        if text:
            return humanize_cursor_error(text)
    return "run error (no detail from Cursor SDK)"


def is_quota_error_message(message: str | None) -> bool:
    if not message:
        return False
    err = message.lower()
    return any(
        k in err
        for k in (
            "quota",
            "rate limit",
            "rate_limit",
            "capacity",
            "usage limit",
            "usage_limit",
            "billing",
            "spend limit",
            "overloaded",
            "switch to auto",
            "hit your usage",
        )
    )


def humanize_cursor_error(message: str | None) -> str:
    """Collapse long Cursor billing/quota SDK dumps into a short operator line.

    The raw SDK text often includes marketing ("You've saved $N…") which is
    noise in the TUI swarm log — keep one actionable sentence instead.
    """
    text = (message or "").strip()
    if not text:
        return text
    if not is_quota_error_message(text):
        # Still trim runaway single-line SDK dumps.
        if len(text) > 220:
            return text[:200].rstrip() + "…"
        return text

    lower = text.lower()
    # Prefer the monthly usage-cap phrasing when both "usage limit" and
    # "spend limit" appear (Cursor's Ultra dump mentions both).
    if "hit your usage" in lower or "usage limit" in lower or "usage_limit" in lower:
        reset = ""
        import re

        m = re.search(r"reset[^\d]{0,40}?(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text, re.I)
        if m:
            reset = f" (resets {m.group(1)})"
        return f"Cursor usage limit reached — switch model or wait for reset{reset}"
    if "spend limit" in lower:
        return "Cursor spend limit reached — raise the limit or switch model"
    if "rate limit" in lower or "rate_limit" in lower or "overloaded" in lower:
        return "Cursor rate-limited — wait a moment or switch model"
    return "Cursor usage limit reached — switch model or wait for reset"
