"""Phase 3 setup bake helpers (no Docker required for unit bits)."""

from pathlib import Path

from backend.sandbox.setup_bake import (
    DEFAULT_BAKE_PACKS,
    dockerfile_digest,
    pack_cache_incomplete,
    pack_cache_stale,
    probe_docker_env,
    write_pack_ready_marker,
)


def test_default_bake_packs_cover_common_jeopardy():
    assert "mobile" in DEFAULT_BAKE_PACKS
    assert "pwn" in DEFAULT_BAKE_PACKS
    assert "crypto" in DEFAULT_BAKE_PACKS
    assert "ml" not in DEFAULT_BAKE_PACKS  # stay light by default


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
