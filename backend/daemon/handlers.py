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

# Pending dialogs keyed by (session_id, request_id) → (future, kind, meta).
_pending_dialogs: dict[tuple[str, str], tuple[asyncio.Future[Any], str, dict[str, Any]]] = {}


def _operator_followup_crumb(
    *,
    delivery: str,
    who: str,
    no_fanout: bool,
    models: list[str],
    target: str | None,
    roster: list[str],
) -> str:
    """Human crumb for Send now / Queue — interrupt vs idle-queue."""
    if no_fanout:
        if delivery == "queue":
            return f"[artemis] followup — Hold · queued for {who} (after writeup)"
        return f"[artemis] followup — Hold · delivering to {who}"

    from backend.models import agent_display_key, provider_from_spec

    def _is_cursor(spec: str) -> bool:
        try:
            return provider_from_spec(spec) == "cursor"
        except Exception:
            return False

    # Resolve which models the crumb applies to (target agent → matching specs).
    # Prefer roster display keys (``opus#2``) over raw specs so #N targets match.
    relevant = list(models)
    if target and models:
        matched: list[str] = []
        for i, spec in enumerate(models):
            keys: list[str] = []
            if i < len(roster):
                keys.append(roster[i])
            try:
                keys.append(agent_display_key(spec, spec))
            except Exception:
                keys.append(spec.rsplit("/", 1)[-1])
            for key in keys:
                if (
                    key == target
                    or key.startswith(f"{target}#")
                    or target.startswith(f"{key}#")
                ):
                    matched.append(spec)
                    break
        if matched:
            relevant = matched

    has_cursor = any(_is_cursor(m) for m in relevant) if relevant else False
    all_soft = bool(relevant) and not has_cursor
    # Unknown models (empty) → Cursor-style copy (historical default).
    if delivery == "queue":
        if all_soft:
            return (
                f"[artemis] followup — queued until next turn boundary on {who} "
                "(not interrupting)"
            )
        return (
            f"[artemis] followup — queued until idle on {who} "
            "(not interrupting)"
        )
    return (
        f"[artemis] followup — interrupting {who} "
        "(cancels mid-turn when possible, then processes your message)"
    )


def cancel_pending_dialogs(broadcast=None, session: str | None = None) -> None:
    """Cancel pending dialog futures.

    When ``session`` is set, only that session's dialogs are cancelled.
    When ``session`` is None, cancel everything (daemon shutdown).
    """
    sid_filter = normalize_session_id(session) if session is not None else None
    for key, (fut, kind, _meta) in list(_pending_dialogs.items()):
        sid, rid = key
        if sid_filter is not None and sid != sid_filter:
            continue
        if not fut.done():
            fut.set_result({"ok": False, "cancelled": True})
        _pending_dialogs.pop(key, None)
        if callable(broadcast):
            dismiss = (
                "flags_ask_dismiss" if kind == "flags_ask" else "flag_confirm_dismiss"
            )
            try:
                broadcast(
                    {
                        "type": dismiss,
                        "session": sid,
                        "request_id": rid,
                    }
                )
            except Exception:
                logger.debug("%s broadcast failed", dismiss, exc_info=True)


def register_dialog(
    rid: str,
    fut: asyncio.Future[Any],
    session: str | None = None,
    *,
    kind: str = "flag_confirm",
    meta: dict[str, Any] | None = None,
) -> None:
    sid = normalize_session_id(session)
    _pending_dialogs[(sid, rid)] = (
        fut,
        kind if kind in ("flag_confirm", "flags_ask") else "flag_confirm",
        dict(meta or {}),
    )


def pop_dialog(rid: str, session: str | None = None) -> asyncio.Future[Any] | None:
    sid = normalize_session_id(session)
    entry = _pending_dialogs.pop((sid, rid), None)
    return entry[0] if entry else None


