"""Cursor and Codex solvers must offer the same sandbox tools.

Divergent surfaces make an agent reach for a tool its backend does not have and
burn a turn discovering that, so the two lists are pinned together.
"""

from __future__ import annotations

from backend.agents.codex_solver import SANDBOX_TOOLS
from backend.agents.cursor_solver import SOLVER_PREAMBLE, CursorSolver

EXPECTED = {
    "bash",
    "read_file",
    "write_file",
    "list_files",
    "submit_flag",
    "web_fetch",
    "webhook_create",
    "webhook_get_requests",
    "view_image",
    "notify_coordinator",
}


def _cursor_tool_names() -> set[str]:
    solver = CursorSolver.__new__(CursorSolver)
    solver.sandbox = None
    solver.use_vision = True
    solver.notify_coordinator = None
    return set(solver._build_custom_tools())


def test_cursor_exposes_the_expected_tools():
    assert _cursor_tool_names() == EXPECTED


def test_codex_exposes_the_expected_tools():
    assert {t["name"] for t in SANDBOX_TOOLS} == EXPECTED


def test_cursor_and_codex_surfaces_match():
    assert _cursor_tool_names() == {t["name"] for t in SANDBOX_TOOLS}


def test_preamble_documents_every_tool_it_binds():
    """A bound-but-undocumented tool goes unused; a documented-but-unbound one errors."""
    for name in _cursor_tool_names():
        assert name in SOLVER_PREAMBLE, f"{name} missing from the Cursor preamble"


def test_cursor_view_image_schema_accepts_path():
    solver = CursorSolver.__new__(CursorSolver)
    solver.sandbox = None
    solver.use_vision = True
    solver.notify_coordinator = None
    schema = solver._build_custom_tools()["view_image"].input_schema
    assert "path" in schema["properties"]
    assert "filename" in schema["properties"]
