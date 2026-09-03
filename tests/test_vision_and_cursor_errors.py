"""Vision gate + Cursor error formatting."""

from __future__ import annotations

from backend.agents.cursor_runtime import format_cursor_run_error
from backend.models import supports_vision


def test_supports_vision_always_true() -> None:
    assert supports_vision("cursor/grok-4.5") is True
    assert supports_vision("cursor/composer-2.5") is True
    assert supports_vision("claude-sdk/claude-opus-4-6") is True
    assert supports_vision("codex/gpt-5.4") is True


def test_format_cursor_run_error_prefers_status_detail() -> None:
    assert (
        format_cursor_run_error(
            result_text="run error",
            status_message="Bridge request timed out after 30s",
        )
        == "Bridge request timed out after 30s"
    )


def test_format_cursor_run_error_keeps_real_result() -> None:
    assert (
        format_cursor_run_error(
            result_text="Bridge request timed out after 600s",
            status_message="",
        )
        == "Bridge request timed out after 600s"
    )


def test_format_cursor_run_error_empty() -> None:
    assert format_cursor_run_error() == "run error (no detail from Cursor SDK)"


def test_humanize_cursor_usage_limit() -> None:
    from backend.agents.cursor_runtime import humanize_cursor_error

    raw = (
        "You've hit your usage limit You've saved $2506 on API model usage this month "
        "with Ultra. Switch to a different model or set a Spend Limit to continue with Auto. "
        "Your usage limits will reset when your monthly cycle ends on 7/24/26"
    )
    out = humanize_cursor_error(raw)
    assert "usage limit" in out.lower()
    assert "$2506" not in out
    assert "Ultra" not in out
    assert "7/24/26" in out


def test_format_cursor_run_error_humanizes_quota() -> None:
    out = format_cursor_run_error(
        result_text="run error",
        status_message="You've hit your usage limit Switch to Auto. resets on 8/1/26",
    )
    assert "usage limit" in out.lower()
    assert "You've hit your usage limit Switch" not in out


def test_is_benign_cancel_error() -> None:
    from backend.agents.cursor_runtime import is_benign_cancel_error

    assert is_benign_cancel_error(
        "unsupported_run_operation: Run 'run-abc' is already in terminal status "
        "'cancelled'; cancel is not applicable."
    )
    assert is_benign_cancel_error(
        "Error: unsupported_run_operation: Run 'x' is already in terminal status 'cancelled'"
    )
    assert not is_benign_cancel_error("Bridge request timed out after 30s")
    assert not is_benign_cancel_error(None)
