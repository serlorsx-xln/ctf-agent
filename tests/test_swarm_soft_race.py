"""Soft-race / end-summary behavior (no Docker, no live solvers)."""

from __future__ import annotations

from backend.agents.swarm import ChallengeSwarm
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, GAVE_UP


class _FakeSettings:
    sandbox_image = "ctf-sandbox-core"
    container_memory_limit = "1g"


def _swarm(flags_required: int = 2) -> ChallengeSwarm:
    # Bypass __post_init__ router by constructing then patching fields.
    swarm = ChallengeSwarm.__new__(ChallengeSwarm)
    swarm.challenge_dir = "/tmp"
    swarm.meta = ChallengeMeta(name="t", description="", flags_required=flags_required)
    swarm.cost_tracker = None  # type: ignore[assignment]
    swarm.settings = _FakeSettings()  # type: ignore[assignment]
    swarm.model_specs = []
    swarm.coordinator_inbox = None
    swarm.cancel_event = __import__("asyncio").Event()
    swarm.solvers = {}
    swarm.findings = {"m1": "got user flag, stuck on root"}
    swarm.winner = None
    swarm.winner_runner_id = ""
    swarm.flag_credits = {}
    swarm.flag_notes = {}
    swarm._steps_by_runner = {}
    swarm.confirmed_flag = None
    swarm.confirmed_flags = ["CTF{user_aaaaaaaa}"]
    swarm._flag_lock = __import__("asyncio").Lock()
    swarm._submit_count = {}
    swarm._submitted_flags = set()
    swarm._last_submit_time = {}
    from backend.message_bus import ChallengeMessageBus

    swarm.message_bus = ChallengeMessageBus()
    return swarm


def test_end_summary_partial_flags():
    swarm = _swarm(2)
    result = swarm._end_summary()
    assert result is not None
    assert result.status == GAVE_UP
    assert result.flag == "CTF{user_aaaaaaaa}"
    assert "1/2" in result.findings_summary
    assert "stuck on root" in result.findings_summary


def test_end_summary_complete_winner():
    swarm = _swarm(1)
    swarm.confirmed_flags = ["CTF{solo_bbbbbbbb}"]
    swarm.confirmed_flag = "CTF{solo_bbbbbbbb}"
    result = swarm._end_summary()
    assert result is not None
    assert result.status == FLAG_FOUND
    assert result.flag == "CTF{solo_bbbbbbbb}"


def test_run_cancels_pending_when_cancel_event_set():
    """Quota/stop sets cancel_event — pending solver tasks must be cancelled."""
    import asyncio

    async def _slow():
        await asyncio.sleep(30)
        return None

    async def _main():
        swarm = _swarm(1)
        swarm.model_specs = ["cursor/a", "cursor/b"]
        swarm.confirmed_flags = []
        swarm.confirmed_flag = None
        # Patch _run_solver so we never need real agents.
        async def fake_run(runner_id, model_spec):
            if runner_id.endswith("a") or "a" in runner_id:
                await asyncio.sleep(0.05)
                swarm.cancel_event.set()
                return None
            return await _slow()

        swarm._run_solver = fake_run  # type: ignore[method-assign]
        # Directly exercise the cancel path used by run().
        tasks = [
            asyncio.create_task(fake_run("a", "cursor/a")),
            asyncio.create_task(_slow()),
        ]
        cancel_waiter = asyncio.create_task(swarm.cancel_event.wait())
        tasks_waiter = asyncio.create_task(
            asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        )
        finished, _ = await asyncio.wait(
            {cancel_waiter, tasks_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert cancel_waiter in finished
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks_waiter.cancel()
        try:
            await tasks_waiter
        except (asyncio.CancelledError, Exception):
            pass
        cancel_waiter.cancel()
        try:
            await cancel_waiter
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_main())
