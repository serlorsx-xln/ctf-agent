"""Phase 3 setup bake helpers (no Docker required for unit bits)."""

from pathlib import Path

import pytest

from backend.sandbox.setup_bake import (
    DEFAULT_BAKE_PACKS,
    FULL_BAKE_PACKS,
    GATE_REQUIRED_PACKS,
    STATUS_INVENTORY_PACKS,
    LITE_BAKE_PACKS,
    dockerfile_digest,
    pack_cache_incomplete,
    pack_cache_stale,
    pack_cache_trees_present,
    probe_docker_env,
    restamp_pack_ready_if_complete,
    write_pack_ready_marker,
)


def test_full_default_and_lite_bake_sets():
    assert STATUS_INVENTORY_PACKS == FULL_BAKE_PACKS
    assert GATE_REQUIRED_PACKS == STATUS_INVENTORY_PACKS
    assert DEFAULT_BAKE_PACKS == FULL_BAKE_PACKS
    assert "web" in LITE_BAKE_PACKS
    assert "steg" in LITE_BAKE_PACKS
    assert "forensics" in LITE_BAKE_PACKS
    assert "crypto" not in LITE_BAKE_PACKS
    assert "pwn" not in LITE_BAKE_PACKS
    assert "mobile" in FULL_BAKE_PACKS
    assert "pwn" in FULL_BAKE_PACKS
    assert "crypto" in FULL_BAKE_PACKS
    assert "linux" in FULL_BAKE_PACKS
    assert "ml" not in FULL_BAKE_PACKS


def test_probe_docker_env_reports_something(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    lines = probe_docker_env()
    assert lines
    assert any("Docker" in x or "DOCKER" in x or "Colima" in x for x in lines)


def test_probe_docker_env_honors_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/fake.sock")
    assert probe_docker_env() == ["DOCKER_HOST=unix:///tmp/fake.sock"]


def test_pack_cache_incomplete_detects_truncated_crypto(monkeypatch, tmp_path: Path):
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir("crypto")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64" if pack_id == "crypto" else cache,
    )
    c = tmp_path / "crypto" / "arm64"
    (c / "opt" / "sagemath").mkdir(parents=True)
    (c / ".ready").write_text("ok\n", encoding="utf-8")
    assert pack_cache_incomplete("crypto") is True
    (c / "opt" / "sagemath" / "bin").mkdir()
    (c / "opt" / "sagemath" / "bin" / "sage").write_text("", encoding="utf-8")
    (c / "opt" / "sagemath" / "bin" / "python3").write_text("", encoding="utf-8")
    assert pack_cache_incomplete("crypto") is False


def test_pack_cache_incomplete_ignores_apt_only_packs():
    assert pack_cache_incomplete("forensics") is False
    assert pack_cache_incomplete("web") is False


def test_pack_cache_incomplete_linux_needs_bin_sentinels(monkeypatch, tmp_path: Path):
    """Top-level opt/linux-tools is not enough — ffuf + linpeas.sh must exist."""
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir("linux")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64" if pack_id == "linux" else cache,
    )
    c = tmp_path / "linux" / "arm64"
    (c / "opt" / "linux-tools").mkdir(parents=True)
    (c / ".ready").write_text("ok\n", encoding="utf-8")
    assert pack_cache_incomplete("linux") is True
    bin_dir = c / "opt" / "linux-tools" / "bin"
    bin_dir.mkdir()
    (bin_dir / "ffuf").write_text("", encoding="utf-8")
    assert pack_cache_incomplete("linux") is True
    (bin_dir / "linpeas.sh").write_text("", encoding="utf-8")
    assert pack_cache_incomplete("linux") is False


def test_pack_cache_incomplete_crypto_tools_needs_flatter_and_cado(
    monkeypatch, tmp_path: Path
):
    """RsaCtfTool alone is not enough — flatter + cado-nfs bins must exist."""
    from backend.tool_router import pack_cache_dir

    cache = pack_cache_dir("crypto-tools")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id / "arm64" if pack_id == "crypto-tools" else cache,
    )
    c = tmp_path / "crypto-tools" / "arm64"
    (c / "opt" / "RsaCtfTool").mkdir(parents=True)
    (c / ".ready").write_text("ok\n", encoding="utf-8")
    assert pack_cache_incomplete("crypto-tools") is True
    (c / "opt" / "flatter" / "bin").mkdir(parents=True)
    (c / "opt" / "flatter" / "bin" / "flatter").write_text("", encoding="utf-8")
    assert pack_cache_incomplete("crypto-tools") is True
    (c / "opt" / "cado-nfs" / "bin").mkdir(parents=True)
    (c / "opt" / "cado-nfs" / "bin" / "cado-nfs").write_text("", encoding="utf-8")
    assert pack_cache_incomplete("crypto-tools") is False


