"""Owned Jeopardy fixtures stay file-only, pack-correct, and flag-safe."""

from __future__ import annotations

from backend.challenge import guess_connection, list_attachment_names, load_challenge
from backend.flags import accept_flag, is_decoy_flag
from backend.jeopardy_fixtures import FLAGS, SPECS, fixture_dir, write_text_fixtures
from backend.prompts import build_prompt, wants_crypto_tool_names
from backend.tool_router import detect_packs


def test_write_text_fixtures_and_contracts(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.jeopardy_fixtures.FIXTURES_ROOT", tmp_path)
    write_text_fixtures(tmp_path)

    for spec in SPECS:
        root = fixture_dir(spec.slug, tmp_path)
        meta = load_challenge(root)
        names = list_attachment_names(root)
        packs = detect_packs(root)
        flag = FLAGS[spec.slug]

        assert not is_decoy_flag(flag)
        assert guess_connection(meta.description) == ""
        assert "FIRST ACTION" not in build_prompt(meta, names, "arm64", True)

        if spec.needs_elf:
            assert spec.slug in {"rev-crackme", "pwn-auth"}
            continue

        assert names, spec.slug
        assert set(packs) == set(spec.prefetch), (spec.slug, packs, spec.prefetch)
        msg, done = accept_flag(flag, human_confirmed=True)
        assert done and msg.startswith("CORRECT"), (spec.slug, msg)

    assert wants_crypto_tool_names(
        (tmp_path / "crypto-xor" / "challenge.txt").read_text(encoding="utf-8"),
        ["cipher.hex"],
    ) is False
    assert wants_crypto_tool_names("Tags: crypto", ["cipher.hex"]) is True
    steg_txt = (tmp_path / "steg-append" / "challenge.txt").read_text(encoding="utf-8")
    assert "Tags: steg" in steg_txt
    assert "steg" in detect_packs(tmp_path / "steg-append")
    assert "web" in detect_packs(tmp_path / "web-html")
    assert "RsaCtfTool" not in build_prompt(
        load_challenge(tmp_path / "misc-b64"),
        list_attachment_names(tmp_path / "misc-b64"),
        "arm64",
        True,
    )
