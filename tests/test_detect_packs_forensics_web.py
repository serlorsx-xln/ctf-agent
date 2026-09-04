"""Prefetch forensics (pcap) and web (php/html) packs from distfiles."""

from pathlib import Path

from backend.tool_router import (
    PACK_SPECS,
    TOOL_TO_PACK,
    detect_packs,
    infer_pack_from_command,
)


def test_pcap_prefetch_forensics(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "capture.pcap").write_bytes(b"\xd4\xc3\xb2\xa1")
    assert "forensics" in detect_packs(tmp_path)


def test_pcapng_prefetch_forensics(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "traffic.pcapng").write_bytes(b"\n\r\r\n")
    assert "forensics" in detect_packs(tmp_path)


def test_har_prefetch_forensics(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "session.har").write_text('{"log":{"entries":[]}}', encoding="utf-8")
    assert "forensics" in detect_packs(tmp_path)


def test_tags_forensics_prefetch_even_without_pcap(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "evidence.zip").write_bytes(b"PK\x03\x04")
    (tmp_path / "challenge.txt").write_text(
        "6278\n\nTags: forensics\n\nRecover the flag.\n",
        encoding="utf-8",
    )
    assert "forensics" in detect_packs(tmp_path)


def test_tags_network_prefetch_forensics(tmp_path: Path):
    (tmp_path / "challenge.txt").write_text(
        "pcap challenge\n\nTags: network\n",
        encoding="utf-8",
    )
    assert "forensics" in detect_packs(tmp_path)


def test_php_prefetch_web_not_linux(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "index.php").write_text("<?php echo 1;")
    packs = detect_packs(tmp_path)
    assert "web" in packs
    assert "linux" not in packs


def test_js_alone_does_not_prefetch_web(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "app.js").write_text("console.log(1)")
    assert "web" not in detect_packs(tmp_path)


def test_wasm_prefetch_web(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "vault.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    assert "web" in detect_packs(tmp_path)


def test_jpg_does_not_prefetch_steg(tmp_path: Path):
    """A JPEG is not enough — strings/exif live on L0; steg attaches on demand."""
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "SecretOrSeeReal.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (tmp_path / "challenge.txt").write_text(
        "Secret or See Real\n\nTags: crypto\n\nRecover the flag.\n",
        encoding="utf-8",
    )
    packs = detect_packs(tmp_path)
    assert "steg" not in packs
    assert "crypto" in packs


def test_tags_stego_still_prefetch_steg(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "hidden.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "challenge.txt").write_text(
        "Hidden\n\nTags: stego\n\nRecover the flag.\n",
        encoding="utf-8",
    )
    assert "steg" in detect_packs(tmp_path)


def test_xlsm_prefetch_forensics(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "horse.xlsm").write_bytes(b"PK\x03\x04")
    assert "forensics" in detect_packs(tmp_path)


def test_img_prefetch_forensics(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "PiCam.img").write_bytes(b"PICAM")
    assert "forensics" in detect_packs(tmp_path)


def test_forensics_firmware_tools_mapped():
    assert "squashfs-tools" in PACK_SPECS["forensics"].apt
    assert "sqlite3" in PACK_SPECS["forensics"].apt
    assert "qemu-user-static" in PACK_SPECS["forensics"].apt
    assert "libc6-armhf-cross" in PACK_SPECS["forensics"].apt
    assert "libstdc++6-armhf-cross" in PACK_SPECS["forensics"].apt
    assert TOOL_TO_PACK["unsquashfs"] == "forensics"
    assert TOOL_TO_PACK["sqlite3"] == "forensics"
    assert TOOL_TO_PACK["qemu-arm-static"] == "forensics"
    assert TOOL_TO_PACK["qarm"] == "forensics"
    assert infer_pack_from_command("unsquashfs -d out root.sqsh") == "forensics"
    assert infer_pack_from_command("qemu-arm-static ./picam-verify --help") == "forensics"
    from backend.tool_router import bootstrap_script

    boot = bootstrap_script("forensics")
    assert "qemu-arm-static" in boot
    assert "/usr/local/bin/qarm" in boot


