"""Tests for Strix-style donor auto-build helpers."""

from __future__ import annotations

import pytest

from backend.sandbox import donor_build


def test_donor_specs_cover_common_packs():
    for pack in ("linux", "mobile", "ghidra", "crypto", "pwn", "steg", "crypto-tools"):
        assert pack in donor_build.DONOR_BUILD_SPECS
        dockerfile, image = donor_build.DONOR_BUILD_SPECS[pack]
        assert dockerfile.startswith("sandbox/Dockerfile.")
        assert image.startswith("ctf-sandbox-")


def test_donor_image_for_unknown_is_none():
    assert donor_build.donor_image_for("nope") is None


@pytest.mark.asyncio
async def test_ensure_donor_skips_when_image_present(monkeypatch):
    async def fake_cli(*args, timeout_s=30):
        if args[:2] == ("image", "inspect"):
            return 0, "", ""
        raise AssertionError(f"unexpected docker cli {args}")

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", fake_cli)
    ok, msg = await donor_build.ensure_donor_image("linux")
    assert ok is True
    assert "already present" in msg


@pytest.mark.asyncio
async def test_ensure_pwn_skips_when_functional(monkeypatch):
    async def fake_cli(*args, timeout_s=30):
        if args[:2] == ("image", "inspect"):
            return 0, "", ""
        if args[:3] == ("run", "--rm", "--entrypoint"):
            return 0, "", ""
        raise AssertionError(f"unexpected docker cli {args}")

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", fake_cli)
    donor_build._donor_functional_cache.clear()
    ok, msg = await donor_build.ensure_donor_image("pwn")
    assert ok is True
    assert "already present" in msg


@pytest.mark.asyncio
async def test_ensure_pwn_rebuilds_when_functional_fails(monkeypatch, tmp_path):
    calls: list[tuple] = []

    async def fake_cli(*args, timeout_s=30):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return 0, "", ""
        if args[:2] == ("rmi", "-f"):
            return 0, "", ""
        if args[:3] == ("run", "--rm", "--entrypoint"):
            return 1, "", "missing angr"
        if args[0] == "build":
            return 0, "ok", ""
        raise AssertionError(args)

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", fake_cli)
    monkeypatch.setattr(donor_build, "repo_root", lambda: tmp_path)
    dockerfile = tmp_path / "sandbox" / "Dockerfile.pwn"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    donor_build._donor_functional_cache.clear()
    ok, msg = await donor_build.ensure_donor_image("pwn")
    assert ok is True
    assert "Built donor" in msg
    assert any(c[:2] == ("rmi", "-f") for c in calls)
    assert any(c[0] == "build" for c in calls)


@pytest.mark.asyncio
async def test_ensure_donor_builds_when_missing(monkeypatch, tmp_path):
    calls: list[tuple] = []

    async def fake_cli(*args, timeout_s=30):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return 1, "", "missing"
        if args[0] == "build":
            return 0, "ok", ""
        raise AssertionError(args)

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", fake_cli)
    monkeypatch.setattr(donor_build, "repo_root", lambda: tmp_path)
    dockerfile = tmp_path / "sandbox" / "Dockerfile.linux"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    ok, msg = await donor_build.ensure_donor_image("linux")
    assert ok is True
    assert "Built donor" in msg
    assert any(c[0] == "build" for c in calls)


@pytest.mark.asyncio
async def test_ensure_mobile_rebuilds_when_guest_libs_fail(monkeypatch, tmp_path):
    calls: list[tuple] = []

    async def fake_cli(*args, timeout_s=30):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return 0, "", ""
        if args[:2] == ("rmi", "-f"):
            return 0, "", ""
        if args[:3] == ("run", "--rm", "--entrypoint"):
            return 1, "", "missing libstdc++"
        if args[0] == "build":
            return 0, "ok", ""
        raise AssertionError(args)

    monkeypatch.setattr("backend.sandbox.docker_client._docker_cli", fake_cli)
    monkeypatch.setattr(donor_build, "repo_root", lambda: tmp_path)
    dockerfile = tmp_path / "sandbox" / "Dockerfile.mobile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    donor_build._donor_functional_cache.clear()
    ok, msg = await donor_build.ensure_donor_image("mobile")
    assert ok is True
    assert "Built donor" in msg
    assert any(c[:2] == ("rmi", "-f") for c in calls)
    assert any(c[0] == "build" for c in calls)