def rebroadcast_pending_dialogs(broadcast, session: str | None = None) -> int:
    """Re-push open confirm/ask dialogs after a TUI reconnect. Returns count."""
    if not callable(broadcast):
        return 0
    sid_filter = normalize_session_id(session) if session is not None else None
    n = 0
    for (sid, rid), (fut, kind, meta) in list(_pending_dialogs.items()):
        if sid_filter is not None and sid != sid_filter:
            continue
        if fut.done():
            continue
        try:
            if kind == "flags_ask":
                broadcast(
                    {
                        "type": "flags_ask_request",
                        "request_id": rid,
                        "default": meta.get("default"),
                        "challenge": meta.get("challenge"),
                        "session": sid,
                    }
                )
            else:
                broadcast(
                    {
                        "type": "flag_confirm_request",
                        "request_id": rid,
                        "flag": meta.get("flag"),
                        "session": sid,
                    }
                )
            n += 1
        except Exception:
            logger.debug("rebroadcast pending dialog failed", exc_info=True)
    return n


class Handlers:
    """Holds shared state for request dispatch."""

    def __init__(self, state: DaemonState, supervisor: SwarmSupervisor) -> None:
        self.state = state
        self.supervisor = supervisor
        self._setup_task: asyncio.Task[None] | None = None

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
        from backend.sandbox.setup_ready import probe_setup_status

        if self.supervisor.is_running(session):
            return {
                "text": "ERROR: swarm still running — /stop first, then load a new challenge",
                "session_state": self.state.get_session(session),
            }
        status = probe_setup_status()
        if not status.ready:
            return {
                "text": f"ERROR: sandbox not installed — {status.message}",
                "session_state": self.state.get_session(session),
                "setup": status.as_dict(),
            }
        from backend.shell.bridge import _load_challenge

        text = await _load_challenge(self._bridge_payload(payload, session))
        from backend.shell.sandbox_session import load_session_state

        if str(text).lstrip().upper().startswith("ERROR"):
            # Bridge did not write session.json — do not re-read disk into daemon
            # state or push stale challenge_dir back to the TUI on a failed load.
            return {"text": text, "session_state": self.state.get_session(session)}

        self.state.set_session(load_session_state(session), session_id=session)
        # TUI owns the gate. Broadcast so flags → mode → models opens after load.
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
        if self.supervisor.is_running(session):
            return {
                "ok": False,
                "error": "swarm still running — /stop first",
                "session_state": self.state.get_session(session),
            }
        self.state.clear_session(session)
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
        """Stop containers + swarm via supervisor (not raw bridge race kill)."""
        from backend.shell.sandbox_session import stop_sandbox

        parts = [
            await stop_sandbox(payload.get("challenge_dir"), session_id=session),
            await self.supervisor.stop(session_id=session),
        ]
        return {"text": "\n".join(parts)}

    # ---- swarm -----------------------------------------------------------
    async def _h_swarm_start(self, payload: dict, *, session: str) -> dict:
        from backend.sandbox.setup_ready import probe_setup_status

        status = probe_setup_status()
        if not status.ready:
            return {
                "ok": False,
                "error": f"sandbox not installed — {status.message}",
                "setup": status.as_dict(),
            }
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

    async def _h_swarm_operator_message(self, payload: dict, *, session: str) -> dict:
        """Append a mid-solve operator note for solvers to pick up."""
        if not self.supervisor.is_running(session):
            return {"ok": False, "error": "no swarm running"}
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        delivery = str(payload.get("delivery") or "steer").strip().lower()
        if delivery not in ("steer", "queue"):
            delivery = "steer"
        raw_target = str(payload.get("target") or "").strip() or None
        no_fanout = bool(payload.get("no_fanout") or payload.get("noFanout"))
        from backend.operator_inbox import append_operator_note, normalize_operator_target

        target = normalize_operator_target(raw_target)
        roster = [str(a).strip() for a in self.supervisor.last_roster(session) if str(a).strip()]
        # Fan-out broadcast → one inbox row per agent so each Cursor can claim.
        # no_fanout / Hold: single row scoped to the sole roster agent, or error
        # when multi-agent and no explicit winner target (avoid unscoped steal).
        targets: list[str | None]
        if target:
            targets = [target]
        elif no_fanout:
            # Hold / single without explicit target — prefer sole roster agent.
            # Multi-agent without a winner key must not write an unscoped row
            # (siblings could steal it; Hold refuses claimer=None on multi).
            if len(roster) == 1:
                targets = [roster[0]]
            else:
                return {
                    "ok": False,
                    "error": "Hold target unknown — open the winner agent page or wait for Solved by",
                }
        elif len(roster) > 1:
            targets = list(roster)
        elif len(roster) == 1:
            targets = [roster[0]]
        else:
            targets = [None]

        try:
            for tgt in targets:
                append_operator_note(
                    text,
                    session_id=session,
                    delivery=delivery,  # type: ignore[arg-type]
                    target=tgt,
                )
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        label = "queue" if delivery == "queue" else "steer"
        # One crumb per inbox row so sticky pending matches multi-agent fan-out.
        for tgt in targets:
            scope = f"→{tgt}" if tgt else ""
            self.state.broadcast(
                {
                    "type": "swarm_log",
                    "session": session,
                    "text": f"[artemis] you ({label}{scope}): {text[:2000]}",
                }
            )
        who = target or ("all agents" if len(targets) > 1 else "solver")
        models = [
            str(m).strip()
            for m in self.supervisor.last_models(session)
            if str(m).strip()
        ]
        crumb = _operator_followup_crumb(
            delivery=delivery,  # type: ignore[arg-type]
            who=who,
            no_fanout=no_fanout,
            models=models,
            target=target,
            roster=roster,
        )
        self.state.broadcast(
            {
                "type": "swarm_log",
                "session": session,
                "text": crumb,
            }
        )
        return {
            "ok": True,
            "delivery": delivery,
            "target": target,
            "fanout": len(targets),
        }

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

    # ---- first-run sandbox install gate ---------------------------------
    async def _h_setup_status(self, _payload: dict, *, session: str) -> dict:
        from backend.sandbox.setup_ready import probe_setup_status

        status = probe_setup_status().as_dict()
        task = getattr(self, "_setup_task", None)
        status["installing"] = bool(task is not None and not task.done())
        return status

    async def _h_setup_install(self, payload: dict, *, session: str) -> dict:
        """Start (or report) first-run L0+pack bake. Progress via setup_log pushes."""
        if getattr(self, "_setup_task", None) is not None and not self._setup_task.done():
            return {"ok": True, "running": True}
        skip_warm = payload.get("skip_warm_runtime", True)
        if skip_warm is None:
            skip_warm = True

        async def _run() -> None:
            from backend.sandbox.setup_ready import probe_setup_status, run_gate_install

            # Accumulate every session that was subscribed during this bake so
            # a mid-install chat rebind cannot leave fan-out empty / stuck on
            # the original session only.
            seen: set[str] = {session}

            def _fanout(event: dict) -> None:
                seen.update(self.state.subscribed_sessions())
                targets = seen or {session}
                for sid in targets:
                    self.state.broadcast({**event, "session": sid})

            def _push(text: str) -> None:
                _fanout({"type": "setup_log", "text": text})

            _push("Starting sandbox install (Docker L0 + pack bake)…")
            try:
                await run_gate_install(
                    skip_warm_runtime=bool(skip_warm),
                    on_progress=_push,
                )
            except Exception as e:
                logger.exception("setup_install failed")
                _push(f"FAIL {type(e).__name__}: {e}")
            status = probe_setup_status()
            _fanout({"type": "setup_done", **status.as_dict()})

        self._setup_task = asyncio.create_task(_run())
        return {"ok": True, "running": True}
