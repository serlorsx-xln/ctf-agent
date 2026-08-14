"""Shared Cursor SDK bridge workspace (not first-solver temp)."""

from __future__ import annotations

from pathlib import Path

import backend.agents.cursor_runtime as runtime


def test_shared_bridge_workspace_is_stable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_bridge_workspace", None)
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "bridge"))
    (tmp_path / "bridge").mkdir()
    a = runtime.shared_bridge_workspace()
    b = runtime.shared_bridge_workspace()
    assert a == b
    assert Path(a, "AGENTS.md").is_file()
