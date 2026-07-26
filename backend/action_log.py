"""Early solver prose captured at flag accept (before the writeup turn)."""

from __future__ import annotations

import re


def notes_from_prose(prose: str) -> str:
    """Return usable solver prose for flag_notes, or empty when not narrative."""
    from backend.writeup import clean_how_lines, is_usable_narrative

    cleaned = _useful_prose(prose)
    if cleaned and is_usable_narrative(cleaned):
        return "\n".join(clean_how_lines(cleaned)).strip()
    if cleaned:
        lines = clean_how_lines(cleaned)
        if lines:
            return "\n".join(lines).strip()
    return ""


def _useful_prose(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if t.startswith(("Error:", "Turn failed:", "Infra:", "Rejected decoy")):
        return ""
    if re.fullmatch(r"Flag found via \?: .*", t):
        return ""
    return t
