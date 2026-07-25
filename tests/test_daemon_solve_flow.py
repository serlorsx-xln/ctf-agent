"""Solve-flow start: plugin request pushes solve_flow_request to TUI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon.server import Daemon


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = Path("/tmp") / f"artemis-sf-{os.getpid()}"
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


def test_solve_flow_start_registered(daemon_env: str) -> None:
    """solve_flow_start request broadcasts solve_flow_request to TUI subscribers."""

    async def run() -> None:
        cache = Path(os.environ["ARTEMIS_CACHE"])
        chal = cache / "chal"
        chal.mkdir(parents=True, exist_ok=True)
        (chal / "challenge.txt").write_text("test\n", encoding="utf-8")
        from backend.shell.sandbox_session import save_session_state

        save_session_state("s1", challenge_dir=str(chal), challenge_name="chal")

        d, task = await _start()
        try:
            tr, tw = await asyncio.open_unix_connection(daemon_env)
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            tw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "sf1",
                            "type": "solve_flow_start",
                            "default": 2,
                            "session": "s1",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await tw.drain()

            seen = False
            for _ in range(10):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "solve_flow_request":
                    assert msg.get("default_flags") == 2
                    seen = True
                    break
                if msg.get("id") == "sf1" and msg.get("type") == "solve_flow_start":
                    assert msg.get("ok") is True, msg
            assert seen, "never saw solve_flow_request push"
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
