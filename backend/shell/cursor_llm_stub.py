"""OpenAI-compatible front for Cursor in Artemis TUI.

Serves official model catalog + chat/completions SSE. Does NOT run Cursor
Agent SDK chat. Flexible single-message CTF flow:
  - fresh turn with any path(s)/paste → artemis_load_challenge
  - challenge loaded + fresh turn → artemis_ask_flags (ALWAYS — TUI digits
    dialog asks the flag count and starts the swarm)
  - agent-loop continuation → plain text (no re-fire)
  - greeting only → local guidance
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from backend.challenge_paste import (
    extract_challenge_paths,
    extract_paste_without_paths,
    is_greeting,
    looks_like_challenge_paste,
)

HOST = "127.0.0.1"
PORT = 18765
logger = logging.getLogger("cursor-proxy")

_PLACEHOLDER_MODELS = ("default", "auto")
_MODELS_CACHE: tuple[float, tuple[str, ...], str] | None = None
_MODELS_CACHE_TTL_S = 600.0


def _extract_cursor_model(body: dict) -> str:
    model = str(body.get("model") or "default")
    model = model.split("/", 1)[-1]
    return model or "default"


def _official_model_ids() -> list[str]:
    global _MODELS_CACHE
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not key:
        return list(_PLACEHOLDER_MODELS)
    now = time.time()
    if (
        _MODELS_CACHE
        and now - _MODELS_CACHE[0] < _MODELS_CACHE_TTL_S
        and _MODELS_CACHE[2] == key
    ):
        return list(_MODELS_CACHE[1])

    try:
        req = urllib.request.Request(
            "https://api.cursor.com/v1/models",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        items = payload.get("items") or []
        ids = [str(item.get("id")) for item in items if item.get("id")]
        if ids:
            _MODELS_CACHE = (now, tuple(ids), key)
            return ids
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.warning("Cursor Bearer models failed: %s", e)

    try:
        import base64

        basic = base64.b64encode(f"{key}:".encode()).decode()
        req = urllib.request.Request(
            "https://api.cursor.com/v1/models",
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        items = payload.get("items") or []
        ids = [str(item.get("id")) for item in items if item.get("id")]
        if ids:
            _MODELS_CACHE = (now, tuple(ids), key)
            return ids
    except Exception as e:  # noqa: BLE001
        logger.warning("Cursor Basic models failed: %s", e)

    try:
        from cursor_sdk import Cursor

        models = Cursor.models.list(api_key=key)
        ids = [str(getattr(m, "id", None) or "") for m in models]
        ids = [i for i in ids if i]
        if ids:
            _MODELS_CACHE = (now, tuple(ids), key)
            return ids
    except Exception as e:  # noqa: BLE001
        logger.warning("Cursor SDK models.list failed: %s", e)

    return list(_PLACEHOLDER_MODELS)


def _last_user_text(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts).strip()
    return ""


def _recent_assistant_tool(messages: object) -> str | None:
    """Most recent assistant tool_call name — used to advance the flow on
    continuation (after load → ask_flags; after ask/swarm → stop)."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        first = calls[0]
        if not isinstance(first, dict):
            continue
        fn = first.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
    return None


