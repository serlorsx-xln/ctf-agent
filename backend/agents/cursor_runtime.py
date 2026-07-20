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
_client: AsyncClient | None = None
_refs = 0
_token_patch_applied = False


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

    tool_callback._new_auth_token = _safe_token  # type: ignore[attr-defined]
    store_callback._new_auth_token = _safe_token  # type: ignore[attr-defined]
    _token_patch_applied = True


async def acquire_client(workspace: str | None = None) -> AsyncClient:
    """Return a process-wide AsyncClient, launching the bridge on first use."""
    global _client, _refs
    async with _lock:
        if _client is None:
            _patch_sdk_auth_tokens()
            cwd = workspace or str(Path.cwd())
            logger.info("Launching Cursor SDK bridge (workspace=%s)", cwd)
            _client = await AsyncClient.launch_bridge(workspace=cwd)
        _refs += 1
        return _client


async def release_client() -> None:
    """Drop a reference; close the bridge when the last user exits."""
    global _client, _refs
    async with _lock:
        _refs = max(0, _refs - 1)
        if _refs == 0 and _client is not None:
            logger.info("Closing Cursor SDK bridge")
            try:
                await _client.aclose()
            except Exception as e:
                logger.warning("Error closing Cursor SDK bridge: %s", e)
            _client = None


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
