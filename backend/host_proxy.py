"""Host-side SOCKS5 proxy so Docker sandboxes inherit the host VPN/routes.

On Mac, ``CTF_HOST_PROXY=auto`` starts this hop, then the sandbox calibrates:
prefer **direct** container routing when the lab is reachable; wrap agent bash
with proxychains only when direct fails (typical Docker Desktop + host VPN).
Agent ``nmap`` is hardened to ``-Pn -sT`` separately.

Force with ``CTF_HOST_PROXY=1`` / ``0``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import secrets
import socket
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18200


def host_proxy_mode() -> str:
    """``auto`` | ``1``/``on`` | ``0``/``off`` from ``CTF_HOST_PROXY``."""
    return os.environ.get("CTF_HOST_PROXY", "auto").strip().lower()


def host_proxy_should_enable() -> bool:
    mode = host_proxy_mode()
    if mode in ("0", "false", "no", "off"):
        return False
    if mode in ("1", "true", "yes", "on"):
        return True
    # auto: always on for macOS (Colima/Docker Desktop VPN gap).
    # Linux bridge usually inherits host routes; leave off unless forced.
    return platform.system() == "Darwin"


def host_proxy_port() -> int:
    raw = os.environ.get("CTF_HOST_PROXY_PORT", str(DEFAULT_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


@dataclass
class _ProxyState:
    servers: list[asyncio.AbstractServer] = field(default_factory=list)
    port: int = 0
    leases: int = 0
    owned: bool = True
    task_loop: asyncio.AbstractEventLoop | None = None
    user: str = ""
    password: str = ""


_state = _ProxyState()
_mu = asyncio.Lock()


def socks_bind_hosts() -> list[str]:
    """Interfaces the SOCKS hop may listen on (never ``0.0.0.0``).

    Override with ``CTF_HOST_PROXY_BIND=127.0.0.1,172.17.0.1``.
    Default: loopback; on Linux also the docker0 gateway so
    ``host.docker.internal`` (host-gateway) can reach the hop.
    """
    raw = (os.environ.get("CTF_HOST_PROXY_BIND") or "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip() and h.strip() != "0.0.0.0"]
    hosts = ["127.0.0.1"]
    if platform.system() != "Darwin":
        gw = _linux_docker_bridge_ip()
        if gw and gw not in hosts:
            hosts.append(gw)
    return hosts


def _linux_docker_bridge_ip() -> str:
    import re
    import subprocess

    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", "docker0"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def host_proxy_socks_auth() -> tuple[str, str]:
    """Username/password the sandbox proxychains client must present."""
    return _state.user, _state.password


async def _socks5_greeting_ok(port: int, host: str = "127.0.0.1") -> bool:
    """True if something on ``port`` answers a SOCKS5 no-auth greeting."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
    except Exception:
        return False
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readexactly(2), timeout=1.0)
        return resp[0] == 0x05
    except Exception:
        return False
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def acquire_host_proxy() -> int | None:
    """Start (or reuse) the host SOCKS5 proxy; return listen port."""
    if not host_proxy_should_enable():
        return None
    port = host_proxy_port()
    async with _mu:
        _state.leases += 1
        if _state.servers:
            return _state.port or port
        if not _state.user or not _state.password:
            _state.user = "artemis"
            _state.password = secrets.token_urlsafe(18)
        hosts = socks_bind_hosts()
        started: list[asyncio.AbstractServer] = []
        try:
            for host in hosts:
                started.append(
                    await asyncio.start_server(_handle_client, host=host, port=port)
                )
        except OSError as e:
            for srv in started:
                srv.close()
            _state.leases = max(0, _state.leases - 1)
            logger.error(
                "Host SOCKS5 proxy failed to bind %s:%s: %s "
                "(will not adopt an existing listener)",
                hosts,
                port,
                e,
            )
            return None
        _state.servers = started
        _state.port = port
        _state.owned = True
        _state.task_loop = asyncio.get_running_loop()
        addrs: list[str] = []
        for srv in started:
            addrs.extend(str(s.getsockname()) for s in srv.sockets or [])
        logger.info(
            "Host SOCKS5 proxy listening on %s (sandboxes use host.docker.internal:%s)",
            ", ".join(addrs),
            port,
        )
        return port


async def release_host_proxy() -> None:
    async with _mu:
        if _state.leases > 0:
            _state.leases -= 1
        if _state.leases > 0:
            return
        servers = list(_state.servers)
        _state.servers.clear()
        _state.port = 0
        _state.user = ""
        _state.password = ""
        for server in servers:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        if servers:
            logger.info("Host SOCKS5 proxy stopped")


def proxychains_conf(
    port: int,
    proxy_host: str = "host.docker.internal",
    *,
    user: str = "",
    password: str = "",
) -> str:
    """Config that sends non-local TCP via host SOCKS (incl. HTB 10.x).

    ``proxy_host`` must be a numeric IP for proxychains4 (hostnames are rejected
    as the first hop unless proxy_dns quirks apply — we resolve before write).
    """
    if not user and not password:
        user, password = host_proxy_socks_auth()
    auth = f" {user} {password}" if user and password else ""
    return f"""# Generated by ctf-agent — do not edit
strict_chain
proxy_dns
tcp_read_time_out 15000
tcp_connect_time_out 10000
[ProxyList]
socks5 {proxy_host} {port}{auth}
"""


# ── SOCKS5 implementation (CONNECT only) ─────────────────────────────────────


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        data = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        ver, nmethods = data[0], data[1]
        if ver != 5:
            return
        methods = await reader.readexactly(nmethods)
        user, password = _state.user, _state.password
        if user and password:
            if 0x02 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return
            writer.write(b"\x05\x02")
            await writer.drain()
            auth_ver = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            if auth_ver != 1:
                return
            ulen = (await reader.readexactly(1))[0]
            uname = await reader.readexactly(ulen)
            plen = (await reader.readexactly(1))[0]
            passwd = await reader.readexactly(plen)
            if uname.decode("utf-8", errors="replace") != user or passwd.decode(
                "utf-8", errors="replace"
            ) != password:
                writer.write(b"\x01\x01")
                await writer.drain()
                return
            writer.write(b"\x01\x00")
            await writer.drain()
        else:
            writer.write(b"\x05\x00")  # no auth (tests / auth not yet set)
            await writer.drain()

        hdr = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        ver, cmd, _, atyp = hdr
        if ver != 5 or cmd != 1:  # CONNECT only
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        if atyp == 1:  # IPv4
            addr_b = await reader.readexactly(4)
            host = socket.inet_ntoa(addr_b)
        elif atyp == 3:  # domain
            ln = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(ln)).decode("utf-8", errors="replace")
        elif atyp == 4:  # IPv6
            addr_b = await reader.readexactly(16)
            host = socket.inet_ntop(socket.AF_INET6, addr_b)
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        port_b = await reader.readexactly(2)
        port = struct.unpack("!H", port_b)[0]

        try:
            remote_r, remote_w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15
            )
        except Exception:
            writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return

        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await writer.drain()

        async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            _pipe(reader, remote_w),
            _pipe(remote_r, writer),
            return_exceptions=True,
        )
    except Exception as e:
        logger.debug("SOCKS client %s error: %s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
