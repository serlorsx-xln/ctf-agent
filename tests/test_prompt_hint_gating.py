"""Prompt stays Veria-shaped: full tutoring list, pyghidra only when a binary exists."""

from __future__ import annotations

from backend.prompts import ChallengeMeta, build_prompt, has_binary_distfiles, parse_tag_labels

GHIDRA = "pyghidra"
VIEW_IMAGE = "view_image"
WEB = "fuzz params"
CRYPTO = "RsaCtfTool"
PWN = "stty raw"
ALL_HINTS = (GHIDRA, VIEW_IMAGE, WEB, CRYPTO, PWN)


def prompt(files: list[str], conn: str = "", desc: str = "a challenge") -> str:
    return build_prompt(
        ChallengeMeta(name="c", description=desc, connection_info=conn),
        files,
        "x86_64",
        True,
    )


def hints(files: list[str], conn: str = "", desc: str = "a challenge") -> set[str]:
    p = prompt(files, conn=conn, desc=desc)
    return {h for h in ALL_HINTS if h in p}


def test_binary_detection():
    assert has_binary_distfiles(["chall.elf"])
    assert has_binary_distfiles(["challenge_local"])
    assert has_binary_distfiles(["app.apk"])
    assert not has_binary_distfiles(["chall.py", "output.txt"])
    assert not has_binary_distfiles(["secret.jpg"])
    assert not has_binary_distfiles(["capture.pcapng"])


def test_tag_labels_are_display_only_except_rsa_tools():
    assert parse_tag_labels("Tags: crypto") == ["crypto"]
    assert parse_tag_labels("Category: Reverse\nTags: rev,pwn") == ["reverse", "rev", "pwn"]
    # Image/web/pwn tutoring stays; sage/RsaCtfTool only when crypto is signaled.
    a = prompt(["chall.py"], desc="Tags: crypto")
    b = prompt(["chall.py"], desc="Tags: rev,pwn")
    assert GHIDRA not in a and GHIDRA not in b
    assert VIEW_IMAGE in a and VIEW_IMAGE in b
    assert WEB in a and WEB in b
    assert CRYPTO in a and CRYPTO not in b
    assert PWN in a and PWN in b


def test_rsa_tools_absent_without_crypto_signal():
    assert CRYPTO not in prompt(["notes.txt"], desc="Look at the dump.")
    assert CRYPTO not in prompt(["pic.png"], desc="Tags: forensics")
    assert CRYPTO in prompt(["rsa.sage"], desc="factor me")
    assert CRYPTO in prompt(["out.txt"], desc="Tags: cryptography")


def test_script_only_challenge_skips_ghidra_but_keeps_veria_tutoring():
    """factor-wars shape: no binary → no pyghidra; Veria still listed image/web/pwn."""
    assert hints(["chall.py", "output.txt"], desc="Tags: crypto") == {
        VIEW_IMAGE,
        WEB,
        CRYPTO,
        PWN,
    }


def test_binary_challenge_gets_ghidra():
    assert GHIDRA in hints(["challenge_local"], desc="Tags: rev,pwn")


def test_header_surfaces_tags_when_present():
    p = prompt(["a.py"], desc="Factor Wars\n\nTags: crypto\n\nRecover the flag.")
    assert "**Tags**    : crypto" in p
    assert "**Category**: crypto" in p  # single tag doubles as category
    assert "## Challenge" in p
    assert "highest-value surfaces" in p
    assert "Flag-only" in p
    assert "decoy" in p.lower()
    assert "**Scope**" not in p
    assert "human confirms" in p.lower()


def test_no_scope_guard_language():
    p = prompt(["a.py"])
    assert "Docker networks" not in p
    assert "answer keys" not in p
    assert "scoring harness" not in p


def test_human_confirm_not_ctfd_verify():
    p = prompt(["a.py"])
    assert "Verify every candidate" not in p
    assert "human confirms" in p.lower()
