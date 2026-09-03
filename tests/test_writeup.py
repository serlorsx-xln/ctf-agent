"""Tests for post-solve narrative writeup capture."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.writeup import WRITEUP_PROMPT, capture_solver_writeup


@pytest.mark.asyncio
async def test_capture_uses_produce_writeup():
    async def produce_writeup() -> str:
        return "  The binary checked a XOR'd password.  "

    solver = SimpleNamespace(produce_writeup=produce_writeup)
    assert await capture_solver_writeup(solver) == "The binary checked a XOR'd password."


@pytest.mark.asyncio
async def test_capture_returns_empty_without_method():
    assert await capture_solver_writeup(SimpleNamespace()) == ""


@pytest.mark.asyncio
async def test_capture_swallows_errors():
    async def produce_writeup() -> str:
        raise RuntimeError("boom")

    assert await capture_solver_writeup(SimpleNamespace(produce_writeup=produce_writeup)) == ""


@pytest.mark.asyncio
async def test_capture_times_out():
    calls = 0

    async def produce_writeup(prompt: str | None = None) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(60)
        return "late"

    from backend import writeup as writeup_mod

    old = writeup_mod.WRITEUP_TIMEOUT_S
    writeup_mod.WRITEUP_TIMEOUT_S = 0.05
    try:
        assert await capture_solver_writeup(SimpleNamespace(produce_writeup=produce_writeup)) == ""
        # Timeout must not burn a second full wait (retry).
        assert calls == 1
    finally:
        writeup_mod.WRITEUP_TIMEOUT_S = old


def test_coalesce_writeup_falls_back_to_run_result():
    from backend.writeup import coalesce_writeup_output

    result = SimpleNamespace(result="## Challenge\nShop PIN in libapp.so.")
    assert "Shop PIN" in coalesce_writeup_output([], result)
    assert coalesce_writeup_output(["streamed body here"], result) == "streamed body here"


def test_writeup_prompt_asks_for_structured_prose():
    from backend.writeup import WRITEUP_RETRY_PROMPT

    assert "Do not call any tools" in WRITEUP_PROMPT
    assert "## Challenge" in WRITEUP_PROMPT
    assert "## Key insight" in WRITEUP_PROMPT
    assert "## How" in WRITEUP_PROMPT
    assert "## What I tried" in WRITEUP_PROMPT
    assert "## Why it worked" in WRITEUP_PROMPT
    assert "## Flag" not in WRITEUP_PROMPT
    assert "15–40 sentences" in WRITEUP_PROMPT
    assert "## Challenge" in WRITEUP_RETRY_PROMPT
    assert "too short" in WRITEUP_RETRY_PROMPT


def test_normalize_writeup_keeps_sections():
    from backend.writeup import normalize_writeup_text

    raw = """## Challenge
Flutter APK with three flags.

## Key insight
Secret shop PIN unlocks the API.

## How
1. strings on libapp.so
2. blutter dump shop logic
3. call the buy endpoint

## What I tried
jadx on the wrapper APK showed only Flutter glue.

## Why it worked
The PIN is checked client-side before the buy call.

