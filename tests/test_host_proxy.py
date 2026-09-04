"""Host SOCKS5 hop — bind, auth, no adopt."""

from __future__ import annotations

import asyncio
import socket

import pytest

from backend.host_proxy import (
    _state,
    acquire_host_proxy,
    release_host_proxy,
    socks_bind_hosts,
)


def test_socks_bind_never_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTF_HOST_PROXY_BIND", "0.0.0.0,127.0.0.1")
    hosts = socks_bind_hosts()
    assert "0.0.0.0" not in hosts
    assert "127.0.0.1" in hosts


def test_socks_bind_default_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTF_HOST_PROXY_BIND", raising=False)
    assert socks_bind_hosts()[0] == "127.0.0.1"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _reset_proxy_state() -> None:
    _state.servers.clear()
    _state.port = 0
    _state.leases = 0
    _state.user = ""
    _state.password = ""


@pytest.mark.asyncio
async def test_socks_requires_userpass(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_proxy_state()
    monkeypatch.setenv("CTF_HOST_PROXY", "1")
    monkeypatch.setenv("CTF_HOST_PROXY_BIND", "127.0.0.1")
    port = _free_port()
    monkeypatch.setenv("CTF_HOST_PROXY_PORT", str(port))
    got = await acquire_host_proxy()
    try:
        assert got == port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readexactly(2), timeout=2)
        writer.close()
        await writer.wait_closed()
        assert resp == b"\x05\xff"
    finally:
        await release_host_proxy()
        _state.leases = 0


@pytest.mark.asyncio
async def test_socks_does_not_adopt_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_proxy_state()
    monkeypatch.setenv("CTF_HOST_PROXY", "1")
    monkeypatch.setenv("CTF_HOST_PROXY_BIND", "127.0.0.1")
    port = _free_port()
    monkeypatch.setenv("CTF_HOST_PROXY_PORT", str(port))
    srv = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=port)
    try:
        got = await acquire_host_proxy()
        assert got is None
        assert not _state.servers
    finally:
        srv.close()
        await srv.wait_closed()
        await release_host_proxy()
        _state.leases = 0
