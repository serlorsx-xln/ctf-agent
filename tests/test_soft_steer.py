"""Soft-solver Send-now helpers (claim + interrupt prompt)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.agents.soft_steer import (
    claim_soft_steer_notes,
    operator_interrupt_prompt,
    restore_pending_soft_notes,
    soft_claimer_key,
)
from backend.operator_inbox import (
    append_operator_note,
    clear_operator_inbox,
    drain_operator_notes_to_bus,
)


def test_soft_claimer_key_prefers_display_key():
    solver = SimpleNamespace(runner_id="claude-sdk/opus#2", model_spec="claude-sdk/opus")
    assert soft_claimer_key(solver) == "opus#2"


def test_operator_interrupt_prompt_includes_notes():
    body = operator_interrupt_prompt(["try IDOR", "check /admin"], "Continue solving.")
    assert "Operator note: try IDOR" in body
    assert "Operator note: check /admin" in body
    assert "Continue solving." in body
    assert "Send now" in body


def test_operator_interrupt_prompt_empty_notes_passthrough():
    assert operator_interrupt_prompt([], "Continue solving.") == "Continue solving."


def test_restore_pending_soft_notes_requeues(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()

    solver = SimpleNamespace(
        runner_id="claude-sdk/opus",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus",
        _pending_soft_notes=["try IDOR"],
        _force_followup=asyncio.Event(),
    )
    solver._force_followup.set()
    restore_pending_soft_notes(solver)
    assert solver._pending_soft_notes == []
    assert not solver._force_followup.is_set()
    out = capsys.readouterr().out
    assert "[artemis] you (queue→opus): try IDOR" in out

    async def _run():
        queued = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="opus"
        )
        assert queued == ["try IDOR"]

    asyncio.run(_run())


def test_restore_pending_soft_notes_sibling_cancel_unscoped(
    tmp_path: Path, monkeypatch, capsys
):
    """After CORRECT, cancelled siblings must not park notes as queue→self."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()

    cancel = asyncio.Event()
    cancel.set()
    solver = SimpleNamespace(
        runner_id="claude-sdk/opus#2",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus#2",
        _pending_soft_notes=["follow up on XSS"],
        _force_followup=asyncio.Event(),
        cancel_event=cancel,
        _confirmed=False,
    )
    restore_pending_soft_notes(solver)
    out = capsys.readouterr().out
    assert "[artemis] you (queue): follow up on XSS" in out
    assert "queue→opus" not in out

    async def _run():
        # Unscoped queue row — Hold winner (any claimer) can take it.
        queued = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="default"
        )
        assert queued == ["follow up on XSS"]

    asyncio.run(_run())


def test_restore_pending_soft_notes_winner_keeps_scope(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()

    cancel = asyncio.Event()
    cancel.set()
    solver = SimpleNamespace(
        runner_id="claude-sdk/opus",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus",
        _pending_soft_notes=["winner note"],
        _force_followup=asyncio.Event(),
        cancel_event=cancel,
        _confirmed=True,
    )
    restore_pending_soft_notes(solver)
    out = capsys.readouterr().out
    assert "[artemis] you (queue→opus): winner note" in out


def test_claim_soft_steer_notes_skips_when_cancelled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("unscoped-for-hold", delivery="steer")

    cancel = asyncio.Event()
    cancel.set()
    solver = SimpleNamespace(
        message_bus=None,
        runner_id="claude-sdk/opus#2",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus#2",
        cancel_event=cancel,
        _confirmed=False,
    )

    async def _run():
        assert await claim_soft_steer_notes(solver) == []
        left = await drain_operator_notes_to_bus(
            None, delivery="steer", broadcast=False, claimer="default"
        )
        assert left == ["unscoped-for-hold"]

    asyncio.run(_run())


def test_claim_soft_steer_notes_allows_confirmed_winner(tmp_path: Path, monkeypatch):
    """Hold winner stays cancelled+_confirmed and must still claim steer."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("hold follow-up", delivery="steer", target="opus")

    cancel = asyncio.Event()
    cancel.set()
    solver = SimpleNamespace(
        message_bus=None,
        runner_id="claude-sdk/opus",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus",
        cancel_event=cancel,
        _confirmed=True,
    )

    async def _run():
        notes = await claim_soft_steer_notes(solver)
        assert notes == ["hold follow-up"]

    asyncio.run(_run())


def test_claim_soft_steer_notes_drains_steer_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("steer-me", delivery="steer", target="opus")
    append_operator_note("queue-me", delivery="queue", target="opus")

    solver = SimpleNamespace(
        message_bus=None,
        runner_id="claude-sdk/opus",
        model_spec="claude-sdk/opus",
        agent_name="chal/opus",
    )

    async def _run():
        notes = await claim_soft_steer_notes(solver)
        assert notes == ["steer-me"]
        left = await drain_operator_notes_to_bus(
            None, delivery="queue", broadcast=False, claimer="opus"
        )
        assert left == ["queue-me"]

    asyncio.run(_run())
