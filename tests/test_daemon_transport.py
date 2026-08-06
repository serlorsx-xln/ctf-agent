"""Cross-platform daemon transport tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.daemon.server import Daemon
from backend.daemon.transport import daemon_alive, open_connection, uses_tcp
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def tcp_daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-tcp")
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "1")
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
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
    for _ in range(20):
        if daemon_alive():
            break
        await asyncio.sleep(0.05)
    assert daemon_alive()
    return d, task


async def _stop_daemon(d: Daemon, task: asyncio.Task) -> None:
    d.request_stop()
    task.cancel()
    try:
        await asyncio.wait_for(task, 2.0)
    except (asyncio.CancelledError, TimeoutError):
        pass


def test_uses_tcp_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.daemon.transport.sys.platform", "win32")
    monkeypatch.delenv("ARTEMIS_DAEMON_TCP", raising=False)
    assert uses_tcp() is True


def test_daemon_alive_false_before_tcp_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "1")
    monkeypatch.delenv("ARTEMIS_DAEMON_PORT", raising=False)
    cache = daemon_cache_dir("artemis-alive")
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    assert daemon_alive() is False


def test_tcp_daemon_roundtrip(tcp_daemon_env: Path) -> None:
    async def run() -> None:
        d, task = await _start_daemon()
        try:
            assert uses_tcp()
            assert (tcp_daemon_env / "daemon.port").is_file()
            reader, writer = await open_connection()
            writer.write(
                (json.dumps({"v": 1, "id": "h", "type": "hello", "role": "tui"}) + "\n").encode()
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 3)
            assert line
            msg = json.loads(line)
            assert msg.get("type") in ("hello", "hello_ack", "status")
        finally:
            await _stop_daemon(d, task)

    asyncio.run(run())
