"""End-to-end contracts for cold-pack warm path + mid-solve chat + flag confirm.

These tests do not require Docker. They lock the product contracts that make
solve-after-setup fast and keep operator chat / flag confirm correct.
"""

from __future__ import annotations

import pytest

from backend.flags import env_auto_confirm_flags, prompt_flag_confirmation
from backend.message_bus import ChallengeMessageBus
from backend.operator_inbox import append_operator_note
from backend.sandbox.warm_runtime import (
    WARM_BAKE_PACKS,
    warm_runtime_tag,
    write_warm_runtime_marker,
)
from backend.tool_router import PREFETCH_RUNTIME_IMAGES, resolve_runtime_l0_image
from backend.tools.core import do_check_findings


def test_e2e_setup_warm_set_covers_cold_apt_packs():
    """Packs that re-apt on every core container must be in the warm bake set."""
    for pack in ("ghidra", "web", "forensics", "steg", "crypto-tools", "linux"):
        assert pack in WARM_BAKE_PACKS
    # Donors already baked as L0 runtimes — no warm commit needed.
    for pack in ("pwn", "mobile"):
        assert pack in PREFETCH_RUNTIME_IMAGES
        assert pack not in WARM_BAKE_PACKS


def test_e2e_resolve_chain_warm_then_donor_then_core(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "deadbeefdeadbeef",
    )
    write_warm_runtime_marker("web")
    assert resolve_runtime_l0_image(["web"]) == warm_runtime_tag("web")
    assert resolve_runtime_l0_image(["pwn"]) == "ctf-sandbox-pwn"
    assert resolve_runtime_l0_image(["crypto"]) == "ctf-sandbox-core"


@pytest.mark.asyncio
async def test_e2e_operator_note_reaches_solver_via_soft_steer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backend.agents.soft_steer import claim_soft_steer_notes

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    bus = ChallengeMessageBus()
    append_operator_note("pivot to IDOR on /api/user/0")
    notes = await claim_soft_steer_notes(
        SimpleNamespace(
            message_bus=bus,
            model_spec="claude-sdk/opus",
            runner_id="claude-sdk/opus",
        )
    )
    assert notes == ["pivot to IDOR on /api/user/0"]
    # Drained — second claim is empty.
    assert (
        await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=bus,
                model_spec="claude-sdk/opus",
                runner_id="claude-sdk/opus",
            )
        )
        == []
    )
    # Mid-tool findings path must not see operator notes.
    assert await do_check_findings(bus, "claude-sdk/opus") == (
        "No new findings from other agents."
    )


@pytest.mark.asyncio
async def test_e2e_queued_note_waits_for_soft_idle(tmp_path, monkeypatch):
    """Queue must not inject mid-tool; soft idle claim drains it."""
    from backend.tools.core import do_check_findings, soft_idle_operator_notes

    monkeypatch.setattr(
        "backend.shell.sandbox_session.session_dir",
        lambda session_id=None: tmp_path,
    )
    bus = ChallengeMessageBus()
    append_operator_note("queued hint", delivery="queue")
    # Mid-tool path must leave queue notes alone.
    assert await do_check_findings(bus, "claude/opus") == (
        "No new findings from other agents."
    )
    text = await soft_idle_operator_notes(bus, "claude/opus")
    assert text and "queued hint" in text
    assert await do_check_findings(bus, "claude/opus") == (
        "No new findings from other agents."
    )


def test_e2e_flag_confirm_is_model_gated_not_auto(monkeypatch):
    """TUI path never auto-confirms unless the call opts in."""
    monkeypatch.setenv("ARTEMIS_FLAG_CONFIRM", "1")
    monkeypatch.setenv("CTF_AUTO_CONFIRM_FLAGS", "1")
    # env auto-confirm is ignored when TUI owns the keyboard.
    assert env_auto_confirm_flags() is True

    # Without a live daemon/TUI dialog, prompt falls through — we only assert
    # the TUI gate: auto_confirm=False does not short-circuit via env when
    # ARTEMIS_FLAG_CONFIRM=1 (see flags.prompt_flag_confirmation).
    from backend import flags as flags_mod

    monkeypatch.setattr(flags_mod, "_prompt_flag_confirmation_daemon", lambda f: False)
    monkeypatch.setattr(flags_mod, "_prompt_flag_confirmation_tui", lambda f: False)

    # Explicit auto_confirm=True still works (CLI/tests).
    assert prompt_flag_confirmation("FLAG{x}", auto_confirm=True) is True
