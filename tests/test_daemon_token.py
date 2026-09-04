"""Daemon hello token is required only when ARTEMIS_DAEMON_TOKEN is set."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.daemon.auth import hello_authorized, with_hello_token
from backend.daemon.server import Daemon
from backend.daemon.transport import daemon_alive_async, open_connection
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def token_daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-token")
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "1")
    monkeypatch.setenv("ARTEMIS_DAEMON_TOKEN", "test-daemon-token")
    yield cache
    for name in ("daemon.port", "daemon.sock"):
        try:
            (cache / name).unlink(missing_ok=True)
        except OSError:
            pass


async def _start_daemon() -> tuple[Daemon, asyncio.Task]:
    d = Daemon()
    await d.start()
    task = asyncio.create_task(d.serve())
    for _ in range(40):
        if await daemon_alive_async(timeout=0.2):
            break
        await asyncio.sleep(0.05)
    assert await daemon_alive_async(timeout=0.5)
    return d, task


async def _stop_daemon(d: Daemon, task: asyncio.Task) -> None:
    d.request_stop()
    task.cancel()
    try:
        await asyncio.wait_for(task, 2.0)
    except (asyncio.CancelledError, TimeoutError):
        pass


def test_hello_authorized_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTEMIS_DAEMON_TOKEN", raising=False)
    assert hello_authorized({"type": "hello"}) is True
    monkeypatch.setenv("ARTEMIS_DAEMON_TOKEN", "secret")
    assert hello_authorized({"type": "hello"}) is False
    assert hello_authorized({"type": "hello", "token": "secret"}) is True
    assert hello_authorized({"type": "hello", "token": "nope"}) is False


def test_with_hello_token_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_DAEMON_TOKEN", "abc")
    assert with_hello_token({"type": "hello"})["token"] == "abc"


def test_hello_without_token_rejected(token_daemon_env: Path) -> None:
    async def run() -> None:
        d, task = await _start_daemon()
        try:
            reader, writer = await open_connection()
            writer.write(
                (
                    json.dumps(
                        {"v": 1, "id": "h", "type": "hello", "role": "tui", "session": "s1"}
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            msg = json.loads(line)
            writer.close()
            await writer.wait_closed()
            assert msg.get("ok") is False
            assert "unauthorized" in str(msg.get("error", "")).lower()
        finally:
            await _stop_daemon(d, task)

    asyncio.run(run())


def test_hello_with_token_accepted(token_daemon_env: Path) -> None:
    async def run() -> None:
        d, task = await _start_daemon()
        try:
            reader, writer = await open_connection()
            writer.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "h",
                            "type": "hello",
                            "role": "tui",
                            "session": "s1",
                            "token": "test-daemon-token",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            msg = json.loads(line)
            writer.close()
            await writer.wait_closed()
            assert msg.get("type") == "hello"
            assert msg.get("ok", True) is not False
        finally:
            await _stop_daemon(d, task)

    asyncio.run(run())
