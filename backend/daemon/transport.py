"""Cross-platform Artemis daemon transport.

Unix/macOS/Linux: NDJSON over ``~/.cache/artemis/daemon.sock``.
Windows (and optional ``ARTEMIS_DAEMON_TCP=1``): NDJSON over TCP localhost;
port is written to ``daemon.port`` in the cache dir.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from typing import Any

from backend.cache import cache_dir

DEFAULT_TCP_HOST = "127.0.0.1"


def uses_tcp() -> bool:
    forced = (os.environ.get("ARTEMIS_DAEMON_TCP") or "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    return sys.platform == "win32"


def daemon_socket_path() -> Path:
    return cache_dir() / "daemon.sock"


def daemon_port_file() -> Path:
    return cache_dir() / "daemon.port"


def daemon_tcp_port() -> int:
    env = (os.environ.get("ARTEMIS_DAEMON_PORT") or "").strip()
    if env.isdigit():
        return int(env)
    pf = daemon_port_file()
    if pf.is_file():
        try:
            return int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return 0


def write_daemon_port(port: int) -> None:
    cache_dir().mkdir(parents=True, exist_ok=True)
    daemon_port_file().write_text(f"{port}\n", encoding="utf-8")


def clear_stale_endpoint_files() -> None:
    if uses_tcp():
        try:
            daemon_port_file().unlink(missing_ok=True)
        except OSError:
            pass
        return
    sock = daemon_socket_path()
    try:
        sock.unlink(missing_ok=True)
    except OSError:
        pass


def child_daemon_env() -> dict[str, str]:
    """Environment for swarm / usage child processes."""
    if uses_tcp():
        port = daemon_tcp_port()
        if port <= 0:
            port = int((os.environ.get("ARTEMIS_DAEMON_PORT") or "0") or 0)
        endpoint = f"tcp:{DEFAULT_TCP_HOST}:{port}"
        out = {"ARTEMIS_DAEMON_ENDPOINT": endpoint}
        if port > 0:
            out["ARTEMIS_DAEMON_PORT"] = str(port)
        return out
    sock = str(daemon_socket_path())
    return {
        "ARTEMIS_DAEMON_SOCK": sock,
        "ARTEMIS_DAEMON_ENDPOINT": f"unix:{sock}",
    }


def _parse_endpoint() -> tuple[str, Any]:
    ep = (os.environ.get("ARTEMIS_DAEMON_ENDPOINT") or "").strip()
    if ep.startswith("unix:"):
        return "unix", ep[5:]
    if ep.startswith("tcp:"):
        rest = ep[4:]
        host, _, port_s = rest.rpartition(":")
        if host and port_s.isdigit():
            return "tcp", (host, int(port_s))

    sock = (os.environ.get("ARTEMIS_DAEMON_SOCK") or "").strip()
    if sock:
        return "unix", sock

    if uses_tcp():
        port = daemon_tcp_port()
        if port > 0:
            return "tcp", (DEFAULT_TCP_HOST, port)

    return "unix", str(daemon_socket_path())


def sync_connect(timeout: float = 0.25) -> socket.socket:
    kind, addr = _parse_endpoint()
    fam = socket.AF_INET if kind == "tcp" else socket.AF_UNIX
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        if kind == "tcp":
            host, port = addr
            s.connect((host, port))
        else:
            s.connect(addr)
    except Exception:
        s.close()
        raise
    return s


async def open_connection() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    kind, addr = _parse_endpoint()
    if kind == "tcp":
        host, port = addr
        return await asyncio.open_connection(host, port)
    return await asyncio.open_unix_connection(addr)


def daemon_alive(timeout: float = 0.25) -> bool:
    try:
        s = sync_connect(timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def daemon_configured_in_env() -> bool:
    """True when this process was spawned to talk to the control-plane daemon."""
    return bool(
        (os.environ.get("ARTEMIS_DAEMON_ENDPOINT") or "").strip()
        or (os.environ.get("ARTEMIS_DAEMON_SOCK") or "").strip()
    )


async def start_daemon_server(
    on_connect: Any,
) -> tuple[asyncio.AbstractServer, str]:
    """Bind daemon listener; exit process if another instance is live."""
    cache_dir().mkdir(parents=True, exist_ok=True)

    if uses_tcp():
        if daemon_alive():
            print("daemon already running (tcp)", file=sys.stderr)
            sys.exit(0)
        clear_stale_endpoint_files()
        server = await asyncio.start_server(on_connect, DEFAULT_TCP_HOST, 0)
        sockets = server.sockets or []
        if not sockets:
            raise RuntimeError("daemon TCP server failed to bind")
        port = int(sockets[0].getsockname()[1])
        write_daemon_port(port)
        os.environ["ARTEMIS_DAEMON_PORT"] = str(port)
        os.environ["ARTEMIS_DAEMON_ENDPOINT"] = f"tcp:{DEFAULT_TCP_HOST}:{port}"
        return server, f"tcp://{DEFAULT_TCP_HOST}:{port}"

    sock_path = daemon_socket_path()
    if sock_path.exists() and daemon_alive():
        print(f"daemon already running on {sock_path}", file=sys.stderr)
        sys.exit(0)
    clear_stale_endpoint_files()
    server = await asyncio.start_unix_server(on_connect, path=str(sock_path))
    try:
        sock_path.chmod(0o600)
    except OSError:
        pass
    os.environ["ARTEMIS_DAEMON_SOCK"] = str(sock_path)
    os.environ["ARTEMIS_DAEMON_ENDPOINT"] = f"unix:{sock_path}"
    return server, str(sock_path)
