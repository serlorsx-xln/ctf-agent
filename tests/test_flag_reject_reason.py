"""The operator's rejection reason reaching the solver.

A bare "no" leaves the solver unable to tell a wrong flag from a wrong
technique, so it resubmits variants or starts doubting the checker. These tests
pin the whole path: TUI answer → daemon → confirm result → tool message.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.flags import _emit_confirm_verdict, normalize_confirm
from backend.tools.core import do_submit_flag


def test_reject_verdict_does_not_emit_a_second_banner(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("backend.flags._emit_line", lines.append)
    _emit_confirm_verdict(False)
    assert lines == []
    _emit_confirm_verdict(True)
    assert lines == [">>> Confirmed — counting this flag.\n"]


def test_normalize_confirm_accepts_both_shapes():
    # Confirm callables predate the reason channel; bare bools must keep working.
    assert normalize_confirm(True) == (True, "")
    assert normalize_confirm(False) == (False, "")
    assert normalize_confirm((True, "")) == (True, "")
    assert normalize_confirm((False, "that's the placeholder")) == (
        False,
        "that's the placeholder",
    )
    # Defensive shapes seen from cancelled/timed-out dialogs.
    assert normalize_confirm(None) == (False, "")
    assert normalize_confirm(()) == (False, "")
    assert normalize_confirm((False, None)) == (False, "")
    assert normalize_confirm((False, "  padded  ")) == (False, "padded")


@pytest.mark.asyncio
async def test_rejection_relays_the_reason_to_the_agent():
    msg, done = await do_submit_flag(
        "chal",
        "flag{s33d_1s_n0t_th3_4nsw3r}",
        confirm_fn=lambda _f: (False, "that's the constant in chall.py, not the real flag"),
    )
    assert not done
    assert msg.startswith("REJECTED by operator")
    assert "Operator says: that's the constant in chall.py, not the real flag" in msg
    assert "Continue hunting" in msg


@pytest.mark.asyncio
async def test_rejection_without_a_reason_keeps_the_old_message():
    msg, done = await do_submit_flag("chal", "hello world", confirm_fn=lambda _f: False)
    assert not done
    assert msg == 'REJECTED by operator — "hello world" not confirmed. Continue hunting.'


@pytest.mark.asyncio
async def test_reason_is_ignored_on_accept():
    msg, done = await do_submit_flag(
        "chal", "hello world", confirm_fn=lambda _f: (True, "looks right")
    )
    assert done
    assert msg.startswith("CORRECT")
    assert "looks right" not in msg


@pytest.mark.asyncio
async def test_bare_bool_confirm_still_accepts():
    msg, done = await do_submit_flag("chal", "hello world", confirm_fn=lambda _f: True)
    assert done
    assert msg.startswith("CORRECT")


def test_daemon_handler_forwards_the_reason_to_the_waiting_solver():
    from backend.daemon import handlers as handlers_mod

    async def _run() -> None:
        handler = handlers_mod.Handlers.__new__(handlers_mod.Handlers)
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        handlers_mod.register_dialog("rid-1", fut)
        ack = await handler._h_flag_confirm_answer(
            {"request_id": "rid-1", "ok": False, "reason": "  placeholder  "},
            session=None,
        )
        assert ack == {"ok": True}
        assert await fut == {"ok": False, "reason": "placeholder"}

    asyncio.run(_run())


def test_daemon_handler_defaults_the_reason_to_empty():
    from backend.daemon import handlers as handlers_mod

    async def _run() -> None:
        handler = handlers_mod.Handlers.__new__(handlers_mod.Handlers)
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        handlers_mod.register_dialog("rid-2", fut)
        await handler._h_flag_confirm_answer({"request_id": "rid-2", "ok": True}, session=None)
        assert await fut == {"ok": True, "reason": ""}

    asyncio.run(_run())


def test_daemon_handler_caps_a_pasted_essay():
    from backend.daemon import handlers as handlers_mod

    async def _run() -> None:
        handler = handlers_mod.Handlers.__new__(handlers_mod.Handlers)
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        handlers_mod.register_dialog("rid-3", fut)
        await handler._h_flag_confirm_answer(
            {"request_id": "rid-3", "ok": False, "reason": "x" * 5000}, session=None
        )
        result = await fut
        assert len(result["reason"]) == 400

    asyncio.run(_run())
