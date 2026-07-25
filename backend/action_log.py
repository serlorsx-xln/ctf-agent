"""Compact per-solver action log — records tool calls during a solve.

Action lines are kept for debugging/tracing only. The operator recap uses
narrative writeups from ``backend.writeup``, not numbered command dumps.
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


def notes_from_actions(
    actions: Sequence[str],
    *,
    prose: str = "",
) -> str:
    """Return early solver prose for flag_notes — never a command trail."""
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
    # Drop empty / placeholder method strings.
    if re.fullmatch(r"Flag found via \?: .*", t):
        return ""
    return t


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
