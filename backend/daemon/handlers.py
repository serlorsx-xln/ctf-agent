"""Request handlers for the Artemis daemon (session-scoped)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.daemon import protocol
from backend.daemon.session_id import normalize_session_id
from backend.daemon.state import DaemonState
from backend.daemon.supervisor import SwarmSupervisor

logger = logging.getLogger(__name__)

# Pending dialogs keyed by (session_id, request_id).
_pending_dialogs: dict[tuple[str, str], asyncio.Future[Any]] = {}


def cancel_pending_dialogs(broadcast=None, session: str | None = None) -> None:
    """Cancel pending dialog futures.

    When ``session`` is set, only that session's dialogs are cancelled.
    When ``session`` is None, cancel everything (daemon shutdown).
    """
    sid_filter = normalize_session_id(session) if session is not None else None
    for key, fut in list(_pending_dialogs.items()):
        sid, rid = key
        if sid_filter is not None and sid != sid_filter:
            continue
        if not fut.done():
            fut.set_result({"ok": False, "cancelled": True})
        _pending_dialogs.pop(key, None)
        if callable(broadcast):
            try:
                broadcast(
                    {
                        "type": "flag_confirm_dismiss",
                        "session": sid,
                        "request_id": rid,
                    }
                )
            except Exception:
                logger.debug("flag_confirm_dismiss broadcast failed", exc_info=True)


def register_dialog(rid: str, fut: asyncio.Future[Any], session: str | None = None) -> None:
    sid = normalize_session_id(session)
    _pending_dialogs[(sid, rid)] = fut


def pop_dialog(rid: str, session: str | None = None) -> asyncio.Future[Any] | None:
    sid = normalize_session_id(session)
    return _pending_dialogs.pop((sid, rid), None)


class Handlers:
    """Holds shared state for request dispatch."""

    def __init__(self, state: DaemonState, supervisor: SwarmSupervisor) -> None:
        self.state = state
        self.supervisor = supervisor

    async def dispatch(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mtype = msg.get("type", "")
        req_id = msg.get("id")
        session = msg.get("session")
        sid = normalize_session_id(session)
        payload = {k: v for k, v in msg.items() if k not in ("v", "id", "type", "session")}

        handler = getattr(self, f"_h_{mtype}", None)
        if handler is None:
            return protocol.make_error(req_id, f"unknown request type: {mtype}", session=sid)

        try:
            result = await handler(payload, session=sid)
        except Exception as e:
            logger.exception("handler %s failed", mtype)
            return protocol.make_error(req_id, f"{type(e).__name__}: {e}", session=sid)

        if result is None:
            return protocol.make_response(req_id=req_id, type=mtype, session=sid, ok=True)
        if isinstance(result, dict) and result.get("__push_only__"):
            return protocol.make_response(req_id=req_id, type=mtype, session=sid, ok=True)
        if isinstance(result, dict):
            ok = bool(result.get("ok", True))
            fields = {k: v for k, v in result.items() if k != "ok"}
            return protocol.make_response(
                req_id=req_id, type=mtype, session=sid, ok=ok, **fields
            )
        return protocol.make_response(req_id=req_id, type=mtype, session=sid, ok=True)

    @staticmethod
    def _bridge_payload(payload: dict, session: str) -> dict:
        """Re-attach session so bridge load/save hits the correct disk slot."""
        return {**payload, "session": session}

    # ---- bridge-backed ops ----------------------------------------------
    async def _h_load(self, payload: dict, *, session: str) -> dict:
        from backend.shell.bridge import _load_challenge

        text = await _load_challenge(self._bridge_payload(payload, session))
        from backend.shell.sandbox_session import load_session_state

        self.state.set_session(load_session_state(session), session_id=session)
        # TUI owns the gate. Broadcast so flags → mode → models opens after load.
        if not str(text).lstrip().upper().startswith("ERROR"):
            self._broadcast_solve_flow(session, from_load=True)
        return {"text": text, "session_state": self.state.get_session(session)}

    async def _h_status(self, _payload: dict, *, session: str) -> dict:
        import json

        st = self.state.get_session(session)
        return {
            "text": json.dumps(st, ensure_ascii=False, indent=2),
            "session_state": st,
            "swarm_running": self.supervisor.is_running(session),
        }

    async def _h_clear_session(self, _payload: dict, *, session: str) -> dict:
        self.state.clear_session(session)
        return {"session_state": self.state.get_session(session)}

    async def _h_session_refresh(self, _payload: dict, *, session: str) -> dict:
        from backend.shell.sandbox_session import load_session_state

        self.state.set_session(load_session_state(session) or {}, session_id=session)
        return {"session_state": self.state.get_session(session)}

    def _broadcast_solve_flow(
        self,
        session: str,
        *,
        default: int | None = None,
        preselected: Any = None,
        from_load: bool = False,
    ) -> bool:
        """Push solve_flow_request when a challenge dir is on disk. Returns True if sent."""
        from pathlib import Path

        from backend.shell.sandbox_session import load_session_state

        st = load_session_state(session) or self.state.get_session(session)
        challenge_dir = str(st.get("challenge_dir") or "").strip()
        if not challenge_dir or not Path(challenge_dir).is_dir():
            return False
        challenge = st.get("challenge_name") or challenge_dir
        if default is None:
            raw = st.get("flags_required")
            default = int(raw) if isinstance(raw, int) and raw > 0 else 1
        event: dict[str, Any] = {
            "type": "solve_flow_request",
            "session": session,
            "default_flags": int(default),
            "challenge": challenge,
            "from_load": from_load,
        }
        if preselected is not None:
            event["preselected"] = preselected
        self.state.broadcast(event)
        self.state.set_session(st, session_id=session)
        return True

    async def _h_solve_flow_start(self, payload: dict, *, session: str) -> dict:
        default = payload.get("default", 1)
        if not self._broadcast_solve_flow(
            session,
            default=int(default) if default is not None else None,
            preselected=payload.get("preselected"),
        ):
            return {
                "ok": False,
                "error": "load a challenge first (path must be an existing directory)",
            }
        return {"__push_only__": True}

    async def _h_sandbox_stop(self, payload: dict, *, session: str) -> dict:
        from backend.shell.bridge import _stop

        return {"text": await _stop(self._bridge_payload(payload, session))}

    # ---- swarm -----------------------------------------------------------
    async def _h_swarm_start(self, payload: dict, *, session: str) -> dict:
        from backend.models import missing_swarm_credentials, normalize_swarm_specs
        from backend.shell.sandbox_session import load_session_state

        st = load_session_state(session)
        challenge = (payload.get("challenge_dir") or st.get("challenge_dir") or "").strip()
        if not challenge:
            return {"ok": False, "error": "load a challenge first"}
        from pathlib import Path

        if not Path(challenge).is_dir():
            return {
                "ok": False,
                "error": "challenge path is not an existing directory — reload the challenge",
            }
        models = payload.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.replace(",", " ").split() if m.strip()]
        try:
            models = normalize_swarm_specs(models) if models else []
        except ValueError as e:
            return {"ok": False, "error": f"invalid model spec — {e}"}
        if not models:
            from backend.models import missing_models_error

            return {"ok": False, "error": missing_models_error()}
        missing = missing_swarm_credentials(models)
        if missing:
            from backend.models import missing_credentials_error

            return {"ok": False, "error": missing_credentials_error(missing)}

        fr = payload.get("flags_required")
        if fr is not None:
            fr = int(fr)
            self.state.update_session(session_id=session, flags_required=fr, flags_explicit=True)
        auto_confirm = bool(payload.get("auto_confirm"))

        try:
            swarm_id = await self.supervisor.spawn(
                challenge=challenge,
                models=models,
                flags_required=fr,
                auto_confirm=auto_confirm,
                session_id=session,
            )
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "swarm_id": swarm_id}

    async def _h_swarm_stop(self, _payload: dict, *, session: str) -> dict:
        text = await self.supervisor.stop(session_id=session)
        return {"text": text}

    async def _h_swarm_replay(self, _payload: dict, *, session: str) -> dict:
        """Re-push roster + log tail when the TUI missed boot/swarm_log pushes."""
        if not self.supervisor.is_running(session):
            return {"ok": True, "running": False}
        sess = self.state.get_session(session)
        challenge = str(sess.get("challenge_dir") or "").strip()
        roster = self.supervisor.last_roster(session)
        models = self.supervisor.last_models(session)
        if roster:
            self.state.broadcast(
                {
                    "type": "swarm_roster",
                    "session": session,
                    "agents": list(roster),
                    "models": list(models),
                }
            )
        for text in self.supervisor.replay_tail(challenge or None, session):
            self.state.broadcast({"type": "swarm_log", "session": session, "text": text})
        return {"ok": True, "running": True}

    # ---- dialog answers (TUI → daemon → swarm) --------------------------
    async def _h_flag_confirm_answer(self, payload: dict, *, session: str) -> dict:
        rid = payload.get("request_id")
        ok = bool(payload.get("ok"))
        reason = str(payload.get("reason") or "").strip()[:400]
        fut = pop_dialog(rid, session=session) if rid else None
        if fut and not fut.done():
            fut.set_result({"ok": ok, "reason": reason})
            return {"ok": True}
        return {"ok": False, "error": "no pending confirm for that id"}

    async def _h_flags_ask_answer(self, payload: dict, *, session: str) -> dict:
        rid = payload.get("request_id")
        n = payload.get("n")
        fut = pop_dialog(rid, session=session) if rid else None
        if fut and not fut.done():
            fut.set_result({"n": n, "ok": True})
            return {"ok": True}
        return {"ok": False, "error": "no pending flags-ask for that id"}
