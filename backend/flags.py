"""Local flag acceptance — no external scoreboard (CTFd) required.

A submitted flag is accepted when it looks like a real CTF flag and is not a
known decoy/placeholder. Acceptance ends the challenge run (FLAG FOUND).

Without a scoreboard this is a *plausibility* gate (stop the run), not proof of
correctness. Agents should submit the exact string the challenge awards —
do not wrap or rewrite formats just to satisfy the checker.
"""

from __future__ import annotations

import re

_DECOY_MARKERS = (
    "fake_flag",
    "placeholder",
    "tryharder",
    "ctf{flag}",
    "flag{flag}",
    "default_flag",
)

# Classic CTF: PREFIX{body}
_FLAG_BRACE = re.compile(r"^[A-Za-z0-9_-]{2,32}\{[^}]{4,256}\}$")
# Dash style: FLAG-..., NSEC-..., etc. (prefix letters, long body)
_FLAG_DASH = re.compile(r"^[A-Za-z]{2,16}-[A-Za-z0-9_-]{8,128}$")
# Formatless secret token (no whitespace). Excludes short segmented PINs.
_FLAG_TOKEN = re.compile(r"^[A-Za-z0-9_+\/=-]{16,200}$")
_LICENSE_LIKE = re.compile(r"^(?:[A-Za-z0-9]{1,5}-){2,}[A-Za-z0-9]{1,5}$")


def is_decoy_flag(flag: str) -> bool:
    f = (flag or "").strip().lower()
    if not f:
        return True
    return any(m in f for m in _DECOY_MARKERS)


def _looks_secret_token(f: str) -> bool:
    """Compact formatless flag: mixed charset, not a license/PIN pattern."""
    if not _FLAG_TOKEN.match(f) or _LICENSE_LIKE.match(f):
        return False
    classes = sum(
        (
            any(c.islower() for c in f),
            any(c.isupper() for c in f),
            any(c.isdigit() for c in f),
        )
    )
    return classes >= 2


def is_plausible_flag(flag: str) -> bool:
    f = (flag or "").strip()
    if not f or is_decoy_flag(f):
        return False
    if _FLAG_BRACE.match(f) or _FLAG_DASH.match(f):
        return True
    # Odd brace layouts (spaces inside, unusual prefixes)
    if "{" in f and "}" in f and len(f) >= 10:
        return True
    # Challenges with no published format — secret-like single token only
    if _looks_secret_token(f):
        return True
    return False


def accept_flag(flag: str) -> tuple[str, bool]:
    """Validate and accept a flag locally.

    Returns (display_message, is_confirmed).
    Confirmed → solvers should treat as FLAG FOUND / stop the swarm.
    """
    f = (flag or "").strip()
    if not f:
        return "Empty flag — nothing to submit.", False
    if is_decoy_flag(f):
        return (
            f'REJECTED decoy/placeholder "{f}". '
            "Recover the real flag from challenge logic.",
            False,
        )
    if not is_plausible_flag(f):
        return (
            f'REJECTED "{f}" — does not look like a CTF flag. '
            "Submit the exact awarded string (PREFIX{...}, PREFIX-..., "
            "or a compact secret). Do not wrap/rewrite just to pass checks.",
            False,
        )
    return (
        f'CORRECT — accepted "{f}". Challenge complete for this run.',
        True,
    )
