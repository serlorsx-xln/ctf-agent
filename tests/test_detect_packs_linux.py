"""Prefetch linux pack for Assumed Breach / AD remote labs."""

from pathlib import Path

from backend.tool_router import _wants_linux_remote_pack, detect_packs


def test_assumed_breach_wants_linux():
    text = Path("challenges/pingpong/challenge.txt").read_text()
    assert _wants_linux_remote_pack(text)
    assert "linux" in detect_packs("challenges/pingpong")


def test_crypto_wording_alone_does_not_force_linux():
    assert not _wants_linux_remote_pack(
        "This crypto challenge uses RSA and lattice crypto techniques."
    )
