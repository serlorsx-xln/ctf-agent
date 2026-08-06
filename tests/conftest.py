"""Shared pytest hooks for Artemis daemon tests."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _force_unix_daemon_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI/dev on macOS/Linux: keep unix-socket daemon unless explicitly testing TCP."""
    monkeypatch.delenv("ARTEMIS_DAEMON_SOCK", raising=False)
    monkeypatch.delenv("ARTEMIS_DAEMON_ENDPOINT", raising=False)
    monkeypatch.delenv("ARTEMIS_DAEMON_PORT", raising=False)
    if sys.platform != "win32":
        monkeypatch.setenv("ARTEMIS_DAEMON_TCP", "0")
