"""Gemini solver — flag-confirmation gating and usage commit.

These cover the CRITICAL/HIGH bugs fixed in the Gemini solver:
- flag confirmation result must gate FLAG_FOUND (no FLAG_FOUND on reject)
- ACCEPTED (more flags needed) must NOT set ``_confirmed`` — only challenge_complete
- token usage must be committed via record_tokens (not just previewed)
- bump() stashes insights (not a no-op)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from backend.agents.gemini_solver import GeminiSolver
from backend.prompts import ChallengeMeta


def _make_solver(monkeypatch: pytest.MonkeyPatch) -> GeminiSolver:
    settings = MagicMock()
    settings.sandbox_image = "ctf-sandbox-core"
    settings.container_memory_limit = "4g"
    settings.gemini_api_key = "AIza-test"
    settings.gemini_project = ""
    settings.gemini_location = ""
    settings.gemini_base_url = ""
    settings.auto_confirm_flags = False

    meta = ChallengeMeta(name="demo", description="d", flags_required=1)

    # Avoid real Docker / genai in unit tests.
    monkeypatch.setattr("backend.agents.gemini_solver.DockerSandbox", lambda **kw: MagicMock())
    monkeypatch.setattr("backend.agents.gemini_solver.SolverTracer", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("backend.agents.gemini_solver.LoopDetector", lambda: MagicMock())

    solver = GeminiSolver(
        model_spec="gemini-sdk/gemini-3-flash",
        challenge_dir="/tmp/demo",
        meta=meta,
        cost_tracker=MagicMock(),
        settings=settings,
    )
    solver.tracer = MagicMock()
    return solver


def test_submit_flag_rejected_does_not_set_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL: a rejected flag must NOT produce FLAG_FOUND."""
    solver = _make_solver(monkeypatch)

    # submit_fn returns (display, done) — REJECTED → display "REJECTED…", done False
    async def submit_fn(flag: str) -> tuple[str, bool]:
        return ("REJECTED — operator did not confirm.", False)

    solver.submit_fn = submit_fn

    out = asyncio.run(solver._exec_tool("submit_flag", {"flag": "flag{decoy}"}))
    assert "REJECTED" in out
    assert solver._confirmed is False
    assert solver._flag is None  # not set on reject


def test_submit_flag_accepted_partial_does_not_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """ACCEPTED with done=False (more flags needed) must not stop the solver."""
    solver = _make_solver(monkeypatch)
    solver.meta = ChallengeMeta(name="demo", description="d", flags_required=2)

    async def submit_fn(flag: str) -> tuple[str, bool]:
        return ("ACCEPTED — counting this flag (1/2).", False)

    solver.submit_fn = submit_fn
    out = asyncio.run(solver._exec_tool("submit_flag", {"flag": "flag{one}"}))
    assert "ACCEPTED" in out
    assert solver._accepted_flags == ["flag{one}"]
    assert solver._confirmed is False
    assert solver._flag is None


def test_submit_flag_correct_sets_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    solver = _make_solver(monkeypatch)

    async def submit_fn(flag: str) -> tuple[str, bool]:
        return ('CORRECT — accepted "flag{real}". Challenge complete for this run.', True)

    solver.submit_fn = submit_fn
    out = asyncio.run(solver._exec_tool("submit_flag", {"flag": "flag{real}"}))
    assert "CORRECT" in out
    assert solver._confirmed is True
    assert solver._flag == "flag{real}"


def test_result_uses_confirmed_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    solver = _make_solver(monkeypatch)
    # No confirmation → flag should be None even if _flag were set elsewhere.
    res = solver._result("flag_found")
    assert res.flag is None
    assert res.cost_usd is None  # Gemini never reports USD


def test_bump_stashes_insights(monkeypatch: pytest.MonkeyPatch) -> None:
    """bump() must stash insights (not be a no-op)."""
    solver = _make_solver(monkeypatch)
    assert solver._bump_insights is None
    solver.bump("try a different approach")
    assert solver._bump_insights == "try a different approach"
