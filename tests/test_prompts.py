"""Prompt builder keeps Veria operational hints without CTFd metadata."""

from backend.prompts import ChallengeMeta, build_prompt


def test_remote_nc_gets_first_action_and_heredoc():
    text = build_prompt(
        ChallengeMeta(name="pwn", description="hi", connection_info="nc 1.2.3.4 1337"),
        distfile_names=[],
        has_named_tools=True,
    )
    assert "FIRST ACTION REQUIRED" in text
    assert "nc 1.2.3.4 1337 <<'EOF'" in text
    assert "TOOLS.txt" in text


def test_assumed_breach_without_endpoint_no_first_action():
    text = build_prompt(
        ChallengeMeta(
            name="pingpong",
            description="Target IP Address\n10.129.63.138\ncreds c.roberts",
            connection_info="",
            flags_required=2,
        ),
        distfile_names=[],
        has_named_tools=False,
    )
    assert "FIRST ACTION REQUIRED" not in text
    assert "2 distinct flags" in text
    assert "TOOLS.txt" in text


def test_image_and_binary_hints():
    text = build_prompt(
        ChallengeMeta(name="re", description="rev me"),
        distfile_names=["chal.bin", "pic.png"],
        has_named_tools=True,
    )
    assert "view_image" in text
    assert "pyghidra** is available" in text or "**pyghidra** is available" in text
    assert "cado-nfs" not in text
    assert "RsaCtfTool" not in text
    assert "Binary Analysis" in text


def test_crypto_tag_keeps_rsa_tools():
    text = build_prompt(
        ChallengeMeta(name="rsa", description="Tags: crypto\n\nFactor n."),
        distfile_names=["output.txt"],
        has_named_tools=True,
    )
    assert "cado-nfs" in text
    assert "RsaCtfTool" in text
