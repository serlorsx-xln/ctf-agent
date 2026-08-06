"""Multi-session swarm subprocess supervisor for the daemon.

Each OpenCode chat session can own one live swarm (``dict[sid, SwarmSlot]``).
No hard concurrent-session cap — machine resources decide how many can run.
Pidfiles and logs live under ``sessions/<sid>/``. Detached subprocesses
(``start_new_session=True``) outlive a daemon crash; adopt reattaches via
pidfile + disk log.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.daemon.session_id import DEFAULT_SESSION_ID, normalize_session_id
from backend.daemon.state import DaemonState

logger = logging.getLogger(__name__)

#: How many tail lines to replay on subscribe / adopt.
REPLAY_TAIL_LINES = 5000


@dataclass
class SwarmSlot:
    session_id: str
    proc: asyncio.subprocess.Process | None = None
    stream_task: asyncio.Task[None] | None = None
    swarm_id: str | None = None
    challenge_dir: str | None = None
    generation: int = 0
    adopted: bool = False
    last_roster: list[str] = field(default_factory=list)
    last_models: list[str] = field(default_factory=list)


def _kill_pid_tree(pid: int) -> None:
    from backend.shell.bridge import _kill_pid_tree as _impl

    _impl(pid)


def _is_artemis_race_pid(pid: int) -> bool:
    from backend.shell.bridge import _is_artemis_race_pid as _impl

    return _impl(pid)


def _pid_path(session_id: str | None = None) -> Path:
    from backend.shell.sandbox_session import swarm_pid_path

    return swarm_pid_path(session_id)


def _roster_path(session_id: str | None = None) -> Path:
    from backend.shell.sandbox_session import session_dir

    return session_dir(session_id) / "swarm_roster.json"


def _legacy_global_pid_path() -> Path:
    from backend.daemon.socket_path import cache_dir

    return cache_dir() / "swarm.pid"


def _legacy_race_pid_path() -> Path:
    from backend.daemon.socket_path import cache_dir

    return cache_dir() / "race.pid"


def swarm_log_path(challenge_dir: str | None, session_id: str | None = None) -> Path:
    """Append-only disk log path for a challenge's swarm stdout (per session)."""
    from backend.shell.sandbox_session import swarm_log_path_for_session

    return swarm_log_path_for_session(challenge_dir, session_id)


def _migrate_legacy_pidfile(session_id: str | None = None) -> None:
    sid = normalize_session_id(session_id)
    if sid != DEFAULT_SESSION_ID:
        return
    pid_path = _pid_path(sid)
    if pid_path.is_file():
        return
    for legacy in (_legacy_global_pid_path(), _legacy_race_pid_path()):
        if not legacy.is_file():
            continue
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(pid_path)
            return
        except OSError:
            try:
                pid_path.parent.mkdir(parents=True, exist_ok=True)
                pid_path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
                legacy.unlink(missing_ok=True)
                return
            except OSError:
                pass


def _read_running_pid(session_id: str | None = None) -> int | None:
    sid = normalize_session_id(session_id)
    _migrate_legacy_pidfile(sid)
    pid_path = _pid_path(sid)
    if not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if pid and _is_artemis_race_pid(pid):
        return pid
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


