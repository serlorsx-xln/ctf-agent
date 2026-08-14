"""Paste rules: greetings skip, everything else loads (TUI interceptor)."""

from __future__ import annotations

from pathlib import Path

from backend.challenge_paste import (
    _SPEC_PATH,
    extract_challenge_paths,
    is_greeting,
    looks_like_challenge_paste,
)


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
    assert is_greeting("hi")
    assert is_greeting("hello")
    assert is_greeting("สวัสดี")
    assert not looks_like_challenge_paste("hi")
    assert not looks_like_challenge_paste("hello")
    assert not looks_like_challenge_paste("")


def test_accepts_plain_prose_without_ctf_keywords():
    text = (
        "Line one of a normal chat paragraph that is quite long enough.\n"
        "Line two continues the ordinary conversation without those keywords."
    )
    assert looks_like_challenge_paste(text)


def test_accepts_pwnknight_style_narrative():
    text = (
        "A knight born of experiment, a fusion of code and magic.\n"
        "PwnKnight walks the dungeon of darkness in a massive sandbox."
    )
    assert looks_like_challenge_paste(text)


def test_accepts_bare_https_and_one_liners():
    assert looks_like_challenge_paste("see https://docs.example.com/guide")
    assert looks_like_challenge_paste("Connect: https://chal.example.com:8443/")
    assert looks_like_challenge_paste("solve this")


def test_shared_spec_file_is_canonical():
    assert _SPEC_PATH.is_file()
    assert _SPEC_PATH.resolve() == (
        Path(__file__).resolve().parents[1] / "shared" / "challenge_paste.json"
    ).resolve()


def test_extract_windows_drive_path():
    paths = extract_challenge_paths(r"C:\Users\me\challenges\glass please solve")
    assert paths == [r"C:\Users\me\challenges\glass"]


def test_extract_windows_drive_path_forward_slash():
    paths = extract_challenge_paths("C:/Users/me/challenges/glass please solve")
    assert paths == ["C:/Users/me/challenges/glass"]
