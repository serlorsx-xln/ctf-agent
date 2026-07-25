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
    async def produce_writeup() -> str:
        await asyncio.sleep(60)
        return "late"

    from backend import writeup as writeup_mod

    old = writeup_mod.WRITEUP_TIMEOUT_S
    writeup_mod.WRITEUP_TIMEOUT_S = 0.05
    try:
        assert await capture_solver_writeup(SimpleNamespace(produce_writeup=produce_writeup)) == ""
    finally:
        writeup_mod.WRITEUP_TIMEOUT_S = old


def test_writeup_prompt_asks_for_structured_prose():
    assert "Do not call any tools" in WRITEUP_PROMPT
    assert "## Challenge" in WRITEUP_PROMPT
    assert "## Key insight" in WRITEUP_PROMPT
    assert "## How" in WRITEUP_PROMPT
    assert "## Flag" not in WRITEUP_PROMPT


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

## Flag
ARCHA{test}
"""
    out = normalize_writeup_text(raw)
    assert "Challenge" in out
    assert "Key insight" in out
    assert "ARCHA{test}" not in out
    assert "```" not in out


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
    from backend.writeup import is_usable_narrative

    assert not is_usable_narrative("The flag was accepted as CORRECT. FLAG: flag{x}")
    assert is_usable_narrative(
        "Challenge\nPCAP with DNS + TCP.\n\n"
        "Key insight\nTXT holds the XOR key.\n\n"
        "How\n1. dig TXT\n2. decrypt the TCP stream"
    )


def test_notes_expand_jammed_prose_before_steps():
    from backend.action_log import notes_from_actions

    note = notes_from_actions(
        ["bash: tshark -r cap.pcapng", "submit_flag: flag{x}"],
        prose=(
            "**Solution summary** 1. DNS key mango from TXT "
            "2. TCP chat recovers CIPHER_PART 3. XOR decrypts the flag body"
        ),
    )
    assert "Solution summary" in note or "1. DNS key" in note
    assert "bash:" not in note
    assert "submit_flag:" not in note


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

