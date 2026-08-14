"""TUI auth.json → env credential bridge."""

from __future__ import annotations

import json

from backend.shell import credentials


def test_read_tui_api_keys(tmp_path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "cursor": {"type": "api", "key": "cursor_test"},
                "anthropic": {"type": "api", "key": "sk-ant-test"},
                "openai": {"type": "oauth", "access": "x", "refresh": "y", "expires": 1},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials, "auth_json_path", lambda: auth)
    keys = credentials.read_tui_api_keys()
    assert keys == {
        "CURSOR_API_KEY": "cursor_test",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }


def test_read_tui_api_keys_anthropic_custom_base_url(tmp_path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "anthropic": {
                    "type": "api",
                    "key": "sk-custom-test-key",
                    "metadata": {
                        "baseURL": "https://gateway.example/anthropic/v1/models",
                        "model_id": "claude-sonnet-4-6",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials, "auth_json_path", lambda: auth)
    keys = credentials.read_tui_api_keys()
    assert keys["ANTHROPIC_API_KEY"] == "sk-custom-test-key"
    assert keys["ANTHROPIC_BASE_URL"] == "https://gateway.example/anthropic"
    assert keys["ANTHROPIC_MODEL_ID"] == "claude-sonnet-4-6"


def test_normalize_anthropic_base_url() -> None:
    n = credentials.normalize_anthropic_base_url
    assert n("https://host/anthropic") == "https://host/anthropic"
    assert n("https://host/anthropic/") == "https://host/anthropic"
    assert n("https://host/anthropic/v1") == "https://host/anthropic"
    assert n("https://host/v1/models") == "https://host"
    assert n("https://api.anthropic.com") == "https://api.anthropic.com"
    assert n("") == ""


def test_apply_tui_credentials_overwrites(tmp_path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"cursor": {"type": "api", "key": "from-tui"}}), encoding="utf-8")
    monkeypatch.setattr(credentials, "auth_json_path", lambda: auth)
    monkeypatch.setenv("CURSOR_API_KEY", "from-env")
    applied = credentials.apply_tui_credentials(overwrite=True)
    assert applied["CURSOR_API_KEY"] == "from-tui"
    import os

    assert os.environ["CURSOR_API_KEY"] == "from-tui"
