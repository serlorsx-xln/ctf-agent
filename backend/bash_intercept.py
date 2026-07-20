"""Parse harness commands embedded in compound bash lines.

Claude solvers rewrite Bash through docker exec; ``submit_flag`` /
``notify_coordinator`` are harness verbs (not container binaries). Agents often
prefix them with ``cd … &&``, so matching must tolerate compound commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Command boundary: start of string, or after ; && || newline
_BOUNDARY = r"(?:^|(?<=;)|(?<=&&)|(?<=\|\|)|(?<=\n))"

_SUBMIT_FLAG_RE = re.compile(
    rf"{_BOUNDARY}\s*submit_flag\s+"
    r"(?:(?P<q>['\"])(?P<quoted>.+?)(?P=q)|(?P<bare>\S+))"
    r"\s*(?:$|(?=;)|(?=&&)|(?=\|\|)|(?=\n))",
    re.DOTALL,
)

_NOTIFY_RE = re.compile(
    rf"{_BOUNDARY}\s*notify_coordinator\s+"
    r"(?:(?P<q>['\"])(?P<quoted>.+?)(?P=q)|(?P<bare>\S+))"
    r"\s*(?:$|(?=;)|(?=&&)|(?=\|\|)|(?=\n))",
    re.DOTALL,
)

_SHELL_EXPANSION_RE = re.compile(r"\$\(|\$\{|`")


@dataclass(frozen=True)
class ParsedHarnessVerb:
    """One harness verb match inside a compound bash line."""

    value: str
    start: int
    end: int
    has_expansion: bool

    @property
    def suffix(self) -> str:
        """Remainder of the command after this verb (leading ;/&&/|| stripped)."""
        return ""  # filled by helper using full command


def _strip_cmd_prefix(tail: str) -> str:
    return re.sub(r"^\s*(?:&&|\|\||;)\s*", "", tail).strip()


def parse_submit_flag(command: str) -> ParsedHarnessVerb | None:
    """Parse ``submit_flag``; detect shell expansions that the harness cannot eval."""
    if not command or "submit_flag" not in command:
        return None
    m = _SUBMIT_FLAG_RE.search(command.strip())
    if not m:
        # Unquoted $(...) form: submit_flag $(cat f)
        m2 = re.search(
            rf"{_BOUNDARY}\s*submit_flag\s+(\$\([^)]*\)|\$\{{[^}}]*\}}|`[^`]+`)",
            command.strip(),
        )
        if not m2:
            return None
        raw = m2.group(1)
        return ParsedHarnessVerb(
            value=raw,
            start=m2.start(),
            end=m2.end(),
            has_expansion=True,
        )
    flag = (m.group("quoted") if m.group("quoted") is not None else m.group("bare")) or ""
    flag = flag.strip()
    if not flag:
        return None
    return ParsedHarnessVerb(
        value=flag,
        start=m.start(),
        end=m.end(),
        has_expansion=bool(_SHELL_EXPANSION_RE.search(flag)),
    )


def extract_submit_flag(command: str) -> str | None:
    """Return a literal flag argument, or None if missing/expansion/unparsed."""
    parsed = parse_submit_flag(command)
    if parsed is None or parsed.has_expansion:
        return None
    return parsed.value or None


def submit_flag_suffix(command: str, parsed: ParsedHarnessVerb) -> str:
    """Commands after the matched ``submit_flag`` (if any)."""
    # Match was against strip()'d text — map end onto original when possible.
    stripped = command.strip()
    offset = command.find(stripped)
    if offset < 0:
        offset = 0
    abs_end = offset + parsed.end
    return _strip_cmd_prefix(command[abs_end:])


def extract_notify_coordinator(command: str) -> str | None:
    """Return the message from a ``notify_coordinator`` invocation, if any."""
    if not command or "notify_coordinator" not in command:
        return None
    m = _NOTIFY_RE.search(command.strip())
    if not m:
        return None
    msg = (m.group("quoted") if m.group("quoted") is not None else m.group("bare")) or ""
    msg = msg.strip()
    if msg and _SHELL_EXPANSION_RE.search(msg):
        return None
    return msg or None


SUBMIT_EXPANSION_ERROR = (
    "ERROR: submit_flag does not expand shell $(...), ${...}, or backticks. "
    "Call the submit_flag tool with the literal flag string "
    "(e.g. read the file first, then submit the value)."
)
