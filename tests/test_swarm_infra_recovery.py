"""Swarm infra-recovery path (no Docker / Cursor)."""

from __future__ import annotations

import asyncio

from backend.agents.swarm import ChallengeSwarm, MAX_INFRA_RECOVERIES
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, GAVE_UP, INFRA_ERROR, SolverResult


class _FakeSettings:
    sandbox_image = "ctf-sandbox-core"
    container_memory_limit = "1g"
    detected_packs: list = []
    sandbox_image_locked = False


class _RecoveringSolver:
    model_spec = "cursor/grok-4.5"
    agent_name = "t/grok-4.5"
    sandbox = object()

    def __init__(self) -> None:
        self.calls = 0
        self.recoveries = 0
        self.bumps = 0

    async def start(self) -> None:
        return None

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self.calls += 1
        if self.recoveries < 2:
            return SolverResult(
                flag=None,
                status=INFRA_ERROR,
                findings_summary="Infra: Bridge request timed out: ReadTimeout: ",
                step_count=10,
                cost_usd=0.0,
                log_path="",
            )
        return SolverResult(
            flag="CTF{ok}",
            status=FLAG_FOUND,
            findings_summary="done",
            step_count=12,
            cost_usd=0.0,
            log_path="",
        )

    def bump(self, insights: str) -> None:
        self.bumps += 1

    async def recover_session(self, insights: str | None = None) -> None:
        self.recoveries += 1

    async def stop(self) -> None:
        return None


def _swarm() -> ChallengeSwarm:
    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = "/tmp"
    swarm.meta = ChallengeMeta(name="t", description="", flags_required=1)
    swarm.cost_tracker = None  # type: ignore[assignment]
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = ["cursor/grok-4.5"]
    swarm.coordinator_inbox = None
    swarm.cancel_event = asyncio.Event()
    swarm.solvers = {}
    swarm.findings = {}
    swarm.winner = None
    swarm.confirmed_flag = "CTF{ok}"
    swarm.confirmed_flags = ["CTF{ok}"]
    swarm._flag_lock = asyncio.Lock()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    from backend.message_bus import ChallengeMessageBus

    swarm.message_bus = ChallengeMessageBus()
    return swarm


def test_swarm_recovers_infra_then_wins(monkeypatch):
    import backend.agents.swarm as swarm_mod

    monkeypatch.setattr(swarm_mod, "INFRA_RECOVERY_COOLDOWN_S", 0)
    swarm = _swarm()
    solver = _RecoveringSolver()

    async def _run():
        return await swarm._run_solver_loop(solver, "cursor/grok-4.5")

    result, final = asyncio.run(_run())
    assert result.status == FLAG_FOUND
    assert result.flag == "CTF{ok}"
    assert solver.recoveries == 2
    assert solver.bumps == 0
    assert solver.calls == 3  # 2 infra + 1 success


def test_max_infra_recoveries_constant():
    assert MAX_INFRA_RECOVERIES >= 10
