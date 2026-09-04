"""Lazy pip (angr / z3 / numpy) after pack ensure — no Docker."""

from __future__ import annotations

import pytest

from backend.tool_router import infer_lazy_pip, infer_pack_from_failure
from backend.tools.core import do_bash


def test_infer_lazy_pip_angr():
    assert (
        infer_lazy_pip(
            "python3 -c 'import angr'",
            "ModuleNotFoundError: No module named 'angr'",
        )
        == "angr"
    )


def test_infer_lazy_pip_numpy():
    assert (
        infer_lazy_pip("python3 -c 'import numpy'", "ModuleNotFoundError: No module named 'numpy'")
        == "numpy"
    )


def test_infer_z3_maps_to_crypto_pack():
    assert (
        infer_pack_from_failure(
            "python3 -c 'import z3'",
            "ModuleNotFoundError: No module named 'z3'",
        )
        == "crypto"
    )


def test_angr_still_maps_to_pwn_pack():
    assert (
        infer_pack_from_failure(
            "python3 -c 'import angr'",
            "ModuleNotFoundError: No module named 'angr'",
        )
        == "pwn"
    )


def test_pwn_pip_omits_angr_crypto_has_z3():
    from backend.tool_router import PACK_SPECS

    assert "angr" not in PACK_SPECS["pwn"].pip
    assert "z3-solver" in PACK_SPECS["crypto"].pip
    assert "numpy" in PACK_SPECS["crypto"].pip


class _Exec:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _Sandbox:
    def __init__(self, script: list[tuple[int, str, str]]) -> None:
        self.script = list(script)
        self.cmds: list[str] = []
        self.ensured_packs: set[str] = set()

    async def exec(self, command: str, timeout_s: int = 60):  # noqa: ARG002
        self.cmds.append(command)
        if not self.script:
            return _Exec(0, "ok")
        rc, out, err = self.script.pop(0)
        return _Exec(rc, out, err)

    async def ensure_pack(self, pack: str) -> str:
        self.ensured_packs.add(pack)
        return f"Pack {pack} ready"


@pytest.mark.asyncio
async def test_do_bash_lazy_pips_angr_after_pwn_ensured():
    sb = _Sandbox(
        [
            (1, "", "ModuleNotFoundError: No module named 'angr'"),
            (0, "Successfully installed angr", ""),
            (0, "angr-ok", ""),
        ]
    )
    sb.ensured_packs.add("pwn")
    out = await do_bash(sb, "python3 -c 'import angr'")
    assert "pip installed angr" in out
    assert "angr-ok" in out
    assert any(c.startswith("pip3 install") and "angr" in c for c in sb.cmds)


@pytest.mark.asyncio
async def test_do_bash_lazy_pip_only_once():
    sb = _Sandbox(
        [
            (1, "", "ModuleNotFoundError: No module named 'numpy'"),
            (1, "", "pip explode"),
        ]
    )
    out = await do_bash(sb, "python3 -c 'import numpy'")
    assert "lazy pip numpy failed" in out
    # Second call must not pip again.
    sb.script = [(1, "", "ModuleNotFoundError: No module named 'numpy'")]
    out2 = await do_bash(sb, "python3 -c 'import numpy'")
    assert "lazy pip" not in out2
    assert sum(1 for c in sb.cmds if c.startswith("pip3 install")) == 1


@pytest.mark.asyncio
async def test_prune_extract_donors_honors_keep_env(monkeypatch):
    from backend.sandbox.setup_bake import prune_extract_only_donors

    monkeypatch.setenv("ARTEMIS_KEEP_EXTRACT_DONORS", "1")
    # ... existing code continues below via next replace?

    async def _boom(*_a, **_k):
        raise AssertionError("docker should not run")

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", _boom)
    lines = await prune_extract_only_donors()
    assert any("KEEP_EXTRACT" in ln or "keep extract" in ln.lower() for ln in lines)


@pytest.mark.asyncio
async def test_prune_skips_when_no_extract_packs(monkeypatch):
    from backend.sandbox.setup_bake import prune_extract_only_donors

    monkeypatch.delenv("ARTEMIS_KEEP_EXTRACT_DONORS", raising=False)

    async def _boom(*_a, **_k):
        raise AssertionError("docker should not run")

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", _boom)
    assert await prune_extract_only_donors(pack_ids=[]) == []
    assert await prune_extract_only_donors(pack_ids=["web", "forensics"]) == []
