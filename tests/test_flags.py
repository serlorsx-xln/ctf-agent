"""Regression tests for local flag acceptance (N-required)."""

from __future__ import annotations

from pathlib import Path

from backend.flags import (
    accept_flag,
    collect_artifact_flag_candidates,
    is_complete_accept_message,
    is_counted_accept_message,
    is_decoy_flag,
    is_filename_like_flag_token,
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


def test_reject_flag_hex_filename_token() -> None:
    leakme_name = "flag_166903c90eadca6ffac515cd8a6787f2"
    assert is_filename_like_flag_token(leakme_name)
    msg, done = accept_flag(leakme_name)
    assert not done
    assert msg.startswith("REJECTED")
    assert "artifact" in msg.lower() or "does not look" in msg.lower()


def test_reject_leakme_local_decoy_body() -> None:
    assert is_decoy_flag("THIE_IS_TEST_FLAG")
    msg, done = accept_flag("THIE_IS_TEST_FLAG")
    assert not done
    assert msg.startswith("REJECTED")


def test_real_brace_flag_still_ok() -> None:
    msg, done = accept_flag("ARCHA{s3cr37_sh0p_n07_s0_s3cr378144c2cb}")
    assert done
    assert msg.startswith("CORRECT")


def test_local_test_in_brace_body_not_substring_decoy() -> None:
    # Must not reject real/local brace flags that merely contain "test_flag" text
    msg, done = accept_flag("BZHCTF{local_test_flag_please_find_me}")
    assert done
    assert msg.startswith("CORRECT")


def test_dockerfile_env_flag_filename_rejected(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu\nENV FLAG flag_166903c90eadca6ffac515cd8a6787f2\n",
        encoding="utf-8",
    )
    arts = collect_artifact_flag_candidates(tmp_path)
    assert "flag_166903c90eadca6ffac515cd8a6787f2" in arts
    msg, done = accept_flag(
        "flag_166903c90eadca6ffac515cd8a6787f2",
        challenge_dir=tmp_path,
    )
    assert not done
    assert msg.startswith("REJECTED")


def test_dockerfile_env_brace_flag_not_artifact_blocked(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        'FROM ubuntu\nENV FLAG="CTF{intentional_local_ok}"\n',
        encoding="utf-8",
    )
    arts = collect_artifact_flag_candidates(tmp_path)
    assert "CTF{intentional_local_ok}" not in arts
    msg, done = accept_flag("CTF{intentional_local_ok}", challenge_dir=tmp_path)
    assert done
    assert msg.startswith("CORRECT")


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
