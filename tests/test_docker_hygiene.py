"""Docker hygiene helpers (AppleDouble / sandbox context)."""

from pathlib import Path

from backend.sandbox.docker_hygiene import (
    cleanup_sandbox_build_context,
    prepare_sandbox_build_context,
    sandbox_build_context,
    scrub_appledouble,
)


def test_scrub_appledouble_removes_sidecars(tmp_path: Path):
    (tmp_path / "README.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "._README.md").write_bytes(b"junk")
    nested = tmp_path / "sandbox"
    nested.mkdir()
    (nested / "._Dockerfile.core").write_bytes(b"junk")
    assert scrub_appledouble(tmp_path) >= 2
    assert not (tmp_path / "._README.md").exists()
    assert not (nested / "._Dockerfile.core").exists()
    assert (tmp_path / "README.md").is_file()


def test_prepare_sandbox_build_context_strips_appledouble(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ARTEMIS_REPO_ROOT", str(tmp_path))
    src = tmp_path / "sandbox"
    src.mkdir()
    (src / "Dockerfile.core").write_text("FROM scratch\n", encoding="utf-8")
    (src / "._Dockerfile.core").write_bytes(b"junk")
    (src / ".DS_Store").write_bytes(b"junk")
    ctx = prepare_sandbox_build_context(tmp_path)
    assert ctx.is_dir()
    assert (ctx / "Dockerfile.core").is_file()
    assert not (ctx / "._Dockerfile.core").exists()
    assert not (ctx / ".DS_Store").exists()
    assert sandbox_build_context(tmp_path) == src
    assert ctx.name.startswith("sandbox-")
    assert ctx.parent.name == "docker-ctx"


def test_prepare_sandbox_build_context_unique_per_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    src = tmp_path / "sandbox"
    src.mkdir()
    (src / "Dockerfile.core").write_text("FROM scratch\n", encoding="utf-8")
    a = prepare_sandbox_build_context(tmp_path)
    b = prepare_sandbox_build_context(tmp_path)
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_cleanup_sandbox_build_context(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    src = tmp_path / "sandbox"
    src.mkdir()
    (src / "Dockerfile.core").write_text("FROM scratch\n", encoding="utf-8")
    ctx = prepare_sandbox_build_context(tmp_path)
    assert ctx.is_dir()
    cleanup_sandbox_build_context(ctx)
    assert not ctx.exists()
