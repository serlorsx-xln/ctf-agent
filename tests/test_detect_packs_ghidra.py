"""ELF challenges prefetch ghidra alongside pwn."""

from pathlib import Path

from backend.tool_router import TOOL_TO_PACK, detect_packs, infer_pack_from_command


def test_pyghidra_maps_to_ghidra_pack():
    assert TOOL_TO_PACK["pyghidra"] == "ghidra"
    assert infer_pack_from_command("python3 -c 'import pyghidra'") == "ghidra"


def test_elf_prefetch_includes_ghidra(tmp_path: Path):
    # Minimal ELF magic so _looks_like_elf trips without a real binary.
    elf = tmp_path / "chal"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    (tmp_path / "challenge.txt").write_text("reverse me\n", encoding="utf-8")
    packs = detect_packs(tmp_path)
    assert "pwn" in packs
    assert "ghidra" in packs
