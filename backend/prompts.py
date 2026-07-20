"""System prompt builder + ChallengeMeta."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.tools.core import IMAGE_EXTS_FOR_VISION as IMAGE_EXTS


@dataclass
class ChallengeMeta:
    name: str = "Unknown"
    description: str = ""
    connection_info: str = ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> ChallengeMeta:
        """Compat alias — loads a challenge folder (yaml is not used)."""
        from backend.challenge import load_challenge

        p = Path(path)
        return load_challenge(p if p.is_dir() else p.parent)


def list_distfiles(challenge_dir: str) -> list[str]:
    from backend.challenge import list_attachment_names

    return list_attachment_names(challenge_dir)


def _rewrite_connection_info(conn: str) -> str:
    if not conn:
        return conn
    conn = re.sub(r"\blocalhost\b", "host.docker.internal", conn)
    conn = re.sub(r"\b127\.0\.0\.1\b", "host.docker.internal", conn)
    return conn


def build_prompt(
    meta: ChallengeMeta,
    distfile_names: list[str],
    container_arch: str = "unknown",
    has_named_tools: bool = True,
) -> str:
    """Thin, category-neutral prompt: pasted description + file list."""
    conn_info = _rewrite_connection_info((meta.connection_info or "").strip())

    lines: list[str] = [
        "You are an expert CTF solver. Recover the real flag.",
        "",
        f"**Challenge**: {meta.name}",
        f"**Arch**: {container_arch}",
        "",
        "## Description",
        meta.description or "_No description provided._",
        "",
    ]

    if conn_info:
        lines += [
            "## Endpoint mentioned in description",
            conn_info,
            "",
        ]

    if distfile_names:
        lines.append("## Attached files (`/challenge/distfiles/`)")
        for name in distfile_names:
            note = "  (image)" if Path(name).suffix.lower() in IMAGE_EXTS else ""
            lines.append(f"- `/challenge/distfiles/{name}`{note}")
        lines.append("")
    elif conn_info:
        lines += [
            "## Attached files",
            "_None — remote/web challenge. Work against the endpoint above._",
            "",
        ]

    submit = "call `submit_flag`" if has_named_tools else "run `submit_flag '<flag>'`"
    lines += [
        "## Notes",
        "- Work in `/challenge/workspace`. Local files (if any) are under `/challenge/distfiles`.",
        "- Start with `cat /challenge/TOOLS.txt` and use what is installed.",
        "- Packages are per interpreter (`python3` ≠ `sage`); follow TOOLS.txt.",
        "- Solve from local files and/or any live service. Do not search writeups.",
        "- Ignore decoys (`CTF{flag}`, `CTF{placeholder}`, `*fake_flag*`, `TRYHARDER`).",
        f"- When you have the real flag, {submit}. CORRECT ends the run.",
        "",
        "Use tools immediately.",
    ]
    return "\n".join(lines)
