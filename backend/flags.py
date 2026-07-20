"""Local flag acceptance — no external scoreboard required.

A submitted flag is accepted when it looks like a real CTF flag and is not a
known decoy/placeholder. Challenges may require multiple distinct flags via
``flags_required: N`` in challenge text (default **1**). Only when N distinct
flags are accepted does the run complete (CORRECT).

Without a scoreboard this is a *plausibility* gate (stop the run), not proof of
correctness. Agents should submit the exact string the challenge awards —
do not wrap or rewrite formats just to satisfy the checker.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

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

# Optional in challenge.txt: flags_required: 2  (default 1 if omitted)
_FLAGS_REQUIRED_LINE = re.compile(
    r"^flags_required\s*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_flags_required(text: str) -> int:
    """How many distinct flags this challenge needs. Default 1.

    Only an explicit ``flags_required: N`` line overrides the default.
    No keyword inference from free-form paste (wording varies; CLI will ask later).
    """
    if not text:
        return 1
    m = _FLAGS_REQUIRED_LINE.search(text)
    if not m:
        return 1
    return max(1, int(m.group(1)))


def normalize_flags_required(value: int | None) -> int:
    """Clamp / default a flags_required value to >= 1."""
    if value is None:
        return 1
    try:
        return max(1, int(value))
    except TypeError, ValueError:
        return 1


def is_counted_accept_message(message: str) -> bool:
    """True if this flag should be tracked locally as already accepted.

    Includes swarm ``Already accepted this flag`` so siblings sync progress
    without re-submitting.
    """
    return bool(message) and (
        message.startswith("ACCEPTED")
        or message.startswith("CORRECT")
        or message.startswith("Already accepted")
    )


def is_complete_accept_message(message: str) -> bool:
    """True if the challenge is done (CORRECT or ALREADY SOLVED).

    Use ``startswith`` — never ``"CORRECT" in text`` — because ACCEPTED
    messages may mention the word CORRECT without being complete.
    """
    return bool(message) and (message.startswith("CORRECT") or message.startswith("ALREADY SOLVED"))


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
    return bool(_looks_secret_token(f))


def accept_flag(
    flag: str,
    *,
    already_accepted: Sequence[str] = (),
    required: int = 1,
) -> tuple[str, bool]:
    """Validate and accept a flag locally.

    Returns (display_message, challenge_complete).
    ``challenge_complete`` is True only when ``required`` distinct flags are in.
    Partial accepts return ACCEPTED (n/m) and False so solvers continue.
    """
    f = (flag or "").strip()
    req = normalize_flags_required(required)
    prior = [a.strip() for a in already_accepted if a and a.strip()]

    if not f:
        return "Empty flag — nothing to submit.", False
    if is_decoy_flag(f):
        return (
            f'REJECTED decoy/placeholder "{f}". Recover the real flag from challenge logic.',
            False,
        )
    if not is_plausible_flag(f):
        return (
            f'REJECTED "{f}" — does not look like a CTF flag. '
            "Submit the exact awarded string (PREFIX{...}, PREFIX-..., "
            "or a compact secret). Do not wrap/rewrite just to pass checks.",
            False,
        )

    # Already complete — do not accept additional distinct flags.
    if len(prior) >= req:
        joined = " | ".join(prior)
        return (
            f"ALREADY SOLVED — all {req} flag(s) already accepted: {joined}",
            True,
        )

    if f in prior:
        n = len(prior)
        return (
            f"Already accepted this flag ({n}/{req}). "
            "Continue and submit the remaining distinct flag(s).",
            False,
        )

    accepted = [*prior, f]
    n = len(accepted)
    if n < req:
        return (
            f'ACCEPTED "{f}" ({n}/{req}). '
            "Continue — submit the remaining distinct flag(s). "
            "Do not stop until all required flags are accepted.",
            False,
        )

    joined = " | ".join(accepted)
    if req == 1:
        return (
            f'CORRECT — accepted "{f}". Challenge complete for this run.',
            True,
        )
    return (
        f"CORRECT — accepted all {req} flags: {joined}. Challenge complete for this run.",
        True,
    )
