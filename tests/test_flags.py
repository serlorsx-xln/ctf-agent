"""Regression tests for local flag acceptance (human-confirmed, N-required)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.flags import (
    accept_flag,
    collect_artifact_flag_candidates,
    is_counted_accept_message,
    is_decoy_flag,
    is_filename_like_flag_token,
)


def test_unconfirmed_is_candidate_not_correct() -> None:
    msg, done = accept_flag("CTF{hello_world_ok}")
    assert not done
    assert msg.startswith("CANDIDATE")
    assert not is_counted_accept_message(msg)


def test_confirm_in_progress_mutes_cli_not_tui(monkeypatch) -> None:
    """Sibling live-logs must keep flowing while the TUI confirm bar is up."""
    import backend.flags as flags

    monkeypatch.delenv("ARTEMIS_FLAG_CONFIRM", raising=False)
    flags._confirm_active = True
    assert flags.confirm_in_progress() is True
    monkeypatch.setenv("ARTEMIS_FLAG_CONFIRM", "1")
    assert flags.confirm_in_progress() is False
    flags._confirm_active = False


def test_confirmed_one_flag_correct() -> None:
    msg, done = accept_flag("CTF{hello_world_ok}", human_confirmed=True)
    assert done
    assert msg.startswith("CORRECT")
    assert is_counted_accept_message(msg)


def test_formatless_and_spaces_ok_when_confirmed() -> None:
    msg, done = accept_flag("hello world", human_confirmed=True)
    assert done
    assert msg.startswith("CORRECT")
    hex32 = "2475be69d40e815588a85fd89c7a439d"
    msg2, done2 = accept_flag(hex32, human_confirmed=True)
    assert done2
    assert msg2.startswith("CORRECT")


def test_multi_flag_partial_then_complete() -> None:
    m1, d1 = accept_flag("CTF{user_aaaaaaaa}", required=2, human_confirmed=True)
    assert not d1
    assert m1.startswith("ACCEPTED")
    assert is_counted_accept_message(m1)
    assert "CORRECT" not in m1

    m2, d2 = accept_flag(
        "CTF{root_bbbbbbbb}",
        already_accepted=["CTF{user_aaaaaaaa}"],
        required=2,
        human_confirmed=True,
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
    assert not is_counted_accept_message(msg)


def test_accepted_message_does_not_substring_match_correct() -> None:
    msg, done = accept_flag("CTF{user_aaaaaaaa}", required=2, human_confirmed=True)
    assert not done
    assert "CORRECT" not in msg


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


def test_reject_decoy_body_wrap() -> None:
    msg, done = accept_flag("v1t{fake_flag}")
    assert not done
    assert msg.startswith("REJECTED")
    assert "decoy" in msg.lower() or "placeholder" in msg.lower()


def test_reject_artifact_wrap(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu\nENV FLAG flag_166903c90eadca6ffac515cd8a6787f2\n",
        encoding="utf-8",
    )
    msg, done = accept_flag(
        "v1t{flag_166903c90eadca6ffac515cd8a6787f2}",
        challenge_dir=tmp_path,
    )
    assert not done
    assert msg.startswith("REJECTED")


def test_rewrap_of_tried_token() -> None:
    from backend.flags import is_rewrap_of_tried

    assert is_rewrap_of_tried("v1t{70aa2d5aeeb7d45d}", ["70aa2d5aeeb7d45d"]) == "70aa2d5aeeb7d45d"
    assert is_rewrap_of_tried("70aa2d5aeeb7d45d", ["v1t{70aa2d5aeeb7d45d}"]) == (
        "v1t{70aa2d5aeeb7d45d}"
    )
    assert is_rewrap_of_tried("v1t{totally_new}", ["70aa2d5aeeb7d45d"]) is None


def test_real_brace_flag_still_ok() -> None:
    msg, done = accept_flag(
        "ARCHA{s3cr37_sh0p_n07_s0_s3cr378144c2cb}",
        human_confirmed=True,
    )
    assert done
    assert msg.startswith("CORRECT")


def test_local_test_in_brace_body_not_substring_decoy() -> None:
    # Must not reject real/local brace flags that merely contain "test_flag" text
    msg, done = accept_flag(
        "BZHCTF{local_test_flag_please_find_me}",
        human_confirmed=True,
    )
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
    msg, done = accept_flag(
        "CTF{intentional_local_ok}",
        challenge_dir=tmp_path,
        human_confirmed=True,
    )
    assert done
    assert msg.startswith("CORRECT")


def test_do_submit_flag_prompts_then_accepts() -> None:
    from backend.tools.core import do_submit_flag

    async def _run() -> None:
        msg, done = await do_submit_flag(
            "chal",
            "hello world",
            confirm_fn=lambda _f: True,
        )
        assert done
        assert msg.startswith("CORRECT")

        msg2, done2 = await do_submit_flag(
            "chal",
            "hello world",
            confirm_fn=lambda _f: False,
        )
        assert not done2
        assert msg2.startswith("REJECTED by operator")

    asyncio.run(_run())


def test_do_submit_flag_already_solved_not_complete() -> None:
    """Quota met — do_submit_flag must not propagate challenge_complete=True."""
    from backend.tools.core import do_submit_flag

    async def _run() -> None:
        msg, done = await do_submit_flag(
            "chal",
            "CTF{extra_cccccccc}",
            already_accepted=["CTF{user_aaaaaaaa}", "CTF{root_bbbbbbbb}"],
            required=2,
        )
        assert msg.startswith("ALREADY SOLVED")
        assert done is False

    asyncio.run(_run())


def test_coordinator_no_swarm_multi_flag_progress() -> None:
    from types import SimpleNamespace

    from backend.agents.coordinator_core import _solved_names, do_submit_flag
    from backend.cost_tracker import CostTracker
    from backend.deps import CoordinatorDeps
    from backend.prompts import ChallengeMeta

    async def _run() -> None:
        deps = CoordinatorDeps(
            cost_tracker=CostTracker(),
            settings=SimpleNamespace(auto_confirm_flags=True),
        )
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


def test_tui_flag_confirm_handshake(tmp_path, monkeypatch) -> None:
    """TUI path: pending file + answer file, no stdin."""
    import json
    import threading
    import time

    from backend.flags import prompt_flag_confirmation

    cache = tmp_path / "artemis-cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_FLAG_CONFIRM", "1")
    monkeypatch.delenv("CTF_AUTO_CONFIRM_FLAGS", raising=False)

    def answerer() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = list(cache.glob("flag-confirm-*.pending.json"))
            if pending:
                data = json.loads(pending[0].read_text(encoding="utf-8"))
                req_id = data["id"]
                answer = cache / f"flag-confirm-{req_id}.answer.json"
                answer.write_text(json.dumps({"ok": True, "id": req_id}), encoding="utf-8")
                return
            time.sleep(0.05)

    t = threading.Thread(target=answerer, daemon=True)
    t.start()
    ok = prompt_flag_confirmation("CTF{tui_handshake}")
    t.join(timeout=2)
    assert ok is True


def test_clear_tui_handshakes(tmp_path, monkeypatch) -> None:
    from backend.shell import sandbox_session as ss

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    (tmp_path / "flag-confirm-abc.pending.json").write_text("{}", encoding="utf-8")
    (tmp_path / "flag-confirm-abc.answer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "flags-ask-xyz.pending.json").write_text("{}", encoding="utf-8")
    ss.clear_tui_handshakes()
    assert list(tmp_path.glob("flag-confirm-*")) == []
    assert list(tmp_path.glob("flags-ask-*")) == []


def test_tui_flag_confirm_ignores_env_auto_confirm(tmp_path, monkeypatch) -> None:
    """ARTEMIS_FLAG_CONFIRM wins over CTF_AUTO_CONFIRM_FLAGS for TUI dialogs."""
    import json
    import threading
    import time

    from backend.flags import prompt_flag_confirmation

    cache = tmp_path / "artemis-cache"
    cache.mkdir()
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_FLAG_CONFIRM", "1")
    monkeypatch.setenv("CTF_AUTO_CONFIRM_FLAGS", "1")

    def answerer() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = list(cache.glob("flag-confirm-*.pending.json"))
            if pending:
                data = json.loads(pending[0].read_text(encoding="utf-8"))
                req_id = data["id"]
                answer = cache / f"flag-confirm-{req_id}.answer.json"
                answer.write_text(json.dumps({"ok": False, "id": req_id}), encoding="utf-8")
                return
            time.sleep(0.05)

    t = threading.Thread(target=answerer, daemon=True)
    t.start()
    ok = prompt_flag_confirmation("CTF{should_use_dialog}")
    t.join(timeout=2)
    assert ok is False  # dialog rejected — not env auto-confirm