def test_tool_routing_new_binaries():
    assert TOOL_TO_PACK["tshark"] == "forensics"
    assert TOOL_TO_PACK["olevba"] == "forensics"
    assert TOOL_TO_PACK["sqlmap"] == "web"
    assert TOOL_TO_PACK["node"] == "web"
    assert TOOL_TO_PACK["wasm2wat"] == "web"
    assert TOOL_TO_PACK["nxc"] == "linux"
    assert TOOL_TO_PACK["ldapsearch"] == "linux"
    assert TOOL_TO_PACK["bloodhound-python"] == "linux"
    assert infer_pack_from_command("tshark -r x.pcap") == "forensics"
    assert infer_pack_from_command("olevba horse.xlsm") == "forensics"
    assert infer_pack_from_command("sqlmap -u http://x/") == "web"
    assert infer_pack_from_command("node vault.js") == "web"
    assert infer_pack_from_command("wasm2wat vault.wasm") == "web"
    assert infer_pack_from_command("nxc smb 10.0.0.1") == "linux"


def test_pwn_pack_includes_x86_cross_binutils():
    assert "binutils-x86-64-linux-gnu" in PACK_SPECS["pwn"].apt
    assert "gdb-multiarch" in PACK_SPECS["pwn"].apt
    assert "libstdc++6-amd64-cross" in PACK_SPECS["pwn"].apt
    assert "libstdc++6-i386-cross" in PACK_SPECS["pwn"].apt
    assert "nasm" in PACK_SPECS["pwn"].apt
    assert "libc6-dbg" in PACK_SPECS["pwn"].apt
    from backend.tool_router import TOOL_TO_PACK, bootstrap_script, infer_pack_from_command

    boot = bootstrap_script("pwn")
    assert "x86_64-linux-gnu-objdump" in boot
    assert "pip packages already present; skipping pip" in boot
    assert "gem packages already present; skipping gem install" in boot
    assert "/usr/local/bin/objdump" in boot
    assert "gdb-multiarch" in boot
    assert "/lib/x86_64-linux-gnu/libc.so.6" in boot
    assert "/lib/x86_64-linux-gnu/libstdc++.so.6" in boot
    assert TOOL_TO_PACK["gdb-multiarch"] == "pwn"
    assert infer_pack_from_command("gdb-multiarch -q ./chal") == "pwn"
    root = Path(__file__).resolve().parents[1]
    pwn_df = (root / "sandbox" / "Dockerfile.pwn").read_text(encoding="utf-8")
    mobile_df = (root / "sandbox" / "Dockerfile.mobile").read_text(encoding="utf-8")
    assert "libstdc++6-amd64-cross" in pwn_df
    assert "libstdc++6-i386-cross" in pwn_df
    assert "nasm" in pwn_df
    assert "libc6-dbg" in pwn_df
    assert "libstdc++6-amd64-cross" in mobile_df
    assert "libstdc++6-amd64-cross" in PACK_SPECS["mobile"].apt
    assert "libstdc++6-i386-cross" in PACK_SPECS["mobile"].apt


def test_pack_specs_include_new_packages():
    assert "tshark" in PACK_SPECS["forensics"].apt
    assert "squashfs-tools" in PACK_SPECS["forensics"].apt
    assert "sqlite3" in PACK_SPECS["forensics"].apt
    assert "scapy" in PACK_SPECS["forensics"].pip
    assert "oletools" in PACK_SPECS["forensics"].pip
    assert "openpyxl" in PACK_SPECS["forensics"].pip
    assert "sqlmap" in PACK_SPECS["web"].apt
    assert "nodejs" in PACK_SPECS["web"].apt
    assert "wabt" in PACK_SPECS["web"].apt
    assert "dnfile" in PACK_SPECS["ghidra"].pip
    assert "mono-utils" in PACK_SPECS["ghidra"].apt
    assert "ldap-utils" in PACK_SPECS["linux"].apt
    assert "bloodhound" in PACK_SPECS["linux"].pip
    assert "rustc" in PACK_SPECS["linux"].apt
    # NetExec installs from git in bootstrap (not batched pip — avoids aborting bloodhound).
    assert "netexec" not in PACK_SPECS["linux"].pip
    from backend.tool_router import bootstrap_script

    boot = bootstrap_script("linux")
    assert "Pennyw0rth/NetExec" in boot
    assert "bloodhound" in boot or "certipy-ad" in boot
