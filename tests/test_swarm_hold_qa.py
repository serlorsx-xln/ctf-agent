"""Hold Q&A must honor Esc/`/stop` during an in-flight ``qa_turn``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.message_bus import ChallengeMessageBus
from backend.prompts import ChallengeMeta


def _bare_swarm(tmp_path) -> ChallengeSwarm:
    """Build a ChallengeSwarm without ``__post_init__`` Docker preflight."""
    swarm = object.__new__(ChallengeSwarm)
    swarm.challenge_dir = str(tmp_path)
    swarm.meta = ChallengeMeta(name="demo", description="d")
    swarm.cost_tracker = MagicMock()
    swarm.settings = MagicMock()
    swarm.model_specs = ["cursor/default"]
    swarm.cancel_event = asyncio.Event()
    swarm.release_event = asyncio.Event()
    swarm.hold_active = False
    swarm.message_bus = ChallengeMessageBus()
    return swarm


@pytest.mark.asyncio
async def test_hold_releases_during_slow_qa_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_SOLVER_HOLD", raising=False)
    swarm = _bare_swarm(tmp_path)
    started = asyncio.Event()

    async def slow_qa(question: str) -> str:
        started.set()
        await asyncio.sleep(60)
        return "should not land"

    calls = {"n": 0}

    async def fake_drain(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return ["what was the key step?"]
        return []

    printed: list[str] = []
    monkeypatch.setattr(
        "backend.operator_inbox.drain_operator_notes_to_bus",
        fake_drain,
    )
    monkeypatch.setattr(
        "backend.agents.live_log.emit_line",
        lambda text: printed.append(str(text)),
    )

    solver = SimpleNamespace(
        runner_id="cursor/default",
        model_spec="cursor/default",
        qa_turn=slow_qa,
    )

    async def release_soon() -> None:
        await started.wait()
        await asyncio.sleep(0.05)
        swarm.release_event.set()

    releaser = asyncio.create_task(release_soon())
    try:
        await asyncio.wait_for(swarm._winner_qa_hold(solver, "cursor/default"), timeout=5.0)
    finally:
        swarm.release_event.set()
        releaser.cancel()
        try:
            await releaser
        except asyncio.CancelledError:
            pass

    out = "\n".join(printed)
    assert calls["n"] >= 1
    assert started.is_set()
    assert "hold released" in out
    assert "(hold released)" in out
    assert swarm.hold_active is False


@pytest.mark.asyncio
async def test_hold_requeues_unanswered_notes_on_release(tmp_path, monkeypatch):
    """Release mid-batch must put leftover drained notes back in the inbox."""
    monkeypatch.delenv("ARTEMIS_SKIP_SOLVER_HOLD", raising=False)
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    from backend.operator_inbox import clear_operator_inbox, drain_operator_notes_to_bus

    clear_operator_inbox()
    swarm = _bare_swarm(tmp_path)
    started = asyncio.Event()

    async def slow_qa(question: str) -> str:
        started.set()
        await asyncio.sleep(60)
        return "should not land"

    calls = {"n": 0}

    async def fake_drain(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return ["q1 first", "q2 leftover"]
        return []

    printed: list[str] = []
    monkeypatch.setattr(
        "backend.operator_inbox.drain_operator_notes_to_bus",
        fake_drain,
    )
    monkeypatch.setattr(
        "backend.agents.live_log.emit_line",
        lambda text: printed.append(str(text)),
    )

    solver = SimpleNamespace(
        runner_id="cursor/default",
        model_spec="cursor/default",
        qa_turn=slow_qa,
    )

    async def release_soon() -> None:
        await started.wait()
        await asyncio.sleep(0.05)
        swarm.release_event.set()

    releaser = asyncio.create_task(release_soon())
    try:
        await asyncio.wait_for(swarm._winner_qa_hold(solver, "cursor/default"), timeout=5.0)
    finally:
        swarm.release_event.set()
        releaser.cancel()
        try:
            await releaser
        except asyncio.CancelledError:
            pass

    # Current note was aborted; leftover batch must be claimable again as queue.
    left = await drain_operator_notes_to_bus(
        None, delivery="queue", broadcast=False, claimer="default"
    )
    assert left == ["q1 first", "q2 leftover"]
    assert "hold released" in "\n".join(printed)


@pytest.mark.asyncio
async def test_hold_does_not_requeue_answered_note_on_release(tmp_path, monkeypatch):
    """Esc after a successful qa_turn must not re-inject that question."""
    monkeypatch.delenv("ARTEMIS_SKIP_SOLVER_HOLD", raising=False)
    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    from backend.operator_inbox import clear_operator_inbox, drain_operator_notes_to_bus

    clear_operator_inbox()
    swarm = _bare_swarm(tmp_path)
    answered = asyncio.Event()

    async def fast_qa(question: str) -> str:
        answered.set()
        return f"answer for {question}"

    calls = {"n": 0}

    async def fake_drain(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return ["q1 answered", "q2 leftover"]
        return []

    printed: list[str] = []

    def on_emit(text: str) -> None:
        printed.append(str(text))
        # Esc after the answer is on the feed (not mid-qa abort).
        if "answer for q1 answered" in str(text):
            swarm.release_event.set()

    monkeypatch.setattr(
        "backend.operator_inbox.drain_operator_notes_to_bus",
        fake_drain,
    )
    monkeypatch.setattr(
        "backend.agents.live_log.emit_line",
        on_emit,
    )

    solver = SimpleNamespace(
        runner_id="cursor/default",
        model_spec="cursor/default",
        qa_turn=fast_qa,
    )

    await asyncio.wait_for(swarm._winner_qa_hold(solver, "cursor/default"), timeout=5.0)
    assert answered.is_set()

    left = await drain_operator_notes_to_bus(
        None, delivery="queue", broadcast=False, claimer="default"
    )
    assert left == ["q2 leftover"]
    assert "answer for q1 answered" in "\n".join(printed)
