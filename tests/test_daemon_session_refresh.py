"""session_refresh: rehydrate session.json and push session_update to TUI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon.server import Daemon
from backend.shell.sandbox_session import save_session_state


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = Path("/tmp") / f"artemis-refresh-{os.getpid()}"
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


def test_session_refresh_pushes_accepted_flags(daemon_env: str) -> None:
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
            tr, tw = await asyncio.open_unix_connection(daemon_env)
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            tw.write(
                (json.dumps({"v": 1, "id": "r1", "type": "session_refresh", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()

            seen = False
            for _ in range(40):
                line = await asyncio.wait_for(tr.readline(), 3)
                msg = json.loads(line)
                if msg.get("type") == "session_update":
                    st = msg.get("session_state") or {}
                    if "CTF{ok}" in (st.get("accepted_flags") or []):
                        seen = True
                        break
                if msg.get("id") == "r1" and msg.get("type") == "session_refresh":
                    continue
            assert seen, "session_update never carried accepted_flags"
        finally:
            tw.close()
            await _stop(d, task)

    asyncio.run(run())
