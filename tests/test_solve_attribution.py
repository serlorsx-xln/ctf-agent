"""A finished swarm must say which agent solved it, and how."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.flags import accept_flag
from backend.log_context import AgentTagFilter, log_agent
from backend.prompts import ChallengeMeta


class _FakeSettings:
    auto_confirm_flags = True
    eval_out = ""


def _swarm(specs: list[str]) -> ChallengeSwarm:
    from backend.eval_run import EvalRunState
    from backend.message_bus import ChallengeMessageBus

    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = "/tmp"
    swarm.meta = ChallengeMeta(name="t", description="", flags_required=1)
    swarm.cost_tracker = None  # type: ignore[assignment]
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = specs
    swarm.coordinator_inbox = None
    swarm.cancel_event = asyncio.Event()
    swarm.solvers = {}
    swarm.findings = {}
    swarm.winner = None
    swarm.winner_runner_id = ""
    swarm.flag_credits = {}
    swarm.flag_notes = {}
    swarm._steps_by_runner = {}
    swarm.confirmed_flag = None
    swarm.confirmed_flags = []
    swarm._flag_lock = asyncio.Lock()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    swarm.message_bus = ChallengeMessageBus()
    swarm._eval = EvalRunState()
    swarm._infra_recoveries_total = 0
    swarm._last_model_spec = ""
    swarm._last_preflight_ms = 0.0
    swarm._last_steps = 0
    swarm._last_status = ""
    swarm._last_flag = None
    swarm._how_emitted = False
    swarm._writeup_attempted = False
    return swarm


def test_accept_message_names_the_submitting_runner():
    msg, done = accept_flag(
        "CTF{aaaaaaaaaaaa}",
        required=1,
        human_confirmed=True,
        by="cursor/grok-4.5",
    )
    assert done
    assert "via cursor/grok-4.5" in msg


def test_accept_message_without_author_is_unchanged():
    msg, done = accept_flag("CTF{aaaaaaaaaaaa}", required=1, human_confirmed=True)
    assert done
    assert " via " not in msg


def test_writeup_credits_the_winner_with_the_box_label():
    swarm = _swarm(["cursor/grok-4.5", "cursor/composer-2.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    # Usable narrative — How shows prose only (no Steps / flag spam).
    swarm.flag_notes = {
        "cursor/grok-4.5": (
            "Challenge\nFlutter APK hid the key in libapp.so.\n\n"
            "Key insight\nXOR with the APK signature recovers the blob.\n\n"
            "How\n1. strings on libapp.so\n2. XOR the blob with the signature"
        )
    }
    swarm._steps_by_runner = {"cursor/grok-4.5": 42}

    lines = swarm.solve_writeup()

    assert lines[0] == "Solved by grok-4.5"
    assert "How:" in lines
    assert any("libapp.so" in ln for ln in lines)
    assert not any(ln.strip().startswith("Flag:") for ln in lines)
    assert not any("submit_flag:" in ln for ln in lines)
    assert not any("42 steps" in ln for ln in lines)


def test_writeup_shows_no_writeup_when_only_commands_were_recorded():
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    swarm.flag_notes = {
        "cursor/grok-4.5": (
            "The flag was accepted as CORRECT. FLAG: CTF{aaaaaaaaaaaa}\n"
            "Steps:\n1. bash: tshark -r cap.pcapng\n2. submit_flag: CTF{aaaaaaaaaaaa}"
        )
    }

    lines = swarm.solve_writeup()

    assert "How:" in lines
    assert "  (no writeup recorded)" in lines
    assert not any("bash:" in ln for ln in lines)


def test_writeup_prefers_late_findings_over_command_trail():
    """AI Solution Summary often arrives after submit_flag stored the trail."""
    swarm = _swarm(["cursor/composer-2.5"])
    swarm.confirmed_flags = ["flag{50fba}"]
    swarm.flag_credits = {"flag{50fba}": "cursor/composer-2.5"}
    swarm.flag_notes = {
        "cursor/composer-2.5": "\n".join(
            [
                "1. bash: cat /challenge/TOOLS.txt",
                "2. bash: tshark -r /challenge/distfiles/challenge.pcapng",
            ]
        )
    }

    class _Solver:
        _findings = (
            "**FLAG: flag{50fba}** ## Solution Summary "
            "1. **DNS exfiltration** — mango "
            "2. **TCP chat** — CIPHER_PART_1/2. "
            "3. **ICMP noise** — decoy"
        )

    swarm.solvers = {"cursor/composer-2.5": _Solver()}  # type: ignore[assignment]

    # Simulate the FLAG_FOUND late-findings promotion used in _run_solver.
    from backend.writeup import clean_how_lines, is_usable_narrative

    existing = swarm.flag_notes["cursor/composer-2.5"]
    late = swarm.solvers["cursor/composer-2.5"]._findings
    assert not is_usable_narrative(existing)
    assert is_usable_narrative(late)
    swarm.flag_notes["cursor/composer-2.5"] = "\n".join(clean_how_lines(late))

    lines = swarm.solve_writeup()
    joined = "\n".join(lines)
    assert "DNS exfiltration" in joined
    assert "bash: tshark" not in joined
    assert "How:" in lines


def test_writeup_attributes_each_flag_when_two_agents_contributed():
    swarm = _swarm(["cursor/grok-4.5", "cursor/composer-2.5"])
    swarm.confirmed_flags = ["CTF{user_aaaa}", "CTF{root_bbbb}"]
    swarm.flag_credits = {
        "CTF{user_aaaa}": "cursor/grok-4.5",
        "CTF{root_bbbb}": "cursor/composer-2.5",
    }
    swarm.flag_notes = {
        "cursor/composer-2.5": (
            "Challenge\nLocal privilege escalation on the box.\n\n"
            "Key insight\nThe setuid helper leaked the root flag.\n\n"
            "How\n1. run the helper\n2. read /root/flag"
        )
    }

    lines = swarm.solve_writeup()

    assert lines[0] == "Solved by grok-4.5, composer-2.5"
    assert "How (composer-2.5):" in lines
    assert not any(ln.strip().startswith("Flag:") for ln in lines)


def test_writeup_says_when_there_is_no_narrative():
    swarm = _swarm(["cursor/default"])
    swarm.confirmed_flags = ["flag{x}"]
    swarm.flag_credits = {"flag{x}": "cursor/default"}
    lines = swarm.solve_writeup()
    assert "  (no writeup recorded)" in lines


def test_writeup_keeps_duplicate_runner_suffix():
    swarm = _swarm(["cursor/grok-4.5", "cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5#2"}

    assert swarm.solve_writeup()[0] == "Solved by grok-4.5#2"


def test_writeup_is_empty_without_an_accepted_flag():
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.findings = {"cursor/grok-4.5": "tried a lot"}
    assert swarm.solve_writeup() == []


def test_interim_how_prints_notes_immediately(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("backend.agents.live_log.emit_line", lines.append)
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    swarm.flag_notes = {
        "cursor/grok-4.5": (
            "Challenge\nFlutter APK hid the key in libapp.so.\n\n"
            "Key insight\nXOR with the APK signature recovers the blob.\n\n"
            "How\n1. strings on libapp.so\n2. XOR the blob with the signature"
        )
    }
    swarm._emit_how_recap(interim=True)
    joined = "\n".join(lines)
    assert "[artemis] summary How:" in joined
    assert "libapp.so" in joined
    assert swarm._how_emitted is True


def test_interim_how_prints_pending_without_notes(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("backend.agents.live_log.emit_line", lines.append)
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    swarm._emit_how_recap(interim=True)
    joined = "\n".join(lines)
    assert "[artemis] summary How:" in joined
    assert "Writing recap from the winning solver" in joined
    assert swarm._how_emitted is False


@pytest.mark.asyncio
async def test_writeup_still_emits_when_flag_found_path_is_skipped(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("backend.agents.live_log.emit_line", lines.append)

    async def produce_writeup() -> str:
        return (
            "Challenge\nShop PIN in the Flutter binary.\n\n"
            "Key insight\nClient-side check before the buy call.\n\n"
            "How\n1. strings libapp.so\n2. call the buy endpoint"
        )

    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.confirmed_flag = "CTF{aaaaaaaaaaaa}"
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    swarm.winner_runner_id = "cursor/grok-4.5"
    solver = SimpleNamespace(produce_writeup=produce_writeup, _findings="")
    await swarm._capture_and_emit_writeup(solver, "cursor/grok-4.5")
    joined = "\n".join(lines)
    assert "Shop PIN" in joined
    assert swarm._how_emitted is True
    assert swarm._writeup_attempted is True


def test_solved_by_falls_back_to_the_winner_runner():
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.winner_runner_id = "cursor/grok-4.5"
    assert swarm.solved_by() == ["cursor/grok-4.5"]


def _outcome_text(swarm, result) -> tuple[str, str]:
    """Rendered console text and the captured ``[artemis] summary`` channel."""
    import io
    from contextlib import redirect_stdout

    from rich.console import Console

    from backend.cli import print_swarm_outcome

    rich_buf = io.StringIO()
    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        print_swarm_outcome(swarm, result, out=Console(file=rich_buf, width=80))
    return rich_buf.getvalue(), stdout_buf.getvalue()


def test_flag_found_line_keeps_the_flag_on_one_line():
    from backend.solver_base import FLAG_FOUND, SolverResult

    flag = "flag{" + "a" * 64 + "}"
    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = [flag]
    swarm.flag_credits = {flag: "cursor/grok-4.5"}
    result = SolverResult(
        flag=flag, status=FLAG_FOUND, findings_summary="", step_count=1, cost_usd=None, log_path=""
    )

    rendered, _ = _outcome_text(swarm, result)

    headline = next(ln for ln in rendered.splitlines() if ln.startswith("FLAG FOUND:"))
    assert flag in headline
    assert "solved by grok-4.5" in headline


def test_flag_found_falls_back_to_accepted_flags_when_result_has_none():
    from backend.solver_base import FLAG_FOUND, SolverResult

    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    result = SolverResult(
        flag=None, status=FLAG_FOUND, findings_summary="", step_count=1, cost_usd=None, log_path=""
    )

    rendered, _ = _outcome_text(swarm, result)

    assert "FLAG FOUND: CTF{aaaaaaaaaaaa}" in rendered


def test_writeup_reaches_the_tui_on_its_own_channel():
    from backend.solver_base import FLAG_FOUND, SolverResult

    swarm = _swarm(["cursor/grok-4.5"])
    swarm.confirmed_flags = ["CTF{aaaaaaaaaaaa}"]
    swarm.flag_credits = {"CTF{aaaaaaaaaaaa}": "cursor/grok-4.5"}
    swarm.flag_notes = {
        "cursor/grok-4.5": (
            "Challenge\nHidden XOR key in the APK.\n\n"
            "Key insight\nThe signature bytes xor the blob.\n\n"
            "How\n1. dump libapp.so\n2. xor with the APK signature"
        )
    }
    result = SolverResult(
        flag="CTF{aaaaaaaaaaaa}",
        status=FLAG_FOUND,
        findings_summary="",
        step_count=3,
        cost_usd=None,
        log_path="",
    )

    _, channel = _outcome_text(swarm, result)

    assert "[artemis] summary Solved by grok-4.5" in channel
    assert "[artemis] summary How:" in channel
    assert "xor with the APK signature" in channel
    assert "Flag:" not in channel


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, __file__, 1, msg, None, None)


def test_log_filter_tags_untagged_sandbox_lines():
    filt = AgentTagFilter()
    with log_agent("chal/cursor/grok-4.5"):
        rec = _record("Starting Docker sandbox")
        filt.filter(rec)
    assert str(rec.msg) == "[chal/cursor/grok-4.5] Starting Docker sandbox"


def test_log_filter_leaves_self_tagged_lines_alone():
    filt = AgentTagFilter()
    with log_agent("chal/cursor/grok-4.5"):
        rec = _record("[chal] L0 image=x")
        filt.filter(rec)
    assert str(rec.msg) == "[chal] L0 image=x"


def test_log_filter_is_a_noop_outside_a_solver():
    rec = _record("Starting Docker sandbox")
    AgentTagFilter().filter(rec)
    assert str(rec.msg) == "Starting Docker sandbox"


def test_log_agent_does_not_leak_across_sibling_tasks():
    seen: dict[str, str] = {}

    async def runner(name: str) -> None:
        with log_agent(name):
            await asyncio.sleep(0)
            rec = _record("Sandbox started")
            AgentTagFilter().filter(rec)
            seen[name] = str(rec.msg)

    async def main() -> None:
        await asyncio.gather(runner("a"), runner("b"))

    asyncio.run(main())
    assert seen == {"a": "[a] Sandbox started", "b": "[b] Sandbox started"}
