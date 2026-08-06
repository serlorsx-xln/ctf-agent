"""ROLE_USAGE peer: usage + session_refresh without canceling flag confirms."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon import handlers as handlers_mod
from backend.daemon.server import Daemon
from backend.daemon.transport import open_connection
from backend.shell.sandbox_session import save_session_state


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = Path("/tmp") / f"artemis-usage-role-{os.getpid()}"
    cache.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
    sock = cache / "daemon.sock"
    if sock.exists():
        sock.unlink()
    yield str(sock)
    if sock.exists():
        try:
            sock.unlink()
        except OSError:
            pass


async def _start() -> tuple[Daemon, asyncio.Task]:
    d = Daemon()
    await d.start()
    task = asyncio.create_task(d.serve())
    for _ in range(5):
        await asyncio.sleep(0.1)
    return d, task


async def _stop(d: Daemon, task: asyncio.Task) -> None:
    await asyncio.sleep(0.15)
    d.request_stop()
    task.cancel()
    try:
        await asyncio.wait_for(task, 2.0)
    except (asyncio.CancelledError, TimeoutError):
        pass


def test_usage_disconnect_does_not_cancel_pending_dialogs(daemon_env: str) -> None:
    async def run() -> None:
        d, task = await _start()
        try:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            handlers_mod.register_dialog("confirm-1", fut)

            ur, uw = await open_connection()
            uw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "usage", "session": "s1"}) + "\n").encode()
            )
            await uw.drain()
            await asyncio.wait_for(ur.readline(), 3)  # hello ack
            uw.close()
            try:
                await uw.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.2)

            assert not fut.done(), "usage disconnect must not cancel pending flag confirms"
            handlers_mod.cancel_pending_dialogs()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_usage_role_session_refresh_rehydrates(daemon_env: str) -> None:
    async def run() -> None:
        save_session_state(
            "s1",
            challenge_dir="/tmp/chal",
            challenge_name="chal",
            flags_required=1,
            accepted_flags=["CTF{ok}"],
        )
        d, task = await _start()
        try:
            # Real TUI subscriber to observe the push.
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            ur, uw = await open_connection()
            uw.write(
                (json.dumps({"v": 1, "id": "hu", "type": "hello", "role": "usage", "session": "s1"}) + "\n").encode()
            )
            await uw.drain()
            await asyncio.wait_for(ur.readline(), 3)
            uw.write(
                (
                    json.dumps({"v": 1, "id": "r1", "type": "session_refresh", "session": "s1"}) + "\n"
                ).encode()
            )
            await uw.drain()
            await asyncio.wait_for(ur.readline(), 3)  # refresh ack

            seen = False
            for _ in range(40):
                line = await asyncio.wait_for(tr.readline(), 3)
                msg = json.loads(line)
                if msg.get("type") == "session_update":
                    st = msg.get("session_state") or {}
                    if "CTF{ok}" in (st.get("accepted_flags") or []):
                        seen = True
                        break
            assert seen, "usage-role session_refresh must push accepted_flags to TUI"
            uw.close()
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
