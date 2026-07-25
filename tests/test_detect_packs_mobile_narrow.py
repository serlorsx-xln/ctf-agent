"""Narrow prefetch: mobile APK should not pull sage/ghidra/pwn up front."""

from pathlib import Path

from backend.tool_router import detect_packs


def test_apk_only_prefetches_mobile(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "PWNKnight.apk").write_bytes(b"PK\x03\x04")
    (tmp_path / "challenge.txt").write_text(
        "Flutter mobile CTF\nflags_required: 3\n",
        encoding="utf-8",
    )
    packs = detect_packs(tmp_path)
    assert packs == ["mobile"]


def test_apk_with_crypto_tag_keeps_crypto(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "app.apk").write_bytes(b"PK\x03\x04")
    (tmp_path / "challenge.txt").write_text("Tags: mobile, crypto\n", encoding="utf-8")
    packs = detect_packs(tmp_path)
    assert "mobile" in packs
    assert "crypto" in packs
