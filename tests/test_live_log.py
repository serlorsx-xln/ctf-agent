"""Stream join must not invent spaces (breaks flags / hex mid-token)."""

from pathlib import Path

from backend.agents.live_log import (
    _GARBLED_SUPPRESSED,
    _join_stream,
    emit_line,
    flush_stream,
    is_garbled_model_text,
    live,
)


def test_join_stream_concatenates_without_spaces():
    assert _join_stream("fl", "ag{") == "flag{"
    assert _join_stream("flag{", "50fba860}") == "flag{50fba860}"
    assert _join_stream("hello", " world") == "hello world"
    assert _join_stream("hello ", "world") == "hello world"


def test_quiet_live_suppresses_tool_dumps(capsys):
    from backend.agents.live_log import quiet_live

    flush_stream()
    with quiet_live():
        live("default tool#1 → bash", "cat huge.bin")
        live("default think", "should stay quiet too")
    flush_stream()
    out = capsys.readouterr().out
    assert "huge.bin" not in out
    assert "should stay quiet" not in out
    live("default ai", "after quiet")
    flush_stream()
    assert "after quiet" in capsys.readouterr().out


def test_quiet_live_drops_buffered_stream(capsys):
    """Timers/buffers scheduled before quiet must not flush after CORRECT."""
    import time

    from backend.agents.live_log import quiet_live

    flush_stream()
    live("default think", "pre-quiet buffer")
    with quiet_live():
        time.sleep(0.2)
        flush_stream()
    out = capsys.readouterr().out
    assert "pre-quiet buffer" not in out


def test_live_think_buffer_keeps_flag_intact(capsys):
    flush_stream()
    live("chal/default think", "fl")
    live("chal/default think", "ag{")
    live("chal/default think", "50fba860}")
    flush_stream()
    out = capsys.readouterr().out
    assert "flag{50fba860}" in out
    assert "fl ag" not in out
    assert "50 fba" not in out


def test_is_garbled_arabic_heavy():
    assert is_garbled_model_text("وف و٧ؤ ١٢٢ئ٣٠ىغ ي٧ض٢ثي هذه من في")
    assert not is_garbled_model_text("Looking at the APK entry points next")


def test_live_think_suppresses_garbled(capsys):
    flush_stream()
    live("chal/claude think", "وف و٧ؤ ١٢٢ئ٣٠ىغ ي٧ض٢ثي هذه من في ٣٨")
    flush_stream()
    out = capsys.readouterr().out
    assert _GARBLED_SUPPRESSED in out
    assert "وف" not in out


def test_live_collapses_newlines_to_one_tagged_line(capsys):
    flush_stream()
    live("chal/default ai", "line one\nline two\nline three")
    flush_stream()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("[chal/default ai]")
    assert "line one" in out[0] and "line two" in out[0]


def test_emit_line_tees_artemis_outcome_to_disk(tmp_path: Path, monkeypatch, capsys):
    """CORRECT recap must hit the swarm disk log (reconnect/replay), not only stdout."""
    log = tmp_path / "swarm.log"
    monkeypatch.setenv("ARTEMIS_SWARM_LOG", str(log))
    from backend.agents import live_log as live_log_mod

    live_log_mod.close_disk_tee()
    try:
        emit_line("[artemis] outcome CORRECT — accepted all 1 flags: CTF{x}. Challenge complete for this run.")
        emit_line("[artemis] summary Solved by default")
    finally:
        live_log_mod.close_disk_tee()
    text = log.read_text(encoding="utf-8")
    assert "[artemis] outcome CORRECT" in text
    assert "[artemis] summary Solved by default" in text
    out = capsys.readouterr().out
    assert "CORRECT" in out
