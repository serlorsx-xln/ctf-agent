"""Shared first-message paste rules (TUI load interceptor).

``path_re`` extracts host paths from mixed paste. ``greetings`` is the only
reject list — anything else on a fresh session is a challenge (story, URL,
folder, attachments). Not a CTF-keyword allowlist.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = _REPO_ROOT / "shared" / "challenge_paste.json"


@lru_cache(maxsize=1)
def _spec() -> dict:
    return json.loads(_SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _path_re() -> re.Pattern[str]:
    return re.compile(_spec()["path_re"])


@lru_cache(maxsize=1)
def _greetings() -> re.Pattern[str]:
    return re.compile(_spec()["greetings"], re.IGNORECASE)


def is_greeting(text: str) -> bool:
    """True for empty or a bare hello — not a challenge."""
    t = (text or "").strip()
    if not t:
        return True
    return bool(_greetings().fullmatch(t))


def looks_like_challenge_paste(text: str) -> bool:
    """True for any non-empty, non-greeting paste."""
    t = (text or "").strip()
    if not t or is_greeting(t):
        return False
    return True


def extract_challenge_paths(text: str) -> list[str]:
    """Host paths mentioned in the text (folder / attachments)."""
    if not text:
        return []
    seen: list[str] = []
    for m in _path_re().finditer(text):
        p = m.group(1).rstrip(".,;:)\"'")
        if p not in seen:
            seen.append(p)
    return seen