def _last_tool_result_text(messages: object) -> str:
    """Content of the most recent role=tool message (load success/ERROR)."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts).strip()
    return ""


def _load_challenge_failed(result: str) -> bool:
    """True when artemis_load_challenge returned an ERROR (bad path, etc.)."""
    t = (result or "").strip()
    if not t:
        return True
    if t.upper().startswith("ERROR"):
        return True
    if "not a directory" in t.lower():
        return True
    if "path not found" in t.lower():
        return True
    if "not a file or directory" in t.lower():
        return True
    return "path or prompt" in t.lower()


def _most_recent_load_result(messages: object) -> str | None:
    """Content of the latest artemis_load_challenge tool result in the transcript.

    Used on fresh turns so a failed reload (session still has an old
    challenge_dir) does not reopen ask_flags via 2d.
    """
    if not isinstance(messages, list):
        return None
    load_ids: set[str] = set()
    last: str | None = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if not isinstance(fn, dict) or fn.get("name") != "artemis_load_challenge":
                    continue
                cid = call.get("id")
                if isinstance(cid, str) and cid:
                    load_ids.add(cid)
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if not isinstance(tid, str) or tid not in load_ids:
                continue
            content = msg.get("content")
            if isinstance(content, str):
                last = content.strip()
            elif isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        parts.append(part)
                last = "\n".join(parts).strip()
    return last


def _in_tool_continuation(messages: object) -> bool:
    """True while the agent loop is continuing after a tool call (not a fresh user turn).

    Fresh user turns always end with role=user. Continuations end with tool/assistant
    tool_calls — those must not re-trigger load for the same paste.
    """
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict):
        return False
    role = last.get("role")
    if role == "tool":
        return True
    return bool(role == "assistant" and last.get("tool_calls"))




def _session_id_from_headers(headers: dict) -> str | None:
    """Per-request Artemis session (TUI window) — never fall back to env here."""
    for key, value in headers.items():
        if str(key).lower() in ("x-artemis-session-id", "x-opencode-session-id"):
            sid = str(value or "").strip()
            if sid:
                return sid
    return None


def _wants_stream(body: dict, headers: dict) -> bool:
    if body.get("stream") is True:
        return True
    accept = (headers.get("Accept") or headers.get("accept") or "").lower()
    return "text/event-stream" in accept


def _completion_payload(model: str, text: str, *, tool_call: dict | None = None) -> dict:
    if tool_call:
        message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"
    prompt_tokens = 32
    completion_tokens = max(16, len(text) // 4) if text else 16
    return {
        "id": "artemis-cursor-sdk",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse_text_chunks(model: str, text: str) -> list[str]:
    chunks: list[str] = []
    base = {
        "id": "artemis-cursor-sdk",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    chunks.append(
        "data: "
        + json.dumps(
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }
        )
        + "\n\n"
    )
    step = 48
    for i in range(0, len(text), step):
        part = text[i : i + step]
        chunks.append(
            "data: "
            + json.dumps(
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                }
            )
            + "\n\n"
        )
    chunks.append(
        "data: "
        + json.dumps(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 32,
                    "completion_tokens": max(16, len(text) // 4),
                    "total_tokens": 32 + max(16, len(text) // 4),
                },
            }
        )
        + "\n\n"
    )
    chunks.append("data: [DONE]\n\n")
    return chunks


def _sse_tool_call(model: str, tool_call: dict) -> list[str]:
    base = {
        "id": "artemis-cursor-sdk",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    return [
        "data: "
        + json.dumps(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": tool_call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tool_call["function"]["name"],
                                        "arguments": tool_call["function"]["arguments"],
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        + "\n\n",
        "data: "
        + json.dumps(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 48,
                    "completion_tokens": 24,
                    "total_tokens": 72,
                },
            }
        )
        + "\n\n",
        "data: [DONE]\n\n",
    ]


def _load_tool_call_v2(
    *, path: str | None = None, attachments: list[str] | None = None, prompt: str | None = None
) -> dict:
    """Build artemis_load_challenge with path + attachments + prompt."""
    args: dict[str, Any] = {"mode": "artemis"}
    if path:
        args["path"] = path
    if attachments:
        args["attachments"] = attachments
    if prompt:
        args["prompt"] = prompt
    return {
        "id": "call_artemis_load",
        "type": "function",
        "function": {
            "name": "artemis_load_challenge",
            "arguments": json.dumps(args),
        },
    }


def _guidance_text(*, challenge_name: str | None = None) -> str:
    if challenge_name:
        return (
            f"Challenge `{challenge_name}` is loaded. "
            "Send another message to start the swarm, or paste a new challenge."
        )
    return (
        "Paste the challenge (description / web links / nc / ssh) here — like challenge.txt — "
        "and/or drop one or more folder and file paths in the same message. "
        "Web links stay in the text (not downloaded). Artemis will load it and run the swarm."
    )



def _ask_flags_tool_call(model: str, default: int = 1) -> dict:
    return {
        "id": "call_artemis_ask_flags",
        "type": "function",
        "function": {
            "name": "artemis_ask_flags",
            "arguments": json.dumps({"models": [f"cursor/{model}"], "default": max(1, min(64, int(default) or 1))}),
        },
    }


def _emit_text(model: str, text: str, stream: bool) -> tuple[str, Any]:
    if stream:
        return "sse", _sse_text_chunks(model, text)
    return "json", _completion_payload(model, text)


def _emit_tool(model: str, tool: dict, stream: bool) -> tuple[str, Any]:
    if stream:
        return "sse", _sse_tool_call(model, tool)
    return "json", _completion_payload(model, "", tool_call=tool)


def _handle_chat(body: dict, headers: dict) -> tuple[str, Any]:
    """Single CTF flow: fresh path/paste → load; loaded + fresh turn → ask_flags (always).

    Maximally flexible: one message may contain folder path(s), file paths,
    pasted text, or any mix — with or without an instruction. Continuation
    turns (agent loop after a tool result) return plain text so we never
    re-fire a tool call for the same paste.
    """
    model = _extract_cursor_model(body)
    messages = body.get("messages") or []
    user_text = _last_user_text(messages)
    stream = _wants_stream(body, headers)
    session_id = _session_id_from_headers(headers)

    challenge_dir = ""
    try:
        from backend.shell.sandbox_session import load_session_state

        # Prefer the TUI window session. Without a session header, do not inherit
        # another window's challenge_dir from ``_default`` / env.
        if session_id:
            st = load_session_state(session_id)
            challenge_dir = str(st.get("challenge_dir") or "").strip()
    except Exception:
        pass

    # 1) Continuation (last msg = tool result or assistant+tool_calls). Advance
    #    the flow based on which tool just ran — do NOT return "ready" (that
    #    killed the flow and skipped the flag dialog).
    if _in_tool_continuation(messages):
        last_tool = _recent_assistant_tool(messages)
        # After load → ask_flags only when load succeeded. Failed path must not
        # open the Flags dialog (user fixes the path and sends again).
        if last_tool == "artemis_load_challenge":
            result = _last_tool_result_text(messages)
            if _load_challenge_failed(result):
                return _emit_text(model, "", stream)
            return _emit_tool(model, _ask_flags_tool_call(model, default=1), stream)
        # After ask_flags / swarm / stop_swarm → end the turn (empty stop). The
        # swarm is already running via the daemon; don't re-fire a tool call.
        return _emit_text(model, "", stream)

    # 2) Fresh user turn.
    paths = extract_challenge_paths(user_text)

    # 2a) Pure greeting (no paths) with no challenge loaded → guidance.
    if not paths and not challenge_dir and is_greeting(user_text):
        return _emit_text(model, _guidance_text(), stream)

    paste = extract_paste_without_paths(user_text, paths)

    # 2b) A NEW path on a fresh turn → (re)load (multi-path → attachments).
    #     This reloads even if the path matches the loaded challenge.
    if paths:
        tool = _load_tool_call_v2(
            path=paths[0],
            attachments=paths[1:] if len(paths) > 1 else None,
            prompt=paste or None,
        )
        return _emit_tool(model, tool, stream)

    # 2c) Last load in this chat failed — don't treat "go" as a new challenge
    #     and don't reopen Flags for the previous challenge_dir.
    recent_load = _most_recent_load_result(messages)
    if recent_load is not None and _load_challenge_failed(recent_load):
        return _emit_text(
            model,
            "Load failed — send a valid challenge folder, file path(s), "
            "and/or paste the challenge text (web links stay in the paste).",
            stream,
        )

    # 2d) Nothing loaded yet: any non-greeting paste is a challenge.
    if not challenge_dir and paste and looks_like_challenge_paste(paste):
        tool = _load_tool_call_v2(prompt=paste)
        return _emit_tool(model, tool, stream)

    # 2e) Challenge already loaded, fresh turn, not a new path → ask_flags.
    if challenge_dir:
        return _emit_tool(model, _ask_flags_tool_call(model, default=1), stream)

    # 2e) Nothing loaded, no path, non-greeting text → guidance.
    return _emit_text(model, _guidance_text(), stream)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[cursor-proxy] " + (format % args) + "\n")

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse_static(self, chunks: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            ids = _official_model_ids()
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": mid, "object": "model"} for mid in ids],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        if "/chat/completions" not in self.path and "/completions" not in self.path:
            self._json(404, {"error": {"message": "not found"}})
            return
        kind, payload = _handle_chat(body, dict(self.headers))
        if kind == "sse":
            self._sse_static(payload)
            return
        self._json(200, payload)


def main() -> None:
    from backend.stdio_platform import ensure_standard_streams

    ensure_standard_streams()
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"artemis cursor proxy on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
