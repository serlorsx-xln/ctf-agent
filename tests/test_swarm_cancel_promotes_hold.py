"""Winner CANCELLED after CORRECT must still enter Hold."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.message_bus import ChallengeMessageBus
from backend.prompts import ChallengeMeta
from backend.solver_base import CANCELLED, FLAG_FOUND, SolverResult


class _FakeSettings:
    sandbox_image = "ctf-sandbox-core"
    container_memory_limit = "1g"
    auto_confirm_flags = True
    max_solver_bumps = 0


@pytest.mark.asyncio
async def test_cancelled_confirmed_winner_still_holds(tmp_path, monkeypatch):
    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = str(tmp_path)
    swarm.meta = ChallengeMeta(name="hold", description="", flags_required=1)
    swarm.cost_tracker = MagicMock()
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = ["cursor/default"]
    swarm.cancel_event = asyncio.Event()
    swarm.release_event = asyncio.Event()
    swarm.hold_active = False
    swarm.solvers = {}
    swarm.findings = {}
    swarm.winner = None
    swarm.winner_runner_id = "cursor/default"
    swarm.flag_credits = {}
    swarm.flag_notes = {}
    swarm._steps_by_runner = {}
    swarm.confirmed_flag = "CTF{done_aaaaaaaa}"
    swarm.confirmed_flags = ["CTF{done_aaaaaaaa}"]
    swarm._flag_lock = asyncio.Lock()
    swarm._confirm_dialog_lock = asyncio.Lock()
    swarm._flag_inflight = set()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    swarm._writeup_attempted = False
    swarm._how_emitted = True
    swarm._eval = None
    swarm._infra_recoveries_total = 0
    swarm._quota_outcome_printed = False
    swarm.message_bus = ChallengeMessageBus()

    hold_calls = {"n": 0}

    async def fake_hold(*_a, **_k):
        hold_calls["n"] += 1

    async def fake_writeup(*_a, **_k):
        return None

    monkeypatch.setattr(swarm, "_winner_qa_hold", fake_hold)
    monkeypatch.setattr(swarm, "_capture_and_emit_writeup", fake_writeup)

    solver = SimpleNamespace(
        _confirmed=True,
        _findings="",
        sandbox=None,
        start=AsyncMock(),
        stop=AsyncMock(),
        run_until_done_or_gave_up=AsyncMock(
            return_value=SolverResult(
                flag="CTF{done_aaaaaaaa}",
                status=CANCELLED,
                findings_summary="",
                step_count=3,
                cost_usd=None,
                log_path="",
            )
        ),
        bump=MagicMock(),
    )

    result, _ = await swarm._run_solver_loop(solver, "cursor/default", "cursor/default")
    assert result.status == FLAG_FOUND
    assert hold_calls["n"] == 1
    assert swarm.hold_active is True


@pytest.mark.asyncio
async def test_error_after_correct_winner_still_holds(tmp_path, monkeypatch):
    """Provider ERROR after CORRECT must not skip writeup Hold."""
    from backend.solver_base import ERROR

    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = str(tmp_path)
    swarm.meta = ChallengeMeta(name="hold", description="", flags_required=1)
    swarm.cost_tracker = MagicMock()
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = ["cursor/default"]
    swarm.cancel_event = asyncio.Event()
    swarm.release_event = asyncio.Event()
    swarm.hold_active = False
    swarm.solvers = {}
    swarm.findings = {}
    swarm.winner = None
    swarm.winner_runner_id = "cursor/default"
    swarm.flag_credits = {}
    swarm.flag_notes = {}
    swarm._steps_by_runner = {}
    swarm.confirmed_flag = "CTF{done_aaaaaaaa}"
    swarm.confirmed_flags = ["CTF{done_aaaaaaaa}"]
    swarm._flag_lock = asyncio.Lock()
    swarm._confirm_dialog_lock = asyncio.Lock()
    swarm._flag_inflight = set()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    swarm._writeup_attempted = False
    swarm._how_emitted = True
    swarm._eval = None
    swarm._infra_recoveries_total = 0
    swarm.message_bus = ChallengeMessageBus()

    hold_calls = {"n": 0}

    async def fake_hold(*_a, **_k):
        hold_calls["n"] += 1

    monkeypatch.setattr(swarm, "_winner_qa_hold", fake_hold)
    monkeypatch.setattr(swarm, "_capture_and_emit_writeup", AsyncMock())

    solver = SimpleNamespace(
        _confirmed=True,
        _findings="",
        sandbox=None,
        start=AsyncMock(),
        stop=AsyncMock(),
        run_until_done_or_gave_up=AsyncMock(
            return_value=SolverResult(
                flag="CTF{done_aaaaaaaa}",
                status=ERROR,
                findings_summary="boom",
                step_count=3,
                cost_usd=None,
                log_path="",
            )
        ),
        bump=MagicMock(),
    )

    result, _ = await swarm._run_solver_loop(solver, "cursor/default", "cursor/default")
    assert result.status == FLAG_FOUND
    assert hold_calls["n"] == 1
    assert swarm.hold_active is True
