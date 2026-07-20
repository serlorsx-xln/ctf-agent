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


def test_tool_routing_new_binaries():
    assert TOOL_TO_PACK["tshark"] == "forensics"
    assert TOOL_TO_PACK["sqlmap"] == "web"
    assert TOOL_TO_PACK["nxc"] == "linux"
    assert TOOL_TO_PACK["ldapsearch"] == "linux"
    assert TOOL_TO_PACK["bloodhound-python"] == "linux"
    assert infer_pack_from_command("tshark -r x.pcap") == "forensics"
    assert infer_pack_from_command("sqlmap -u http://x/") == "web"
    assert infer_pack_from_command("nxc smb 10.0.0.1") == "linux"


def test_pack_specs_include_new_packages():
    assert "tshark" in PACK_SPECS["forensics"].apt
    assert "scapy" in PACK_SPECS["forensics"].pip
    assert "sqlmap" in PACK_SPECS["web"].apt
    assert "ldap-utils" in PACK_SPECS["linux"].apt
    assert "bloodhound" in PACK_SPECS["linux"].pip
    assert "rustc" in PACK_SPECS["linux"].apt
    # NetExec installs from git in bootstrap (not batched pip — avoids aborting bloodhound).
    assert "netexec" not in PACK_SPECS["linux"].pip
    from backend.tool_router import bootstrap_script

    boot = bootstrap_script("linux")
    assert "Pennyw0rth/NetExec" in boot
    assert "bloodhound" in boot or "certipy-ad" in boot
