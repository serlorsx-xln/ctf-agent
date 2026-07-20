"""Unit tests for Claude bash harness verb extraction."""

from backend.bash_intercept import (
    extract_notify_coordinator,
    extract_submit_flag,
    parse_submit_flag,
    submit_flag_suffix,
)


def test_submit_flag_plain():
    assert extract_submit_flag("submit_flag 'ECSC{abc}'") == "ECSC{abc}"
    assert extract_submit_flag('submit_flag "ECSC{abc}"') == "ECSC{abc}"
    assert extract_submit_flag("submit_flag ECSC{abc}") == "ECSC{abc}"


def test_submit_flag_after_cd():
    cmd = "cd /challenge && submit_flag 'ECSC{1s0m0rph1sms_w1th_0ur_0ld_fr13nd_Evariste_8beb83d57fb48ea1}'"
    assert extract_submit_flag(cmd) == (
        "ECSC{1s0m0rph1sms_w1th_0ur_0ld_fr13nd_Evariste_8beb83d57fb48ea1}"
    )


def test_submit_flag_after_semicolon():
    assert extract_submit_flag("pwd; submit_flag FLAG{hello_world_xx}") == "FLAG{hello_world_xx}"


def test_submit_flag_not_present():
    assert extract_submit_flag("echo submit_flag is a tool") is None
    assert extract_submit_flag("ls /challenge") is None


def test_submit_flag_shell_expansion_not_literal():
    assert extract_submit_flag('submit_flag "$(cat /tmp/f)"') is None
    parsed = parse_submit_flag('submit_flag "$(cat /tmp/f)"')
    assert parsed is not None and parsed.has_expansion
    parsed2 = parse_submit_flag("submit_flag $(cat /tmp/f)")
    assert parsed2 is not None and parsed2.has_expansion


def test_submit_flag_suffix_kept():
    cmd = "submit_flag 'FLAG{abc}' && echo next"
    parsed = parse_submit_flag(cmd)
    assert parsed is not None
    assert submit_flag_suffix(cmd, parsed) == "echo next"


def test_notify_coordinator_compound():
    assert (
        extract_notify_coordinator("cd /tmp && notify_coordinator 'stuck on kerberos'")
        == "stuck on kerberos"
    )
