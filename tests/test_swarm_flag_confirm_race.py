"""Multi-flag confirm races under ``try_submit_flag`` (no Docker)."""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.message_bus import ChallengeMessageBus
from backend.prompts import ChallengeMeta


class _FakeSettings:
    sandbox_image = "ctf-sandbox-core"
    container_memory_limit = "1g"
    auto_confirm_flags = True


def _bare_swarm(flags_required: int = 2) -> ChallengeSwarm:
    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = "/tmp"
    swarm.meta = ChallengeMeta(name="race", description="", flags_required=flags_required)
    swarm.cost_tracker = None  # type: ignore[assignment]
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = ["cursor/a", "cursor/b"]
    swarm.cancel_event = asyncio.Event()
    swarm.release_event = asyncio.Event()
    swarm.hold_active = False
    swarm.solvers = {}
    swarm.winner = None
    swarm.winner_runner_id = ""
    swarm.flag_credits = {}
    swarm.flag_notes = {}
    swarm._steps_by_runner = {}
    swarm.confirmed_flag = None
    swarm.confirmed_flags = []
    swarm._flag_lock = asyncio.Lock()
    swarm._confirm_dialog_lock = asyncio.Lock()
    swarm._flag_inflight = set()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    swarm._writeup_attempted = False
    swarm._how_emitted = False
    swarm.message_bus = ChallengeMessageBus()
    return swarm


@pytest.mark.asyncio
async def test_concurrent_multi_flag_confirms_complete_challenge(monkeypatch):
    """Stale already_accepted=[] must not leave N/N without CORRECT/Hold."""
    swarm = _bare_swarm(2)
    calls: list[list[str]] = []

    async def fake_submit(
        _name,
        flag,
        *,
        already_accepted=None,
        required=1,
        **_kw,
    ):
        prior = list(already_accepted or [])
        calls.append(prior)
        n = len(prior) + 1
        if n >= required:
            return f"CORRECT — all {required} flag(s) confirmed", True
        return f"ACCEPTED ({n}/{required}) — keep going", False

    monkeypatch.setattr("backend.tools.core.do_submit_flag", fake_submit)
    monkeypatch.setattr(
        "backend.shell.sandbox_session.sync_accepted_flags",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(swarm, "_emit_correct_recap", lambda *_a, **_k: None)
    monkeypatch.setattr(swarm, "_emit_how_recap", lambda *_a, **_k: None)

    # Serialize via dialog lock but each call still snapshots before the other
    # finishes bookkeeping — exercise the live-count completion path.
    r1, r2 = await asyncio.gather(
        swarm.try_submit_flag("CTF{user_aaaaaaaa}", "cursor/a"),
        swarm.try_submit_flag("CTF{root_bbbbbbbb}", "cursor/b"),
    )
    assert swarm.confirmed_flag
    assert len(swarm.confirmed_flags) == 2
    assert swarm.cancel_event.is_set()
    assert swarm.hold_active is True
    assert r1[1] is True or r2[1] is True
    # Second confirm must see the first flag in already_accepted (fresh snapshot).
    assert any(len(c) == 1 for c in calls)


@pytest.mark.asyncio
async def test_live_count_correct_emits_outcome(monkeypatch):
    """When live N/N upgrades ACCEPTED → CORRECT, publish outcome on the feed."""
    swarm = _bare_swarm(2)
    printed: list[str] = []

    async def fake_submit(_name, flag, *, already_accepted=None, required=1, **_kw):
        prior = list(already_accepted or [])
        n = len(prior) + 1
        # Always claim incomplete so swarm live-count must upgrade.
        return f"ACCEPTED ({n}/{required}) — keep going", False

    monkeypatch.setattr("backend.tools.core.do_submit_flag", fake_submit)
    monkeypatch.setattr(
        "backend.shell.sandbox_session.sync_accepted_flags",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(swarm, "_emit_correct_recap", lambda *_a, **_k: None)
    monkeypatch.setattr(swarm, "_emit_how_recap", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "backend.agents.live_log.emit_line",
        lambda text: printed.append(str(text)),
    )

    await swarm.try_submit_flag("CTF{user_aaaaaaaa}", "cursor/a")
    out, done = await swarm.try_submit_flag("CTF{root_bbbbbbbb}", "cursor/b")
    assert done is True
    assert out.startswith("CORRECT")
    assert any("outcome CORRECT" in line for line in printed)
    swarm = _bare_swarm(1)
    swarm.confirmed_flags = ["CTF{done_aaaaaaaa}"]
    swarm.confirmed_flag = "CTF{done_aaaaaaaa}"
    called = {"n": 0}

    async def fake_submit(*_a, **_k):
        called["n"] += 1
        return "CORRECT", True

    monkeypatch.setattr("backend.tools.core.do_submit_flag", fake_submit)
    out, done = await swarm.try_submit_flag("CTF{late_bbbbbbbb}", "cursor/b")
    assert done is False  # sibling must not claim challenge_complete / Hold
    assert "ALREADY SOLVED" in out
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_already_solved_from_do_submit_flag_never_complete(monkeypatch):
    """Late ALREADY SOLVED from do_submit_flag must not flip challenge_complete."""
    swarm = _bare_swarm(2)
    swarm.confirmed_flags = ["CTF{user_aaaaaaaa}"]

    async def fake_submit(*_a, **_k):
        return (
            "ALREADY SOLVED — all 2 flag(s) already accepted: "
            "CTF{user_aaaaaaaa} | CTF{root_bbbbbbbb}",
            True,
        )

    monkeypatch.setattr("backend.tools.core.do_submit_flag", fake_submit)
    out, done = await swarm.try_submit_flag("CTF{root_bbbbbbbb}", "cursor/b")
    assert done is False
    assert "ALREADY SOLVED" in out


@pytest.mark.asyncio
async def test_late_flag_found_does_not_steal_hold(monkeypatch):
    """Sibling FLAG_FOUND after CORRECT must not overwrite winner / start Hold."""
    from backend.solver_base import FLAG_FOUND, SolverResult

    swarm = _bare_swarm(1)
    swarm.confirmed_flag = "CTF{winner_aaaaaaaa}"
    swarm.winner_runner_id = "cursor/a"
    swarm.hold_active = True
    swarm.findings = {}
    swarm._eval = None
    swarm._last_preflight_ms = 0.0
    swarm._last_model_spec = ""

    class FakeSolver:
        sandbox = None
        _findings = ""

        async def start(self):
            return None

        async def stop(self):
            return None

        async def run_until_done_or_gave_up(self):
            return SolverResult(
                flag="CTF{late_bbbbbbbb}",
                status=FLAG_FOUND,
                findings_summary="",
                step_count=1,
                cost_usd=None,
                log_path="",
            )

    hold_calls = {"n": 0}

    async def no_hold(*_a, **_k):
        hold_calls["n"] += 1

    monkeypatch.setattr(swarm, "_capture_and_emit_writeup", no_hold)
    monkeypatch.setattr(swarm, "_winner_qa_hold", no_hold)

    result, _solver = await swarm._run_solver_loop(FakeSolver(), "cursor/b", "cursor/b")
    assert result.status == FLAG_FOUND
    assert swarm.winner_runner_id == "cursor/a"
    assert hold_calls["n"] == 0
