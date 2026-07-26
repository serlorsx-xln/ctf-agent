"""Post-solve narrative writeup — IDE-style summary for the operator recap.

After CORRECT the winning solver still has session context. Asking it for a short
prose writeup (no tools) powers the operator recap.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Cursor/Grok writeup turns need headroom; swarm waits the full timeout.
WRITEUP_TIMEOUT_S = 45.0

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
_NUMBERED_START_RE = re.compile(r"(?:^|(?<=\s))(\d{1,2})\.\s+")
# Colon required for How/Steps (avoid splitting "How the…"). Optional for
# Solution summary / Key insight / Challenge which models often omit.
_SECTION_SPLIT_RE = re.compile(
    r"(?=\b(?:Solution summary|Key insight|Challenge)\b\s*:?|\b(?:How|Steps)\s*:)",
    re.IGNORECASE,
)
_MD_SECTION_RE = re.compile(
    r"(?i)\s*#{1,3}\s*((?:Solution summary|Key insight|Challenge|How|Steps)\b\s*:?)"
)
_DECRYPT_BREAK_RE = re.compile(r"(?=\bDecryption\s*:)", re.IGNORECASE)
_FLAG_TOKEN_RE = re.compile(r"(?i)\b(?:flag|ctf|archa)\{[^{}\n]{4,200}\}")
_ACCEPT_NOISE_RE = re.compile(
    r"(?i)\b(?:the flag was accepted|CORRECT|FLAG FOUND|Confirmed —|"
    r"Challenge complete|counting this flag|Cogitated)\b"
)
_SECTION_LABEL_ONLY_RE = re.compile(
    r"(?i)^(Challenge|Key insight|How|Solution summary|Steps)\s*:?\s*$"
)
_BARE_HASH_RE = re.compile(r"^#{1,3}$")


def _merge_broken_heading_lines(lines: list[str]) -> list[str]:
    """``##`` on one line + ``Challenge`` on the next → ``## Challenge``."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if _BARE_HASH_RE.fullmatch(s) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not _HEADING_RE.match(nxt):
                out.append(f"{s} {nxt}")
                i += 2
                continue
        out.append(lines[i])
        i += 1
    return out


def is_fragmented_prose(text: str) -> bool:
    """True when model output is token-streamed (one word per line)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 6:
        return False
    bodyish = 0
    short = 0
    for ln in lines:
        if _HEADING_RE.match(ln) or _BARE_HASH_RE.fullmatch(ln):
            continue
        if _NUMBERED_START_RE.match(ln):
            continue
        if _SECTION_LABEL_ONLY_RE.match(ln):
            continue
        bodyish += 1
        if len(ln.split()) <= 2 and not ln.endswith((".", "!", "?", ":", ";")):
            short += 1
    if bodyish < 4:
        return False
    return short >= max(4, int(bodyish * 0.55))


def collapse_prose_fragments(text: str) -> str:
    """Merge word-per-line streamed prose into normal paragraphs."""
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = _merge_broken_heading_lines(raw.splitlines())
    if not is_fragmented_prose("\n".join(lines)):
        merged = "\n".join(lines).strip()
        return merged if merged != raw else raw

    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(" ".join(buf))
            buf.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if _HEADING_RE.match(line) or _BARE_HASH_RE.fullmatch(line):
            flush()
            title = _HEADING_RE.sub("", line).strip() or line
            out.append(title)
            continue
        if _SECTION_LABEL_ONLY_RE.match(line):
            flush()
            out.append(line)
            continue
        if _NUMBERED_START_RE.match(line):
            flush()
            out.append(line)
            continue
        if len(line.split()) <= 3 and not line.endswith((".", "!", "?", ":", ";")):
            buf.append(line)
            continue
        if buf:
            buf.append(line)
            flush()
        else:
            out.append(line)
    flush()
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


def join_streamed_text_parts(parts: list[str]) -> str:
    """Join Cursor/Gemini writeup deltas without word-per-line paragraphs."""
    chunks = [p.strip() for p in parts if (p or "").strip()]
    if not chunks:
        return ""
    if len(chunks) >= 3:
        tiny = sum(1 for c in chunks if len(c.split()) <= 4 and "\n" not in c)
        if tiny >= len(chunks) * 0.6:
            return collapse_prose_fragments(" ".join(chunks))
    return collapse_prose_fragments("\n\n".join(chunks))


def expand_summary_line(line: str) -> list[str]:
    """Split jammed numbered lists / section labels; strip markdown bold."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", (line or "").strip())
    if not text:
        return []
    # ``…} ## Solution Summary 1. …`` → section on its own line (no bare '#').
    text = _MD_SECTION_RE.sub(r"\n\1\n", text).strip()
    sections = [s.strip() for s in _SECTION_SPLIT_RE.split(text) if s.strip()]
    if not sections:
        sections = [text]
    out: list[str] = []
    for section in sections:
        section = _HEADING_RE.sub("", section).strip()
        if not section:
            continue
        matches = list(_NUMBERED_START_RE.finditer(section))
        pieces: list[str]
        if len(matches) <= 1:
            pieces = [section]
        else:
            pieces = []
            head = section[: matches[0].start()].strip()
            if head:
                pieces.append(head)
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
                chunk = section[m.start() : end].strip()
                if chunk:
                    pieces.append(chunk)
        for piece in pieces:
            for part in _DECRYPT_BREAK_RE.split(piece):
                part = part.strip()
                if part:
                    out.append(part)
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
    return bool(re.fullmatch(r"(?i)(?:FLAG|Flag|Steps)\s*:?", t))

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
    body = narrative_body(collapse_prose_fragments(text))
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


def is_command_trail(text: str) -> bool:
    """True when the note is mostly numbered tool lines (bash: …), not prose."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    toolish = 0
    for ln in lines:
        if re.match(r"^\d+\.\s+\S+:\s", ln) or re.match(
            r"^(?:bash|Bash|shell|read_file|write_file|list_files|submit_flag)\s*:",
            ln,
        ):
            toolish += 1
    return toolish >= max(1, (len(lines) + 1) // 2)


def is_usable_narrative(text: str) -> bool:
    """True when cleaned prose is worth showing in the operator recap."""
    if is_command_trail(text):
        return False
    collapsed = collapse_prose_fragments(text)
    if is_fragmented_prose(collapsed):
        return False
    lines = [ln for ln in clean_how_lines(collapsed) if ln.strip()]
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
    cleaned = collapse_prose_fragments(str(text or ""))
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
