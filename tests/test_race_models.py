"""Race model spec normalization + credential checks."""

from __future__ import annotations

import pytest

from backend.models import (
    missing_race_credentials,
    normalize_race_spec,
    normalize_race_specs,
)


def test_normalize_tui_aliases() -> None:
    assert normalize_race_spec("anthropic/claude-sonnet-4-6") == "claude-sdk/claude-sonnet-4-6"
    assert normalize_race_spec("openai/gpt-5.4") == "codex/gpt-5.4"
    assert normalize_race_spec("cursor/composer-2") == "cursor/composer-2"
    assert normalize_race_spec("composer-2.5") == "cursor/composer-2.5"
    assert normalize_race_spec("anthropic/bigmodel/glm-5.2") == "claude-sdk/bigmodel/glm-5.2"


def test_normalize_multipliers() -> None:
    assert normalize_race_specs(["cursor/auto*2", "anthropic/claude-sonnet-4-6"]) == [
        "cursor/auto",
        "cursor/auto",
        "claude-sdk/claude-sonnet-4-6",
    ]


def test_normalize_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown swarm provider"):
        normalize_race_spec("groq/whisper")


def test_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "backend.shell.credentials.read_tui_api_keys",
        lambda: {},
    )
    missing = missing_race_credentials(["cursor/auto", "anthropic/claude-sonnet-4-6"])
    assert any("Cursor" in m for m in missing)
    assert any("Claude" in m for m in missing)
