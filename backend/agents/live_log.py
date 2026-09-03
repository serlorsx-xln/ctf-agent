"""Shared live terminal logging for solver agents (cursor / claude / codex).

Stdout lines are streamed into the Artemis TUI and parsed into agent cards.
Thinking/AI deltas are buffered so token-by-token SDK streams become one
continuous block — not one Thinking card per word.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("backend.agents.live")

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

_lock = threading.Lock()
_stream_tag: str | None = None
_stream_body: str = ""
_flush_timer: threading.Timer | None = None
_FLUSH_IDLE_S = 0.15
_FLUSH_FIRST_S = 0.02
_FLUSH_MAX_CHARS = 600
_GARBLED_SUPPRESSED = "provider returned unreadable output (suppressed)"
_stream_first_chunk = False
# Nestable mute for writeup / Hold Q&A — tool dumps must not flood the feed.
_quiet_depth = 0


def is_garbled_model_text(text: str) -> bool:
    """True for mojibake / blocked-provider junk (CJK+Arabic soup, etc.).

    Mirrors TUI ``isGarbledModelText`` so backend can drop junk before stdout.
    """
    t = (text or "").strip()
    if len(t) < 6:
        return False
    if "\ufffd" in t:
        return True
    cjk = len(re.findall(r"[\u3400-\u9FFF]", t))
    arabic = len(re.findall(r"[\u0600-\u06FF]", t))
    hangul = len(re.findall(r"[\uAC00-\uD7AF]", t))
    weird = cjk + arabic + hangul
    if weird < 4:
        return False
    latin = len(re.findall(r"[A-Za-z]", t))
    if weird >= len(t) * 0.4 and latin < 8:
        return True
    if cjk > 0 and arabic > 0 and latin < 12:
        return True
    # Arabic-heavy short bursts from blocked keys (screenshots).
    return arabic >= 6 and latin < 6 and arabic >= len(t) * 0.35


def _cancel_timer() -> None:
    global _flush_timer
    if _flush_timer is not None:
        try:
            _flush_timer.cancel()
        except Exception:
            pass
        _flush_timer = None


def emit_line(line: str) -> None:
    """Print a preformatted live-log line and tee it to the swarm disk log.

    Use for ``[artemis] outcome`` / ``[artemis] summary`` so reconnect/replay
    still sees CORRECT after a hung writeup. Bare ``print`` is not teed.
    """
    text = (line or "").rstrip()
    if not text:
        return
    _disk_tee(text)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)
    except (BrokenPipeError, OSError):
        pass
    logger.debug("%s", text)


def _emit(tag: str, body: str) -> None:
    from backend.flags import confirm_in_progress

    if confirm_in_progress():
        return
    text = body.rstrip()
    if not text:
        return
    # One tagged line only — multi-line bodies otherwise leak untagged
    # continuation lines into the TUI (# Artemis status panel).
    text = re.sub(r"[\r\n]+", " ", text).strip()
    if (tag.endswith(" think") or tag.endswith(" ai")) and is_garbled_model_text(text):
        text = _GARBLED_SUPPRESSED
    emit_line(f"[{tag}] {text}")


_disk_log_fh = None
_disk_log_lock = threading.Lock()
_DISK_LOG_MAX_BYTES = 10 * 1024 * 1024
_disk_log_size = 0


def _disk_tee(line: str) -> None:
    """Append a live-log line to the swarm disk log when running under the daemon.

    The daemon sets ``ARTEMIS_SWARM_LOG`` when spawning the swarm subprocess.
    Tee-ing here (rather than in the daemon) means the log survives a daemon
    crash — the new daemon tails this file to re-adopt a still-running swarm.
    Headless CLI runs (no env) are unaffected.

    Capped at ``_DISK_LOG_MAX_BYTES``: once exceeded, the file is truncated to
    its tail so it never grows unbounded across long swarms.
    """
    global _disk_log_fh, _disk_log_size
    path = os.environ.get("ARTEMIS_SWARM_LOG")
    if not path:
        return
    try:
        with _disk_log_lock:
            if _disk_log_fh is None or getattr(_disk_log_fh, "closed", False):
                _disk_log_fh = open(path, "a", encoding="utf-8", errors="replace")
                try:
                    _disk_log_size = os.fstat(_disk_log_fh.fileno()).st_size
                except OSError:
                    _disk_log_size = 0
            _disk_log_fh.write(line + "\n")
            _disk_log_fh.flush()
            _disk_log_size += len(line) + 1
            if _disk_log_size > _DISK_LOG_MAX_BYTES:
                _rotate_disk_log(path)
    except OSError:
        pass


def _rotate_disk_log(path: str) -> None:
    """Truncate the disk log to its tail, keeping the most recent lines."""
    global _disk_log_fh, _disk_log_size
    fh = _disk_log_fh
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
    try:
        with open(path, "rb") as raw:
            data = raw.read()
        keep = data[-_DISK_LOG_MAX_BYTES // 2 :]
        # Align to a newline so we never keep a torn UTF-8 / mid-line fragment.
        nl = keep.find(b"\n")
        if 0 <= nl < len(keep) - 1:
            keep = keep[nl + 1 :]
        with open(path, "wb") as raw:
            raw.write(keep)
        _disk_log_size = len(keep)
    except OSError:
        _disk_log_size = 0
    _disk_log_fh = open(path, "a", encoding="utf-8", errors="replace")


def close_disk_tee() -> None:
    """Close the disk-log file handle (e.g. on interpreter exit)."""
    global _disk_log_fh
    with _disk_log_lock:
        if _disk_log_fh is not None and not getattr(_disk_log_fh, "closed", False):
            try:
                _disk_log_fh.close()
            except OSError:
                pass
        _disk_log_fh = None


def flush_stream() -> None:
    """Flush any buffered think/ai text to stdout."""
    global _stream_tag, _stream_body
    with _lock:
        _cancel_timer()
        if _quiet_depth > 0:
            _stream_tag, _stream_body = None, ""
            return
        tag, body = _stream_tag, _stream_body
        _stream_tag, _stream_body = None, ""
    if tag:
        _emit(tag, body)


def _schedule_flush(*, delay: float | None = None) -> None:
    global _flush_timer
    _cancel_timer()

    def _fire() -> None:
        flush_stream()

    t = threading.Timer(delay if delay is not None else _FLUSH_IDLE_S, _fire)
    t.daemon = True
    _flush_timer = t
    t.start()


def _join_stream(prev: str, chunk: str) -> str:
    """Concatenate stream deltas as-is.

    Never insert spaces — SDK chunks already include whitespace when needed.
    Inserting spaces between alnum pieces breaks flags (``fl ag{…}``) and hashes.
    """
    if not prev:
        return chunk
    if not chunk:
        return prev
    return prev + chunk


def _append_stream(tag: str, text: str) -> None:
    global _stream_tag, _stream_body, _stream_first_chunk
    chunk = text or ""
    if not chunk:
        return

    to_emit: list[tuple[str, str]] = []
    first = False
    with _lock:
        if _quiet_depth > 0:
            return
        if _stream_tag and _stream_tag != tag and _stream_body.strip():
            to_emit.append((_stream_tag, _stream_body))
            _stream_body = ""
            _stream_first_chunk = False
        first = not _stream_body
        _stream_tag = tag
        _stream_body = _join_stream(_stream_body, chunk)
        body = _stream_body
        hard = len(body) >= _FLUSH_MAX_CHARS or (
            len(body) > 48 and chunk.rstrip().endswith((".", "!", "?", "\n"))
        )
        if hard:
            to_emit.append((tag, body))
            _stream_tag, _stream_body = None, ""
            _stream_first_chunk = False
            _cancel_timer()
            schedule = False
            delay = None
        else:
            schedule = True
            delay = _FLUSH_FIRST_S if first else _FLUSH_IDLE_S
            if first:
                _stream_first_chunk = True

    for t, b in to_emit:
        _emit(t, b)
    if schedule:
        _schedule_flush(delay=delay)


def live(tag: str, text: str, *, limit: int = 1200) -> None:
    """Print AI activity to stdout so the TUI can stream it into chat cards."""
    from backend.flags import confirm_in_progress

    if confirm_in_progress():
        return
    with _lock:
        if _quiet_depth > 0:
            return

    # Buffer streaming think / ai deltas into one continuous block
    if tag.endswith(" think") or tag.endswith(" ai"):
        _append_stream(tag, text)
        return

    flush_stream()
    body = text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars]"
    _emit(tag, body)


@contextlib.contextmanager
def quiet_live() -> Iterator[None]:
    """Suppress ``live`` / tool dumps (writeup + Hold Q&A turns).

    Drops any in-flight stream buffer so a timer scheduled before quiet cannot
    leak think/ai into the feed after CORRECT.
    """
    global _quiet_depth, _stream_tag, _stream_body
    with _lock:
        _cancel_timer()
        _stream_tag, _stream_body = None, ""
        _quiet_depth += 1
    try:
        yield
    finally:
        with _lock:
            _quiet_depth = max(0, _quiet_depth - 1)


def live_json(tag: str, payload: Any, *, limit: int = 800) -> None:
    """Pretty-print a small JSON-ish payload for tool args."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    live(tag, text, limit=limit)