def test_ready_marker_digest_roundtrip(tmp_path: Path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile.mobile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    digest = dockerfile_digest(dockerfile)
    assert len(digest) == 16

    cache = tmp_path / "cache"
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: digest,
    )
    write_pack_ready_marker(cache, "mobile")
    body = (cache / ".ready").read_text(encoding="utf-8").strip()
    assert body == f"ok {digest}"

    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: cache,
    )
    assert pack_cache_stale("mobile") is False

    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "deadbeefdeadbeef",
    )
    assert pack_cache_stale("mobile") is True

    # Legacy bare ``ok`` is stale until restamp (when trees are complete).
    (cache / ".ready").write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: digest,
    )
    assert pack_cache_stale("mobile") is True


def _pwn_trees(cache: Path) -> None:
    for rel in ("root/.gdbinit-gef.py", "root/.gdbinit"):
        dest = cache / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("x\n", encoding="utf-8")


def test_restamp_complete_stale_pack(monkeypatch, tmp_path: Path):
    cache = tmp_path / "pwn" / "arm64"
    cache.mkdir(parents=True)
    _pwn_trees(cache)
    (cache / ".ready").write_text("ok olddigestolddige\n", encoding="utf-8")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: cache,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "newdigestnewdige",
    )
    assert pack_cache_trees_present("pwn") is True
    assert pack_cache_stale("pwn") is True
    assert restamp_pack_ready_if_complete("pwn") is True
    assert pack_cache_stale("pwn") is False
    assert (cache / ".ready").read_text(encoding="utf-8").strip() == "ok newdigestnewdige"


def test_restamp_refuses_incomplete_trees(monkeypatch, tmp_path: Path):
    cache = tmp_path / "pwn" / "arm64"
    cache.mkdir(parents=True)
    (cache / ".ready").write_text("ok olddigestolddige\n", encoding="utf-8")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: cache,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "newdigestnewdige",
    )
    assert pack_cache_trees_present("pwn") is False
    assert restamp_pack_ready_if_complete("pwn") is False
    assert (cache / ".ready").read_text(encoding="utf-8").strip() == "ok olddigestolddige"


def test_pack_cache_is_ready_restamps_stale(monkeypatch, tmp_path: Path):
    from backend.sandbox.packs import _pack_cache_is_ready

    cache = tmp_path / "pwn" / "arm64"
    cache.mkdir(parents=True)
    _pwn_trees(cache)
    (cache / ".ready").write_text("ok olddigestolddige\n", encoding="utf-8")
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: cache,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "newdigestnewdige",
    )
    assert _pack_cache_is_ready("pwn") is True
    assert (cache / ".ready").read_text(encoding="utf-8").strip() == "ok newdigestnewdige"


def test_find_blutter_warm_apk_env_and_size(tmp_path: Path, monkeypatch):
    from backend.sandbox import setup_bake

    missing = tmp_path / "missing.apk"
    monkeypatch.setenv("ARTEMIS_BLUTTER_WARM_APK", str(missing))
    assert setup_bake.find_blutter_warm_apk() is None

    sample = tmp_path / "PWNKnight.apk"
    sample.write_bytes(b"x" * 1_000_001)
    monkeypatch.setenv("ARTEMIS_BLUTTER_WARM_APK", str(sample))
    assert setup_bake.find_blutter_warm_apk() == sample

    monkeypatch.delenv("ARTEMIS_BLUTTER_WARM_APK", raising=False)
    challenges = tmp_path / "challenges" / "mobile"
    challenges.mkdir(parents=True)
    apk = challenges / "demo.apk"
    apk.write_bytes(b"y" * 600_000)
    monkeypatch.setattr(
        "backend.sandbox.donor_build.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "backend.cache.cache_dir",
        lambda: tmp_path / "cache-empty",
    )
    assert setup_bake.find_blutter_warm_apk() == apk


@pytest.mark.asyncio
async def test_warm_shared_blutter_skip_vm_compile(monkeypatch, tmp_path: Path):
    from backend.sandbox import setup_bake

    home = tmp_path / "blutter-home"
    home.mkdir()
    monkeypatch.setattr(
        "backend.tool_router.pack_state_dir",
        lambda *a, **k: home,
    )
    monkeypatch.setattr(
        "backend.tool_router._blutter_vm_present",
        lambda p: False,
    )
    msg = await setup_bake.warm_shared_blutter_state(skip_vm_compile=True)
    assert "prepared" in msg.lower() or "still builds" in msg.lower()
    assert "ARTEMIS_BLUTTER_WARM_APK" not in msg  # skip path does not hunt APK


@pytest.mark.asyncio
async def test_materialize_pack_repairs_runtime_donor(monkeypatch):
    from backend.sandbox.setup_bake import materialize_pack

    async def fake_ensure(pack_id: str):
        assert pack_id == "mobile"
        return False, "guest libs missing"

    monkeypatch.setattr("backend.sandbox.donor_build.ensure_donor_image", fake_ensure)
    ok, msg = await materialize_pack("mobile")
    assert ok is False
    assert "guest libs" in msg
