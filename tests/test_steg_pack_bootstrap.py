"""Pack donor vs apt/pip-only consistency."""

from backend.tool_router import PACK_SPECS, bootstrap_script, donor_build_hint


def test_apt_only_packs_use_core_and_empty_paths():
    for pack_id in ("forensics", "web", "ml", "containers"):
        spec = PACK_SPECS[pack_id]
        assert spec.image == "ctf-sandbox-core", pack_id
        assert spec.paths == (), pack_id
        assert not spec.requires_donor, pack_id


def test_donor_packs_declare_non_core_image_and_paths():
    for pack_id in (
        "steg",
        "pwn",
        "ghidra",
        "crypto",
        "crypto-tools",
        "linux",
        "mobile",
    ):
        spec = PACK_SPECS[pack_id]
        assert spec.image != "ctf-sandbox-core", pack_id
        assert spec.paths, pack_id
        assert spec.requires_donor, pack_id


def test_steg_is_donor_pack_with_stegseek_tree():
    spec = PACK_SPECS["steg"]
    assert spec.image == "ctf-sandbox-steg"
    assert "/opt/stegseek" in spec.paths
    assert "libimage-exiftool-perl" in spec.apt
    script = bootstrap_script("steg")
    assert "stegseek" in script
    hint = donor_build_hint("steg")
    assert "Dockerfile.steg" in hint


def test_forensics_upgrades_capstone_for_binwalk():
    assert any(p.startswith("capstone") for p in PACK_SPECS["forensics"].pip)


def test_pack_marker_changes_when_apt_recipe_grows():
    """Stale .ready markers must not skip bootstrap after pack recipe edits."""
    from backend.tool_router import pack_marker_path

    before = pack_marker_path("forensics")
    spec = PACK_SPECS["forensics"]
    original = spec.apt
    object.__setattr__(spec, "apt", (*original, "__test_only_pkg__"))
    try:
        after = pack_marker_path("forensics")
    finally:
        object.__setattr__(spec, "apt", original)
    assert before != after
    assert pack_marker_path("forensics") == before
