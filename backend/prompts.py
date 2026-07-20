"""System prompt builder + ChallengeMeta."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.flags import normalize_flags_required
from backend.tools.core import IMAGE_EXTS_FOR_VISION as IMAGE_EXTS


@dataclass
class ChallengeMeta:
    name: str = "Unknown"
    description: str = ""
    connection_info: str = ""
    # Distinct flags needed before CORRECT (default 1). Set via flags_required: N in challenge.txt.
    flags_required: int = 1


def list_distfiles(challenge_dir: str) -> list[str]:
    from backend.challenge import list_attachment_names

    return list_attachment_names(challenge_dir)


def _rewrite_connection_info(conn: str) -> str:
    if not conn:
        return conn
    conn = re.sub(r"\blocalhost\b", "host.docker.internal", conn)
    conn = re.sub(r"\b127\.0\.0\.1\b", "host.docker.internal", conn)
    return conn


def _has_non_image_distfiles(names: list[str]) -> bool:
    return any(Path(n).suffix.lower() not in IMAGE_EXTS for n in names)


def build_prompt(
    meta: ChallengeMeta,
    distfile_names: list[str],
    container_arch: str = "unknown",
    has_named_tools: bool = True,
) -> str:
    """Category-neutral prompt: description + files + operational hints (Veria-style)."""
    conn_info = _rewrite_connection_info((meta.connection_info or "").strip())

    lines: list[str] = [
        "You are an expert CTF solver. Recover the real flag.",
        "",
    ]

    if conn_info:
        lines += [
            "> **FIRST ACTION REQUIRED**: Your very first tool call MUST connect to the service.",
            f"> Run: `{conn_info}` (use a heredoc or pwntools script as shown below).",
            "> Do NOT explore the sandbox filesystem first. The flag is on the service, not in the container.",
            "",
        ]

    lines += [
        f"**Challenge**: {meta.name}",
        f"**Arch**: {container_arch}",
        "",
        "## Description",
        meta.description or "_No description provided._",
        "",
    ]

    if conn_info:
        if re.match(r"^https?://", conn_info):
            hint = (
                "This is a **web service**. Use `bash` with `curl`/`python3 requests`"
                + (", or use `web_fetch`" if has_named_tools else "")
                + "."
            )
        elif conn_info.startswith("nc "):
            hint = (
                "This is a **TCP service**. Each `bash` call is a fresh process — "
                "use a heredoc to send multiple lines in one shot:\n"
                "```\n"
                f"{conn_info} <<'EOF'\ncommand1\ncommand2\nEOF\n"
                "```\n"
                "Or write a Python `socket` / `pwntools` script for stateful interaction."
            )
        else:
            hint = "Connect using the details above."
        lines += ["## Service Connection", "```", conn_info, "```", hint, ""]

    if distfile_names:
        lines.append("## Attached files (`/challenge/distfiles/`)")
        for name in distfile_names:
            ext = Path(name).suffix.lower()
            is_img = ext in IMAGE_EXTS
            if is_img and has_named_tools:
                suffix = "  <- **IMAGE: call `view_image` immediately** (fix magic bytes first if corrupt)"
            elif is_img:
                suffix = "  <- **IMAGE: use `exiftool`, `steghide`, `zsteg`, `strings` via bash**"
            else:
                suffix = ""
            lines.append(f"- `/challenge/distfiles/{name}`{suffix}")
        lines.append("")
    elif conn_info:
        lines += [
            "## Attached files",
            "_None — remote/web challenge. Work against the endpoint above._",
            "",
        ]

    if distfile_names and _has_non_image_distfiles(distfile_names):
        # Same push as upstream Veria — packs install on first use if missing.
        lines += [
            "## Binary Analysis",
            "**pyghidra** is available for decompilation. Use it via bash:",
            "```python",
            "import pyghidra",
            "with pyghidra.open_program('/challenge/distfiles/binary') as flat_api:",
            "    listing = flat_api.currentProgram.getListing()",
            "    # Iterate functions, decompile, etc.",
            "```",
            "Also available: radare2 (`r2`), gdb, angr, capstone.",
            "",
        ]

    if has_named_tools:
        image_hint = "**Images: call `view_image` FIRST, before any other analysis.**"
        web_hint = (
            "Web: fuzz params, check JS source, cookies, robots.txt. "
            "For XSS/SSRF: use `webhook_create`."
        )
        submit = "call `submit_flag`"
        submit_hint = "**Verify every candidate with `submit_flag`** before reporting."
    else:
        image_hint = "**Images: use `exiftool`, `steghide`, `zsteg`, `strings`, `xxd` via bash.**"
        web_hint = (
            "Web: fuzz params, check JS source, cookies, robots.txt. "
            "For XSS/SSRF: use `curl` to webhook.site."
        )
        submit = "run `submit_flag '<flag>'`"
        submit_hint = (
            "**Verify every candidate with `submit_flag '<flag>'`** (bash command) "
            "before reporting."
        )

    req = normalize_flags_required(getattr(meta, "flags_required", 1))
    if req <= 1:
        flag_note = f"- When you have a real flag, {submit}. CORRECT ends the run."
    else:
        flag_note = (
            f"- This challenge needs {req} distinct flags. When you find each one, {submit}. "
            "ACCEPTED (n/m) means continue; only CORRECT (all accepted) ends the run."
        )

    lines += [
        "## Notes",
        "- Work in `/challenge/workspace`. Local files (if any) are under `/challenge/distfiles`.",
        "- Start with `cat /challenge/TOOLS.txt` and use what is installed.",
        "- Packages are per interpreter (`python3` ≠ `sage`); follow TOOLS.txt.",
        "- Solve from local files and/or any live service. Do not search writeups.",
        "- Ignore decoys (`CTF{flag}`, `CTF{placeholder}`, `*fake_flag*`, `TRYHARDER`).",
        flag_note,
        "- Before finishing a turn without CORRECT: briefly state flags found (if any), "
        "confidence, and the top blocker — even if submit failed.",
        "",
        "## Instructions",
        "**Use tools immediately. Do not describe — execute.**",
        "",
        "1. " + ("Connect to the service now." if conn_info else "Inspect distfiles now."),
        "2. Keep using tools until you have the flag.",
        "3. **Be creative and thorough** — try the obvious path, then explore further:",
        "   - Hidden files, env vars, backup files, HTTP headers, error messages, timing, encoding tricks.",
        f"   - {image_hint}",
        f"   - {web_hint}",
        (
            "   - Crypto: identify algorithm, weak keys, nonce reuse, padding oracles. "
            "For RSA: use `RsaCtfTool`, sage ECM, or `cado-nfs`."
        ),
        "   - Pwn: `stty raw -echo` before launching vulnerable binaries over nc.",
        "4. **Ignore placeholder flags** — `CTF{flag}`, `CTF{placeholder}` are not real flags.",
        f"5. {submit_hint}",
        "6. Once CORRECT: output `FLAG: <value>` on its own line.",
        "7. Do not guess. Do not ask. Cover maximum surface area.",
    ]
    return "\n".join(lines)
