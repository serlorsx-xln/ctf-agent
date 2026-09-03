"""Solve-flow start: plugin request pushes solve_flow_request to TUI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.daemon.server import Daemon
from backend.daemon.transport import open_connection
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-sf")
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
            tr, tw = await open_connection()
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


def test_load_broadcasts_solve_flow(daemon_env: str) -> None:
    """Successful load must push solve_flow_request so the TUI gate opens
    even when the chat model never calls artemis_ask_flags."""

    async def run() -> None:
        cache = Path(os.environ["ARTEMIS_CACHE"])
        chal = cache / "chal-load"
        chal.mkdir(parents=True, exist_ok=True)
        (chal / "challenge.txt").write_text("pwn story\n", encoding="utf-8")

        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s-load"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            tw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "ld1",
                            "type": "load",
                            "session": "s-load",
                            "path": str(chal),
                        }
                    )
                    + "\n"
                ).encode()
            )
            await tw.drain()

            seen = False
            load_ok = False
            for _ in range(20):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("id") == "ld1" and msg.get("type") == "load":
                    assert msg.get("ok") is not False, msg
                    assert "ERROR" not in str(msg.get("text", ""))
                    load_ok = True
                if msg.get("type") == "solve_flow_request":
                    assert msg.get("from_load") is True
                    assert msg.get("session") == "s-load"
                    seen = True
                    if load_ok:
                        break
            assert load_ok, "load response missing"
            assert seen, "successful load never pushed solve_flow_request"
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_failed_load_does_not_broadcast_solve_flow(daemon_env: str) -> None:
    """ERROR load must not open the flags → models gate."""

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s-bad"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            tw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "ld-bad",
                            "type": "load",
                            "session": "s-bad",
                            "path": "/no/such/challenge/dir",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await tw.drain()

            saw_error = False
            for _ in range(15):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "solve_flow_request":
                    raise AssertionError(f"failed load pushed solve_flow_request: {msg}")
                if msg.get("id") == "ld-bad":
                    text = str(msg.get("text") or msg.get("error") or "")
                    assert "ERROR" in text.upper() or msg.get("ok") is False
                    saw_error = True
                    break
            assert saw_error, "failed load response missing"
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_failed_load_keeps_daemon_session_state(daemon_env: str) -> None:
    """ERROR load must not re-read disk into daemon state or drop prior challenge."""

    async def run() -> None:
        cache = Path(os.environ["ARTEMIS_CACHE"])
        chal = cache / "keep"
        chal.mkdir(parents=True, exist_ok=True)
        (chal / "challenge.txt").write_text("test\n", encoding="utf-8")
        from backend.shell.sandbox_session import save_session_state

        save_session_state("s-keep", challenge_dir=str(chal), challenge_name="keep")

        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (
                    json.dumps(
                        {"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s-keep"}
                    )
                    + "\n"
                ).encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            tw.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "ld-keep",
                            "type": "load",
                            "session": "s-keep",
                            "path": "/no/such/challenge/dir",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await tw.drain()

            saw = False
            for _ in range(15):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("id") != "ld-keep":
                    continue
                saw = True
                st = msg.get("session_state") or {}
                assert st.get("challenge_name") == "keep"
                assert str(st.get("challenge_dir") or "").endswith("keep")
                assert "ERROR" in str(msg.get("text") or "").upper()
                break
            assert saw, "failed load response missing"
            mem = d.state.get_session("s-keep")
            assert mem.get("challenge_name") == "keep"
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
