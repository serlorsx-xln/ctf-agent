"""Early prose notes captured at flag accept."""

from __future__ import annotations

from backend.action_log import notes_from_prose


def test_notes_include_usable_prose():
    note = notes_from_prose(
        "Challenge\nBrute seed recovery on the firmware dump.\n\n"
        "Key insight\nThe PRNG seed is only 16 bits.\n\n"
        "How\n1. dump flash\n2. brute the seed"
    )
    assert "16 bits" in note
    assert "bash:" not in note


def test_notes_empty_without_prose():
    assert notes_from_prose("") == ""


def test_notes_drop_error_prose():
    assert notes_from_prose("Error: boom") == ""


def test_notes_skip_accept_spam():
    assert notes_from_prose("The flag was accepted as CORRECT. FLAG: flag{x}") == ""
