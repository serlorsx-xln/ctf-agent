"""Bridge helpers that do not need Docker."""

from __future__ import annotations

import json

from backend.shell import bridge


def test_challenge_dir_helper_prefers_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda _sid=None: {"challenge_dir": str(tmp_path / "from-session")},
    )
    assert bridge._challenge_dir({"challenge_dir": str(tmp_path / "from-payload")}) == str(
        tmp_path / "from-payload"
    )


def test_challenge_dir_helper_falls_back_to_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda _sid=None: {"challenge_dir": str(tmp_path / "from-session")},
    )
    assert bridge._challenge_dir({}) == str(tmp_path / "from-session")


def test_status_returns_json(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(
        "backend.shell.sandbox_session.load_session_state",
        lambda _sid=None: {"challenge_dir": "/chal", "flags_required": 1},
    )
    out = asyncio.run(bridge._status({}))
    data = json.loads(out)
    assert data["challenge_dir"] == "/chal"
    assert data["flags_required"] == 1


def test_clear_session_wipes_progress(monkeypatch, tmp_path) -> None:
    import asyncio

    from backend.shell import sandbox_session as ss

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    ss.save_session_state(
        challenge_dir="/chal",
        challenge_name="chal",
        flags_required=1,
        flags_explicit=True,
        accepted_flags=["flag{x}"],
    )
    assert ss.load_session_state().get("accepted_flags") == ["flag{x}"]
    out = asyncio.run(bridge._clear_session({}))
    data = json.loads(out)
    assert data == {} or data.get("accepted_flags") in (None, [])
    assert not data.get("challenge_dir")
    assert not data.get("flags_explicit")


def test_reset_accepted_flags(monkeypatch, tmp_path) -> None:
    from backend.shell import sandbox_session as ss

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    ss.save_session_state(
        challenge_dir="/chal",
        flags_required=2,
        flags_explicit=True,
        accepted_flags=["flag{a}"],
    )
    st = ss.reset_accepted_flags()
    assert st.get("accepted_flags") == []
    assert st.get("challenge_dir") == "/chal"
    assert st.get("flags_required") == 2
    assert st.get("flags_explicit") is True


def test_clear_flags_required_pops_keys(monkeypatch, tmp_path) -> None:
    """clear_flags_required must actually delete the keys (save_session_state
    can't write None). This is what lets the daemon ask the dialog."""
    from backend.shell import sandbox_session as ss

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    ss.save_session_state(
        challenge_dir="/chal",
        flags_required=3,
        flags_explicit=True,
    )
    assert ss.load_session_state().get("flags_required") == 3
    ss.clear_flags_required()
    st = ss.load_session_state()
    assert "flags_required" not in st
    assert "flags_explicit" not in st
    assert st.get("challenge_dir") == "/chal"  # other keys preserved


def test_load_challenge_isolates_sessions(monkeypatch, tmp_path) -> None:
    """Two session ids must not share challenge_dir on disk."""
    import asyncio

    from backend.shell import sandbox_session as ss

    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path / "cache"))
    a = tmp_path / "chal-a"
    b = tmp_path / "chal-b"
    a.mkdir()
    b.mkdir()
    (a / "challenge.txt").write_text("A\n", encoding="utf-8")
    (b / "challenge.txt").write_text("B\n", encoding="utf-8")

    async def _noop(*_a, **_k):
        return "ok"

    monkeypatch.setattr(ss, "stop_sandbox", _noop)

    asyncio.run(bridge._load_challenge({"session": "win-a", "path": str(a)}))
    asyncio.run(bridge._load_challenge({"session": "win-b", "path": str(b)}))

    assert ss.load_session_state("win-a").get("challenge_dir") == str(a.resolve())
    assert ss.load_session_state("win-b").get("challenge_dir") == str(b.resolve())
    assert ss.load_session_state("_default").get("challenge_dir") in (None, "")

