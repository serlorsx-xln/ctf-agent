"""Challenge folder loading — drop files + plaintext only.

Layouts::

    # With files
    challenges/my-chal/
      challenge.txt
      …files…                 # at root and/or under distfiles/

    # Web-only (just a URL / paste from the CTF page)
    challenges/my-web/
      challenge.txt           # may contain only a link + short text

No metadata.yml.

Optional in ``challenge.txt``::

    flags_required: 2   # distinct accepts before CORRECT (default 1 if omitted)

Prompt follows Veria's skeleton — see ``backend/prompts.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
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
# ssh user@host -pPORT  |  ssh host -p PORT  |  ssh user@host
_SSH_LINE = re.compile(
    r"\bssh\s+(?:([^\s@]+)@)?([A-Za-z0-9._-]+)(?:\s+-p\s*(\d{2,5}))?",
    re.IGNORECASE,
)


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
            f"Challenge `{folder_name}`. No challenge.txt provided — inspect attached files."
        )

    # flags_required is no longer parsed from the description — the operator is
    # always asked via the TUI digits dialog. Keep the ChallengeMeta default (1)
    # as a harmless placeholder; the daemon/swarm ask when None is propagated.
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
            p.name for p in dist.iterdir() if not p.name.startswith(".") and p.name != "__pycache__"
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


# Hosts that appear in challenge writeups / archive pages but are not the service.
_DOC_HOST_FRAGMENTS = (
    "archive.ooo",
    "ctftime.org",
    "github.com",
    "githubusercontent.com",
    "gitlab.com",
    "defcon.org",
    "scoreboard",
    "oooverflow.io",
    "hub.docker.com",
    "docker.io",
    "amazonaws.com",
    "web.archive.org",
)


def _is_doc_url(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in _DOC_HOST_FRAGMENTS)


def is_doc_host(host: str) -> bool:
    """True when a hostname looks like a writeup/archive site, not the lab."""
    low = (host or "").strip().rstrip(".").lower()
    if not low:
        return False
    return any(h in low for h in _DOC_HOST_FRAGMENTS)


def guess_connection(text: str) -> str:
    """Extract an endpoint mention from pasted text — no site-specific assumptions."""
    if not text:
        return ""
    # Prefer explicit connect/nc/ssh lines over incidental Source:/writeup URLs.
    # Writeup ``nc ctftime.org 443`` examples must not become FIRST ACTION.
    for m in _NC_LINE.finditer(text):
        if not is_doc_host(m.group(1)):
            return f"nc {m.group(1)} {m.group(2)}"
    for m in _SSH_LINE.finditer(text):
        if is_doc_host(m.group(2)):
            continue
        user, host, port = m.group(1), m.group(2), m.group(3)
        target = f"{user}@{host}" if user else host
        if port:
            return f"ssh {target} -p{port}"
        return f"ssh {target}"
    for m in _CONNECT_AT.finditer(text):
        if not is_doc_host(m.group(1)):
            return f"{m.group(1)}:{m.group(2)}"
    for m in _HTTP_URL.finditer(text):
        url = m.group(0).rstrip(".,;)")
        # Scope lines like https://lab.example/* → base URL
        url = re.sub(r"/\*$", "", url)
        if _is_doc_url(url):
            continue
        return url
    return ""


def _find_description_file(root: Path) -> Path | None:
    lower_map = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    for cand in _DESC_CANDIDATES:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def challenges_cache_root() -> Path:
    from backend.cache import cache_dir

    return cache_dir() / "challenges"


def load_allowlist_roots() -> list[Path]:
    """Host paths that may be copied into a challenge workspace."""
    roots: list[Path] = []

    def _add(raw: Path | str) -> None:
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            return
        if resolved not in roots:
            roots.append(resolved)

    _add(Path.cwd())
    _add(challenges_cache_root())
    _add(Path(__file__).resolve().parents[1] / "challenges")
    try:
        _add(tempfile.gettempdir())
    except OSError:
        pass
    for part in (os.environ.get("ARTEMIS_LOAD_ROOTS") or "").split(os.pathsep):
        if part.strip():
            _add(part.strip())
    return roots


def is_allowed_load_path(path: Path | str) -> bool:
    """True when ``path`` (after symlink resolve) stays under an allowlisted root."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    for root in load_allowlist_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def assert_allowed_load_path(path: Path | str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not is_allowed_load_path(resolved):
        raise PermissionError(
            f"load path outside allowlist (cwd, cache, repo challenges/, "
            f"temp, ARTEMIS_LOAD_ROOTS): {resolved}"
        )
    return resolved


def slugify_challenge_name(text: str, fallback: str = "paste") -> str:
    first = ""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            first = s
            break
    if not first:
        return fallback
    # Drop URL scheme for slug
    first = re.sub(r"^https?://", "", first, flags=re.I)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", first)[:48].strip("-._")
    return slug.lower() or fallback


