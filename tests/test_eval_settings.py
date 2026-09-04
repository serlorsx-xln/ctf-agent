"""Eval settings + CLI wiring (no Docker)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from backend.config import Settings
from backend.eval_run import (
    EVAL_BUDGET,
    EvalRunState,
    agent_failed_from_status,
    write_eval_summary,
)
from backend.solver_base import ERROR, GAVE_UP, INFRA_ERROR


def test_settings_eval_defaults() -> None:
    s = Settings()
    assert s.force_packs == []
    assert s.eval_max_wall_s is None
    assert s.eval_max_usd is None
    assert s.eval_strict_packs is False
    assert s.eval_out == ""


def test_cli_pack_and_eval_flags_help() -> None:
    from backend.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["race", "--help"])
    assert result.exit_code == 0
    assert "--pack" in result.output
    assert "--eval-out" in result.output
    assert "--eval-max-wall-s" in result.output
    assert "--eval-max-usd" in result.output
    assert "--eval-strict-packs" in result.output


def test_cli_applies_pack_and_eval_to_settings(tmp_path: Path, monkeypatch) -> None:
    """Exercise Click option → Settings wiring without Docker/swarm."""
    from backend import cli as cli_mod

    chal = tmp_path / "chal"
    chal.mkdir()
    (chal / "challenge.txt").write_text("test challenge\n", encoding="utf-8")

    captured: dict = {}

    async def fake_run_single(
        settings, challenge_dir, model_specs, max_challenges, flags_required=None
    ):
        captured["settings"] = settings
        captured["challenge_dir"] = challenge_dir
        captured["models"] = model_specs
        captured["flags_required"] = flags_required

    monkeypatch.setattr(cli_mod, "_run_single", fake_run_single)
    monkeypatch.setattr(
        "backend.tool_router.resolve_sandbox_image",
        lambda *a, **k: ("ctf-sandbox-core", ["web"]),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.main,
        [
            "race",
            "--challenge",
            str(chal),
            "--pack",
            "pwn",
            "--pack",
            "crypto",
            "--eval-out",
            str(tmp_path / "out.json"),
            "--eval-max-wall-s",
            "90",
            "--eval-max-usd",
            "2.5",
            "--eval-strict-packs",
            "--models",
            "cursor/composer-2",
        ],
    )
    assert result.exit_code == 0, result.output
    s = captured["settings"]
    assert s.force_packs == ["pwn", "crypto"]
    assert s.eval_out == str(tmp_path / "out.json")
    assert s.eval_max_wall_s == 90.0
    assert s.eval_max_usd == 2.5
    assert s.eval_strict_packs is True


def test_eval_budget_wall() -> None:
    state = EvalRunState(started_monotonic=0.0)
    state.wall_s = lambda: 100.0  # type: ignore[method-assign]
    s = Settings()
    s.eval_max_wall_s = 10.0
    assert state.budget_exceeded(s, 0.0) == EVAL_BUDGET


def test_eval_budget_usd() -> None:
    state = EvalRunState()
    s = Settings()
    s.eval_max_usd = 1.5
    assert state.budget_exceeded(s, 2.0) == EVAL_BUDGET
    assert state.budget_exceeded(s, 0.5) is None
    assert state.budget_exceeded(s, None) == EVAL_BUDGET


def test_eval_usd_unknown_fails_open_when_wall_set() -> None:
    state = EvalRunState()
    s = Settings()
    s.eval_max_usd = 1.0
    s.eval_max_wall_s = 180.0
    assert state.budget_exceeded(s, None) is None
    assert state.budget_exceeded(s, 1.5) == EVAL_BUDGET


def test_agent_failed_excludes_infra() -> None:
    assert agent_failed_from_status(INFRA_ERROR) is False
    assert agent_failed_from_status(EVAL_BUDGET) is False
    assert agent_failed_from_status(ERROR) is True
    assert agent_failed_from_status(GAVE_UP) is True


def test_write_eval_summary_agent_failed_true(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    write_eval_summary(
        str(path),
        challenge="demo",
        model="cursor/composer-2",
        status=ERROR,
        steps=3,
        infra_recoveries=0,
        preflight_ms=10.0,
        cost_usd=0.1,
        flag=None,
    )
    data = json.loads(path.read_text())
    assert data["agent_failed"] is True
    assert data["status"] == ERROR


def test_write_eval_summary_infra_not_agent_failed(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    write_eval_summary(
        str(path),
        challenge="demo",
        model="cursor/composer-2",
        status=INFRA_ERROR,
        steps=12,
        infra_recoveries=3,
        preflight_ms=1500.5,
        cost_usd=0.0,
        flag=None,
    )
    data = json.loads(path.read_text())
    assert data["challenge"] == "demo"
    assert data["infra_recoveries"] == 3
    assert data["preflight_ms"] == 1500.5
    assert data["agent_failed"] is False


def test_swarm_eval_budget_cancels() -> None:
    """Wall budget stops the solver loop with eval_budget status."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.eval_run import EvalRunState
    from backend.message_bus import ChallengeMessageBus
    from backend.prompts import ChallengeMeta
    from backend.solver_base import SolverResult

    class _FakeSettings:
        sandbox_image = "ctf-sandbox-core"
        container_memory_limit = "1g"
        detected_packs: list = []
        sandbox_image_locked = False
        force_packs: list = []
        eval_max_wall_s = 0.001
        eval_max_usd = None
        eval_strict_packs = False
        eval_out = ""

    class _SlowSolver:
        model_spec = "cursor/x"
        agent_name = "t/x"
        sandbox = SimpleNamespace(preflight_ms=42.0)

        async def start(self) -> None:
            return None

        async def run_until_done_or_gave_up(self) -> SolverResult:
            await asyncio.sleep(0.05)
            return SolverResult(
                flag=None,
                status="gave_up",
                findings_summary="still going",
                step_count=1,
                cost_usd=0.0,
                log_path="",
            )

        def bump(self, insights: str) -> None:
            return None

        async def stop(self) -> None:
            return None

    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = "/tmp"
    swarm.meta = ChallengeMeta(name="t", description="", flags_required=1)
    swarm.cost_tracker = None  # type: ignore[assignment]
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = ["cursor/x"]
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
    swarm._eval = EvalRunState(started_monotonic=0.0)
    swarm._eval.wall_s = lambda: 10.0  # type: ignore[method-assign]
    swarm._infra_recoveries_total = 0
    swarm._last_model_spec = ""
    swarm._last_preflight_ms = 0.0
    swarm._last_steps = 0
    swarm._last_status = ""
    swarm._last_flag = None

    result, _ = asyncio.run(swarm._run_solver_loop(_SlowSolver(), "cursor/x", "cursor/x"))
    assert result.status == EVAL_BUDGET
    assert swarm.cancel_event.is_set()


