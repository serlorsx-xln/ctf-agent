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
            env_port = (os.environ.get("ARTEMIS_DAEMON_PORT") or "").strip()
            if env_port.isdigit():
                port = int(env_port)
        if port <= 0:
            env = {"ARTEMIS_DAEMON_TCP": "1"}
        else:
            endpoint = f"tcp:{DEFAULT_TCP_HOST}:{port}"
            env = {
                "ARTEMIS_DAEMON_ENDPOINT": endpoint,
                "ARTEMIS_DAEMON_PORT": str(port),
                "ARTEMIS_DAEMON_TCP": "1",
            }
    else:
        sock = str(daemon_socket_path())
        env = {
            "ARTEMIS_DAEMON_SOCK": sock,
            "ARTEMIS_DAEMON_ENDPOINT": f"unix:{sock}",
        }
    from backend.daemon.auth import client_daemon_token

    tok = client_daemon_token()
    if tok:
        env["ARTEMIS_DAEMON_TOKEN"] = tok
    return env


def _parse_endpoint() -> tuple[str, Any]:
    ep = (os.environ.get("ARTEMIS_DAEMON_ENDPOINT") or "").strip()
    if ep.startswith("unix:"):
        if uses_tcp():
            raise OSError("unix endpoint configured while ARTEMIS_DAEMON_TCP is active")
        return "unix", ep[5:]
    if ep.startswith("tcp:"):
        rest = ep[4:]
        host, _, port_s = rest.rpartition(":")
        if host and port_s.isdigit():
            return "tcp", (host, int(port_s))

    sock = (os.environ.get("ARTEMIS_DAEMON_SOCK") or "").strip()
    if sock:
        if uses_tcp():
            raise OSError("ARTEMIS_DAEMON_SOCK set while TCP daemon mode is active")
        return "unix", sock

    if uses_tcp():
        port = daemon_tcp_port()
        env_port = (os.environ.get("ARTEMIS_DAEMON_PORT") or "").strip()
        if port <= 0 and env_port.isdigit():
            port = int(env_port)
        if port > 0:
            return "tcp", (DEFAULT_TCP_HOST, port)
        raise OSError("daemon TCP port not configured")

    return "unix", str(daemon_socket_path())


def sync_connect(timeout: float = 0.25) -> socket.socket:
    kind, addr = _parse_endpoint()
    if kind == "tcp":
        fam = socket.AF_INET
    elif hasattr(socket, "AF_UNIX"):
        fam = socket.AF_UNIX
    else:
        raise OSError("unix socket not supported on this platform")
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
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("unix socket not supported on this platform")
    return await asyncio.open_unix_connection(addr)


def _tcp_endpoint_configured() -> bool:
    if not uses_tcp():
        return True
    if daemon_tcp_port() > 0:
        return True
    env_port = (os.environ.get("ARTEMIS_DAEMON_PORT") or "").strip()
    return env_port.isdigit() and int(env_port) > 0


def daemon_alive(timeout: float = 0.25) -> bool:
    """True when the Artemis daemon endpoint looks live.

    * Outside an asyncio loop (bootstrap / CLI): perform a full NDJSON ``hello``
      handshake so a random listener on the port/sock cannot spoof readiness.
    * Inside a running loop: connect-only. A full handshake would deadlock
      (the accept loop cannot run while this thread blocks on ``recv``).
    """
    if not _tcp_endpoint_configured():
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _daemon_alive_handshake(timeout)
    return _daemon_alive_connect(timeout)


async def daemon_alive_async(timeout: float = 0.25) -> bool:
    """Async-safe full protocol probe (runs handshake in a worker thread)."""
    if not _tcp_endpoint_configured():
        return False
    return await asyncio.to_thread(_daemon_alive_handshake, timeout)


def _daemon_alive_connect(timeout: float) -> bool:
    try:
        s = sync_connect(timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _daemon_alive_handshake(timeout: float) -> bool:
    import json

    from backend.daemon import protocol

    try:
        s = sync_connect(timeout=timeout)
    except OSError:
        return False
    try:
        s.settimeout(max(float(timeout), 0.15))
        from backend.daemon.auth import with_hello_token

        hello = with_hello_token(
            protocol.make_message(
                type="hello",
                id="alive",
                role=protocol.ROLE_USAGE,
            )
        )
        s.sendall(protocol.encode(hello))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                return False
            buf += chunk
            if len(buf) > 65536:
                return False
        line = buf.split(b"\n", 1)[0]
        msg = json.loads(line.decode("utf-8", errors="replace"))
        if msg.get("type") == "error":
            return False
        return msg.get("type") == "hello" and msg.get("ok", True) is not False
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


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
