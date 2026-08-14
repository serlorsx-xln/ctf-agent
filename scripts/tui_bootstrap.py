"""Fast TUI bootstrap: credentials + background daemon/stub (no long blocks)."""

from __future__ import annotations

import os
import shlex
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

    if daemon_alive(timeout=0.05) and port_open("127.0.0.1", 18765):
        return

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
    from backend.shell.credentials import read_tui_api_keys

    for key, value in read_tui_api_keys().items():
        if mode == "export":
            print(f"export {key}={shlex.quote(value)}")
        else:
            print(f"{key}={value}")


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
    # Hot path: services already up — skip backend/asyncio imports and spawn.
    if services_already_up():
        emit_credentials(mode)
        return
    ensure_background_services()
    emit_credentials(mode)


if __name__ == "__main__":
    main()
