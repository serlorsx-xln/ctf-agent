"""Unit tests for Claude bash harness verb extraction."""

from backend.bash_intercept import (
    SUBMIT_EXPANSION_ERROR,
    SUBMIT_UNPARSED_ERROR,
    extract_notify_coordinator,
    parse_submit_flag,
    submit_flag_attempted,
    submit_flag_suffix,
)


def _literal_flag(command: str) -> str | None:
    parsed = parse_submit_flag(command)
    if parsed is None or parsed.has_expansion:
        return None
    return parsed.value or None


def test_submit_flag_plain():
    assert _literal_flag("submit_flag 'ECSC{abc}'") == "ECSC{abc}"
    assert _literal_flag('submit_flag "ECSC{abc}"') == "ECSC{abc}"
    assert _literal_flag("submit_flag ECSC{abc}") == "ECSC{abc}"


def test_submit_flag_after_cd():
    cmd = "cd /challenge && submit_flag 'ECSC{1s0m0rph1sms_w1th_0ur_0ld_fr13nd_Evariste_8beb83d57fb48ea1}'"
    assert _literal_flag(cmd) == (
        "ECSC{1s0m0rph1sms_w1th_0ur_0ld_fr13nd_Evariste_8beb83d57fb48ea1}"
    )


def test_submit_flag_after_semicolon():
    assert _literal_flag("pwd; submit_flag FLAG{hello_world_xx}") == "FLAG{hello_world_xx}"


def test_submit_flag_not_present():
    assert _literal_flag("echo submit_flag is a tool") is None
    assert _literal_flag("ls /challenge") is None
    assert not submit_flag_attempted("echo submit_flag is a tool")


def test_submit_flag_shell_expansion_not_literal():
    assert _literal_flag('submit_flag "$(cat /tmp/f)"') is None
    parsed = parse_submit_flag('submit_flag "$(cat /tmp/f)"')
    assert parsed is not None and parsed.has_expansion
    parsed2 = parse_submit_flag("submit_flag $(cat /tmp/f)")
    assert parsed2 is not None and parsed2.has_expansion


def test_submit_flag_dollar_var_is_expansion():
    parsed = parse_submit_flag('submit_flag "$f"')
    assert parsed is not None
    assert parsed.has_expansion
    assert _literal_flag('submit_flag "$f"') is None
    assert SUBMIT_EXPANSION_ERROR.startswith("ERROR:")


def test_submit_flag_with_redirect_and_pipe():
    cmd = (
        'cd /challenge/workspace && for f in "v1t{abc}" "70aa"; do '
        'echo "trying: $f"; submit_flag "$f" 2>&1 | head -2; done'
    )
    parsed = parse_submit_flag(cmd)
    assert parsed is not None
    assert parsed.has_expansion
    assert parsed.value == "$f"


def test_submit_flag_literal_with_redirect():
    cmd = 'submit_flag "v1t{real_flag_here}" 2>&1 | head -2'
    assert _literal_flag(cmd) == "v1t{real_flag_here}"
    parsed = parse_submit_flag(cmd)
    assert parsed is not None
    assert submit_flag_suffix(cmd, parsed) == ""


def test_submit_flag_attempted_unparsed_loop_without_arg():
    # bare submit_flag with no args still counts as an attempt
    assert submit_flag_attempted("submit_flag")
    assert parse_submit_flag("submit_flag") is None
    assert SUBMIT_UNPARSED_ERROR.startswith("ERROR:")


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
