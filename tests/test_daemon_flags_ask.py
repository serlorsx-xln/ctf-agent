"""Flags-ask dialog flow: swarm-initiated ``flags_ask_request`` over the socket."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon.server import Daemon


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = Path("/tmp") / f"artemis-fa-{os.getpid()}"
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


def test_flags_ask_dialog_flow(daemon_env: str) -> None:
    """Swarm asks N → TUI sees push → TUI answers → swarm gets N."""

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await asyncio.open_unix_connection(daemon_env)
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)  # hello ack

            async def swarm_side() -> dict:
                sr, sw = await asyncio.open_unix_connection(daemon_env)
                sw.write(
                    (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "swarm", "session": "s1"}) + "\n").encode()
                )
                await sw.drain()
                await asyncio.wait_for(sr.readline(), 3)
                sw.write(
                    (
                        json.dumps(
                            {
                                "v": 1,
                                "id": "fa",
                                "type": "flags_ask_request",
                                "request_id": "FA1",
                                "default": 1,
                                "challenge": "demo",
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await sw.drain()
                resp = json.loads(await asyncio.wait_for(sr.readline(), 5))
                sw.close()
                return resp

            swarm_task = asyncio.create_task(swarm_side())

            seen = False
            for _ in range(10):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "flags_ask_request":
                    assert msg["request_id"] == "FA1"
                    tw.write(
                        (
                            json.dumps(
                                {
                                    "v": 1,
                                    "id": "a",
                                    "type": "flags_ask_answer",
                                    "request_id": "FA1",
                                    "n": 3,
                                    "session": "s1",
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await tw.drain()
                    await asyncio.wait_for(tr.readline(), 3)  # ack
                    seen = True
                    break
            assert seen, "never saw flags_ask_request"

            resp = await asyncio.wait_for(swarm_task, 5)
            assert resp["ok"] is True
            assert resp["n"] == 3
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
