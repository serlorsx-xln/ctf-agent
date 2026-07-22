"""Shared solver_control turn-error classification."""

from __future__ import annotations

from types import SimpleNamespace

from backend.agents.solver_control import classify_turn_error, stash_bump
from backend.loop_detect import LoopDetector
from backend.solver_base import ERROR, INFRA_ERROR, QUOTA_ERROR


def test_classify_bridge_timeout_infra() -> None:
    assert classify_turn_error("Bridge request timed out after 600s") == INFRA_ERROR


def test_classify_readtimeout_infra() -> None:
    assert classify_turn_error("httpx.ReadTimeout: timed out") == INFRA_ERROR


def test_classify_internal_error_infra() -> None:
    assert classify_turn_error("internal: internal error") == INFRA_ERROR


def test_classify_opaque_cursor_error_infra() -> None:
    assert classify_turn_error("error") == INFRA_ERROR
    assert classify_turn_error("run error") == INFRA_ERROR
    assert classify_turn_error("failed") == INFRA_ERROR


def test_classify_quota() -> None:
    assert classify_turn_error("You have exceeded your quota") == QUOTA_ERROR
    assert classify_turn_error("rate limit exceeded") == QUOTA_ERROR
    assert classify_turn_error("billing issue") == QUOTA_ERROR


def test_classify_generic_error() -> None:
    assert classify_turn_error("segmentation fault in exploit") == ERROR
    assert classify_turn_error(None) == ERROR


def test_stash_bump_resets_loop_and_traces() -> None:
    events: list[tuple] = []

    class _Tracer:
        def event(self, name: str, **kw) -> None:
            events.append((name, kw))

    detector = LoopDetector()
    # Build up identical calls toward a loop
    statuses = [detector.check("bash", {"command": "id"}) for _ in range(5)]
    assert "break" in statuses

    solver = SimpleNamespace(
        _bump_insights=None,
        loop_detector=detector,
        tracer=_Tracer(),
    )
    stash_bump(solver, "try sibling insight")
    assert solver._bump_insights == "try sibling insight"
    # Reset should clear streak so identical call is allowed again
    assert solver.loop_detector.check("bash", {"command": "id"}) is None
    assert events and events[0][0] == "bump"
    assert "sibling" in (events[0][1].get("insights") or "")


def test_start_sandbox_basics_skips_restart_when_live(tmp_path) -> None:
    import asyncio

    from backend.agents.solver_control import start_sandbox_basics
    from backend.prompts import ChallengeMeta

    class _SB:
        _container = object()
        started = 0

        async def start(self) -> None:
            self.started += 1

        async def exec(self, cmd: str, timeout_s: int = 10):
            return SimpleNamespace(stdout="aarch64\n")

    sb = _SB()
    meta = ChallengeMeta(name="t", description="", flags_required=1)
    arch, names = asyncio.run(start_sandbox_basics(sb, meta, str(tmp_path)))
    assert sb.started == 0
    assert arch == "aarch64"
    assert isinstance(names, list)
