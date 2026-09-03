"""Fast start: skip apt/pip when L0 already is the pack donor / warm image."""

from __future__ import annotations

from backend.tool_router import (
    bootstrap_script,
    image_tag_base,
    l0_already_provides_pack,
)


def test_image_tag_base_strips_tag_and_digest():
    assert image_tag_base("ctf-sandbox-mobile") == "ctf-sandbox-mobile"
    assert image_tag_base("ctf-sandbox-mobile:latest") == "ctf-sandbox-mobile"
    assert image_tag_base("ctf-sandbox-mobile@sha256:abc") == "ctf-sandbox-mobile"
    assert image_tag_base("localhost:5000/ctf-sandbox-mobile:v1") == "localhost:5000/ctf-sandbox-mobile"


def test_l0_already_provides_pack_for_donors_and_warm():
    assert l0_already_provides_pack("ctf-sandbox-mobile", "mobile")
    assert l0_already_provides_pack("ctf-sandbox-mobile:latest", "mobile")
    assert l0_already_provides_pack("ctf-sandbox-pwn", "pwn")
    assert l0_already_provides_pack("ctf-sandbox-warm-web", "web")
    assert not l0_already_provides_pack("ctf-sandbox-core", "mobile")
    assert not l0_already_provides_pack("ctf-sandbox-mobile", "pwn")


def test_mobile_light_bootstrap_skips_apt_pip_keeps_wrapper():
    full = bootstrap_script("mobile", packages=True)
    light = bootstrap_script("mobile", packages=False)
    assert "apt-get" in full
    assert "pip3" in full or "PIP3" in full
    assert "import frida" in full or "frida-tools" in full
    assert "apt-get" not in light
    assert "PIP3" not in light
    assert "cat > /usr/local/bin/blutter" in light
    assert "tools ready" in light


def test_pwn_light_bootstrap_still_writes_qemu_wrappers():
    light = bootstrap_script("pwn", packages=False)
    assert "apt-get" not in light
    assert "qemu-x86_64-static" in light
    assert "tools ready" in light


def test_ghidra_light_bootstrap_still_seeds_env():
    light = bootstrap_script("ghidra", packages=False)
    assert "apt-get" not in light
    assert "GHIDRA_INSTALL_DIR" in light
    assert "analyzeHeadless" in light


def test_forensics_light_bootstrap_still_writes_qemu_arm():
    light = bootstrap_script("forensics", packages=False)
    assert "apt-get" not in light
    assert "qemu-arm-static" in light
