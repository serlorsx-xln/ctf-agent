"""Compact per-solver action log — fallback for the end-of-run solve recap.

After CORRECT the swarm prefers a narrative writeup from the winning solver
(``backend.writeup``). This module records tool calls so a silent writeup still
has a factual trail for ``notes_from_actions``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_MAX_ENTRIES = 40
_MAX_LINE = 160


def format_tool_line(name: str, args: Mapping[str, Any] | str | None = None) -> str:
    """One human-readable line for a tool call."""
    label = name.split("__")[-1] if "__" in name else name
    preview = ""
    if isinstance(args, Mapping):
        tool_l = label.lower()
        if tool_l in ("bash", "shell"):
            preview = str(args.get("command") or args.get("cmd") or "").strip()
        elif tool_l in ("read_file", "write_file", "list_files", "read", "write"):
            preview = str(args.get("path") or args.get("file_path") or "").strip()
        elif tool_l == "submit_flag" or tool_l.endswith("submit_flag"):
            preview = str(args.get("flag") or "").strip()
        elif tool_l == "view_image":
            preview = str(args.get("filename") or args.get("path") or "").strip()
        elif tool_l in ("web_fetch", "webfetch"):
            preview = str(args.get("url") or "").strip()
        elif tool_l == "notify_coordinator" or tool_l.endswith("notify_coordinator"):
            preview = str(args.get("message") or "").strip()
        else:
            try:
                preview = json.dumps(dict(args), ensure_ascii=False, default=str)
            except Exception:
                preview = str(args)
    elif args is not None:
        preview = str(args).strip()

    preview = _collapse(preview)
    if len(preview) > _MAX_LINE:
        preview = preview[: _MAX_LINE - 1] + "…"
    return f"{label}: {preview}" if preview else label


def append_action(log: list[str], name: str, args: Mapping[str, Any] | str | None = None) -> None:
    """Append a tool line, keeping the log bounded."""
    log.append(format_tool_line(name, args))
    if len(log) > _MAX_ENTRIES:
        del log[: len(log) - _MAX_ENTRIES]


def command_how_lines(actions: Sequence[str], *, limit: int = 24) -> list[str]:
    """Numbered tool trail for How: — no submit_flag (flag already on CORRECT)."""
    useful = [
        a
        for a in actions
        if a and not a.startswith("submit_flag:") and not a.lower().startswith("submit_flag:")
    ]
    if not useful:
        return []
    shown = useful if len(useful) <= limit else useful[-limit:]
    start = len(useful) - len(shown) + 1
    return [f"{i}. {line}" for i, line in enumerate(shown, start=start)]


def notes_from_actions(
    actions: Sequence[str],
    *,
    prose: str = "",
) -> str:
    """Build the note block stored on an accepted flag.

    Prefer a usable narrative XOR the command trail — never both. Mixing a
    jammed model summary with Steps: dumps is what made the TUI look cluttered.
    """
    from backend.writeup import clean_how_lines, is_usable_narrative

    cleaned = _useful_prose(prose)
    if cleaned and is_usable_narrative(cleaned):
        return "\n".join(clean_how_lines(cleaned)).strip()

    trail = command_how_lines(actions)
    if trail:
        return "\n".join(trail).strip()
    # Last resort: keep non-usable prose if it is the only signal.
    if cleaned:
        return "\n".join(clean_how_lines(cleaned) or [cleaned]).strip()
    return ""


def _useful_prose(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if t.startswith(("Error:", "Turn failed:", "Infra:", "Rejected decoy")):
        return ""
    # Drop empty / placeholder method strings.
    if re.fullmatch(r"Flag found via \?: .*", t):
        return ""
    return t


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
