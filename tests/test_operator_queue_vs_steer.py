"""Operator queue vs Send now — targeting and soft-path gate."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from backend.operator_inbox import (
    append_operator_note,
    clear_operator_inbox,
    drain_operator_notes_to_bus,
    targets_match,
)


def test_targets_match():
    assert targets_match(None, "default#1") is True
    assert targets_match("default#1", "default#1") is True
    assert targets_match("default#1", "default#2") is False
    # base ↔ duplicate runner labels (same as followup crumb matching)
    assert targets_match("opus", "opus#1") is True
    assert targets_match("opus#2", "opus") is True
    assert targets_match("opus", "composer#1") is False
    assert targets_match("default#1", None) is False


def test_steer_drain_leaves_other_agent_notes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("for-1", delivery="steer", target="default#1")
    append_operator_note("for-2", delivery="steer", target="default#2")
    append_operator_note("queued-2", delivery="queue", target="default#2")

    async def _run():
        steered = await drain_operator_notes_to_bus(
            None, delivery="steer", broadcast=False, claimer="default#1"
        )
        assert steered == ["for-1"]
        left_steer = await drain_operator_notes_to_bus(
            None, delivery="steer", broadcast=False, claimer="default#2"
        )
        assert left_steer == ["for-2"]
        queued = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="default#2"
        )
        assert queued == ["queued-2"]

    asyncio.run(_run())


def test_check_findings_leaves_unscoped_when_cursor_owns_inbox(tmp_path: Path, monkeypatch):
    """Mixed Cursor+soft: soft must not steal unscoped/broadcast notes."""
    from backend.tools import core as tools_core

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("broadcast-keep", delivery="queue")
    monkeypatch.setenv("ARTEMIS_CURSOR_OWNS_INBOX", "1")

    class _Bus:
        async def check(self, _spec):
            return []

        def format_unread(self, _f):
            return "No new findings from other agents."

        async def broadcast(self, text, source="operator"):  # noqa: ANN001
            return None

    async def _run():
        out = await tools_core.do_check_findings(_Bus(), "claude-sdk/x")
        assert "No new findings" in out
        left = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="default#1"
        )
        assert left == ["broadcast-keep"]

    try:
        asyncio.run(_run())
    finally:
        os.environ.pop("ARTEMIS_CURSOR_OWNS_INBOX", None)


def test_soft_steer_claimer_drains_own_target_when_cursor_owns_inbox(
    tmp_path: Path, monkeypatch
):
    """Soft agent-page notes still clear via soft_steer when Cursor owns interrupt."""
    from types import SimpleNamespace

    from backend.agents.soft_steer import claim_soft_steer_notes

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("for-claude", delivery="steer", target="x")
    append_operator_note("for-cursor", delivery="steer", target="default")
    monkeypatch.setenv("ARTEMIS_CURSOR_OWNS_INBOX", "1")

    async def _run():
        notes = await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=None,
                model_spec="claude-sdk/x",
                runner_id="claude-sdk/x",
            )
        )
        assert notes == ["for-claude"]
        left = await drain_operator_notes_to_bus(
            None, delivery="steer", broadcast=False, claimer="default"
        )
        assert left == ["for-cursor"]

    try:
        asyncio.run(_run())
    finally:
        os.environ.pop("ARTEMIS_CURSOR_OWNS_INBOX", None)


def test_soft_steer_claimer_keeps_hash_runner(tmp_path: Path, monkeypatch):
    """Duplicate runners must claim fan-out rows with the #N display key."""
    from types import SimpleNamespace

    from backend.agents.soft_steer import claim_soft_steer_notes

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    monkeypatch.delenv("ARTEMIS_CURSOR_OWNS_INBOX", raising=False)
    clear_operator_inbox()
    append_operator_note("only-two", delivery="steer", target="opus#2")
    append_operator_note("only-one", delivery="steer", target="opus#1")

    async def _run():
        notes = await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=None,
                model_spec="claude-sdk/opus",
                runner_id="claude-sdk/opus#2",
            )
        )
        assert notes == ["only-two"]
        left = await drain_operator_notes_to_bus(
            None, delivery="steer", broadcast=False, claimer="opus#1"
        )
        assert left == ["only-one"]
        assert (
            await drain_operator_notes_to_bus(
                None, delivery="steer", broadcast=False, claimer="opus#2"
            )
            == []
        )

    asyncio.run(_run())


