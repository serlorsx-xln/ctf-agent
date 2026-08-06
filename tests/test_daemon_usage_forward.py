"""Usage push: swarm ``usage_report`` → daemon ``usage_update`` → TUI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon.server import Daemon
from backend.daemon.transport import open_connection


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = Path("/tmp") / f"artemis-uf-{os.getpid()}"
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


def test_usage_report_forwards_to_tui(daemon_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Run under the daemon so cost_tracker._emit_usage_to_daemon fires.
    monkeypatch.setenv("ARTEMIS_DAEMON_ENDPOINT", f"unix:{daemon_env}")
    monkeypatch.setenv("ARTEMIS_DAEMON_SOCK", daemon_env)
    monkeypatch.setenv("ARTEMIS_SESSION_ID", "s1")
    # Force the publish throttle to fire.
    import backend.cost_tracker as ct

    monkeypatch.setattr(ct, "_LAST_PUBLISH_MONO", 0.0)

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)  # hello ack

            # Trigger a usage publish from the swarm side (this process).
            ct.publish_usage_snapshot(tokens=123, input_tokens=10, output_tokens=20, force=True)

            seen = None
            for _ in range(20):
                line = await asyncio.wait_for(tr.readline(), 2)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "usage_update":
                    seen = msg
                    break
            assert seen is not None, "never saw usage_update"
            assert seen["tokens"] == 123
            assert seen["cost_usd"] is None  # provider has not reported cost
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
