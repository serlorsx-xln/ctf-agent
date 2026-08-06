"""Prefetch linux pack for Assumed Breach / AD remote labs."""

from backend.tool_router import _wants_linux_remote_pack, detect_packs


def test_assumed_breach_wants_linux(tmp_path):
    chal = tmp_path / "assumed-breach"
    chal.mkdir()
    (chal / "challenge.txt").write_text(
        "Assumed Breach AD lab. You have a foothold on the Windows domain.\n"
        "Use NetExec / CrackMapExec and LDAP to enumerate.\n",
        encoding="utf-8",
    )
    text = (chal / "challenge.txt").read_text(encoding="utf-8")
    assert _wants_linux_remote_pack(text)
    assert "linux" in detect_packs(str(chal))


def test_crypto_wording_alone_does_not_force_linux():
    assert not _wants_linux_remote_pack(
        "This crypto challenge uses RSA and lattice crypto techniques."
    )


def test_ssh_guest_paste_wants_linux(tmp_path):
    chal = tmp_path / "kcrc"
    chal.mkdir()
    (chal / "challenge.txt").write_text(
        "This is a simple CRC calculator for kernel module programming exercise.\n"
        "ssh kcrc@pwnable.kr -p2222 (pw: guest)\n",
        encoding="utf-8",
    )
    text = (chal / "challenge.txt").read_text(encoding="utf-8")
    assert _wants_linux_remote_pack(text)
    assert "linux" in detect_packs(str(chal))


def test_sshpass_mention_wants_linux():
    assert _wants_linux_remote_pack("Connect with sshpass -p guest ssh user@host -p 2222")