def materialize_challenge(
    *,
    description: str,
    name: str | None = None,
    attachments: list[str] | None = None,
    challenge_id: str | None = None,
    cache_root: Path | None = None,
) -> Path:
    """Write pasted challenge text (+ optional host files) into a cache challenge dir."""
    import uuid

    text = (description or "").strip()
    if not text:
        raise ValueError("empty challenge description")

    root = cache_root or challenges_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    cid = challenge_id or f"{slugify_challenge_name(text, name or 'paste')}-{uuid.uuid4().hex[:8]}"
    dest = (root / cid).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "challenge.txt").write_text(text + "\n", encoding="utf-8")

    dist = dest / "distfiles"
    dist.mkdir(exist_ok=True)
    _copy_attachments(dist, attachments)
    return dest


def _copy_attachments(dist: Path, attachments: list[str] | None) -> None:
    """Copy host folders/files into a distfiles dir (dirs via copytree, files via copy2)."""
    for raw in attachments or []:
        src = Path(str(raw)).expanduser().resolve()
        if not src.exists():
            continue
        assert_allowed_load_path(src)
        target = dist / src.name
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)


def _attachment_paths_from_challenge_dir(root: Path) -> list[str]:
    """Host paths to copy into a materialized ``distfiles/`` (flat, no extra nesting)."""
    out: list[str] = []
    nested = root / "distfiles"
    if nested.is_dir() and any(nested.iterdir()):
        for p in sorted(nested.iterdir()):
            if p.name.startswith(".") or p.name == "__pycache__":
                continue
            out.append(str(p))
        for p in sorted(root.iterdir()):
            if p.name == "distfiles" or p.name.startswith("."):
                continue
            low = p.name.lower()
            if low in _DESC_NAMES_LOWER or low in _RESERVED_NAMES:
                continue
            out.append(str(p))
        return out
    for p in sorted(root.iterdir()):
        if p.name.startswith("."):
            continue
        low = p.name.lower()
        if low in _DESC_NAMES_LOWER or low in _RESERVED_NAMES:
            continue
        out.append(str(p))
    return out


def _merged_dir_paste_description(root: Path, paste: str) -> str:
    """Prefer paste (often ``nc host port``); keep on-disk description if present."""
    paste = paste.strip()
    existing = _find_description_file(root)
    if existing is None:
        return paste
    on_disk = existing.read_text(encoding="utf-8", errors="replace").strip()
    if not on_disk:
        return paste
    if not paste:
        return on_disk
    if paste in on_disk or on_disk in paste:
        return paste if len(paste) >= len(on_disk) else on_disk
    return f"{on_disk}\n\n{paste}"


def resolve_load_target(
    *,
    path: str | None = None,
    prompt: str | None = None,
    description: str | None = None,
    name: str | None = None,
    attachments: list[str] | None = None,
) -> Path:
    """Resolve an existing challenge path, or materialize from pasted prompt.

    Flexible inputs (no URL fetch — web links belong in paste text):

    - Existing **directory** as ``path`` **without** paste: use as challenge root;
      copy ``attachments`` into ``distfiles/``.
    - Existing **directory** + paste: materialize a cache workspace with
      ``challenge.txt`` from the paste (merged with any on-disk description) and
      copy the folder's files into ``distfiles/`` — so ``nc host port`` in the
      first prompt is not dropped.
    - Existing **file** as ``path``: materialize a cache challenge from paste
      (or a short name fallback) and copy the file + all attachments into
      ``distfiles/``.
    - Paste only (no path): materialize from prompt/description + attachments.
    - Missing path: ``FileNotFoundError: path not found: …``.
    """
    text = (prompt or description or "").strip()
    p = (path or "").strip() or None
    extra = [str(a) for a in (attachments or []) if str(a).strip()]

    if p:
        root = Path(p).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"path not found: {root}")
        assert_allowed_load_path(root)
        if root.is_dir():
            if text:
                # Do not mutate the user's folder; paste must reach /challenge.
                kids = _attachment_paths_from_challenge_dir(root)
                return materialize_challenge(
                    description=_merged_dir_paste_description(root, text),
                    name=name or root.name,
                    attachments=[*kids, *extra] or None,
                )
            if extra:
                dist = root / "distfiles"
                dist.mkdir(exist_ok=True)
                _copy_attachments(dist, extra)
            return root
        if root.is_file():
            # File-primary (or multi-file): materialize + attach this file first.
            all_attach = [str(root), *extra]
            desc = text or f"File challenge: {root.name}"
            return materialize_challenge(
                description=desc,
                name=name or root.stem,
                attachments=all_attach,
            )
        raise ValueError(f"not a file or directory: {root}")

    if text or extra:
        # Attachments-only (no paste) still gets a usable workspace.
        desc = text or (
            f"File challenge: {Path(extra[0]).name}" if extra else ""
        )
        if not desc:
            raise ValueError("path or prompt/description required")
        return materialize_challenge(
            description=desc,
            name=name,
            attachments=extra or None,
        )
    raise ValueError("path or prompt/description required")
