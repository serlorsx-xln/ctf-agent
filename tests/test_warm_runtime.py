"""Warm runtime markers + resolve_runtime_l0_image preference."""

from pathlib import Path

from backend.sandbox.warm_runtime import (
    WARM_BAKE_PACKS,
    clear_warm_runtime_marker,
    is_warm_runtime_image,
    read_warm_runtime,
    warm_runtime_tag,
    write_warm_runtime_marker,
)
from backend.tool_router import resolve_runtime_l0_image


def test_warm_bake_packs_cover_apt_heavy():
    assert "ghidra" in WARM_BAKE_PACKS
    assert "web" in WARM_BAKE_PACKS
    assert "forensics" in WARM_BAKE_PACKS
    assert "pwn" not in WARM_BAKE_PACKS  # donor runtime already


def test_warm_marker_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "abcd1234abcd1234",
    )
    assert read_warm_runtime("web") is None
    write_warm_runtime_marker("web")
    assert read_warm_runtime("web") == warm_runtime_tag("web")
    clear_warm_runtime_marker("web")
    assert read_warm_runtime("web") is None


def test_warm_marker_stale_digest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "aaaaaaaaaaaaaaaa",
    )
    write_warm_runtime_marker("ghidra")
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "bbbbbbbbbbbbbbbb",
    )
    assert read_warm_runtime("ghidra") is None


def test_warm_marker_rejects_legacy_ok_without_digest(tmp_path: Path, monkeypatch):
    """``… ok`` must not skip digest checks (stale warm after recipe edits)."""
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "aaaaaaaaaaaaaaaa",
    )
    path = tmp_path / "web" / ".warm_runtime"
    path.parent.mkdir(parents=True)
    path.write_text(f"{warm_runtime_tag('web')} ok\n", encoding="utf-8")
    assert read_warm_runtime("web") is None


def test_warm_marker_require_image_when_missing(tmp_path: Path, monkeypatch):
    """Launch assess must not treat pruned images as warm-ready."""
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "abcd1234abcd1234",
    )
    write_warm_runtime_marker("web")
    assert read_warm_runtime("web") == warm_runtime_tag("web")
    monkeypatch.setattr(
        "backend.sandbox.warm_runtime._image_exists_sync",
        lambda tag: False,
    )
    assert read_warm_runtime("web", require_image=True) is None
    monkeypatch.setattr(
        "backend.sandbox.warm_runtime._image_exists_sync",
        lambda tag: True,
    )
    assert read_warm_runtime("web", require_image=True) == warm_runtime_tag("web")


def test_pack_source_digest_includes_recipe_fingerprint(tmp_path: Path, monkeypatch):
    """Recipe-only PackSpec edits must change digest even if Dockerfile is fixed."""
    from backend.sandbox.setup_bake import pack_source_digest

    df = tmp_path / "Dockerfile.web"
    df.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(
        "backend.sandbox.donor_build.donor_dockerfile_for",
        lambda pack_id: df,
    )
    monkeypatch.setattr(
        "backend.tool_router.pack_recipe_fingerprint",
        lambda pack_id: "recipe-aaaaaaaa",
    )
    a = pack_source_digest("web")
    monkeypatch.setattr(
        "backend.tool_router.pack_recipe_fingerprint",
        lambda pack_id: "recipe-bbbbbbbb",
    )
    b = pack_source_digest("web")
    assert a and b and a != b
    assert len(a) == 16


def test_resolve_prefers_warm_over_core(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.tool_router.pack_cache_dir",
        lambda pack_id: tmp_path / pack_id,
    )
    monkeypatch.setattr(
        "backend.sandbox.setup_bake.pack_source_digest",
        lambda pack_id: "abcd1234abcd1234",
    )
    write_warm_runtime_marker("ghidra")
    assert resolve_runtime_l0_image(["ghidra"]) == warm_runtime_tag("ghidra")
    assert resolve_runtime_l0_image(["crypto"]) == "ctf-sandbox-core"
    assert is_warm_runtime_image(warm_runtime_tag("web"))
