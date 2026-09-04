"""Fast TUI bootstrap: credentials + background daemon/stub (no long blocks)."""

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

_REPO: Path | None = None


def repo_root() -> Path:
    global _REPO
    if _REPO is None:
        _REPO = Path(__file__).resolve().parents[1]
    return _REPO


def ensure_import_path() -> None:
    root = str(repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def port_open(host: str, port: int, timeout: float = 0.02) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cache_dir() -> Path:
    env = (os.environ.get("ARTEMIS_CACHE") or "").strip()
    return Path(env) if env else Path.home() / ".cache" / "artemis"


def _uses_tcp() -> bool:
    forced = (os.environ.get("ARTEMIS_DAEMON_TCP") or "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    return sys.platform == "win32"


def _daemon_connectable(*, timeout: float = 0.05) -> bool:
    """Connect-only probe — no backend/asyncio import."""
    if _uses_tcp():
        port = 0
        env_port = (os.environ.get("ARTEMIS_DAEMON_PORT") or "").strip()
        if env_port.isdigit():
            port = int(env_port)
        if port <= 0:
            try:
                port = int((_cache_dir() / "daemon.port").read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return False
        if port <= 0:
            return False
        return port_open("127.0.0.1", port, timeout)
    if not hasattr(socket, "AF_UNIX"):
        return False
    sock = _cache_dir() / "daemon.sock"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(os.fspath(sock))
        finally:
            s.close()
        return True
    except OSError:
        return False


def services_already_up() -> bool:
    """True when daemon + cursor stub already accept connections."""
    return _daemon_connectable() and port_open("127.0.0.1", 18765)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_code_stale() -> bool:
    """True when the live daemon was started from an older backend tree."""
    ensure_import_path()
    from backend.cache import cache_dir
    from backend.daemon.code_stamp import backend_code_stamp, running_stamp

    have = running_stamp(cache_dir())
    if not have:
        return True
    return have != backend_code_stamp(repo_root())


def _live_swarm() -> bool:
    ensure_import_path()
    from backend.cache import cache_dir

    cache = cache_dir()
    candidates = [cache / "swarm.pid"]
    sessions = cache / "sessions"
    if sessions.is_dir():
        candidates.extend(sessions.glob("*/swarm.pid"))
    for path in candidates:
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            return True
    return False


def _stop_stale_daemon() -> None:
    ensure_import_path()
    from backend.cache import cache_dir
    from backend.daemon.code_stamp import pid_path
    from backend.daemon.transport import clear_stale_endpoint_files

    cache = cache_dir()
    try:
        pid = int(pid_path(cache).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        _wait_until(lambda: not _pid_alive(pid), timeout_s=2.0)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        clear_stale_endpoint_files()
    except OSError:
        pass
    try:
        pid_path(cache).unlink(missing_ok=True)
    except OSError:
        pass


def python_cmd() -> list[str]:
    ensure_import_path()
    from backend.subprocess_platform import resolve_venv_python

    py = resolve_venv_python(repo_root())
    if py is not None:
        return [str(py)]
    return [sys.executable]


def _service_log(name: str) -> Path:
    ensure_import_path()
    from backend.cache import cache_dir

    log_dir = cache_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{name}.log"


def _warn(msg: str) -> None:
    """Write bootstrap warnings to stderr + persistent log (launchers may redirect)."""
    line = f"artemis: {msg}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except OSError:
        pass
    try:
        path = _service_log("bootstrap")
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(line)
    except OSError:
        pass


def spawn_background(args: list[str], *, log_name: str) -> None:
    """Start a detached service with valid stdio and an on-disk error log."""
    ensure_import_path()
    from backend.subprocess_platform import background_popen_kwargs

    repo = repo_root()
    log_path = _service_log(log_name)
    # Keep the handle open for the child's lifetime (Popen inherits it).
    log_fh = open(log_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        log_fh.write(f"\n--- spawn {' '.join(args)} ---\n")
        log_fh.flush()
    except OSError:
        pass
    kwargs = {
        "cwd": str(repo),
        **background_popen_kwargs(log_file=log_fh),
    }
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as e:
        try:
            log_fh.write(f"spawn failed: {e}\n")
            log_fh.flush()
        except OSError:
            pass
        log_fh.close()
        raise


def _wait_until(predicate, *, timeout_s: float = 2.0, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return bool(predicate())


def _refresh_install_path() -> None:
    try:
        from backend.install_path import remember_install_path

        remember_install_path(repo_root())
    except Exception:
        pass


def ensure_background_services() -> None:
    ensure_import_path()
    from backend.daemon.transport import daemon_alive
    from backend.stdio_platform import ensure_standard_streams

    ensure_standard_streams()
    from backend.daemon.auth import ensure_daemon_token

    ensure_daemon_token()

    if daemon_alive(timeout=0.05) and port_open("127.0.0.1", 18765):
        if not daemon_code_stale():
            return
        if _live_swarm():
            _warn("backend changed but a swarm is still running — keep current daemon")
            return
        _warn("restarting daemon (backend code changed)")
        _stop_stale_daemon()

    # Cold start only — daemon also sweeps bridges on boot.
    _refresh_install_path()

    from backend.cache import cache_dir
    from backend.daemon.transport import clear_stale_endpoint_files
    from backend.file_lock import release, try_acquire

    py = python_cmd()
    lock_path = cache_dir() / "bootstrap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = try_acquire(lock_path)
    if lock_fd is None:
        if not _wait_until(
            lambda: _daemon_connectable(timeout=0.1) and port_open("127.0.0.1", 18765),
            timeout_s=3.0,
        ):
            _warn("another launch is starting services — see ~/.cache/artemis/logs/")
        return

    try:
        if not daemon_alive(timeout=0.05):
            try:
                clear_stale_endpoint_files()
            except OSError:
                pass
            spawn_background(py + ["-m", "backend.daemon.server"], log_name="daemon")
            if not _wait_until(lambda: _daemon_connectable(timeout=0.1), timeout_s=3.0):
                _warn(f"daemon not ready — see {_service_log('daemon')}")

        if not port_open("127.0.0.1", 18765):
            spawn_background(
                py + ["-m", "backend.shell.cursor_llm_stub"], log_name="cursor-stub"
            )
            if not _wait_until(lambda: port_open("127.0.0.1", 18765), timeout_s=2.5):
                _warn(f"cursor stub not ready — see {_service_log('cursor-stub')}")
    finally:
        try:
            release(lock_fd)
        except OSError:
            pass


def emit_credentials(mode: str) -> None:
    ensure_import_path()
    from backend.daemon.auth import client_daemon_token, ensure_daemon_token
    from backend.shell.credentials import read_tui_api_keys

    ensure_daemon_token()
    for key, value in read_tui_api_keys().items():
        if mode == "export":
            print(f"export {key}={shlex.quote(value)}")
        else:
            print(f"{key}={value}")
    tok = client_daemon_token()
    if tok:
        if mode == "export":
            print(f"export ARTEMIS_DAEMON_TOKEN={shlex.quote(tok)}")
        else:
            print(f"ARTEMIS_DAEMON_TOKEN={tok}")


def _emit_mode(argv: list[str]) -> str:
    if "--emit" in argv:
        i = argv.index("--emit")
        if i + 1 < len(argv) and argv[i + 1] in ("cmd", "export"):
            return argv[i + 1]
    for arg in argv:
        if arg.startswith("--emit=") and arg.split("=", 1)[1] in ("cmd", "export"):
            return arg.split("=", 1)[1]
    return "cmd"


def main() -> None:
    mode = _emit_mode(sys.argv[1:])
    # Hot path: current daemon + stub — skip spawn. A stale daemon (quit TUI,
    # code changed, daemon kept running) must be replaced so roster/writeup fixes load.
    if services_already_up() and not daemon_code_stale():
        emit_credentials(mode)
        return
    ensure_background_services()
    emit_credentials(mode)


if __name__ == "__main__":
    main()
