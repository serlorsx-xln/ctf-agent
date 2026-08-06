"""End-to-end daemon socket tests: hello, status, flag-confirm dialog flow.

These use plain ``asyncio.run`` inside sync test functions (not pytest-asyncio
markers) because pytest-asyncio 1.4's managed event loop does not cooperate
well with a long-lived ``start_unix_server`` task in the same loop.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.daemon.server import Daemon
from backend.daemon.transport import open_connection
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-dfc")
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
    except (TimeoutError, asyncio.CancelledError):
        pass


def test_hello_and_status(daemon_env: str) -> None:
    async def run() -> None:
        d, task = await _start()
        try:
            r, w = await open_connection()
            w.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await w.drain()
            ack = json.loads(await asyncio.wait_for(r.readline(), 3))
            assert ack["ok"] is True
            w.write(
                (json.dumps({"v": 1, "id": "q", "type": "status", "session": "s1"}) + "\n").encode()
            )
            await w.drain()
            seen = False
            for _ in range(10):
                line = await asyncio.wait_for(r.readline(), 2)
                if not line:
                    break
                m = json.loads(line)
                if m.get("id") == "q":
                    seen = True
                    break
            assert seen, "never saw status response"
            w.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_flag_confirm_dialog_flow(daemon_env: str) -> None:
    """Swarm asks for confirm → TUI sees push → TUI answers → swarm unblocks."""

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)  # hello ack

            async def swarm_side() -> dict:
                sr, sw = await open_connection()
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
                if msg.get("type") == "flag_confirm_request":
                    assert msg["flag"] == "flag{x}"
                    tw.write(
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
                    await tw.drain()
                    await asyncio.wait_for(tr.readline(), 3)  # ack
                    seen = True
                    break
            assert seen, "never saw flag_confirm_request"

            resp = await asyncio.wait_for(swarm_task, 5)
            assert resp["ok"] is True
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_flag_confirm_rejection_carries_the_operator_reason(daemon_env: str) -> None:
    """A rejection must reach the swarm with the operator's explanation attached."""

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            async def swarm_side() -> dict:
                sr, sw = await open_connection()
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
                                "id": "fc",
                                "type": "flag_confirm_request",
                                "request_id": "FC2",
                                "flag": "flag{wrong_but_plausible}",
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
                if msg.get("type") == "flag_confirm_request":
                    tw.write(
                        (
                            json.dumps(
                                {
                                    "v": 1,
                                    "id": "a",
                                    "type": "flag_confirm_answer",
                                    "request_id": "FC2",
                                    "ok": False,
                                    "reason": "that's the sample in the README",
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
            assert seen, "never saw flag_confirm_request"

            resp = await asyncio.wait_for(swarm_task, 5)
            assert resp["ok"] is False
            assert resp["reason"] == "that's the sample in the README"
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_flag_confirm_accept_relays_an_empty_reason(daemon_env: str) -> None:
    """The field is always present so the solver never has to guess its shape."""

    async def run() -> None:
        d, task = await _start()
        try:
            tr, tw = await open_connection()
            tw.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}) + "\n").encode()
            )
            await tw.drain()
            await asyncio.wait_for(tr.readline(), 3)

            async def swarm_side() -> dict:
                sr, sw = await open_connection()
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
                                "id": "fc",
                                "type": "flag_confirm_request",
                                "request_id": "FC3",
                                "flag": "flag{right}",
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

            for _ in range(10):
                line = await asyncio.wait_for(tr.readline(), 5)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "flag_confirm_request":
                    tw.write(
                        (
                            json.dumps(
                                {
                                    "v": 1,
                                    "id": "a",
                                    "type": "flag_confirm_answer",
                                    "request_id": "FC3",
                                    "ok": True,
                                    "session": "s1",
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await tw.drain()
                    await asyncio.wait_for(tr.readline(), 3)
                    break

            resp = await asyncio.wait_for(swarm_task, 5)
            assert resp["ok"] is True
            assert resp["reason"] == ""
            tw.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())


def test_unknown_type_returns_error(daemon_env: str) -> None:
    async def run() -> None:
        d, task = await _start()
        try:
            r, w = await open_connection()
            w.write((json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui"}) + "\n").encode())
            await w.drain()
            await asyncio.wait_for(r.readline(), 3)
            w.write((json.dumps({"v": 1, "id": "x", "type": "bogus_type"}) + "\n").encode())
            await w.drain()
            seen = False
            for _ in range(10):
                line = await asyncio.wait_for(r.readline(), 3)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("id") == "x":
                    assert msg["type"] == "error"
                    assert "bogus_type" in msg["error"]
                    seen = True
                    break
            assert seen, "never saw error response"
            w.close()
        finally:
            await _stop(d, task)

    asyncio.run(run())
