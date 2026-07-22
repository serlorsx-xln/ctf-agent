"""Resource governor unit tests (no Docker daemon required)."""

from __future__ import annotations

import asyncio

from backend.sandbox.governor import (
    apply_live_memory,
    recommended_memory_limit,
    sandbox_nano_cpus,
)


def test_sandbox_nano_cpus_default(monkeypatch) -> None:
    monkeypatch.delenv("CTF_SANDBOX_NANO_CPUS", raising=False)
    assert sandbox_nano_cpus() == 2_000_000_000


def test_sandbox_nano_cpus_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CTF_SANDBOX_NANO_CPUS", "4000000000")
    assert sandbox_nano_cpus() == 4_000_000_000


def test_sandbox_nano_cpus_invalid_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("CTF_SANDBOX_NANO_CPUS", "not-a-number")
    assert sandbox_nano_cpus() == 2_000_000_000


def test_recommended_memory_floors() -> None:
    assert recommended_memory_limit("4g", ["crypto"]) == "12g"
    assert recommended_memory_limit("4g", ["ml"]) == "8g"
    assert recommended_memory_limit("4g", ["pwn"]) == "6g"
    assert recommended_memory_limit("16g", ["pwn"]) == "16g"


def test_apply_live_memory_success() -> None:
    calls: list[tuple] = []

    async def fake_cli(*args: str, timeout_s: float = 60) -> tuple[int, str, str]:
        calls.append((args, timeout_s))
        return 0, "", ""

    ok = asyncio.run(apply_live_memory("cid123", "12g", docker_cli=fake_cli))
    assert ok is True
    assert calls[0][0][:3] == ("update", "--memory=12g", "--memory-swap=12g")
    assert calls[0][0][3] == "cid123"


def test_apply_live_memory_failure() -> None:
    async def fake_cli(*args: str, timeout_s: float = 60) -> tuple[int, str, str]:
        return 1, "", "cannot update"

    ok = asyncio.run(apply_live_memory("cid", "8g", docker_cli=fake_cli))
    assert ok is False
