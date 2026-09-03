"""File-based operator → swarm notes (daemon writes; solvers drain).

Daemon and swarm are separate processes, so mid-solve chat cannot use the
in-memory ``ChallengeMessageBus`` alone. Notes land in
``sessions/<sid>/operator_inbox.jsonl`` and are claimed by solvers.

Delivery mirrors OpenCode IDE/CLI where the provider supports it:
- ``steer`` — force-followup ASAP (Cursor: cancel active run; Claude:
  ``client.interrupt()``; Codex: ``turn/interrupt``; Gemini: cancel in-flight
  ``generate_content`` await). Claimed by interrupt watchers, not mid-tool
  ``check_findings``.
- ``queue`` — hold until idle (Cursor: non-force idle turn; soft: next turn
  boundary via ``do_claim_soft_queue`` / ``soft_idle_operator_notes``)

Mixed Cursor + soft: Cursor owns unscoped/broadcast rows for interrupt; soft
solvers still drain notes **targeted** at their display key.

Targeting (CTF soft-race):
- ``target`` omitted / empty — broadcast (daemon fans out one copy per roster agent)
- ``target`` = agent display key (e.g. ``default#1``) — that agent only
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from backend.file_lock import acquire as _lock_acquire
from backend.file_lock import release as _lock_release

Delivery = Literal["steer", "queue"]


def operator_inbox_path(session_id: str | None = None) -> Path:
    from backend.shell.sandbox_session import session_dir

    return session_dir(session_id) / "operator_inbox.jsonl"


def _inbox_lock_path(session_id: str | None = None) -> Path:
    """Sidecar lock (Unix fcntl / Windows msvcrt via ``backend.file_lock``)."""
    path = operator_inbox_path(session_id)
    return path.with_name(path.name + ".lock")


def _normalize_delivery(value: Any) -> Delivery:
    raw = str(value or "steer").strip().lower()
    return "queue" if raw == "queue" else "steer"


def normalize_operator_target(value: Any) -> str | None:
    """Return a concrete agent key, or None for unscoped/legacy broadcast."""
    raw = str(value or "").strip()
    if not raw or raw in ("*", "all", "broadcast"):
        return None
    return raw


def targets_match(note_target: str | None, claimer: str | None) -> bool:
    """Whether ``claimer`` may take this note.

    Unscoped notes (``target is None``) match any claimer (first wins).
    Scoped notes match the same display key, or ``base`` ↔ ``base#N`` (same
    rule as daemon followup crumb matching).
    """
    if note_target is None:
        return True
    if not claimer:
        return False
    if note_target == claimer:
        return True
    return claimer.startswith(f"{note_target}#") or note_target.startswith(f"{claimer}#")


def append_operator_note(
    text: str,
    *,
    session_id: str | None = None,
    delivery: Delivery | str = "steer",
    target: str | None = None,
) -> dict[str, Any]:
    """Append one operator note. Returns the written record."""
    body = (text or "").strip()
    if not body:
        raise ValueError("empty operator message")
    if len(body) > 4000:
        body = body[:4000]
    rec: dict[str, Any] = {
        "ts": time.time(),
        "text": body,
        "delivery": _normalize_delivery(delivery),
    }
    tgt = normalize_operator_target(target)
    if tgt:
        rec["target"] = tgt
    path = operator_inbox_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    fd = _lock_acquire(_inbox_lock_path(session_id))
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    finally:
        _lock_release(fd)
    return rec


def clear_operator_inbox(*, session_id: str | None = None) -> None:
    """Remove pending notes under the inbox lock (do not delete the lock file)."""
    path = operator_inbox_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _lock_acquire(_inbox_lock_path(session_id))
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    finally:
        _lock_release(fd)


async def drain_operator_notes_to_bus(
    message_bus,
    *,
    session_id: str | None = None,
    delivery: Delivery | str | None = "steer",
    broadcast: bool = True,
    claimer: str | None = None,
    require_target: bool = False,
) -> list[str]:
    """Read inbox lines; optionally broadcast; rewrite file with leftovers.

    ``delivery``:
    - ``"steer"`` (default) — only steers (legacy lines without a field count as steer)
    - ``"queue"`` — only queued notes
    - ``None`` — drain every pending note (QA hold / idle between turns)

    ``claimer``:
    - agent display key — only take notes addressed to this agent (or unscoped)
    - ``None`` — take any matching delivery (legacy / hold without filter)

    ``require_target``:
    - ``True`` — skip unscoped notes (mixed Cursor+soft: leave broadcast for Cursor)

    ``broadcast``:
    - ``True`` — post onto the message bus (legacy soft-inject)
    - ``False`` — return texts only (force-followup / interrupt path)

    Uses an exclusive file lock so concurrent drain+append cannot corrupt the file.
    """
    path = operator_inbox_path(session_id)
    if not path.is_file():
        return []

    want: Delivery | None = (
        None if delivery is None else _normalize_delivery(delivery)
    )
    claim = normalize_operator_target(claimer)

    texts: list[str] = []
    to_broadcast: list[str] = []
    try:
        fd = _lock_acquire(_inbox_lock_path(session_id))
        try:
            with path.open("r+", encoding="utf-8") as f:
                raw = f.read()
                if not raw.strip():
                    return []
                kept: list[str] = []
                for line in raw.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except json.JSONDecodeError:
                        # Preserve corrupt lines — truncating would permanently drop
                        # operator steer/queue notes with no TUI error.
                        kept.append(s)
                        continue
                    text = str(rec.get("text") or "").strip()
                    if not text:
                        continue
                    note_delivery = _normalize_delivery(rec.get("delivery"))
                    if want is not None and note_delivery != want:
                        kept.append(json.dumps(rec, ensure_ascii=False))
                        continue
                    note_target = normalize_operator_target(rec.get("target"))
                    if require_target and note_target is None:
                        kept.append(json.dumps(rec, ensure_ascii=False))
                        continue
                    if claim is not None and not targets_match(note_target, claim):
                        kept.append(json.dumps(rec, ensure_ascii=False))
                        continue
                    if broadcast and message_bus is not None:
                        to_broadcast.append(text)
                    texts.append(text)
                    try:
                        from backend.agents.live_log import emit_line

                        who = f"→{note_target}" if note_target else ""
                        emit_line(
                            f"[artemis] solver ← operator ({note_delivery}{who}): {text[:2000]}"
                        )
                    except Exception:
                        pass
                f.seek(0)
                f.truncate()
                if kept:
                    f.write("\n".join(kept) + "\n")
                f.flush()
        finally:
            _lock_release(fd)
    except OSError:
        return texts
    # Broadcast after releasing the flock so appenders are not stalled on bus I/O.
    if to_broadcast and message_bus is not None:
        for text in to_broadcast:
            try:
                await message_bus.broadcast(
                    f"Operator note: {text}", source="operator"
                )
            except Exception:
                pass
    return texts
