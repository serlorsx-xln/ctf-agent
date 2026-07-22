"""Duplicate-model swarm keys solvers by runner_id."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.agents.swarm import ChallengeSwarm
from backend.models import assign_runner_ids, expand_model_cli_args
from backend.solver_base import SolverResult


class _InstantSolver:
    def __init__(self, name: str) -> None:
        self.name = name
        self.runner_id = name
        self.sandbox = None
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def run_until_done_or_gave_up(self) -> SolverResult:
        return SolverResult(
            flag=None,
            status="gave_up",
            findings_summary=f"from {self.name}",
            step_count=1,
            cost_usd=0.0,
            log_path="",
        )

    def bump(self, _insights: str) -> None:
        pass


def test_expand_cli_then_assign_three_grok():
    specs = expand_model_cli_args(["cursor/grok-4.5*3"])
    slots = assign_runner_ids(specs)
    assert [r for r, _ in slots] == [
        "cursor/grok-4.5#1",
        "cursor/grok-4.5#2",
        "cursor/grok-4.5#3",
    ]
    assert all(m == "cursor/grok-4.5" for _, m in slots)


def test_swarm_creates_distinct_solver_keys(monkeypatch):
    created: list[tuple[str, str]] = []

    def _fake_create(self, model_spec: str, runner_id: str | None = None):
        rid = runner_id or model_spec
        created.append((rid, model_spec))
        return _InstantSolver(rid)

    monkeypatch.setattr(ChallengeSwarm, "_create_solver", _fake_create)

    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.meta = MagicMock(name="chal")
    swarm.meta.name = "chal"
    swarm.challenge_dir = "/tmp"
    swarm.cost_tracker = MagicMock()
    swarm.cost_tracker.total_cost_usd = 0.0
    swarm.settings = MagicMock()
    swarm.settings.eval_out = ""
    swarm.settings.eval_max_wall_s = 0
    swarm.settings.eval_max_usd = 0
    swarm.model_specs = expand_model_cli_args(["cursor/grok-4.5*3"])
    swarm.coordinator_inbox = None
    swarm.cancel_event = asyncio.Event()
    swarm.solvers = {}
    swarm.findings = {}
    swarm.winner = None
    swarm.confirmed_flag = None
    swarm.confirmed_flags = []
    swarm._flag_lock = asyncio.Lock()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    swarm.message_bus = MagicMock()
    swarm.message_bus.post = AsyncMock()
    swarm._eval = MagicMock()
    swarm._eval.budget_exceeded = MagicMock(return_value=None)
    swarm._infra_recoveries_total = 0
    swarm._last_model_spec = ""
    swarm._last_preflight_ms = 0.0
    swarm._last_steps = 0
    swarm._last_status = ""
    swarm._last_flag = None
    swarm._write_eval_artifact = MagicMock()
    swarm._end_summary = MagicMock(return_value=None)
    swarm._gather_sibling_insights = MagicMock(return_value="")

    # Stop after first round: each solver returns gave_up with 0 steps → break
    # Use bump path that exits: step_count 0 breaks without bump
    class _ZeroStep(_InstantSolver):
        async def run_until_done_or_gave_up(self) -> SolverResult:
            return SolverResult(
                flag=None,
                status="gave_up",
                findings_summary="",
                step_count=0,
                cost_usd=0.0,
                log_path="",
            )

    def _fake_create2(self, model_spec: str, runner_id: str | None = None):
        rid = runner_id or model_spec
        created.append((rid, model_spec))
        return _ZeroStep(rid)

    monkeypatch.setattr(ChallengeSwarm, "_create_solver", _fake_create2)

    asyncio.run(swarm.run())

    assert set(swarm.solvers.keys()) == {
        "cursor/grok-4.5#1",
        "cursor/grok-4.5#2",
        "cursor/grok-4.5#3",
    }
    assert created == [
        ("cursor/grok-4.5#1", "cursor/grok-4.5"),
        ("cursor/grok-4.5#2", "cursor/grok-4.5"),
        ("cursor/grok-4.5#3", "cursor/grok-4.5"),
    ]

    status = swarm.get_status()
    assert set(status["agents"].keys()) == set(swarm.solvers.keys())
    assert all(a["model"] == "cursor/grok-4.5" for a in status["agents"].values())
