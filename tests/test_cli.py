"""CLI --challenge file load (TUI-parity via resolve_load_target)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.config import Settings


def _patch_swarm_run(monkeypatch, captured: dict) -> None:
    class _FakeSwarm:
        def __init__(self, *, challenge_dir, meta, **_kwargs):
            captured["challenge_dir"] = challenge_dir
            captured["meta"] = meta
            self.confirmed_flags = []

        def kill(self) -> None:
            return None

        async def run(self):
            return None

    async def _noop_cleanup():
        return None

    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm", _FakeSwarm)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", _noop_cleanup)
    monkeypatch.setattr("backend.sandbox.configure_semaphore", lambda *_a, **_k: None)


def test_run_single_accepts_file_like_tui(tmp_path: Path, monkeypatch) -> None:
    """A single handout file materializes; CLI must not say 'Not a directory'."""
    from backend.cli import _run_single

    blob = tmp_path / "handout.bin"
    blob.write_bytes(b"MZ")
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    captured: dict = {}
    _patch_swarm_run(monkeypatch, captured)

    asyncio.run(_run_single(Settings(), str(blob), ["cursor/x"], 1, flags_required=1))

    root = Path(captured["challenge_dir"])
    assert root.is_dir()
    assert (root / "distfiles" / "handout.bin").is_file()
    assert captured["meta"].name
    assert "Not a directory" not in str(captured)


def test_run_single_keeps_directory(tmp_path: Path, monkeypatch) -> None:
    from backend.cli import _run_single

    chal = tmp_path / "chal"
    chal.mkdir()
    (chal / "challenge.txt").write_text("dir challenge\n", encoding="utf-8")
    captured: dict = {}
    _patch_swarm_run(monkeypatch, captured)

    asyncio.run(_run_single(Settings(), str(chal), ["cursor/x"], 1, flags_required=1))

    assert Path(captured["challenge_dir"]) == chal.resolve()


def test_run_single_rejects_etc_passwd() -> None:
    from backend.cli import _run_single

    etc = Path("/etc/passwd")
    if not etc.is_file():
        pytest.skip("no /etc/passwd on this host")
    with pytest.raises(SystemExit) as ei:
        asyncio.run(_run_single(Settings(), str(etc), ["cursor/x"], 1, flags_required=1))
    assert ei.value.code == 1


def test_run_single_rejects_ssh_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.cli import _run_single

    home = tmp_path / "Users" / "me"
    key = home / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr("backend.challenge._home_dir", lambda: home.resolve())
    with pytest.raises(SystemExit) as ei:
        asyncio.run(_run_single(Settings(), str(key), ["cursor/x"], 1, flags_required=1))
    assert ei.value.code == 1


def test_swarm_help_mentions_file() -> None:
    from click.testing import CliRunner

    from backend.cli import main

    result = CliRunner().invoke(main, ["swarm", "--help"])
    assert result.exit_code == 0
    assert "--challenge" in result.output
    assert "file" in result.output.lower()