def test_sandbox_eval_wall_applies_before_pack_attach() -> None:
    """Pack bootstrap must see the same wall as the swarm loop (no Docker)."""
    from backend.sandbox.container import DockerSandbox

    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir="/tmp", settings=Settings())
    assert sb._eval_wall_exceeded() is False
    sb.settings = Settings()
    sb.settings.eval_max_wall_s = 0.01
    sb._started_monotonic = 0.0
    assert sb._eval_wall_exceeded() is False
    sb._started_monotonic = __import__("time").monotonic() - 1.0
    assert sb._eval_wall_exceeded() is True


def test_eval_skips_cold_pack_prefetch() -> None:
    """Tagged steg/web must not burn an eval wall on apt when L0 is core."""
    from backend.sandbox.container import DockerSandbox

    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir="/tmp", settings=Settings())
    sb.settings.eval_max_wall_s = 180
    assert sb._skip_cold_prefetch_for_eval("steg") is True
    assert sb._skip_cold_prefetch_for_eval("web") is True
    sb.image = "ctf-sandbox-warm-steg"
    assert sb._skip_cold_prefetch_for_eval("steg") is False
    sb.image = "ctf-sandbox-pwn"
    assert sb._skip_cold_prefetch_for_eval("pwn") is False
    sb.image = "ctf-sandbox-core"
    sb.settings.eval_max_wall_s = None
    assert sb._skip_cold_prefetch_for_eval("steg") is False


def test_eval_ensure_pack_does_not_start_donor_build() -> None:
    """Eval wall on core must not start a missing-donor docker build."""
    from backend.sandbox.container import DockerSandbox

    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir="/tmp", settings=Settings())
    sb.settings.eval_max_wall_s = 180
    msg = asyncio.run(sb.ensure_pack("ghidra"))
    assert "cold-install" in msg