## Flag
ARCHA{test}
"""
    out = normalize_writeup_text(raw)
    assert "Challenge" in out
    assert "Key insight" in out
    assert "What I tried" in out
    assert "Why it worked" in out
    assert "ARCHA{test}" not in out
    assert "```" not in out


def test_is_flag_only_line_keeps_bare_flag_brace_tokens():
    from backend.writeup import _is_flag_only_line, normalize_writeup_text

    assert _is_flag_only_line("flag{abc123xxxx}") is True
    assert _is_flag_only_line("FLAG{abc123xxxx}") is True
    assert _is_flag_only_line("CTF{abc123xxxx}") is True
    assert _is_flag_only_line("FLAG: flag{abc123xxxx}") is True
    assert _is_flag_only_line("How we found it") is False
    assert "flag{abc123xxxx}" not in normalize_writeup_text("flag{abc123xxxx}\n\nHow\n1. x")


def test_expand_splits_hash_solution_summary_and_cipher_part_slash():
    from backend.writeup import clean_how_lines, expand_summary_line, is_usable_narrative

    raw = (
        "The flag was accepted. Cogitated **FLAG: flag{50fba860c6c53436cbaffe8391619e67}** "
        "## Solution Summary 1. **DNS exfiltration** — TXT holds XOR key mango. "
        "2. **TCP chat** — CIPHER_PART_1/2. 3. **ICMP \"noise\"** — decoy. "
        "Decryption: `XOR(key=mango)` → flag{50fba860c6c53436cbaffe8391619e67}"
    )
    pieces = expand_summary_line(raw)
    assert "Solution Summary" in pieces
    assert any(p.startswith("1. DNS") for p in pieces)
    assert any(p.startswith("2. TCP") and "CIPHER_PART_1/2." in p for p in pieces)
    assert any(p.startswith("3. ICMP") for p in pieces)
    assert any(p.lower().startswith("decryption:") for p in pieces)
    assert not any(p == "#" for p in pieces)

    cleaned = clean_how_lines(raw)
    assert any(p.startswith("1. DNS") for p in cleaned)
    assert any(p.startswith("2. TCP") for p in cleaned)
    assert not any("The flag was accepted" in p for p in cleaned)
    assert is_usable_narrative(raw)


def test_expand_and_normalize_split_jammed_numbered_list():
    from backend.writeup import expand_summary_line, normalize_writeup_text

    pieces = expand_summary_line(
        "**Solution summary** 1. DNS TXT for XOR key 2. TCP port 4444 chat 3. ICMP noise decoy"
    )
    assert pieces[0] == "Solution summary"
    assert pieces[1].startswith("1. DNS")
    assert pieces[2].startswith("2. TCP")
    assert pieces[3].startswith("3. ICMP")

    out = normalize_writeup_text(
        "**Solution summary** 1. DNS TXT for XOR key 2. TCP port 4444 chat"
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "Solution summary"
    assert lines[1].startswith("1. ")
    assert lines[2].startswith("2. ")


def test_expand_splits_solution_summary_glued_after_flag_prose():
    from backend.writeup import expand_summary_line

    raw = (
        "The flag was accepted as CORRECT. FLAG: flag{50fba} "
        "**Solution summary:** 1. **DNS TXT record** — mango "
        "2. **TCP stream** — CIPHER_PART 3. **XOR decryption** — done"
    )
    pieces = expand_summary_line(raw)
    assert any(p.lower().startswith("solution summary") for p in pieces)
    assert any(p.startswith("1. DNS") for p in pieces)
    assert any(p.startswith("2. TCP") for p in pieces)
    assert any(p.startswith("3. XOR") for p in pieces)
    assert not any("**" in p for p in pieces)


def test_usable_narrative_rejects_accept_spam():
    from backend.writeup import is_detailed_writeup, is_usable_narrative

    assert not is_usable_narrative("The flag was accepted as CORRECT. FLAG: flag{x}")
    assert not is_usable_narrative("redo it to match the ARM register width.")
    structured = (
        "Challenge\nPCAP with DNS + TCP.\n\n"
        "Key insight\nTXT holds the XOR key.\n\n"
        "How\n1. dig TXT\n2. decrypt the TCP stream"
    )
    assert is_usable_narrative(structured)
    assert is_detailed_writeup(structured)
    teaser = (
        "The decoded plaintext is a passage about frequency analysis ending with "
        "instructions: Alan Turing once said machines take me by surprise with "
        "great frequency — take each word in the quote, join with underscores, "
        "put in flag format."
    )
    assert is_usable_narrative(teaser)
    assert not is_detailed_writeup(teaser)


def test_notes_expand_jammed_prose_before_steps():
    from backend.action_log import notes_from_prose

    note = notes_from_prose(
        "**Solution summary** 1. DNS key mango from TXT "
        "2. TCP chat recovers CIPHER_PART 3. XOR decrypts the flag body"
    )
    assert "Solution summary" in note or "1. DNS key" in note
    assert "bash:" not in note


def test_normalize_drops_single_line_error_envelope():
    from backend.writeup import normalize_writeup_text

    assert normalize_writeup_text("Error: turn failed") == ""
    assert normalize_writeup_text("Turn failed: boom") == ""


def test_normalize_keeps_prose_starting_with_error():
    from backend.writeup import normalize_writeup_text

    raw = "Error: the app showed a toast.\n\n## How\n1. open the toast\n2. read the token"
    out = normalize_writeup_text(raw)
    assert "Error: the app showed a toast." in out
    assert "open the toast" in out


@pytest.mark.asyncio
async def test_capture_normalizes_output():
    async def produce_writeup() -> str:
        return "```markdown\n## Challenge\nHello world.\n```"

    solver = SimpleNamespace(produce_writeup=produce_writeup)
    text = await capture_solver_writeup(solver)
    assert text.startswith("Challenge")
    assert "Hello world." in text
    assert "```" not in text


@pytest.mark.asyncio
async def test_capture_retries_thin_writeup():
    from backend.writeup import WRITEUP_RETRY_PROMPT

    calls: list[str | None] = []

    async def produce_writeup(prompt: str | None = None) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return (
                "The decoded plaintext is a passage about frequency analysis "
                "ending with instructions about a Turing quote."
            )
        return (
            "## Challenge\nCiphertext in the challenge file.\n\n"
            "## Key insight\nLetter frequencies match English.\n\n"
            "## How\n1. count letters\n2. map e to the top letter\n"
            "3. decode the quote\n4. join words with underscores\n\n"
            "## What I tried\nROT13 failed.\n\n"
            "## Why it worked\nThe quote was the instruction."
        )

    solver = SimpleNamespace(produce_writeup=produce_writeup)
    text = await capture_solver_writeup(solver)
    assert len(calls) == 2
    assert calls[1] == WRITEUP_RETRY_PROMPT
    assert "Key insight" in text
    assert "count letters" in text


@pytest.mark.asyncio
async def test_capture_skips_retry_when_detailed():
    calls = 0

    async def produce_writeup(prompt: str | None = None) -> str:
        nonlocal calls
        calls += 1
        return (
            "## Challenge\nShop PIN in libapp.so.\n\n"
            "## Key insight\nClient-side check before the buy call.\n\n"
            "## How\n1. strings libapp.so\n2. call the buy endpoint"
        )

    solver = SimpleNamespace(produce_writeup=produce_writeup)
    text = await capture_solver_writeup(solver)
    assert calls == 1
    assert "Shop PIN" in text


def test_collapse_streamed_word_per_line_writeup():
    from backend.writeup import (
        clean_how_lines,
        collapse_prose_fragments,
        is_fragmented_prose,
        is_usable_narrative,
        join_streamed_text_parts,
    )

    streamed = "\n".join(
        [
            "##",
            "Challenge",
            "A",
            "Windows",
            "x",
            "64",
            "reverse",
            "challenge",
            "shipped",
            "as",
            "medium_rare.dll",
        ]
    )
    assert is_fragmented_prose(streamed)
    collapsed = collapse_prose_fragments(streamed)
    flat = collapsed.replace("\n", " ")
    assert "Windows" in flat and "reverse challenge" in flat
    assert "medium_rare.dll" in flat
    assert is_usable_narrative(collapsed)
    cleaned = clean_how_lines(collapsed)
    assert cleaned[0] == "Challenge"
    assert any("medium_rare.dll" in ln for ln in cleaned)

    parts = ["##", "Challenge", "A", "Windows", "x64", "DLL", "with", "encrypted", "blob"]
    joined = join_streamed_text_parts(parts)
    assert "Windows x64 DLL" in joined
    assert not is_fragmented_prose(joined)


def test_join_streamed_parts_keeps_thai_and_identifiers_intact():
    from backend.writeup import join_streamed_text_parts

    thai = join_streamed_text_parts(["รับ", "ทราบ", " — ", "ทดสอบ", "ผ่าน", "แล้ว"])
    assert "รับทราบ" in thai
    assert "รับ ท ราบ" not in thai
    assert "ทดสอบผ่านแล้ว" in thai or "ทดสอบ ผ่าน แล้ว" in thai

    ident = join_streamed_text_parts(["submit", "_", "flag", " ", "รับ", "ครบ"])
    assert "submit_flag" in ident
    assert "submit _ flag" not in ident
    assert "รับครบ" in ident


def test_fragmented_accept_spam_still_rejected():
    from backend.writeup import is_usable_narrative

    spam = "\n".join(["The", "flag", "was", "accepted", "as", "CORRECT"])
    assert not is_usable_narrative(spam)


def test_expand_summary_line_shared_fixtures():
    """Parity with TUI ``expandSummaryLine`` — same cases in fixtures/."""
    import json
    from pathlib import Path

    from backend.writeup import expand_summary_line

    path = Path(__file__).parent / "fixtures" / "summary_expand_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        pieces = expand_summary_line(case["input"])
        if "expect_exact" in case:
            assert pieces == case["expect_exact"], case["id"]
        for needle in case.get("expect_contains", []):
            assert any(needle in p for p in pieces), (case["id"], needle, pieces)

