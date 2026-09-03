"""Multi-session daemon: broadcast / dialog isolation (no session cap)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from backend.daemon.handlers import cancel_pending_dialogs, register_dialog
from backend.daemon.server import Daemon
from backend.daemon.state import DaemonState
from backend.daemon.supervisor import SwarmSupervisor
from backend.daemon.transport import open_connection
from tests.daemon_fixtures import daemon_cache_dir


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch):
    cache = daemon_cache_dir("artemis-ms")
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


def test_many_sessions_spawn_without_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("ARTEMIS_CACHE", str(cache))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(Path(__file__).resolve().parents[1]))

    long_script = tmp_path / "long_swarm.py"
    long_script.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    import backend.daemon.supervisor as sup_mod
    from backend.subprocess_platform import swarm_subprocess_kwargs

    orig = sup_mod.asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kw):
        return await orig(
            sys.executable,
            str(long_script),
            env=kw.get("env", os.environ.copy()),
            **swarm_subprocess_kwargs(),
        )

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)

    async def _noop() -> None:
        return None

    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", _noop)

    state = DaemonState()
    sup = SwarmSupervisor(state)

    async def run() -> None:
        for sid in ("a", "b", "c", "d"):
            await sup.spawn(
                challenge=f"c-{sid}",
                models=["cursor/x"],
                flags_required=1,
                session_id=sid,
            )
            assert sup.is_running(sid)
        await sup.stop_all()

    asyncio.run(run())


def test_dialog_cancel_scoped_to_session() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        fa = loop.create_future()
        fb = loop.create_future()
        register_dialog("r1", fa, session="a")
        register_dialog("r2", fb, session="b")
        cancel_pending_dialogs(session="a")
        assert fa.done()
        assert not fb.done()
        cancel_pending_dialogs(session="b")
        assert fb.done()

    asyncio.run(run())


def test_two_tui_sessions_isolated_pushes(daemon_env: str) -> None:
    async def run() -> None:
        d = Daemon()
        await d.start()
        task = asyncio.create_task(d.serve())
        await asyncio.sleep(0.15)
        try:
            ra, wa = await open_connection()
            rb, wb = await open_connection()
            for w, sid in ((wa, "win-a"), (wb, "win-b")):
                w.write(
                    (
                        json.dumps(
                            {
                                "v": 1,
                                "id": "h",
                                "type": "hello",
                                "role": "tui",
                                "session": sid,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await w.drain()
            await asyncio.wait_for(ra.readline(), 3)
            await asyncio.wait_for(rb.readline(), 3)
            # Drain initial session_update / replay pushes.
            for _ in range(4):
                try:
                    await asyncio.wait_for(ra.readline(), 0.2)
                except TimeoutError:
                    break
            for _ in range(4):
                try:
                    await asyncio.wait_for(rb.readline(), 0.2)
                except TimeoutError:
                    break

            d.state.broadcast({"type": "boot", "session": "win-a", "text": "only-a"})
            d.state.broadcast({"type": "boot", "session": "win-b", "text": "only-b"})

            got_a = None
            got_b = None
            for _ in range(6):
                line = await asyncio.wait_for(ra.readline(), 2)
                m = json.loads(line)
                if m.get("type") == "boot":
                    got_a = m
                    break
            for _ in range(6):
                line = await asyncio.wait_for(rb.readline(), 2)
                m = json.loads(line)
                if m.get("type") == "boot":
                    got_b = m
                    break
            assert got_a and got_a.get("text") == "only-a"
            assert got_b and got_b.get("text") == "only-b"
            wa.close()
            wb.close()
        finally:
            d.request_stop()
            task.cancel()
            try:
                await asyncio.wait_for(task, 2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    asyncio.run(run())


def test_tui_hello_rebinds_session_subscription(daemon_env: str) -> None:
    """Mid-install chat switch re-hellos on the same socket — must rebind push fan-out."""

    async def run() -> None:
        d = Daemon()
        await d.start()
        task = asyncio.create_task(d.serve())
        await asyncio.sleep(0.15)
        try:
            r, w = await open_connection()
            w.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "h1",
                            "type": "hello",
                            "role": "tui",
                            "session": "old-sess",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await w.drain()
            await asyncio.wait_for(r.readline(), 3)
            for _ in range(4):
                try:
                    await asyncio.wait_for(r.readline(), 0.2)
                except TimeoutError:
                    break

            w.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": "h2",
                            "type": "hello",
                            "role": "tui",
                            "session": "new-sess",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await w.drain()
            hello2 = json.loads(await asyncio.wait_for(r.readline(), 3))
            assert hello2.get("type") == "hello"
            assert hello2.get("session") == "new-sess"

            d.state.broadcast({"type": "boot", "session": "new-sess", "text": "new-only"})
            d.state.broadcast({"type": "boot", "session": "old-sess", "text": "old-only"})

            got = None
            for _ in range(8):
                line = await asyncio.wait_for(r.readline(), 2)
                m = json.loads(line)
                if m.get("type") == "boot":
                    got = m
                    break
            assert got and got.get("text") == "new-only"
            w.close()
        finally:
            d.request_stop()
            task.cancel()
            try:
                await asyncio.wait_for(task, 2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    asyncio.run(run())


def test_two_sessions_load_isolated_disk(daemon_env: str, tmp_path: Path) -> None:
    """Daemon load RPCs must write sessions/<sid>/session.json, not share _default."""

    async def run() -> None:
        chal_a = tmp_path / "a"
        chal_b = tmp_path / "b"
        chal_a.mkdir()
        chal_b.mkdir()
        (chal_a / "challenge.txt").write_text("A\n", encoding="utf-8")
        (chal_b / "challenge.txt").write_text("B\n", encoding="utf-8")

        d = Daemon()
        await d.start()
        task = asyncio.create_task(d.serve())
        await asyncio.sleep(0.15)
        try:
            for sid, chal, req in (
                ("ses_aaa", chal_a, "la"),
                ("ses_bbb", chal_b, "lb"),
            ):
                r, w = await open_connection()
                w.write(
                    (
                        json.dumps(
                            {
                                "v": 1,
                                "id": "h",
                                "type": "hello",
                                "role": "tui",
                                "session": sid,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await w.drain()
                await asyncio.wait_for(r.readline(), 3)
                w.write(
                    (
                        json.dumps(
                            {
                                "v": 1,
                                "id": req,
                                "type": "load",
                                "session": sid,
                                "path": str(chal),
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await w.drain()
                for _ in range(20):
                    line = await asyncio.wait_for(r.readline(), 5)
                    m = json.loads(line)
                    if m.get("id") == req:
                        assert m.get("ok") is not False
                        break
                w.close()

            from backend.shell.sandbox_session import load_session_state

            assert load_session_state("ses_aaa").get("challenge_dir") == str(
                chal_a.resolve()
            )
            assert load_session_state("ses_bbb").get("challenge_dir") == str(
                chal_b.resolve()
            )
            assert load_session_state("_default").get("challenge_dir") not in (
                str(chal_a.resolve()),
                str(chal_b.resolve()),
            )
        finally:
            d.request_stop()
            task.cancel()
            try:
                await asyncio.wait_for(task, 2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    asyncio.run(run())
