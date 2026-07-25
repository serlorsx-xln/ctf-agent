"""Artemis control-plane daemon: NDJSON over a Unix socket.

Lifecycle: started by ``chassis/bin/artemis`` alongside the cursor stub. Binds
``~/.cache/artemis/daemon.sock`` (mode 0600). On start: hydrate state from
``session.json`` and adopt any still-running swarm.

Two client roles (distinguished by the initial ``hello`` message):
  - ``tui``   : the TUI. Sends requests, receives push events (subscribed).
  - ``swarm`` : the swarm subprocess. Sends usage/dialog requests, blocks for
                responses. Its ``usage_report`` / ``flag_confirm_request`` /
                ``flags_ask_request`` are forwarded to TUI subscribers.

The daemon is a thin supervisor — it never owns the swarm as an asyncio task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from backend.daemon import handlers as handlers_mod
from backend.daemon import protocol
from backend.daemon.handlers import Handlers
from backend.daemon.session_id import normalize_session_id
from backend.daemon.socket_path import daemon_socket_path
from backend.daemon.state import DaemonState
from backend.daemon.supervisor import SwarmSupervisor

logger = logging.getLogger("backend.daemon")

# How long a pending flag confirm survives with no TUI attached. Long enough to
# cover a TUI restart, short enough that a truly headless swarm still unblocks.
TUI_RECONNECT_GRACE_S = 10.0


class Daemon:
    def __init__(self) -> None:
        self.state = DaemonState()
        self.supervisor = SwarmSupervisor(self.state)
        self.handlers = Handlers(self.state, self.supervisor)
        self.server: asyncio.Server | None = None
        self._stop_event = asyncio.Event()
        # Per-connection tasks, tracked so request_stop can cancel them and the
        # event loop doesn't hang on lingering blocked readers at shutdown.
        self._conn_tasks: set[asyncio.Task] = set()
        # Per-session deferred dialog cancel after last TUI for that session drops.
        self._dialog_cancel_tasks: dict[str, asyncio.Task] = {}

    # ---- TUI presence ----------------------------------------------------
    def tui_attached(self, session: str | None = None) -> None:
        """A TUI subscribed — keep that session's confirms alive across reconnect."""
        sid = normalize_session_id(session)
        task = self._dialog_cancel_tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()

    def tui_detached(self, session: str | None = None) -> None:
        """A TUI dropped. Cancel that session's dialogs only if none reconnects.

        Does not stop the swarm (operator may reconnect). Disconnect never
        cancels other sessions' dialogs.
        """
        sid = normalize_session_id(session)
        if self.state.has_subscribers(sid):
            return
        existing = self._dialog_cancel_tasks.get(sid)
        if existing is not None and not existing.done():
            return
        self._dialog_cancel_tasks[sid] = asyncio.create_task(
            self._cancel_dialogs_after_grace(sid)
        )

    async def _cancel_dialogs_after_grace(self, session_id: str) -> None:
        try:
            await asyncio.sleep(TUI_RECONNECT_GRACE_S)
        except asyncio.CancelledError:
            return
        self._dialog_cancel_tasks.pop(session_id, None)
        if self.state.has_subscribers(session_id):
            return
        logger.info(
            "no TUI for session %s within %.0fs — cancelling pending dialogs",
            session_id,
            TUI_RECONNECT_GRACE_S,
        )
        handlers_mod.cancel_pending_dialogs(
            broadcast=self.state.broadcast, session=session_id
        )

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.state.hydrate_all()
        sock_path = daemon_socket_path()

        # If a socket file exists, probe liveness; adopt-or-rebind.
        if sock_path.exists():
            if _socket_alive(sock_path):
                logger.warning("daemon already running on %s — exiting", sock_path)
                # Idempotent: another daemon owns it.
                sys.exit(0)
            try:
                sock_path.unlink()
            except OSError:
                pass

        self.server = await asyncio.start_unix_server(self._on_connect, path=str(sock_path))
        try:
            sock_path.chmod(0o600)
        except OSError:
            pass
        logger.info("daemon listening on %s", sock_path)

        # Re-adopt a swarm that outlived a previous daemon instance.
        try:
            adopted = await self.supervisor.adopt_if_running()
            if adopted:
                logger.info("adopted running swarm")
        except Exception:
            logger.debug("adopt failed", exc_info=True)

    async def serve(self) -> None:
        assert self.server is not None
        # serve_forever returns when the server is closed (request_stop) or the
        # task is cancelled. We deliberately avoid `async with` / `wait_closed`
        # here — those wait for lingering client connections to close, which
        # hangs when a client socket is still half-open (common in tests).
        try:
            await self.server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self.server.close()
            except Exception:
                pass

    def request_stop(self) -> None:
        """Graceful shutdown: close the socket server and wake ``serve()``.

        Swarm + sandbox cleanup runs in ``shutdown()`` (called from ``_async_main``
        finally) — signal handlers must stay sync and non-blocking.
        """
        self._stop_event.set()
        # Resolve any blocked dialogs so solvers don't hang.
        for task in list(self._dialog_cancel_tasks.values()):
            if not task.done():
                task.cancel()
        self._dialog_cancel_tasks.clear()
        handlers_mod.cancel_pending_dialogs()
        # Cancel per-connection tasks so blocked readers don't keep the loop open.
        for t in list(self._conn_tasks):
            if not t.done():
                t.cancel()
        self._conn_tasks.clear()
        if self.server is not None:
            self.server.close()

    async def shutdown(self) -> None:
        """Stop all live session swarms and orphan sandbox containers, then tear down."""
        try:
            await self.supervisor.stop_all()
        except Exception:
            logger.debug("supervisor.stop_all during shutdown failed", exc_info=True)
        try:
            from backend.sandbox import cleanup_orphan_containers

            await cleanup_orphan_containers()
        except Exception:
            logger.debug("cleanup_orphan_containers during shutdown failed", exc_info=True)
        self.request_stop()

    # ---- connection handling --------------------------------------------
    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Track this connection's task so request_stop can cancel it; otherwise
        # a blocked reader.readline() keeps the event loop alive at shutdown.
        task = asyncio.current_task()
        if task is not None:
            self._conn_tasks.add(task)
            task.add_done_callback(self._conn_tasks.discard)
        peer = _PeerConn(reader, writer, self)
        try:
            await peer.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("connection error", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


class _PeerConn:
    """One socket connection (TUI or swarm). Handles framing + dispatch."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, daemon: Daemon):
        self.reader = reader
        self.writer = writer
        self.daemon = daemon
        self.role: str | None = None
        self.session: str | None = None
        self.sub_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._buf = b""
        # Serialize writes: the pump task (push events) and the read loop
        # (responses) share this writer and must not interleave mid-message.
        self._write_lock = asyncio.Lock()

    async def run(self) -> None:
        # First line: hello.
        hello = await self._read_message()
        if hello is None:
            return
        if hello.get("type") != "hello":
            await self._send(protocol.make_error(hello.get("id"), "expected hello first"))
            return
        self.role = hello.get("role") or protocol.ROLE_TUI
        # Prefer hello.session; swarm/usage peers often set ARTEMIS_SESSION_ID.
        self.session = normalize_session_id(
            hello.get("session") or os.environ.get("ARTEMIS_SESSION_ID")
        )

        if self.role == protocol.ROLE_TUI:
            # Subscribe BEFORE hello ack so the client never races a
            # solve_flow_request pushed in the gap after ack.
            self.sub_queue = self.daemon.state.subscribe(self.session)
            self.daemon.tui_attached(self.session)
            self._pump_task = asyncio.create_task(self._pump_sub_queue())
            await self._send(
                protocol.make_response(
                    req_id=hello.get("id"),
                    type="hello",
                    role=self.role,
                    session=self.session,
                )
            )
            await self._run_tui()
        elif self.role == protocol.ROLE_USAGE:
            await self._send(
                protocol.make_response(
                    req_id=hello.get("id"),
                    type="hello",
                    role=self.role,
                    session=self.session,
                )
            )
            await self._run_usage()
        else:
            await self._send(
                protocol.make_response(
                    req_id=hello.get("id"),
                    type="hello",
                    role=self.role,
                    session=self.session,
                )
            )
            await self._run_swarm()

    def _sid(self, msg: dict[str, Any] | None = None) -> str:
        if msg and msg.get("session") is not None:
            return normalize_session_id(msg.get("session"))
        return normalize_session_id(self.session)

    # ---- TUI side --------------------------------------------------------
    async def _run_tui(self) -> None:
        # sub_queue + pump already started in run() before hello ack.
        assert self.sub_queue is not None
        sid = self._sid()
        pump = getattr(self, "_pump_task", None)
        if pump is None:
            pump = asyncio.create_task(self._pump_sub_queue())
            self._pump_task = pump
        try:
            sess = self.daemon.state.get_session(sid)
            await self._push(
                {
                    "type": "session_update",
                    "session": sid,
                    "session_state": sess,
                }
            )
            usage = self.daemon.state.get_usage(sid)
            if usage:
                await self._push({"type": "usage_update", "session": sid, **usage})
            # Replay swarm log only while this session's swarm is live.
            if self.daemon.supervisor.is_running(sid):
                roster = self.daemon.supervisor.last_roster(sid)
                if roster:
                    await self._push(
                        {
                            "type": "swarm_roster",
                            "session": sid,
                            "agents": list(roster),
                            "models": list(self.daemon.supervisor.last_models(sid)),
                        }
                    )
                challenge = sess.get("challenge_dir")
                for text in self.daemon.supervisor.replay_tail(challenge, sid):
                    await self._push({"type": "swarm_log", "session": sid, "text": text})
                await self._push(
                    {
                        "type": "replay_done",
                        "session": sid,
                        "running": True,
                        "started_at": self.daemon.supervisor.started_at_ms(sid),
                    }
                )
            elif sess.get("challenge_dir"):
                await self._push({"type": "replay_done", "session": sid, "running": False})
            await self._read_loop(handle_subscribe=True)
        finally:
            if pump is not None:
                pump.cancel()
            if self.sub_queue is not None:
                self.daemon.state.unsubscribe(self.sub_queue)
                self.sub_queue = None
            # Last TUI for this session gone: cancel that session's dialogs
            # after grace. Does not stop the swarm (reconnect-friendly).
            self.daemon.tui_detached(sid)

    async def _pump_sub_queue(self) -> None:
        assert self.sub_queue is not None
        while True:
            event = await self.sub_queue.get()
            await self._push(event)

    # ---- swarm side ------------------------------------------------------
    async def _run_usage(self) -> None:
        """Accept usage_report / session_refresh — never cancel flag-confirm dialogs on exit."""
        await self._read_loop(handle_subscribe=False)

    async def _run_swarm(self) -> None:
        """The swarm subprocess sends usage/dialog requests; we forward to TUI
        and hold a future for the answer."""
        try:
            await self._read_loop(handle_subscribe=False)
        finally:
            # Swarm connection dropped: resolve this session's in-flight dialogs.
            handlers_mod.cancel_pending_dialogs(
                broadcast=self.daemon.state.broadcast, session=self._sid()
            )

    async def _read_loop(self, *, handle_subscribe: bool) -> None:
        while True:
            msg = await self._read_message()
            if msg is None:
                return
            # Stamp peer session when the client omitted it.
            if msg.get("session") is None:
                msg = {**msg, "session": self._sid()}
            mtype = msg.get("type")
            if self.role == protocol.ROLE_SWARM:
                await self._handle_swarm_message(msg)
                continue
            if self.role == protocol.ROLE_USAGE:
                # Fire-and-forget peer: usage snapshots + post-ACCEPTED session
                # rehydrate. Must not subscribe or cancel dialogs on disconnect.
                if msg.get("type") in ("usage_report", "session_refresh"):
                    await self._handle_swarm_message(msg)
                continue
            # TUI request.
            if mtype == "subscribe":
                # Already subscribed on connect; replay already sent.
                await self._send(
                    protocol.make_response(
                        req_id=msg.get("id"), type="subscribe", ok=True, session=self._sid(msg)
                    )
                )
                continue
            resp = await self.daemon.handlers.dispatch(msg)
            if resp is not None:
                await self._send(resp)

    async def _handle_swarm_message(self, msg: dict[str, Any]) -> None:
        """Forward swarm-originated events to TUI; coordinate dialog answers."""
        mtype = msg.get("type")
        sid = self._sid(msg)
        if mtype == "usage_report":
            self.daemon.state.set_usage(
                {
                    "tokens": msg.get("tokens", 0),
                    "input": msg.get("input", 0),
                    "output": msg.get("output", 0),
                    "cache_read": msg.get("cache_read", 0),
                    "cost_usd": msg.get("cost_usd"),
                },
                session_id=sid,
            )
            await self._send(
                protocol.make_response(
                    req_id=msg.get("id"), type="usage_report", ok=True, session=sid
                )
            )
            return

        if mtype == "flag_confirm_request":
            rid = msg.get("request_id") or protocol.new_id()
            fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
            handlers_mod.register_dialog(rid, fut, session=sid)
            self.daemon.state.broadcast(
                {
                    "type": "flag_confirm_request",
                    "request_id": rid,
                    "flag": msg.get("flag"),
                    "session": sid,
                }
            )
            try:
                result = await fut
            except asyncio.CancelledError:
                result = {"ok": False}
            handlers_mod.pop_dialog(rid, session=sid)
            await self._send(
                protocol.make_response(
                    req_id=msg.get("id"),
                    type="flag_confirm_request",
                    ok=bool(result.get("ok")),
                    reason=str(result.get("reason") or ""),
                    session=sid,
                )
            )
            return

        if mtype == "flags_ask_request":
            rid = msg.get("request_id") or protocol.new_id()
            fut = asyncio.get_event_loop().create_future()
            handlers_mod.register_dialog(rid, fut, session=sid)
            nsubs = 1 if self.daemon.state.has_subscribers(sid) else 0
            logger.info(
                "flags_ask_request rid=%s session=%s subscribers=%d — broadcasting to TUI",
                rid,
                sid,
                nsubs,
            )
            self.daemon.state.broadcast(
                {
                    "type": "flags_ask_request",
                    "request_id": rid,
                    "default": msg.get("default"),
                    "challenge": msg.get("challenge"),
                    "session": sid,
                }
            )
            try:
                result = await fut
            except asyncio.CancelledError:
                result = {"n": msg.get("default", 1), "ok": False}
            handlers_mod.pop_dialog(rid, session=sid)
            await self._send(
                protocol.make_response(
                    req_id=msg.get("id"),
                    type="flags_ask_request",
                    ok=bool(result.get("ok")),
                    n=result.get("n"),
                    session=sid,
                )
            )
            return

        if mtype == "solver_log":
            self.daemon.state.broadcast(
                {"type": "swarm_log", "session": sid, "text": msg.get("text", "")}
            )
            return

        if mtype == "session_refresh":
            from backend.shell.sandbox_session import load_session_state

            self.daemon.state.set_session(load_session_state(sid) or {}, session_id=sid)
            await self._send(
                protocol.make_response(
                    req_id=msg.get("id"), type="session_refresh", ok=True, session=sid
                )
            )
            return

        # Unknown swarm message — ack so it doesn't block.
        await self._send(
            protocol.make_error(msg.get("id"), f"unknown swarm message: {mtype}", session=sid)
        )

    # ---- framing ---------------------------------------------------------
    async def _read_message(self) -> dict[str, Any] | None:
        # Loop (not recursion) so a peer streaming blank/garbage lines can't
        # overflow the stack.
        while True:
            while b"\n" not in self._buf:
                try:
                    chunk = await self.reader.read(4096)
                except (ConnectionResetError, asyncio.IncompleteReadError):
                    return None
                if not chunk:
                    return None
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                return protocol.decode(line)
            except protocol.ProtocolError as e:
                await self._send(protocol.make_error(None, str(e)))
                continue

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            async with self._write_lock:
                self.writer.write(protocol.encode(msg))
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    async def _push(self, event: dict[str, Any]) -> None:
        await self._send(event)


# ---- helpers --------------------------------------------------------------


def _socket_alive(sock_path: Path) -> bool:
    """Probe whether a daemon is already listening on the socket."""
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.25)
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


async def _async_main() -> None:
    logging.basicConfig(
        level=os.environ.get("ARTEMIS_DAEMON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = Daemon()
    await daemon.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            pass  # win32

    try:
        await daemon.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # Exit / SIGTERM: kill the detached swarm and reclaim Docker sandboxes.
        # (Previously the swarm survived daemon death and burned CPU/disk.)
        try:
            await daemon.shutdown()
        except Exception:
            logger.debug("daemon.shutdown failed", exc_info=True)
        handlers_mod.cancel_pending_dialogs()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
