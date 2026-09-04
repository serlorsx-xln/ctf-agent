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


def test_ssh_line() -> None:
    assert guess_connection(
        "ssh kcrc@pwnable.kr -p2222 (pw: guest)"
    ) == "ssh kcrc@pwnable.kr -p2222"


def test_ssh_without_port() -> None:
    assert guess_connection("ssh user@chal.example.com") == "ssh user@chal.example.com"


def test_real_http_endpoint() -> None:
    assert guess_connection("Open https://chal.example.com:8443/login") == (
        "https://chal.example.com:8443/login"
    )


def test_skips_nc_on_ctftime() -> None:
    assert guess_connection("see writeup then nc ctftime.org 443") == ""


def test_doc_nc_does_not_beat_lab_url() -> None:
    assert guess_connection(
        "https://lab.example:1337 and nc archive.ooo 9999"
    ) == "https://lab.example:1337"


def test_prefers_real_nc_over_doc_nc() -> None:
    assert (
        guess_connection("nc ctftime.org 443 then nc chall.example.com 31337")
        == "nc chall.example.com 31337"
    )

