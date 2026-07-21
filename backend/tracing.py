"""Per-tool-call JSONL event tracing — one file per solver, streamable via tail -f."""

from __future__ import annotations

import atexit
import json
import time
from collections.abc import Mapping
from pathlib import Path


def _sanitize(s: str) -> str:
    return s.replace("/", "_").replace(" ", "_")


class SolverTracer:
    """Append-only JSONL event tracer. Flushes every write for tail -f streaming."""

    def __init__(self, challenge_name: str, model_id: str, log_dir: str = "logs") -> None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.path = str(
            Path(log_dir) / f"trace-{_sanitize(challenge_name)}-{_sanitize(model_id)}-{ts}.jsonl"
        )
        self._fh = open(self.path, "a")
        atexit.register(self._close)

    def close(self) -> None:
        """Explicitly close the trace file. Safe to call multiple times."""
        if not self._fh.closed:
            try:
                self._fh.close()
            except Exception:
                pass

    _close = close  # atexit compat

    def _write(self, event: dict) -> None:
        try:
            self._fh.write(json.dumps({"ts": time.time(), **event}) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def tool_call(
        self, tool_name: str, args: Mapping[str, object] | str, step: int
    ) -> None:
        args_str = args if isinstance(args, str) else json.dumps(dict(args), default=str)
        self._write({"type": "tool_call", "tool": tool_name, "args": args_str[:2000], "step": step})

    def tool_result(self, tool_name: str, result: str, step: int) -> None:
        self._write(
            {"type": "tool_result", "tool": tool_name, "result": result[:2000], "step": step}
        )

    def model_response(
        self, text: str, step: int, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        self._write(
            {
                "type": "model_response",
                "text": text[:1000],
                "step": step,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    def usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cost_usd: float | None = None,
    ) -> None:
        payload: dict = {
            "type": "usage",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
        }
        # Only persist provider-reported USD; omit when unknown.
        if cost_usd is not None:
            payload["cost_usd"] = round(cost_usd, 6)
        self._write(payload)

    def event(self, kind: str, **kwargs) -> None:
        self._write({"type": kind, **kwargs})
