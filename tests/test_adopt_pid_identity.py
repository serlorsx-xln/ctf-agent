"""Adopt tail helpers — PID identity + log truncate reopen."""

from __future__ import annotations

from backend.daemon import supervisor as sup


def test_adopt_pid_still_ours_requires_artemis_identity(monkeypatch):
    monkeypatch.setattr(sup, "_is_artemis_race_pid", lambda pid: pid == 111)
    monkeypatch.setattr(sup, "_read_running_pid", lambda session_id=None: 111)
    assert sup._adopt_pid_still_ours("s1", 111) is True
    assert sup._adopt_pid_still_ours("s1", 222) is False


def test_adopt_pid_still_ours_rejects_pidfile_mismatch(monkeypatch):
    monkeypatch.setattr(sup, "_is_artemis_race_pid", lambda _pid: True)
    monkeypatch.setattr(sup, "_read_running_pid", lambda session_id=None: 999)
    assert sup._adopt_pid_still_ours("s1", 111) is False


def test_adopt_pid_still_ours_allows_missing_pidfile_when_cmdline_matches(monkeypatch):
    monkeypatch.setattr(sup, "_is_artemis_race_pid", lambda pid: pid == 111)
    monkeypatch.setattr(sup, "_read_running_pid", lambda session_id=None: None)
    assert sup._adopt_pid_still_ours("s1", 111) is True
