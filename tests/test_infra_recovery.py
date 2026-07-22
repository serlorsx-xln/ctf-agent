"""Infra error detection + continue-prompt recovery copy."""

from __future__ import annotations

from backend.agents.cursor_runtime import is_infra_error_message
from backend.continue_prompt import INFRA_RECOVERY_BLURB, build_continue_prompt
from backend.solver_base import INFRA_ERROR


def test_is_infra_error_bridge_timeout():
    assert is_infra_error_message("Bridge request timed out: ReadTimeout: ")
    assert is_infra_error_message("internal: internal error")
    assert is_infra_error_message("Infra: Bridge request timed out: ReadTimeout: ")


def test_is_infra_error_opaque_cursor_status():
    assert is_infra_error_message("error")
    assert is_infra_error_message("Error")
    assert is_infra_error_message("run error")
    assert is_infra_error_message("  FAILED  ")
    assert is_infra_error_message("unknown error")


def test_is_infra_error_rejects_normal():
    assert not is_infra_error_message("invalid flag")
    assert not is_infra_error_message("Loop detected")
    assert not is_infra_error_message("segmentation fault in exploit")
    assert not is_infra_error_message("")
    assert not is_infra_error_message(None)


def test_infra_error_constant():
    assert INFRA_ERROR == "infra_error"


def test_continue_prompt_infra_recovery():
    text = build_continue_prompt(infra_recovery=True)
    assert INFRA_RECOVERY_BLURB.split(".")[0] in text
    assert "/challenge/workspace" in text
    assert "timeout_seconds" in text


def test_continue_prompt_no_infra_by_default():
    text = build_continue_prompt()
    assert "transport/bridge timeout" not in text
