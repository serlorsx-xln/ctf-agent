"""Cursor TUI proxy — catalog + empty chat. TUI owns load/gate."""

from __future__ import annotations

from backend.shell import cursor_llm_stub as stub


def test_wants_stream() -> None:
    assert stub._wants_stream({"stream": True}, {}) is True
    assert stub._wants_stream({}, {"Accept": "text/event-stream"}) is True
    assert stub._wants_stream({"stream": False}, {}) is False


def test_greeting_is_fast_guidance() -> None:
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "สวัสดี"}],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None
    assert "challenge" in (msg.get("content") or "").lower()


def test_path_does_not_emit_tools() -> None:
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "./challenges/baby-crypto"}],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_paste_does_not_emit_tools() -> None:
    paste = "Cool Web Chal\n\nConnect: https://chal.example.com:8443/\n\nFind the flag.\n"
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": paste}],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None


def test_loaded_fresh_turn_does_not_ask_flags() -> None:
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "find flag"}],
        },
        {"x-artemis-session-id": "ses_test"},
    )
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None


def test_continuation_after_tool_is_empty_stop() -> None:
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {"role": "user", "content": "/chal/baby"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_load",
                            "type": "function",
                            "function": {"name": "artemis_load_challenge", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_load", "content": "Loaded"},
            ],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None
    assert msg.get("content") in ("", None)
    assert payload["choices"][0]["finish_reason"] == "stop"
