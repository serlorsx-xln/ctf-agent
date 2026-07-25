"""ELF / .NET challenges prefetch ghidra."""

from pathlib import Path

from backend.tool_router import TOOL_TO_PACK, detect_packs, infer_pack_from_command


def test_pyghidra_maps_to_ghidra_pack():
    assert TOOL_TO_PACK["pyghidra"] == "ghidra"
    assert TOOL_TO_PACK["monodis"] == "ghidra"
    assert infer_pack_from_command("python3 -c 'import pyghidra'") == "ghidra"
    assert infer_pack_from_command("monodis --output=il.txt app.dll") == "ghidra"


def test_elf_prefetch_pwn_not_ghidra(tmp_path: Path):
    # Minimal ELF magic so _looks_like_elf trips without a real binary.
    elf = tmp_path / "chal"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    (tmp_path / "challenge.txt").write_text("reverse me\n", encoding="utf-8")
    packs = detect_packs(tmp_path)
    assert "pwn" in packs
    # Ghidra stays lazy unless Tags:rev / .NET — keeps cold start light.
    assert "ghidra" not in packs


def test_rev_tag_prefetch_ghidra(tmp_path: Path):
    elf = tmp_path / "chal"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    (tmp_path / "challenge.txt").write_text("Tags: rev\n", encoding="utf-8")
    packs = detect_packs(tmp_path)
    assert "ghidra" in packs


def test_dotnet_runtimeconfig_prefetch_ghidra(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "very_ez_re.runtimeconfig.json").write_text(
        '{"runtimeOptions":{"tfm":"net8.0"}}',
        encoding="utf-8",
    )
    (tmp_path / "challenge.txt").write_text(
        "very ez re\n\nTags: rev\n",
        encoding="utf-8",
    )
    packs = detect_packs(tmp_path)
    assert "ghidra" in packs
    assert "pwn" not in packs


def test_dotnet_dll_with_bsjb_prefetch_ghidra(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    # MZ + BSJB metadata marker used by CLI assemblies.
    (dist / "app.dll").write_bytes(b"MZ" + b"\x00" * 200 + b"BSJB" + b"\x00" * 40)
    assert "ghidra" in detect_packs(tmp_path)
