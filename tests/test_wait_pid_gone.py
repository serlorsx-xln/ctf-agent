"""Stop waits for the swarm PID to exit before dropping the pidfile."""

from __future__ import annotations

import os

from backend.shell.bridge import _wait_pid_gone


def test_wait_pid_gone_true_for_dead_pid():
    # PID 1 may exist; use a clearly dead high pid that is not alive.
    assert _wait_pid_gone(2_000_000_001, timeout_s=0.2) is True


def test_wait_pid_gone_false_while_self_alive():
    assert _wait_pid_gone(os.getpid(), timeout_s=0.15) is False


def test_wait_pid_gone_permission_error_not_dead(monkeypatch):
    """Unsignalable live PIDs must not look gone (stop/spawn race)."""
    calls = {"n": 0}

    def fake_kill(pid, sig):
        calls["n"] += 1
        raise PermissionError("denied")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert _wait_pid_gone(12345, timeout_s=0.12) is False
    assert calls["n"] >= 1
