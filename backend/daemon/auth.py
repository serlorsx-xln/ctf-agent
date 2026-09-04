"""Daemon hello authentication (shared secret, env-enforced).

The listening process calls ``ensure_daemon_token()`` so ``ARTEMIS_DAEMON_TOKEN``
is set and ``~/.cache/artemis/daemon.token`` exists (mode ``0o600``).

Peers are checked against **the environment only**. A leftover token file must
not reject in-process ``Daemon()`` tests that never called ``ensure_daemon_token``.
Clients may read the file when their env is empty (TUI after bootstrap).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

TOKEN_ENV = "ARTEMIS_DAEMON_TOKEN"


def daemon_token_path() -> Path:
    from backend.cache import cache_dir

    return cache_dir() / "daemon.token"


def ensure_daemon_token() -> str:
    """Create or reuse the daemon token; export it on ``os.environ``."""
    path = daemon_token_path()
    token = ""
    if path.is_file():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    os.environ[TOKEN_ENV] = token
    return token


def required_daemon_token() -> str:
    """Token the daemon process will enforce (env only)."""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def client_daemon_token() -> str:
    """Token a client should send: env first, then the token file."""
    env = required_daemon_token()
    if env:
        return env
    path = daemon_token_path()
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def hello_authorized(hello: dict[str, Any] | None) -> bool:
    """True when the hello may proceed (no env token, or matching ``token``)."""
    required = required_daemon_token()
    if not required:
        return True
    got = str((hello or {}).get("token") or "")
    if len(got) != len(required):
        return False
    return secrets.compare_digest(got, required)


def with_hello_token(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach ``token`` when the client knows one (env or file)."""
    tok = client_daemon_token()
    if tok:
        payload = {**payload, "token": tok}
    return payload