class SwarmSupervisor:
    """Owns concurrent live swarm subprocesses (one slot per session id)."""

    def __init__(self, state: DaemonState) -> None:
        self.state = state
        self._slots: dict[str, SwarmSlot] = {}
        self._lock = asyncio.Lock()

    def _get_slot(self, session_id: str | None) -> SwarmSlot:
        sid = normalize_session_id(session_id)
        slot = self._slots.get(sid)
        if slot is None:
            slot = SwarmSlot(session_id=sid)
            self._slots[sid] = slot
            self._load_persisted_roster(slot)
        return slot

    # ---- back-compat attributes (default / any live slot) ----------------
    @property
    def proc(self) -> asyncio.subprocess.Process | None:
        slot = self._any_live_slot() or self._slots.get(DEFAULT_SESSION_ID)
        return slot.proc if slot else None

    @property
    def swarm_id(self) -> str | None:
        slot = self._any_live_slot() or self._slots.get(DEFAULT_SESSION_ID)
        return slot.swarm_id if slot else None

    @property
    def challenge_dir(self) -> str | None:
        slot = self._any_live_slot() or self._slots.get(DEFAULT_SESSION_ID)
        return slot.challenge_dir if slot else None

    @property
    def _last_roster(self) -> list[str]:
        slot = self._any_live_slot() or self._slots.get(DEFAULT_SESSION_ID)
        return list(slot.last_roster) if slot else []

    @property
    def _last_models(self) -> list[str]:
        slot = self._any_live_slot() or self._slots.get(DEFAULT_SESSION_ID)
        return list(slot.last_models) if slot else []

    def _any_live_slot(self) -> SwarmSlot | None:
        for slot in self._slots.values():
            if self._slot_is_running(slot):
                return slot
        return None

    def _slot_is_running(self, slot: SwarmSlot) -> bool:
        if slot.proc is not None and slot.proc.returncode is None:
            return True
        return _read_running_pid(slot.session_id) is not None

    def _load_persisted_roster(self, slot: SwarmSlot) -> None:
        path = _roster_path(slot.session_id)
        if not path.is_file():
            return
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            agents = data.get("agents") or []
            models = data.get("models") or []
            if isinstance(agents, list) and agents:
                slot.last_roster = [str(a) for a in agents if a]
            if isinstance(models, list) and models:
                slot.last_models = [str(m) for m in models if m]
        except Exception:
            logger.debug("load persisted swarm roster failed", exc_info=True)

    def _persist_roster(self, slot: SwarmSlot) -> None:
        try:
            import json

            path = _roster_path(slot.session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"agents": slot.last_roster, "models": slot.last_models}),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("persist swarm roster failed", exc_info=True)

    def _clear_persisted_roster(self, slot: SwarmSlot) -> None:
        try:
            _roster_path(slot.session_id).unlink(missing_ok=True)
        except OSError:
            pass
        slot.last_roster = []
        slot.last_models = []

    # ---- queries ---------------------------------------------------------
    def is_running(self, session_id: str | None = None) -> bool:
        if session_id is None:
            return any(self._slot_is_running(s) for s in self._slots.values()) or (
                _read_running_pid(DEFAULT_SESSION_ID) is not None
            )
        return self._slot_is_running(self._get_slot(session_id))

    def running_count(self) -> int:
        sids = set(self._slots)
        sids.add(DEFAULT_SESSION_ID)
        # Also count disk pidfiles for sessions not yet in memory.
        try:
            from backend.cache import cache_dir

            root = cache_dir() / "sessions"
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir():
                        sids.add(child.name)
        except Exception:
            pass
        n = 0
        for sid in sids:
            if _read_running_pid(sid) is not None or (
                sid in self._slots and self._slot_is_running(self._slots[sid])
            ):
                n += 1
        return n

    def started_at_ms(self, session_id: str | None = None) -> int | None:
        try:
            return int(_pid_path(session_id).stat().st_mtime * 1000)
        except OSError:
            return None

    def last_roster(self, session_id: str | None = None) -> list[str]:
        return list(self._get_slot(session_id).last_roster)

    def last_models(self, session_id: str | None = None) -> list[str]:
        return list(self._get_slot(session_id).last_models)

    # ---- spawn -----------------------------------------------------------
    async def spawn(
        self,
        *,
        challenge: str,
        models: list[str],
        flags_required: int | None,
        auto_confirm: bool = False,
        session_id: str | None = None,
    ) -> str:
        """Spawn a detached swarm subprocess. Returns a swarm_id."""
        sid = normalize_session_id(session_id)
        async with self._lock:
            replaced = self._slot_is_running(self._get_slot(sid))
            out = await self._spawn_unlocked(
                challenge=challenge,
                models=models,
                flags_required=flags_required,
                auto_confirm=auto_confirm,
                session_id=session_id,
            )
        if replaced:
            # Orphan cleanup used to run inside stop under the supervisor lock;
            # keep it off-lock so a hung Docker API cannot block the new spawn.
            await self._cleanup_orphans_bounded(sid)
        return out

    async def _spawn_unlocked(
        self,
        *,
        challenge: str,
        models: list[str],
        flags_required: int | None,
        auto_confirm: bool = False,
        session_id: str | None = None,
    ) -> str:
        from backend.daemon.protocol import new_id

        sid = normalize_session_id(session_id)
        slot = self._get_slot(sid)

        # Replace only this session's swarm (leave other slots alone).
        if self._slot_is_running(slot):
            await self._stop_unlocked(sid, emit_exit=False)

        slot.generation += 1
        gen = slot.generation

        log_path = swarm_log_path(challenge, sid)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        except Exception:
            logger.debug("truncate swarm log failed", exc_info=True)

        try:
            from backend.shell.sandbox_session import load_session_state
            from backend.shell.swarm_reset import reset_for_new_swarm

            reset_for_new_swarm(sid)
            self.state.set_usage({}, session_id=sid)
            try:
                self.state.set_session(load_session_state(sid) or {}, session_id=sid)
            except Exception:
                logger.debug("pre-spawn session hydrate failed", exc_info=True)
        except Exception:
            logger.debug("pre-spawn reset failed", exc_info=True)

        repo = os.environ.get("ARTEMIS_REPO_ROOT") or str(
            Path(__file__).resolve().parents[2]
        )
        cmd = [
            "uv",
            "run",
            "--directory",
            repo,
            "artemis",
            "swarm",
            "--challenge",
            challenge,
        ]
        if flags_required is not None:
            cmd.extend(["--flags-required", str(flags_required)])
        for m in models:
            cmd.extend(["--models", m])
        if auto_confirm:
            cmd.append("--auto-confirm-flags")

        from backend.daemon.transport import child_daemon_env

        env = {
            **os.environ,
            **child_daemon_env(),
            "ARTEMIS_FLAG_CONFIRM": "1",
            "ARTEMIS_SWARM_LOG": str(log_path),
            "ARTEMIS_SESSION_ID": sid,
        }

        flags_label = flags_required if flags_required is not None else "?"
        self._broadcast(
            sid,
            {"type": "boot", "text": f"Starting swarm · {Path(challenge).name} · flags={flags_label}"},
            gen=gen,
        )
        self._broadcast(sid, {"type": "boot", "text": f"models={', '.join(models)}"}, gen=gen)
        from backend.models import agent_display_key, assign_runner_ids

        agents = [agent_display_key(rid, spec) for rid, spec in assign_runner_ids(list(models))]
        self._broadcast(
            sid,
            {"type": "swarm_roster", "agents": agents, "models": list(models)},
            gen=gen,
        )
        self._broadcast(sid, {"type": "boot", "text": f"agents={', '.join(agents)}"}, gen=gen)
        slot.last_roster = list(agents)
        slot.last_models = list(models)
        self._persist_roster(slot)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        slot.proc = proc
        slot.challenge_dir = challenge
        slot.adopted = False
        slot.swarm_id = new_id()[:12]
        self.state.set_swarm_meta(sid, swarm_id=slot.swarm_id, swarm_running=True)

        pid_path = _pid_path(sid)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(proc.pid), encoding="utf-8")

        slot.stream_task = asyncio.create_task(
            self._stream_swarm_stdout(sid, proc, gen), name=f"swarm-stream-{slot.swarm_id}"
        )
        return slot.swarm_id

    async def _stream_swarm_stdout(
        self, session_id: str, proc: asyncio.subprocess.Process, gen: int
    ) -> None:
        slot = self._get_slot(session_id)
        assert proc.stdout is not None
        lines = 0
        try:
            while True:
                if gen != slot.generation:
                    return
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                lines += 1
                self._broadcast(session_id, {"type": "swarm_log", "text": text}, gen=gen)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("swarm stdout stream ended", exc_info=True)
        finally:
            if gen == slot.generation:
                try:
                    await proc.wait()
                except Exception:
                    pass
                code = proc.returncode
                try:
                    from backend.shell.sandbox_session import load_session_state

                    self.state.set_session(
                        load_session_state(session_id) or {}, session_id=session_id
                    )
                except Exception:
                    logger.debug("rehydrate session before swarm_exit failed", exc_info=True)
                self._broadcast(
                    session_id, {"type": "swarm_exit", "code": code, "lines": lines}, gen=gen
                )
                self._clear_persisted_roster(slot)
                self.state.set_swarm_meta(session_id, swarm_running=False)
                try:
                    _pid_path(session_id).unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- adopt (restart reattach) ---------------------------------------
    async def adopt_if_running(self) -> bool:
        """On daemon start, adopt any still-running session swarms."""
        async with self._lock:
            adopted_any = False
            sids: set[str] = {DEFAULT_SESSION_ID}
            try:
                from backend.cache import cache_dir

                root = cache_dir() / "sessions"
                if root.is_dir():
                    for child in root.iterdir():
                        if child.is_dir():
                            sids.add(child.name)
            except Exception:
                pass
            for sid in sorted(sids):
                if await self._adopt_if_running_unlocked(sid):
                    adopted_any = True
            return adopted_any

    async def _adopt_if_running_unlocked(self, session_id: str) -> bool:
        sid = normalize_session_id(session_id)
        pid = _read_running_pid(sid)
        if not pid:
            return False
        slot = self._get_slot(sid)
        sess = self.state.get_session(sid)
        challenge = (sess.get("challenge_dir") or "").strip() or None
        slot.challenge_dir = challenge
        slot.adopted = True
        slot.swarm_id = f"adopt-{pid}"
        slot.generation += 1
        gen = slot.generation
        self._load_persisted_roster(slot)
        self.state.set_swarm_meta(sid, swarm_id=slot.swarm_id, swarm_running=True)
        self._broadcast(
            sid,
            {
                "type": "swarm_adopted",
                "pid": pid,
                "challenge": challenge or "",
                "started_at": self.started_at_ms(sid),
            },
            gen=gen,
        )
        if slot.last_roster:
            self._broadcast(
                sid,
                {
                    "type": "swarm_roster",
                    "agents": list(slot.last_roster),
                    "models": list(slot.last_models),
                },
                gen=gen,
            )
        slot.stream_task = asyncio.create_task(
            self._tail_for_adopt(sid, pid, gen), name=f"swarm-adopt-{pid}"
        )
        return True

    async def _tail_for_adopt(self, session_id: str, pid: int, gen: int) -> None:
        slot = self._get_slot(session_id)
        log_path = swarm_log_path(slot.challenge_dir, session_id)
        lines = 0
        try:
            end_offset = log_path.stat().st_size if log_path.is_file() else 0
            for text in _read_tail(log_path, REPLAY_TAIL_LINES, end_offset=end_offset):
                if gen != slot.generation:
                    return
                lines += 1
                self._broadcast(session_id, {"type": "swarm_log", "text": text}, gen=gen)
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(end_offset)
                while True:
                    if gen != slot.generation:
                        return
                    if not _pid_alive(pid):
                        break
                    line = fh.readline()
                    if line:
                        lines += 1
                        self._broadcast(
                            session_id,
                            {"type": "swarm_log", "text": line.rstrip("\n")},
                            gen=gen,
                        )
                    else:
                        await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("adopt tail ended", exc_info=True)
        finally:
            if gen == slot.generation:
                try:
                    from backend.shell.sandbox_session import load_session_state

                    self.state.set_session(
                        load_session_state(session_id) or {}, session_id=session_id
                    )
                except Exception:
                    logger.debug("rehydrate session before adopt swarm_exit failed", exc_info=True)
                self._broadcast(
                    session_id, {"type": "swarm_exit", "code": None, "lines": lines}, gen=gen
                )
                self._clear_persisted_roster(slot)
                self.state.set_swarm_meta(session_id, swarm_running=False)
                try:
                    _pid_path(session_id).unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- stop ------------------------------------------------------------
    async def stop(self, session_id: str | None = None) -> str:
        """Stop one session's swarm (default ``_default``)."""
        sid = normalize_session_id(session_id)
        async with self._lock:
            msg = await self._stop_unlocked(sid)
        await self._cleanup_orphans_bounded(sid)
        return msg

    async def stop_all(self) -> str:
        """Stop every live session swarm (daemon shutdown)."""
        sids: set[str] = set()
        async with self._lock:
            msgs: list[str] = []
            sids = set(self._slots)
            sids.add(DEFAULT_SESSION_ID)
            try:
                from backend.cache import cache_dir

                root = cache_dir() / "sessions"
                if root.is_dir():
                    for child in root.iterdir():
                        if child.is_dir():
                            sids.add(child.name)
            except Exception:
                pass
            for sid in sorted(sids):
                msgs.append(await self._stop_unlocked(sid))
        # Cleanup outside the supervisor lock so spawn/stop of other sessions
        # are not blocked by a hung Docker API.
        for sid in sorted(sids):
            await self._cleanup_orphans_bounded(sid)
        return "; ".join(msgs)

    async def _cleanup_orphans_bounded(self, session_id: str) -> None:
        try:
            from backend.sandbox import cleanup_orphan_containers

            await asyncio.wait_for(
                cleanup_orphan_containers(session_id=session_id),
                timeout=45.0,
            )
        except TimeoutError:
            logger.warning(
                "cleanup_orphan_containers timed out after 45s (session=%s)",
                session_id,
            )
        except Exception:
            logger.debug("cleanup_orphan_containers after stop failed", exc_info=True)

    async def _stop_unlocked(self, session_id: str, *, emit_exit: bool = True) -> str:
        sid = normalize_session_id(session_id)
        slot = self._get_slot(sid)
        was_live = self._slot_is_running(slot) or (
            slot.proc is not None and slot.proc.returncode is None
        )
        slot.generation += 1
        gen = slot.generation
        killed: list[str] = []
        pid = _read_running_pid(sid)
        if pid:
            _kill_pid_tree(pid)
            killed.append(str(pid))
        try:
            _pid_path(sid).unlink(missing_ok=True)
        except OSError:
            pass
        # Session-scoped dialog cancel (not global).
        self._cancel_pending_dialogs(sid)
        task = slot.stream_task
        slot.stream_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass
        slot.proc = None
        self.state.set_swarm_meta(sid, swarm_running=False)
        # Emit exit before orphan cleanup so the TUI unlocks immediately.
        if gen == slot.generation:
            self._clear_persisted_roster(slot)
            if was_live and emit_exit:
                self._broadcast(sid, {"type": "swarm_exit", "code": None, "lines": 0}, gen=gen)
        if killed:
            return f"Stopped swarm process(es): {', '.join(killed)}"
        return "No active swarm to stop"

    # ---- replay ----------------------------------------------------------
    def replay_tail(
        self, challenge_dir: str | None, session_id: str | None = None
    ) -> list[str]:
        return _read_tail(swarm_log_path(challenge_dir, session_id), REPLAY_TAIL_LINES)

    # ---- helpers ---------------------------------------------------------
    def _broadcast(
        self, session_id: str, event: dict[str, Any], *, gen: int | None = None
    ) -> None:
        slot = self._get_slot(session_id)
        if gen is not None and gen != slot.generation:
            return
        payload = {**event, "session": normalize_session_id(session_id)}
        self.state.broadcast(payload)

    def _cancel_pending_dialogs(self, session_id: str | None = None) -> None:
        from backend.daemon import handlers as _h

        cancel = getattr(_h, "cancel_pending_dialogs", None)
        if callable(cancel):
            cancel(broadcast=self.state.broadcast, session=session_id)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_tail(path: Path, n: int, *, end_offset: int | None = None) -> list[str]:
    """Return up to ``n`` trailing lines of ``path`` without reading the whole file."""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        read_up_to = size if end_offset is None else min(end_offset, size)
        back = min(read_up_to, max(n * 256, 4096))
        with path.open("rb") as fh:
            fh.seek(max(0, read_up_to - back))
            data = fh.read(back)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    if back < read_up_to and "\n" in text:
        text = text.split("\n", 1)[1]
    lines = text.splitlines()
    return lines[-n:] if len(lines) > n else lines

