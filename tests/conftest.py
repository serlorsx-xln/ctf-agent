"""Shared pytest hooks for Artemis daemon tests."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _force_unix_daemon_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI/dev: unix socket on macOS/Linux; TCP on Windows."""
    monkeypatch.delenv("ARTEMIS_DAEMON_SOCK", raising=False)
    monkeypatch.delenv("ARTEMIS_DAEMON_ENDPOINT", raising=False)
    monkeypatch.delenv("ARTEMIS_DAEMON_PORT", raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "1")
    else:
        monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "0")
    # Most tests assume load/swarm work without a real Docker bake.
    monkeypatch.setenv("ARTEMIS_SKIP_SETUP_GATE", "1")
    # Never block or prompt during pytest helper launches.
    monkeypatch.setenv("ARTEMIS_SKIP_LAUNCH_SETUP", "1")
    monkeypatch.setenv("ARTEMIS_SKIP_SOLVER_HOLD", "1")
    # Leftover ~/.cache/artemis/daemon.token must not reject in-process peers.
    monkeypatch.delenv("ARTEMIS_DAEMON_TOKEN", raising=False)
