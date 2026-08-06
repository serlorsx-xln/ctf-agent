"""Normalize Artemis daemon session ids.

OpenCode chat ``sessionID`` is the canonical id. Missing/null maps to
``_default`` so headless CLI and older single-session tests keep working.
"""

from __future__ import annotations

DEFAULT_SESSION_ID = "_default"


def normalize_session_id(session: str | None) -> str:
    sid = (session or "").strip()
    return sid if sid else DEFAULT_SESSION_ID
