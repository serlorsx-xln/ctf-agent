"""Challenge folder loading — drop files + plaintext only.

Layouts::

    # With files
    challenges/my-chal/
      challenge.txt
      …files…                 # at root and/or under distfiles/

    # Web-only (just a URL / paste from the CTF page)
    challenges/my-web/
      challenge.txt           # may contain only a link + short text

No metadata.yml. No category tutoring.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.prompts import ChallengeMeta

_DESC_CANDIDATES = (
    "challenge.txt",
    "challenge.md",
    "description.txt",
    "description.md",
    "README.md",
    "readme.txt",
    "CHALLENGE",
)

_RESERVED_NAMES = {
    "workspace",
    "distfiles",
    ".git",
    "__pycache__",
    ".ds_store",
}

_DESC_NAMES_LOWER = {n.lower() for n in _DESC_CANDIDATES}

_CONNECT_AT = re.compile(
    r"connect\s+at\s+([A-Za-z0-9._-]+)\s+(\d{2,5})",
    re.IGNORECASE,
)
_HTTP_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_NC_LINE = re.compile(r"\bnc\s+([A-Za-z0-9._-]+)\s+(\d{2,5})\b", re.IGNORECASE)


def is_challenge_dir(path: str | Path) -> bool:
    root = Path(path)
    if not root.is_dir():
        return False
    # challenge.txt alone is enough (web-only challenges).
    if _find_description_file(root) is not None:
        return True
    if (root / "distfiles").is_dir() and any((root / "distfiles").iterdir()):
        return True
    return bool(list_attachment_names(root))


def load_challenge(challenge_dir: str | Path) -> ChallengeMeta:
    """Load from folder name + plaintext description (+ optional endpoint guess)."""
    root = Path(challenge_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Challenge directory not found: {root}")

    folder_name = root.name
    desc_path = _find_description_file(root)
    description = ""
    if desc_path is not None:
        description = desc_path.read_text(encoding="utf-8", errors="replace").strip()
    if not description:
        description = (
            f"Challenge `{folder_name}`. "
            "No challenge.txt provided — inspect attached files."
        )

    return ChallengeMeta(
        name=folder_name,
        description=description,
        connection_info=guess_connection(description),
    )


def distfiles_host_path(challenge_dir: str | Path) -> Path | None:
    """Host path to mount at /challenge/distfiles, or None for link-only challenges."""
    root = Path(challenge_dir).resolve()
    nested = root / "distfiles"
    if nested.is_dir():
        return nested
    # Loose files at challenge root (excluding description / reserved).
    if list_attachment_names(root):
        return root
    return None


def list_attachment_names(challenge_dir: str | Path) -> list[str]:
    root = Path(challenge_dir).resolve()
    dist = root / "distfiles"
    if dist.is_dir():
        return sorted(
            p.name
            for p in dist.iterdir()
            if not p.name.startswith(".") and p.name != "__pycache__"
        )

    names: list[str] = []
    for p in root.iterdir():
        if p.name.startswith("."):
            continue
        low = p.name.lower()
        if low in _RESERVED_NAMES or low in _DESC_NAMES_LOWER:
            continue
        if p.is_file() or p.is_dir():
            names.append(p.name)
    return sorted(names)


def guess_connection(text: str) -> str:
    """Extract an endpoint mention from pasted text — no site-specific assumptions."""
    if not text:
        return ""
    m = _HTTP_URL.search(text)
    if m:
        url = m.group(0).rstrip(".,;)")
        # Scope lines like https://lab.example/* → base URL
        url = re.sub(r"/\*$", "", url)
        return url
    m = _NC_LINE.search(text)
    if m:
        return f"nc {m.group(1)} {m.group(2)}"
    m = _CONNECT_AT.search(text)
    if m:
        # Protocol undecided — agent chooses from context.
        return f"{m.group(1)}:{m.group(2)}"
    return ""


def _find_description_file(root: Path) -> Path | None:
    lower_map = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    for cand in _DESC_CANDIDATES:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None
