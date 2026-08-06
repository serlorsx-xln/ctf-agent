"""A TUI reconnect must not cancel an in-flight flag confirm.

The daemon used to reject every pending dialog the moment a TUI socket closed,
so an auto-reconnect (or a second TUI window) silently declined the operator's
flag. Cancelling is now deferred until no TUI comes back.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.daemon import handlers as handlers_mod
from backend.daemon import server as server_mod
from backend.daemon.server import Daemon
from backend.daemon.transport import open_connection
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-ddg")
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
    await asyncio.sleep(0.2)
    return d, task


async def _stop(d: Daemon, task: asyncio.Task) -> None:
    d.request_stop()
    task.cancel()
    try:
        await asyncio.wait_for(task, 2.0)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def _hello(sock: str, role: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await open_connection()
    hello = {"v": 1, "id": "h", "type": "hello", "role": role, "session": "s1"}
    w.write((json.dumps(hello) + "\n").encode())
    await w.drain()
    await asyncio.wait_for(r.readline(), 3)
    return r, w


async def _await_push(reader: asyncio.StreamReader, mtype: str, timeout: float = 5.0) -> dict:
    for _ in range(20):
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            break
        msg = json.loads(line)
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw {mtype}")


def test_reconnect_keeps_pending_confirm(daemon_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "TUI_RECONNECT_GRACE_S", 1.0)

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await _hello(daemon_env, "tui")
            sr, sw = await _hello(daemon_env, "swarm")
            sw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "fc",
                            "type": "flag_confirm_request",
                            "request_id": "FC1",
                            "flag": "flag{x}",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await sw.drain()
            await _await_push(tr, "flag_confirm_request")

            # TUI restarts inside the grace window.
            tw.close()
            await asyncio.sleep(0.2)
            tr2, tw2 = await _hello(daemon_env, "tui")
            await asyncio.sleep(1.4)

            assert ("s1", "FC1") in handlers_mod._pending_dialogs, "reconnect cancelled the confirm"

            tw2.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "a",
                            "type": "flag_confirm_answer",
                            "request_id": "FC1",
                            "ok": True,
                            "session": "s1",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await tw2.drain()
            resp = json.loads(await asyncio.wait_for(sr.readline(), 5))
            assert resp["ok"] is True
            tw2.close()
            sw.close()
            _ = tr2
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_no_tui_returns_cancels_confirm(daemon_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "TUI_RECONNECT_GRACE_S", 0.3)

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await _hello(daemon_env, "tui")
            sr, sw = await _hello(daemon_env, "swarm")
            sw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "fc",
                            "type": "flag_confirm_request",
                            "request_id": "FC2",
                            "flag": "flag{y}",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await sw.drain()
            await _await_push(tr, "flag_confirm_request")

            tw.close()
            # Nobody reconnects: the swarm must unblock rather than hang.
            resp = json.loads(await asyncio.wait_for(sr.readline(), 5))
            assert resp["ok"] is False
            assert ("s1", "FC2") not in handlers_mod._pending_dialogs
            sw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
