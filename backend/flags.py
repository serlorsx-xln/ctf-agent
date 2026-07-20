"""Local flag acceptance — no external scoreboard required.

A submitted flag is accepted when it looks like a real CTF flag and is not a
known decoy/placeholder. Challenges may require multiple distinct flags via
``flags_required: N`` in challenge text (default **1**). Only when N distinct
flags are accepted does the run complete (CORRECT).

Without a scoreboard this is a *plausibility* gate (stop the run), not proof of
correctness. Agents should submit the exact string the challenge awards —
do not wrap or rewrite formats just to satisfy the checker.

Hardening (still not a scoreboard): reject common test decoys, filename-like
``flag_<hex>`` tokens, and strings that only appear as Dockerfile ``ENV FLAG``
values / attachment basenames under the challenge directory.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

_DECOY_MARKERS = (
    "fake_flag",
    "placeholder",
    "tryharder",
    "ctf{flag}",
    "flag{flag}",
    "default_flag",
    # leakme-style local decoy file body (typo intentional)
    "thie_is_test",
)
# Whole-string / body-only decoys (avoid substring hits on real flags)
_DECOY_EXACT = frozenset(
    {
        "test_flag",
        "this_is_test_flag",
        "thie_is_test_flag",
        "local_test_flag",
        "example_flag",
        "sample_flag",
        "flag",
        "the_flag",
    }
)

# Classic CTF: PREFIX{body}
_FLAG_BRACE = re.compile(r"^[A-Za-z0-9_-]{2,32}\{[^}]{4,256}\}$")
# Dash style: FLAG-..., NSEC-..., etc. (prefix letters, long body)
_FLAG_DASH = re.compile(r"^[A-Za-z]{2,16}-[A-Za-z0-9_-]{8,128}$")
# Formatless secret token (no whitespace). Excludes short segmented PINs.
_FLAG_TOKEN = re.compile(r"^[A-Za-z0-9_+\/=-]{16,200}$")
_LICENSE_LIKE = re.compile(r"^(?:[A-Za-z0-9]{1,5}-){2,}[A-Za-z0-9]{1,5}$")
# Dockerfile-style flag *filename* (leakme): ENV FLAG flag_<md5/sha>
_FLAG_FILENAME_TOKEN = re.compile(r"^flag_[0-9a-f]{16,128}$", re.IGNORECASE)
_ENV_FLAG_ASSIGN = re.compile(
    r"^\s*(?:ENV|ARG)\s+FLAG(?:\s+|=)\s*[\"']?([^\s\"'#]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Optional in challenge.txt: flags_required: 2  (default 1 if omitted)
_FLAGS_REQUIRED_LINE = re.compile(
    r"^flags_required\s*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_ARTIFACT_TEXT_NAMES = frozenset(
    {
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        ".env",
        ".env.example",
        "challenge.txt",
        "metadata.yml",
        "metadata.yaml",
        "readme.md",
        "readme.txt",
    }
)
_SKIP_BASENAME_REJECT = frozenset(
    {
        "challenge.txt",
        "challenge.md",
        "readme.md",
        "readme.txt",
        "dockerfile",
        "metadata.yml",
        "metadata.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "flag.txt",
        "flag",
        ".gitkeep",
        ".ds_store",
    }
)
_MAX_ARTIFACT_FILE_BYTES = 256_000
_MAX_ARTIFACT_WALK_FILES = 400


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


def _flag_body(flag_lower: str) -> str:
    if "{" in flag_lower and flag_lower.endswith("}"):
        return flag_lower[flag_lower.find("{") + 1 : -1]
    return flag_lower


def is_decoy_flag(flag: str) -> bool:
    f = (flag or "").strip().lower()
    if not f:
        return True
    if any(m in f for m in _DECOY_MARKERS):
        return True
    body = _flag_body(f)
    return f in _DECOY_EXACT or body in _DECOY_EXACT


def is_filename_like_flag_token(flag: str) -> bool:
    """True for bare ``flag_<hex>`` tokens (often Dockerfile ENV / on-disk names)."""
    return bool(_FLAG_FILENAME_TOKEN.match((flag or "").strip()))


def _looks_secret_token(f: str) -> bool:
    """Compact formatless flag: mixed charset, not a license/PIN/filename pattern."""
    if not _FLAG_TOKEN.match(f) or _LICENSE_LIKE.match(f):
        return False
    if is_filename_like_flag_token(f):
        return False
    classes = sum(
        (
            any(c.islower() for c in f),
            any(c.isupper() for c in f),
            any(c.isdigit() for c in f),
        )
    )
    return classes >= 2


def collect_artifact_flag_candidates(challenge_dir: str | Path | None) -> set[str]:
    """Strings that look like packaging artifacts, not awarded flags.

    Collects Dockerfile/compose ``ENV|ARG FLAG=...`` values and attachment
    basenames that resemble flag filenames. Used to reject false CORRECT from
    skimming distfiles (e.g. leakme ``ENV FLAG flag_<hex>``).
    """
    if not challenge_dir:
        return set()
    root = Path(challenge_dir)
    if not root.is_dir():
        return set()

    out: set[str] = set()
    seen_files = 0
    for path in root.rglob("*"):
        if seen_files >= _MAX_ARTIFACT_WALK_FILES:
            break
        if not path.is_file():
            continue
        seen_files += 1
        name = path.name
        lower = name.lower()
        if lower not in _SKIP_BASENAME_REJECT and (
            is_filename_like_flag_token(name) or _looks_secret_token(name)
        ):
            out.add(name)

        if lower not in _ARTIFACT_TEXT_NAMES and not lower.startswith("dockerfile"):
            continue
        try:
            if path.stat().st_size > _MAX_ARTIFACT_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _ENV_FLAG_ASSIGN.finditer(text):
            val = (m.group(1) or "").strip().strip("\"'")
            if not val:
                continue
            # Keep PREFIX{...} ENV values out — those may be intentional local flags.
            # Reject filename-like / formatless tokens commonly mistaken for flags.
            if "{" in val or "}" in val:
                continue
            if is_filename_like_flag_token(val) or _looks_secret_token(val) or is_decoy_flag(val):
                out.add(val)
    return out


def is_plausible_flag(
    flag: str,
    *,
    artifact_flags: Sequence[str] | None = None,
) -> bool:
    f = (flag or "").strip()
    if not f or is_decoy_flag(f):
        return False
    if is_filename_like_flag_token(f):
        return False
    if artifact_flags and f in set(artifact_flags):
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
    challenge_dir: str | Path | None = None,
    artifact_flags: Sequence[str] | None = None,
) -> tuple[str, bool]:
    """Validate and accept a flag locally.

    Returns (display_message, challenge_complete).
    ``challenge_complete`` is True only when ``required`` distinct flags are in.
    Partial accepts return ACCEPTED (n/m) and False so solvers continue.
    """
    f = (flag or "").strip()
    req = normalize_flags_required(required)
    prior = [a.strip() for a in already_accepted if a and a.strip()]
    artifacts = set(artifact_flags or ())
    if challenge_dir is not None:
        artifacts |= collect_artifact_flag_candidates(challenge_dir)

    if not f:
        return "Empty flag — nothing to submit.", False
    if is_decoy_flag(f):
        return (
            f'REJECTED decoy/placeholder "{f}". Recover the real flag from challenge logic.',
            False,
        )
    if is_filename_like_flag_token(f) or f in artifacts:
        return (
            f'REJECTED "{f}" — looks like a packaging artifact (Dockerfile ENV / '
            "filename), not the awarded flag. Recover the real flag from challenge logic.",
            False,
        )
    if not is_plausible_flag(f, artifact_flags=list(artifacts)):
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
