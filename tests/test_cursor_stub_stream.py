"""Cursor TUI proxy — flexible single-message CTF flow."""

from __future__ import annotations

import json

from backend.shell import cursor_llm_stub as stub


def test_wants_stream() -> None:
    assert stub._wants_stream({"stream": True}, {}) is True
    assert stub._wants_stream({}, {"Accept": "text/event-stream"}) is True
    assert stub._wants_stream({"stream": False}, {}) is False


def test_greeting_is_fast_guidance(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
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


def test_path_emits_load_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "./challenges/baby-crypto"}],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "./challenges/baby-crypto"


def test_paste_emits_load_with_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    paste = (
        "Cool Web Chal\n\n"
        "Connect: https://chal.example.com:8443/\n\n"
        "Find the flag.\n"
    )
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
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert "prompt" in args
    assert "Cool Web Chal" in args["prompt"]


def test_no_instruction_just_path_loads(monkeypatch) -> None:
    """A bare path with no instruction still loads."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "/Users/x/challenges/network"}],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"


def test_path_plus_instruction_loads_with_prompt(monkeypatch) -> None:
    """Path + 'find flag' in one message → load with path + prompt."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "/Users/x/network find flag"}],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "/Users/x/network"
    assert args.get("prompt") == "find flag"


def test_multi_path_emits_load_with_attachments(monkeypatch) -> None:
    """Multiple paths in one message → first is path, rest are attachments."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": "/Users/x/net /Users/y/capture.pcap solve this",
                }
            ],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "/Users/x/net"
    assert args["attachments"] == ["/Users/y/capture.pcap"]
    assert args.get("prompt") == "solve this"


def test_continuation_after_load_advances_to_ask_flags(monkeypatch) -> None:
    """Continuation right after artemis_load_challenge → ask_flags (NOT "ready").

    This is the fix for the bug where "ready" killed the flow and the flag
    dialog never appeared."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": "/chal", "challenge_name": "baby"},
    )
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
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_ask_flags"


def test_continuation_after_failed_load_does_not_ask_flags(monkeypatch) -> None:
    """Bad path load must not open the Flags dialog."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {"role": "user", "content": "/Users/x/Downloads/pwnknig"},
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
                {
                    "role": "tool",
                    "tool_call_id": "call_load",
                    "content": "ERROR: not a directory: /Users/x/Downloads/pwnknig",
                },
            ],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert not msg.get("tool_calls")
    assert msg.get("content") == ""


def test_continuation_after_swarm_stops_no_ready(monkeypatch) -> None:
    """Continuation after ask_flags/swarm → empty stop, NOT "ready" text."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": "/chal", "challenge_name": "baby"},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {"role": "user", "content": "find flag"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_ask",
                            "type": "function",
                            "function": {"name": "artemis_ask_flags", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_ask", "content": "Swarm started"},
            ],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    # No tool call (don't re-swarm), no "ready" text — just an empty stop.
    assert msg.get("tool_calls") is None
    assert msg.get("content") in ("", None)
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_loaded_fresh_turn_always_asks_flags(monkeypatch) -> None:
    """Challenge loaded + fresh non-greeting turn → ALWAYS ask_flags (no keyword gate)."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": "/chal", "challenge_name": "baby"},
    )
    # "find flag" — previously rejected by the solve-intent keyword gate.
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "find flag"}],
        },
        {"x-artemis-session-id": "ses_test"},
    )
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_ask_flags"


def test_loaded_short_instruction_proceeds(monkeypatch) -> None:
    """Short instructions like 'go'/'ok' proceed (no len<=3 trivial block)."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": "/chal", "challenge_name": "baby"},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "go"}],
        },
        {"x-artemis-session-id": "ses_test"},
    )
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_ask_flags"


def test_fresh_go_after_failed_reload_does_not_ask_flags(monkeypatch) -> None:
    """Prior challenge_dir + failed reload in history → 'go' must not open Flags for old chal."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": "/chal/good", "challenge_name": "good"},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {"role": "user", "content": "/Users/x/Downloads/good"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_load_ok",
                            "type": "function",
                            "function": {
                                "name": "artemis_load_challenge",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_load_ok",
                    "content": "Loaded challenge 'good' at /chal/good",
                },
                {"role": "user", "content": "/Users/x/Downloads/pwnknig"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_load_bad",
                            "type": "function",
                            "function": {
                                "name": "artemis_load_challenge",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_load_bad",
                    "content": "ERROR: path not found: /Users/x/Downloads/pwnknig",
                },
                {"role": "user", "content": "go"},
            ],
        },
        {},
    )
    assert kind == "json"
    msg = payload["choices"][0]["message"]
    assert not msg.get("tool_calls")
    assert "Load failed" in (msg.get("content") or "")


def test_same_path_fresh_turn_reloads(monkeypatch) -> None:
    """Re-paste of the same folder on a fresh turn → reload."""
    chal = "/Users/serlorsx/Downloads/ctf/challenges/tctt-junior-cipher-puzzle"
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *_a, **_k: {"challenge_dir": chal, "challenge_name": "tctt-junior-cipher-puzzle"},
    )
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [
                {"role": "user", "content": f"{chal} please find flag don't cheating"}
            ],
        },
        {},
    )
    msg = payload["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"


def test_ssh_paste_loads_despite_stale_default_challenge(monkeypatch) -> None:
    """Text+ssh paste must load even when _default still has another challenge."""
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda *a, **k: {
            "challenge_dir": "/Users/x/old-glass",
            "challenge_name": "old-glass",
        },
    )
    paste = (
        "This is a simple CRC calculator for kernel module programming exercise.\n"
        "I bet there are no bugs.\n"
        "but you can check it if you want.\n\n"
        "ssh kcrc@pwnable.kr -p2222 (pw: guest)\n"
    )
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
    assert msg["tool_calls"][0]["function"]["name"] == "artemis_load_challenge"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert "ssh kcrc@pwnable.kr" in args["prompt"]


def test_session_header_uses_that_session_state(monkeypatch) -> None:
    """X-Artemis-Session-Id must not fall through to another session's challenge."""

    def _load(sid=None):
        if sid == "ses_new":
            return {}
        return {"challenge_dir": "/old", "challenge_name": "old"}

    monkeypatch.setattr("backend.shell.sandbox_session.load_session_state", _load)
    # Empty session + short non-challenge text → guidance (not ask_flags for /old)
    kind, payload = stub._handle_chat(
        {
            "model": "default",
            "stream": False,
            "messages": [{"role": "user", "content": "hello there friend"}],
        },
        {"X-Artemis-Session-Id": "ses_new"},
    )
    msg = payload["choices"][0]["message"]
    assert msg.get("tool_calls") is None
    assert "challenge" in (msg.get("content") or "").lower()
