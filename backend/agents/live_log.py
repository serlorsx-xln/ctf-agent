"""Shared live terminal logging for solver agents (cursor / claude / codex)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("backend.agents.live")


def live(tag: str, text: str, *, limit: int = 4000) -> None:
    """Print AI activity to the terminal (and log) so runs are watchable live."""
    # Don't bury the interactive flag-confirm prompt with parallel tool spam.
    from backend.flags import confirm_in_progress

    if confirm_in_progress():
        return
    body = text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars]"
    line = f"[{tag}] {body}"
    print(line, flush=True)
    logger.info("%s", line)


def live_json(tag: str, payload: Any, *, limit: int = 1500) -> None:
    """Pretty-print a small JSON-ish payload for tool args."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    live(tag, text, limit=limit)
