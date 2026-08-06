"""Shared CTF challenge-paste heuristics (TUI + Cursor stub).

Patterns live in ``shared/challenge_paste.json`` so TypeScript and Python
cannot drift. Both consumers load that file at import/runtime.
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
def _challenge_hint() -> re.Pattern[str]:
    return re.compile(_spec()["challenge_hint"], re.IGNORECASE)


@lru_cache(maxsize=1)
def _ctf_prose() -> re.Pattern[str]:
    return re.compile(_spec()["ctf_prose"], re.IGNORECASE)


@lru_cache(maxsize=1)
def _path_re() -> re.Pattern[str]:
    return re.compile(_spec()["path_re"])


def looks_like_challenge_paste(text: str) -> bool:
    """True when pasted text looks like a CTF challenge description."""
    t = (text or "").strip()
    if not t:
        return False
    if _challenge_hint().search(t):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    spec = _spec()
    return (
        len(lines) >= int(spec["min_lines"])
        and len(t) >= int(spec["min_chars"])
        and bool(_ctf_prose().search(t))
    )


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


def extract_paste_without_paths(text: str, paths: list[str]) -> str:
    """User text with detected paths removed."""
    if not text:
        return ""
    out = text
    for p in paths:
        out = out.replace(p, " ")
    return out.strip()
