"""OpenAI-compatible front for Cursor in Artemis TUI.

Serves the official model catalog + chat/completions SSE. Does NOT run Cursor
Agent SDK chat and does NOT load or start a swarm — the TUI owns that flow.
If a prompt still reaches this stub, return guidance or an empty stop.
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

from backend.challenge_paste import is_greeting

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


def _in_tool_continuation(messages: object) -> bool:
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict):
        return False
    role = last.get("role")
    if role == "tool":
        return True
    return bool(role == "assistant" and last.get("tool_calls"))


def _wants_stream(body: dict, headers: dict) -> bool:
    if body.get("stream") is True:
        return True
    accept = (headers.get("Accept") or headers.get("accept") or "").lower()
    return "text/event-stream" in accept


def _completion_payload(model: str, text: str) -> dict:
    prompt_tokens = 32
    completion_tokens = max(16, len(text) // 4) if text else 16
    return {
        "id": "artemis-cursor-sdk",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
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


def _guidance_text() -> str:
    return (
        "Paste a challenge, path, or @files. "
        "Artemis loads it, then asks flags / mode / models. "
        "Sandbox solvers do the work after Start."
    )


def _emit_text(model: str, text: str, stream: bool) -> tuple[str, object]:
    if stream:
        return "sse", _sse_text_chunks(model, text)
    return "json", _completion_payload(model, text)


def _handle_chat(body: dict, headers: dict) -> tuple[str, object]:
    model = _extract_cursor_model(body)
    messages = body.get("messages") or []
    user_text = _last_user_text(messages)
    stream = _wants_stream(body, headers)
    if _in_tool_continuation(messages):
        return _emit_text(model, "", stream)
    if is_greeting(user_text):
        return _emit_text(model, _guidance_text(), stream)
    return _emit_text(model, "", stream)


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
