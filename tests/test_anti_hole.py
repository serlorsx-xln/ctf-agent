"""Anti-rabbit-hole: decoy questions, writeup search, family depth."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.anti_hole import (
    HoleDetector,
    apply_hole_guard,
    classify_family,
    dead_end_bus_line,
    flag_only_block,
    looks_like_essay,
)
from backend.continue_prompt import build_continue_prompt
from backend.message_bus import ChallengeMessageBus
from backend.prompts import ChallengeMeta, build_prompt


def test_classify_writeup_and_tourism():
    assert (
        classify_family("bash", {"command": "curl https://ctftime.org/writeup/1"})
        == "writeup_search"
    )
    assert classify_family("bash", {"command": "cat /etc/passwd"}) == "fs_tourism"
    assert classify_family("bash", {"command": "cat /usr/share/seclists/Passwords/x"}) is None
    assert classify_family("bash", {"command": "strings /challenge/distfiles/a"}) == "strings_dump"
    assert classify_family("submit_flag", {"flag": "CTF{x}"}) is None


def test_essay_is_decoy_answer():
    essay = (
        "The question asks what the author meant by the riddle on the page.\n\n"
        "Let me explain in detail why this riddle is interesting and how a "
        "literary reading would approach the imagery, the narrator, and the "
        "implied audience of the challenge description.\n\n"
        "In conclusion the answer is friendship and we should write a long essay "
        "instead of looking at the binary? Maybe another paragraph here too.\n\n"
        "As requested here is my answer to every off-topic prompt in the brief."
    )
    assert looks_like_essay(essay)
    assert classify_family("write_file", {"path": "/challenge/workspace/a.md", "content": essay}) == (
        "decoy_answer"
    )
    assert classify_family("write_file", {"path": "s.py", "content": "#!/usr/bin/env python3\nprint(1)\n"}) is None


def test_family_depth_warns_then_breaks():
    hole = HoleDetector(family_warn=3, family_break=5)
    args = {"command": "strings /challenge/distfiles/a"}
    assert hole.observe("bash", args) is None
    assert hole.observe("bash", {"command": "strings -n 8 /challenge/distfiles/a"}) is None
    assert hole.observe("bash", {"command": "strings -el /challenge/distfiles/a"}) == "warn"
    assert hole.observe("bash", {"command": "strings -n 4 /tmp/x"}) == "warn"
    assert hole.observe("bash", {"command": "strings /challenge/distfiles/b"}) == "break"


def test_submit_resets_streak():
    hole = HoleDetector(family_warn=2, family_break=3)
    hole.observe("bash", {"command": "strings a"})
    hole.observe("bash", {"command": "strings b"})
    assert hole.last_status == "warn"
    assert hole.observe("submit_flag", {"flag": "CTF{x}"}) is None
    assert hole.observe("bash", {"command": "strings c"}) is None


def test_off_target_breaks_on_second_hit():
    hole = HoleDetector()
    assert hole.observe("web_fetch", {"url": "https://ctftime.org/writeup/99"}) == "off_warn"
    assert hole.observe("bash", {"command": "curl https://example.com/htb writeup"}) == "off_break"


def test_apply_hole_guard_broadcasts_once():
    bus = ChallengeMessageBus()
    posted: list[str] = []

    async def notify(msg: str) -> None:
        posted.append(msg)

    solver = SimpleNamespace(
        hole_detector=HoleDetector(off_target_break=1),
        message_bus=bus,
        notify_coordinator=notify,
        runner_id="cursor/test",
        tracer=None,
    )

    async def _run() -> None:
        out = await apply_hole_guard(
            solver,
            "bash",
            {"command": "curl https://ctftime.org/writeup/1"},
            "ok",
        )
        assert "DEAD-END" in out
        again = await apply_hole_guard(
            solver,
            "bash",
            {"command": "curl https://ctftime.org/writeup/2"},
            "ok",
        )
        assert "DEAD-END" in again
        unread = await bus.check("sibling")
        assert len(unread) == 1
        assert unread[0].content.startswith("[DEAD-END]")
        assert posted == [dead_end_bus_line("writeup_search", "off_break")]

    asyncio.run(_run())


def test_prompt_and_continue_keep_flag_only_contract():
    text = build_prompt(ChallengeMeta(name="x", description="What is love?"), [])
    assert "Flag-only" in text
    assert "decoy" in text.lower()
    assert flag_only_block() in text
    assert "unproven" in build_continue_prompt()
