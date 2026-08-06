"""In-memory daemon state: multi-session snapshots + scoped broadcast.

On-disk files under ``sessions/<sid>/`` persist for crash recovery. The TUI
never reads those files once connected — it consumes ``session_update`` pushes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.daemon.session_id import DEFAULT_SESSION_ID, normalize_session_id

logger = logging.getLogger(__name__)


@dataclass
class SessionSlot:
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    swarm_id: str | None = None
    swarm_running: bool = False


class DaemonState:
    """Per-session challenge/usage state with session-scoped subscriber fan-out."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionSlot] = {}
        # session_id → subscriber queues (TUI peers bound to that session)
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # queue → session_id (for unsubscribe / disconnect)
        self._queue_session: dict[asyncio.Queue[dict[str, Any]], str] = {}

    def _slot(self, session_id: str | None) -> SessionSlot:
        sid = normalize_session_id(session_id)
        slot = self._sessions.get(sid)
        if slot is None:
            slot = SessionSlot(session_id=sid)
            self._sessions[sid] = slot
        return slot

    # ---- hydration -------------------------------------------------------
    def hydrate(self, session_id: str | None = None) -> None:
        from backend.shell.sandbox_session import load_session_state

        sid = normalize_session_id(session_id)
        try:
            self._slot(sid).data = load_session_state(sid) or {}
        except Exception:
            logger.debug("hydrate session failed", exc_info=True)
            self._slot(sid).data = {}

    def hydrate_all(self) -> None:
        """Load ``_default`` (incl. legacy migrate) and any existing session dirs."""
        from backend.cache import cache_dir

        self.hydrate(DEFAULT_SESSION_ID)
        root = cache_dir() / "sessions"
        if not root.is_dir():
            return
        for child in root.iterdir():
            if child.is_dir() and child.name != DEFAULT_SESSION_ID:
                self.hydrate(child.name)

    # ---- accessors (back-compat: bare props → _default) -----------------
    @property
    def session(self) -> dict[str, Any]:
        return self.get_session(DEFAULT_SESSION_ID)

    @property
    def usage(self) -> dict[str, Any]:
        return self.get_usage(DEFAULT_SESSION_ID)

    def get_session(self, session_id: str | None = None) -> dict[str, Any]:
        return dict(self._slot(session_id).data)

    def get_usage(self, session_id: str | None = None) -> dict[str, Any]:
        return dict(self._slot(session_id).usage)

    # ---- mutation --------------------------------------------------------
    def set_session(self, snapshot: dict[str, Any], session_id: str | None = None) -> None:
        sid = normalize_session_id(session_id)
        self._slot(sid).data = dict(snapshot)
        self.broadcast(
            {
                "type": "session_update",
                "session": sid,
                "session_state": self.get_session(sid),
            }
        )

    def update_session(self, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        from backend.shell.sandbox_session import save_session_state

        sid = normalize_session_id(session_id)
        merged = save_session_state(sid, **kwargs)
        self._slot(sid).data = dict(merged)
        self.broadcast(
            {
                "type": "session_update",
                "session": sid,
                "session_state": self.get_session(sid),
            }
        )
        return dict(merged)

    def clear_session(self, session_id: str | None = None) -> None:
        from backend.shell.sandbox_session import clear_session_state

        sid = normalize_session_id(session_id)
        clear_session_state(sid)
        slot = self._slot(sid)
        slot.data = {}
        slot.usage = {}
        slot.swarm_id = None
        slot.swarm_running = False
        self.broadcast(
            {
                "type": "session_update",
                "session": sid,
                "session_state": {},
            }
        )

    def set_usage(self, snapshot: dict[str, Any], session_id: str | None = None) -> None:
        sid = normalize_session_id(session_id)
        self._slot(sid).usage = dict(snapshot)
        self.broadcast({"type": "usage_update", "session": sid, **self.get_usage(sid)})

    def set_swarm_meta(
        self,
        session_id: str | None,
        *,
        swarm_id: str | None = None,
        swarm_running: bool | None = None,
    ) -> None:
        slot = self._slot(session_id)
        if swarm_id is not None:
            slot.swarm_id = swarm_id
        if swarm_running is not None:
            slot.swarm_running = swarm_running

    # ---- pub/sub ---------------------------------------------------------
    def subscribe(self, session_id: str | None = None) -> asyncio.Queue[dict[str, Any]]:
        sid = normalize_session_id(session_id)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2048)
        self._subscribers.setdefault(sid, set()).add(q)
        self._queue_session[q] = sid
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        sid = self._queue_session.pop(q, None)
        if sid is None:
            for _, qs in list(self._subscribers.items()):
                qs.discard(q)
            return
        bucket = self._subscribers.get(sid)
        if bucket is not None:
            bucket.discard(q)
            if not bucket:
                self._subscribers.pop(sid, None)

    def has_subscribers(self, session_id: str | None = None) -> bool:
        if session_id is None:
            return any(self._subscribers.values())
        sid = normalize_session_id(session_id)
        return bool(self._subscribers.get(sid))

    def broadcast(self, event: dict[str, Any]) -> None:
        """Fan-out to subscribers of ``event['session']`` (default ``_default``)."""
        sid = normalize_session_id(event.get("session"))
        if event.get("session") is None:
            event = {**event, "session": sid}
        priority = _is_priority(event)
        for q in list(self._subscribers.get(sid, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                _make_room(q, priority)
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


_PRIORITY_TYPES = frozenset(
    {
        "boot",
        "flag_confirm_request",
        "flags_ask_request",
        "solve_flow_request",
        "session_update",
        "swarm_exit",
        "swarm_roster",
        "swarm_adopted",
        "replay_done",
        "outcome",
        "flag_confirm_dismiss",
    }
)


def _is_priority(event: dict[str, Any]) -> bool:
    return event.get("type") in _PRIORITY_TYPES


def _make_room(q: asyncio.Queue, incoming_priority: bool) -> None:
    try:
        held: list[Any] = []
        dropped = False
        while not q.empty():
            item = q.get_nowait()
            if not dropped and not _is_priority(item):
                dropped = True
                continue
            held.append(item)
            if len(held) > 64:
                break
        for item in held:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                break
        if not dropped:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
    except Exception:
        pass