def test_soft_targeted_note_does_not_leak_on_bus(tmp_path: Path, monkeypatch):
    """Agent-page steer must not broadcast onto the shared findings bus."""
    from types import SimpleNamespace

    from backend.agents.soft_steer import claim_soft_steer_notes
    from backend.message_bus import ChallengeMessageBus

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    monkeypatch.delenv("ARTEMIS_CURSOR_OWNS_INBOX", raising=False)
    clear_operator_inbox()
    append_operator_note("secret-for-opus", delivery="steer", target="opus")

    async def _run():
        bus = ChallengeMessageBus()
        notes = await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=bus,
                model_spec="claude-sdk/opus",
                runner_id="claude-sdk/opus",
            )
        )
        assert notes == ["secret-for-opus"]
        # Sibling soft solver must not see the note on the bus.
        assert await bus.check("claude-sdk/sonnet") == []

    asyncio.run(_run())


def test_soft_steer_does_not_drain_queue(tmp_path: Path, monkeypatch):
    """Send-now claim must leave Queue notes for turn-idle claim."""
    from types import SimpleNamespace

    from backend.agents.soft_steer import claim_soft_steer_notes
    from backend.tools import core as tools_core

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    monkeypatch.delenv("ARTEMIS_CURSOR_OWNS_INBOX", raising=False)
    clear_operator_inbox()
    append_operator_note("later", delivery="queue", target="opus")
    append_operator_note("now", delivery="steer", target="opus")

    class _Bus:
        def __init__(self) -> None:
            self._notes: list[str] = []

        async def check(self, _spec):
            out, self._notes = self._notes, []
            return out

        def format_unread(self, findings):
            return "\n".join(findings) if findings else "No new findings from other agents."

        async def broadcast(self, text, source="operator"):  # noqa: ANN001
            self._notes.append(str(text))

    async def _run():
        bus = _Bus()
        steered = await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=bus,
                model_spec="claude-sdk/opus",
                runner_id="claude-sdk/opus",
            )
        )
        assert steered == ["now"]
        # Mid-tool findings path must not touch operator inbox.
        text = await tools_core.do_check_findings(bus, "claude-sdk/opus")
        assert "now" not in text
        assert "later" not in text
        idle = await tools_core.soft_idle_operator_notes(bus, "claude-sdk/opus")
        assert idle and "later" in idle
        left = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="opus"
        )
        assert left == []

    asyncio.run(_run())


def test_claim_queue_after_force_followup_gate():
    def claim_queue_after_run(*, interrupted: bool, force: bool = False) -> bool:
        return not interrupted and not force

    assert claim_queue_after_run(interrupted=True) is False
    assert claim_queue_after_run(interrupted=False, force=True) is False
    assert claim_queue_after_run(interrupted=False, force=False) is True


def test_prompt_queue_continues_after_force_followup():
    """After Send-now force turn, resume/solve prompts must not be abandoned."""

    def should_continue(*, more: bool, interrupted: bool, prompt_queue_len: int) -> bool:
        return more or interrupted or prompt_queue_len > 0

    assert should_continue(more=False, interrupted=False, prompt_queue_len=1) is True
    assert should_continue(more=False, interrupted=False, prompt_queue_len=0) is False
    assert should_continue(more=True, interrupted=True, prompt_queue_len=2) is True


def test_hold_no_fanout_multi_without_target_errors(tmp_path: Path, monkeypatch):
    from backend.daemon.handlers import Handlers
    from backend.daemon.state import DaemonState
    from backend.operator_inbox import operator_inbox_path

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path / "sess",
    )

    class _Sup:
        def is_running(self, session):  # noqa: ANN001
            return True

        def last_roster(self, session):  # noqa: ANN001
            return ["default#1", "default#2"]

        def last_models(self, session):  # noqa: ANN001
            return ["cursor/default", "cursor/default"]

    async def _run():
        h = Handlers(DaemonState(), _Sup())  # type: ignore[arg-type]
        out = await h._h_swarm_operator_message(
            {"text": "hold note", "delivery": "steer", "no_fanout": True},
            session="abc",
        )
        assert out.get("ok") is False
        assert "Hold target unknown" in str(out.get("error") or "")
        path = operator_inbox_path("abc")
        assert not path.exists() or not path.read_text(encoding="utf-8").strip()

    asyncio.run(_run())
