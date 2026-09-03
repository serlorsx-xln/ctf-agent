"""Operator inbox → message bus drain (mid-solve chat path)."""

import asyncio
from pathlib import Path

from backend.message_bus import ChallengeMessageBus
from backend.operator_inbox import (
    append_operator_note,
    clear_operator_inbox,
    drain_operator_notes_to_bus,
)


def test_append_and_drain(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("try the /admin path", session_id="s1")
    append_operator_note("ignore decoy flags", session_id="s1")

    bus = ChallengeMessageBus()

    async def _run():
        texts = await drain_operator_notes_to_bus(bus, session_id="s1")
        assert texts == ["try the /admin path", "ignore decoy flags"]
        # Second drain is a no-op.
        assert await drain_operator_notes_to_bus(bus, session_id="s1") == []
        unread = await bus.check("cursor/composer-2.5")
        assert len(unread) == 2
        assert "Operator note:" in unread[0].content

    asyncio.run(_run())


def test_steer_leaves_queue_for_idle_drain(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("do now", session_id="s1", delivery="steer")
    append_operator_note("do later", session_id="s1", delivery="queue")

    bus = ChallengeMessageBus()

    async def _run():
        steered = await drain_operator_notes_to_bus(
            bus, session_id="s1", delivery="steer"
        )
        assert steered == ["do now"]
        queued = await drain_operator_notes_to_bus(
            bus, session_id="s1", delivery="queue"
        )
        assert queued == ["do later"]
        assert await drain_operator_notes_to_bus(bus, session_id="s1", delivery=None) == []

    asyncio.run(_run())


def test_idle_drain_takes_both(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("a", delivery="steer")
    append_operator_note("b", delivery="queue")
    bus = ChallengeMessageBus()

    async def _run():
        texts = await drain_operator_notes_to_bus(bus, delivery=None)
        assert texts == ["a", "b"]

    asyncio.run(_run())


def test_concurrent_append_during_drain(tmp_path: Path, monkeypatch):
    """flock + rewrite must not drop a note appended mid-drain."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("first", delivery="steer")
    bus = ChallengeMessageBus()

    async def _run():
        # Simulate append racing the rewrite window by injecting after read in drain
        # is hard; instead append then drain twice — second note must survive.
        append_operator_note("second", delivery="queue")
        steered = await drain_operator_notes_to_bus(bus, delivery="steer")
        assert steered == ["first"]
        queued = await drain_operator_notes_to_bus(bus, delivery="queue")
        assert queued == ["second"]

    asyncio.run(_run())


def test_corrupt_jsonl_preserved_on_drain(tmp_path: Path, monkeypatch):
    """Bad inbox lines must survive drain — not be silently deleted."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    from backend.operator_inbox import operator_inbox_path

    clear_operator_inbox()
    append_operator_note("good note", delivery="queue")
    path = operator_inbox_path(None)
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    bus = ChallengeMessageBus()

    async def _run():
        texts = await drain_operator_notes_to_bus(bus, delivery="queue")
        assert texts == ["good note"]
        raw = path.read_text(encoding="utf-8")
        assert "{not valid json" in raw

    asyncio.run(_run())


def test_drain_broadcast_false_skips_bus(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    clear_operator_inbox()
    append_operator_note("force me", delivery="steer")
    bus = ChallengeMessageBus()

    async def _run():
        texts = await drain_operator_notes_to_bus(
            bus, delivery="steer", broadcast=False
        )
        assert texts == ["force me"]
        assert await bus.check("cursor/x") == []

    asyncio.run(_run())


def test_append_rejects_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    import pytest

    with pytest.raises(ValueError):
        append_operator_note("   ")
