"""Sandbox exec concurrency and timeout reap (no real Docker)."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from backend.sandbox.container import DockerSandbox, ExecResult


@pytest.mark.asyncio
async def test_exec_does_not_hold_lifecycle_lock_during_command():
    """Long bash must not serialize other parallel exec calls."""
    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir="/tmp")
    sb.workspace_dir = "/tmp/ws"
    sb._container = object()  # nonempty sentinel

    ensure_calls = 0
    in_flight = 0
    max_in_flight = 0

    async def fake_ensure() -> None:
        nonlocal ensure_calls
        ensure_calls += 1

    async def fake_exec_inner(command: str, timeout_s: int, *, via_host_proxy: bool = False):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return ExecResult(exit_code=0, stdout=command, stderr="")

    sb._ensure_container_unlocked = fake_ensure  # type: ignore[method-assign]
    sb._exec_inner = fake_exec_inner  # type: ignore[method-assign]

    r1, r2 = await asyncio.gather(
        sb.exec("sleep 30", timeout_s=60),
        sb.exec("echo hi", timeout_s=30),
    )
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert ensure_calls == 2
    assert max_in_flight >= 2


@pytest.mark.asyncio
async def test_exec_inner_timeout_calls_reap(monkeypatch):
    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir="/tmp")
    sb.workspace_dir = "/tmp/ws"
    sb._container = AsyncMock()
    sb._reap_timed_out_compilers = AsyncMock()  # type: ignore[method-assign]

    class _Stream:
        async def read_out(self):
            await asyncio.Event().wait()  # block until cancelled by wait_for
            return None

        async def close(self):
            return None

    class _Exec:
        def start(self, detach=False):
            return _Stream()

        async def inspect(self):
            return {"ExitCode": 0}

    sb._container.exec = AsyncMock(return_value=_Exec())  # type: ignore[method-assign]

    real_wait_for = asyncio.wait_for

    async def fast_timeout(aw, timeout):
        if timeout is not None and timeout < 100:
            # Cancel the collector so it does not leak a warning.
            task = asyncio.create_task(aw)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise TimeoutError()
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    result = await sb._exec_inner("blutter libs out", timeout_s=1, via_host_proxy=False)
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()
    sb._reap_timed_out_compilers.assert_awaited()
