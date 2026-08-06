"""Paste-heuristic parity with shared/challenge_paste.json (TUI + Cursor stub)."""

from __future__ import annotations

from pathlib import Path

from backend.challenge_paste import _SPEC_PATH, extract_challenge_paths, looks_like_challenge_paste


def test_ssh_password_paste():
    text = "\n".join(
        [
            "This is a simple CRC calculator for kernel module programming exercise.",
            "I bet there are no bugs.",
            "but you can check it if you want.",
            "",
            "ssh kcrc@pwnable.kr -p2222 (pw: guest)",
        ]
    )
    assert looks_like_challenge_paste(text)


def test_nc_flag_format():
    assert looks_like_challenge_paste("Connect: nc 1.2.3.4 1337\nflag{...}")


def test_rejects_short_greetings():
    assert not looks_like_challenge_paste("hi")
    assert not looks_like_challenge_paste("hello")


def test_rejects_plain_chat_prose():
    text = (
        "Line one of a normal chat paragraph that is quite long enough.\n"
        "Line two continues the ordinary conversation without those keywords."
    )
    assert not looks_like_challenge_paste(text)


def test_accepts_multiline_ctf_prose():
    text = (
        "This challenge hides a secret flag in the binary.\n"
        "You should reverse it carefully and extract the answer."
    )
    assert looks_like_challenge_paste(text)


def test_bare_https_not_enough():
    assert not looks_like_challenge_paste("see https://docs.example.com/guide")


def test_https_with_port_is_enough():
    assert looks_like_challenge_paste("Connect: https://chal.example.com:8443/")


def test_shared_spec_file_is_canonical():
    assert _SPEC_PATH.is_file()
    assert _SPEC_PATH.resolve() == (
        Path(__file__).resolve().parents[1] / "shared" / "challenge_paste.json"
    ).resolve()


def test_extract_windows_drive_path():
    paths = extract_challenge_paths(r"C:\Users\me\challenges\glass please solve")
    assert paths == [r"C:\Users\me\challenges\glass"]
