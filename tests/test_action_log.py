"""Tool-trail notes that back the end-of-run solve recap."""

from __future__ import annotations

from backend.action_log import append_action, format_tool_line, notes_from_actions


def test_format_bash_and_submit():
    assert format_tool_line("bash", {"command": "python3 solve.py"}) == "bash: python3 solve.py"
    assert format_tool_line("submit_flag", {"flag": "flag{x}"}) == "submit_flag: flag{x}"
    assert format_tool_line("Bash", {"command": "ls"}) == "Bash: ls"
    assert "submit_flag: flag{x}" in format_tool_line("mcp__ctf__submit_flag", {"flag": "flag{x}"})


def test_append_keeps_a_bound():
    log: list[str] = []
    for i in range(100):
        append_action(log, "bash", {"command": f"echo {i}"})
    assert len(log) == 40
    assert log[0].endswith("echo 60")
    assert log[-1].endswith("echo 99")


def test_notes_include_prose_xor_steps():
    actions = [
        "bash: cat /challenge/TOOLS.txt",
        "bash: python3 /challenge/workspace/solve.py",
        "submit_flag: flag{abc}",
    ]
    note = notes_from_actions(
        actions,
        prose=(
            "Challenge\nBrute seed recovery on the firmware dump.\n\n"
            "Key insight\nThe PRNG seed is only 16 bits.\n\n"
            "How\n1. dump flash\n2. brute the seed"
        ),
    )
    assert "16 bits" in note
    assert "bash:" not in note
    assert "submit_flag:" not in note


def test_notes_work_without_prose():
    note = notes_from_actions(["bash: ls", "submit_flag: flag{x}"], prose="")
    assert "Brute" not in note
    assert "1. bash: ls" in note
    assert "submit_flag:" not in note


def test_notes_drop_error_prose_but_keep_the_trail():
    note = notes_from_actions(["bash: ls"], prose="Error: boom")
    assert "Error:" not in note
    assert "1. bash: ls" in note


def test_notes_prefer_commands_when_prose_is_accept_spam():
    note = notes_from_actions(
        ["bash: tshark -r cap.pcapng", "submit_flag: flag{x}"],
        prose="The flag was accepted as CORRECT. FLAG: flag{x}",
    )
    assert "1. bash: tshark" in note
    assert "CORRECT" not in note
