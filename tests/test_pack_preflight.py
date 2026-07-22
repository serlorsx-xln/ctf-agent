"""Pack preflight resolution — force_packs wins over detect."""

from __future__ import annotations

from types import SimpleNamespace

from backend.pack_preflight import resolve_prefetch_packs


def test_force_packs_wins_over_detected(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(
        force_packs=["pwn", "crypto"],
        detected_packs=["web"],
    )
    called = {"n": 0}

    def fake_detect(_dir: str) -> list[str]:
        called["n"] += 1
        return ["ml"]

    monkeypatch.setattr("backend.tool_router.detect_packs", fake_detect)
    assert resolve_prefetch_packs(settings, str(tmp_path)) == ["pwn", "crypto"]
    assert called["n"] == 0


def test_uses_settings_detected_packs(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(force_packs=[], detected_packs=["forensics", "linux"])

    def boom(_dir: str) -> list[str]:
        raise AssertionError("detect_packs should not run when detected_packs set")

    monkeypatch.setattr("backend.tool_router.detect_packs", boom)
    assert resolve_prefetch_packs(settings, str(tmp_path)) == ["forensics", "linux"]


def test_falls_back_to_detect(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(force_packs=[], detected_packs=[])
    monkeypatch.setattr(
        "backend.tool_router.detect_packs",
        lambda _d: ["steg"],
    )
    assert resolve_prefetch_packs(settings, str(tmp_path)) == ["steg"]


def test_force_packs_dedupes() -> None:
    settings = SimpleNamespace(force_packs=["pwn", "pwn", "", "web"], detected_packs=[])
    assert resolve_prefetch_packs(settings, ".") == ["pwn", "web"]
