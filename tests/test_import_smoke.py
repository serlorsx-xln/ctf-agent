"""Smoke imports — catch IndentationError / SyntaxError before swarm runtime."""

from __future__ import annotations


def test_import_all_solvers() -> None:
    from backend.agents.claude_solver import ClaudeSolver
    from backend.agents.codex_solver import CodexSolver
    from backend.agents.cursor_solver import CursorSolver
    from backend.agents.gemini_solver import GeminiSolver
    from backend.agents.swarm import ChallengeSwarm

    assert ClaudeSolver and CodexSolver and CursorSolver and GeminiSolver and ChallengeSwarm


def test_import_bridge_swarm_op() -> None:
    from backend.shell import bridge

    assert callable(bridge._swarm)
    assert callable(bridge._clear_session)
    assert callable(bridge._stop_race)
