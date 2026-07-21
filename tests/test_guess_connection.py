"""guess_connection prefers real endpoints over archive/writeup URLs."""

from backend.challenge import guess_connection


def test_connect_at_beats_archive_source_url() -> None:
    text = """
Source: https://archive.ooo/c/barb-metal/
Tags: pwn

Connect at localhost 7828
(From the agent sandbox use host.docker.internal:7828)
"""
    assert guess_connection(text) == "localhost:7828"


def test_skips_archive_ooo_when_offline() -> None:
    text = """
Source: https://archive.ooo/c/smart-cryptooo/
Offline challenge: solve from files under /challenge/distfiles/.
"""
    assert guess_connection(text) == ""


def test_nc_line() -> None:
    assert guess_connection("nc chall.example.com 1337") == "nc chall.example.com 1337"


def test_real_http_endpoint() -> None:
    assert guess_connection("Open https://chal.example.com:8443/login") == (
        "https://chal.example.com:8443/login"
    )
