"""Post-solve narrative writeup — IDE-style summary for the operator recap.

After CORRECT the winning solver still has session context. Asking it for a short
prose writeup (no tools) beats dumping a numbered tool trail into the recap.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Keep short — Cursor writeup turns have hung past wait_for and left Stopping.
WRITEUP_TIMEOUT_S = 20.0

WRITEUP_PROMPT = """The flag was accepted (CORRECT). Write a CTF writeup for the human operator.

Use this exact structure (markdown headings, plain prose under each):

## Challenge
One or two sentences: what the challenge was (files, category, goal).

## Key insight
The vulnerability / trick that made the solve possible.

## How
Numbered steps in plain language (not raw shell dumps). 4–8 steps max.
Put EACH numbered step on its own line (never jam "1. … 2. …" onto one line).
Mention tools only when they mattered (e.g. strings, jadx, blutter).

Rules:
- 5–12 sentences total across sections (plus the short numbered list).
- Do not call any tools.
- Do not invent details you did not observe in this session.
- Do not pad with filler; be concrete.
- Do not use markdown bold (**…**); plain section headings only.
- Do not restate CORRECT / Confirmed / the flag value — the UI already shows it."""


_HEADING_RE = re.compile(r"^#{1,3}\s+")
_NUMBERED_START_RE = re.compile(r"(?<!\d)(\d{1,2})\.\s+")
_SECTION_SPLIT_RE = re.compile(
    r"(?=\b(?:Solution summary|Key insight|Challenge|How|Steps)\s*:)",
    re.IGNORECASE,
)
_FLAG_TOKEN_RE = re.compile(r"(?i)\b(?:flag|ctf|archa)\{[^{}\n]{4,200}\}")
_ACCEPT_NOISE_RE = re.compile(
    r"(?i)\b(?:the flag was accepted|CORRECT|FLAG FOUND|Confirmed —|"
    r"Challenge complete|counting this flag|Cogitated)\b"
)


def expand_summary_line(line: str) -> list[str]:
    """Split jammed numbered lists / section labels; strip markdown bold."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", (line or "").strip())
    if not text:
        return []
    # "… FLAG: x Solution summary: 1. a 2. b" → separate section + steps
    sections = [s.strip() for s in _SECTION_SPLIT_RE.split(text) if s.strip()]
    if not sections:
        sections = [text]
    out: list[str] = []
    for section in sections:
        matches = list(_NUMBERED_START_RE.finditer(section))
        if len(matches) <= 1:
            out.append(section)
            continue
        head = section[: matches[0].start()].strip()
        if head:
            out.append(head)
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
            chunk = section[m.start() : end].strip()
            if chunk:
                out.append(chunk)
    return out


def _is_flag_only_line(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    t = re.sub(r"(?i)^(?:FLAG|Flag)\s*:?\s*", "", t).strip()
    return bool(_FLAG_TOKEN_RE.fullmatch(t))


def _is_accept_noise_line(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _is_flag_only_line(t):
        return True
    if _ACCEPT_NOISE_RE.search(t) and len(t) < 160:
        return True
    return bool(re.fullmatch(r"(?i)(?:FLAG|Flag|Steps|Solution summary)\s*:?", t))


def narrative_body(text: str) -> str:
    """Prose only — drop a trailing ``Steps:`` tool trail if present."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if re.search(r"(?m)^Steps:\s*$", raw):
        return re.split(r"(?m)^Steps:\s*$", raw, maxsplit=1)[0].strip()
    return raw


def clean_how_lines(text: str) -> list[str]:
    """Expand + drop flag/CORRECT noise; ready for the How: recap."""
    body = narrative_body(text)
    if not body:
        return []
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        if _HEADING_RE.match(line.strip()):
            title = _HEADING_RE.sub("", line.strip()).strip()
            title = re.sub(r"\*\*([^*]+)\*\*", r"\1", title)
            if _is_accept_noise_line(title) or re.fullmatch(r"(?i)Flag", title):
                continue
            if out and out[-1] != "":
                out.append("")
            out.append(title)
            continue
        for piece in expand_summary_line(line):
            if _is_accept_noise_line(piece):
                continue
            # Strip leading "FLAG: …" crumbs glued onto real prose.
            piece = _FLAG_TOKEN_RE.sub("", piece)
            piece = re.sub(r"(?i)\bFLAG\s*:\s*", "", piece).strip(" —-\t")
            if piece and not _is_accept_noise_line(piece):
                out.append(piece)
    while out and not out[-1].strip():
        out.pop()
    return out


def is_usable_narrative(text: str) -> bool:
    """True when cleaned prose is worth showing instead of the command trail."""
    lines = [ln for ln in clean_how_lines(text) if ln.strip()]
    if not lines:
        return False
    # Drop bare section labels for the substance check.
    substance = []
    for ln in lines:
        s = re.sub(
            r"(?i)^(Challenge|Key insight|How|Solution summary)\s*:?\s*",
            "",
            ln,
        ).strip()
        if s:
            substance.append(s)
    blob = " ".join(substance)
    blob = _FLAG_TOKEN_RE.sub("", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    if len(blob) < 40:
        return False
    # Reject pure accept-message regurgitation.
    return not (_ACCEPT_NOISE_RE.search(blob) and len(blob) < 100)


def normalize_writeup_text(text: str) -> str:
    """Clean model writeup for the TUI recap (keep structure, drop junk)."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    # Drop only short single-line solver failure envelopes — not prose that
    # happens to start with "Error:" (e.g. a writeup about an error message).
    first = cleaned.splitlines()[0].strip()
    if len(cleaned.splitlines()) == 1 and first.startswith(
        ("Error:", "Turn failed:", "Infra:")
    ):
        return ""
    # Drop accidental leading/trailing code fences.
    cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    lines: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        # Keep markdown headings as plain section labels for the TUI.
        if _HEADING_RE.match(line.strip()):
            title = _HEADING_RE.sub("", line.strip()).strip()
            title = re.sub(r"\*\*([^*]+)\*\*", r"\1", title)
            if re.fullmatch(r"(?i)Flag", title):
                # Flag value already shown on CORRECT — skip the whole section.
                continue
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(title)
            continue
        for piece in expand_summary_line(line):
            if _is_flag_only_line(piece):
                continue
            lines.append(piece)
    # Trim trailing blanks.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()[:4000]


async def capture_solver_writeup(solver: Any) -> str:
    """Ask the solver for a narrative writeup; empty string if unavailable."""
    fn = getattr(solver, "produce_writeup", None)
    if not callable(fn):
        return ""
    try:
        text = await asyncio.wait_for(fn(), timeout=WRITEUP_TIMEOUT_S)
    except TimeoutError:
        logger.warning("writeup capture timed out after %.0fs", WRITEUP_TIMEOUT_S)
        return ""
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("writeup capture failed", exc_info=True)
        return ""
    return normalize_writeup_text(str(text or ""))
