"""Shared sandbox acquire/release refcount (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.shell import sandbox_session as ss


class _FakeBox:
    def __init__(self) -> None:
        self.container_id = "cid"
        self.stops = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stops += 1


async def _noop_cleanup(**kwargs):  # noqa: ANN003
    return None


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    ss.reset_sandbox_cache_for_tests()
    boxes: list[_FakeBox] = []

    def _fake_ctor(**kwargs):  # noqa: ANN003
        box = _FakeBox()
        boxes.append(box)
        return box

    monkeypatch.setattr(
        "backend.config.Settings",
        lambda: SimpleNamespace(
            sandbox_image="ctf-sandbox-core",
            container_memory_limit="4g",
        ),
    )
    monkeypatch.setattr("backend.sandbox.DockerSandbox", _fake_ctor)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", _noop_cleanup)
    monkeypatch.setattr(
        "backend.sandbox.docker_client.ensure_start_semaphore",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(ss, "_write_meta", lambda *_a, **_k: None)
    yield boxes
    ss.reset_sandbox_cache_for_tests()


@pytest.mark.asyncio
async def test_two_solvers_share_one_box_until_last_release(_reset_cache):
    boxes = _reset_cache
    a = await ss.acquire_sandbox("/tmp/chal-share")
    b = await ss.acquire_sandbox("/tmp/chal-share")
    assert a is b
    assert len(boxes) == 1
    assert boxes[0].stops == 0

    msg = await ss.release_sandbox("/tmp/chal-share")
    assert "refs=1" in msg
    assert boxes[0].stops == 0

    msg2 = await ss.release_sandbox("/tmp/chal-share")
    assert "Stopped" in msg2
    assert boxes[0].stops == 1


@pytest.mark.asyncio
async def test_tui_pin_survives_solver_release(_reset_cache):
    boxes = _reset_cache
    pinned = await ss.get_sandbox("/tmp/chal-pin")
    solver = await ss.acquire_sandbox("/tmp/chal-pin")
    assert pinned is solver
    await ss.release_sandbox("/tmp/chal-pin")
    assert boxes[0].stops == 0
    await ss.stop_sandbox("/tmp/chal-pin")
    assert boxes[0].stops == 1
