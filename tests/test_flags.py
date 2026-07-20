"""Regression tests for local flag acceptance (N-required)."""

from __future__ import annotations

from backend.flags import (
    accept_flag,
    is_complete_accept_message,
    is_counted_accept_message,
    parse_flags_required,
)


def test_default_one_flag_correct() -> None:
    msg, done = accept_flag("CTF{hello_world_ok}")
    assert done
    assert msg.startswith("CORRECT")
    assert is_complete_accept_message(msg)
    assert is_counted_accept_message(msg)


def test_multi_flag_partial_then_complete() -> None:
    m1, d1 = accept_flag("CTF{user_aaaaaaaa}", required=2)
    assert not d1
    assert m1.startswith("ACCEPTED")
    assert is_counted_accept_message(m1)
    assert not is_complete_accept_message(m1)
    assert "CORRECT" not in m1

    m2, d2 = accept_flag(
        "CTF{root_bbbbbbbb}",
        already_accepted=["CTF{user_aaaaaaaa}"],
        required=2,
    )
    assert d2
    assert m2.startswith("CORRECT")


def test_already_solved() -> None:
    msg, done = accept_flag(
        "CTF{extra_cccccccc}",
        already_accepted=["CTF{user_aaaaaaaa}", "CTF{root_bbbbbbbb}"],
        required=2,
    )
    assert done
    assert msg.startswith("ALREADY SOLVED")
    assert is_complete_accept_message(msg)
    assert not is_counted_accept_message(msg)


def test_parse_flags_required() -> None:
    assert parse_flags_required("") == 1
    assert parse_flags_required("hello") == 1
    assert parse_flags_required("flags_required: 2\nfoo") == 2


def test_accepted_message_does_not_substring_match_correct() -> None:
    msg, done = accept_flag("CTF{user_aaaaaaaa}", required=2)
    assert not done
    assert "CORRECT" not in msg
    assert not is_complete_accept_message(msg)


def test_solved_names_ignores_incomplete_results() -> None:
    from types import SimpleNamespace

    from backend.agents.coordinator_core import _solved_names

    deps = SimpleNamespace(
        results={
            "partial": {"flag": "CTF{a}", "complete": False},
            "done": {"flag": "CTF{b}", "complete": True},
            "legacy": {"flag": "CTF{c}"},  # spawn path without complete key → solved
        }
    )
    assert _solved_names(deps) == {"done", "legacy"}  # type: ignore[arg-type]


def test_coordinator_no_swarm_multi_flag_progress() -> None:
    import asyncio
    from types import SimpleNamespace

    from backend.agents.coordinator_core import _solved_names, do_submit_flag
    from backend.cost_tracker import CostTracker
    from backend.deps import CoordinatorDeps
    from backend.prompts import ChallengeMeta

    async def _run() -> None:
        deps = CoordinatorDeps(cost_tracker=CostTracker(), settings=SimpleNamespace())
        deps.challenge_metas["ping"] = ChallengeMeta(name="ping", description="x", flags_required=2)
        msg = await do_submit_flag(deps, "ping", "CTF{user_aaaaaaaaaa}")
        assert msg.startswith("ACCEPTED")
        assert deps.results["ping"]["complete"] is False
        assert _solved_names(deps) == set()
        msg2 = await do_submit_flag(deps, "ping", "CTF{root_bbbbbbbbbb}")
        assert msg2.startswith("CORRECT")
        assert deps.results["ping"]["complete"] is True
        assert _solved_names(deps) == {"ping"}

    asyncio.run(_run())
