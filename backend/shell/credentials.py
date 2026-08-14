"""Load API keys stored by Artemis TUI (/connect) into the process environment.

TUI writes ``~/.local/share/artemis/auth.json``. Swarm + chassis launch read it so
operators never need to edit ``.env`` for Cursor / Claude / Codex keys.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# OpenCode/Artemis provider id → process env var used by backend solvers.
_PROVIDER_ENV: dict[str, str] = {
    "cursor": "CURSOR_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
}

# Extra metadata fields to extract from an auth entry's `metadata` into env.
# provider id → { metadata_key: env_var }.
_PROVIDER_METADATA_ENV: dict[str, dict[str, str]] = {
    "anthropic": {"baseURL": "ANTHROPIC_BASE_URL", "model_id": "ANTHROPIC_MODEL_ID"},
    "google": {
        "project": "GEMINI_PROJECT",
        "location": "GEMINI_LOCATION",
        "base_url": "GEMINI_BASE_URL",
    },
}


def normalize_anthropic_base_url(url: str) -> str:
    """Strip trailing slash / ``/v1`` / ``/v1/models`` from a pasted custom URL.

    Claude Code appends ``/v1/messages`` itself. Pasting the models probe
    (``…/v1/models``) or an extra ``/v1`` makes every request 404.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    low = u.lower()
    for suffix in ("/v1/models", "/v1"):
        if low.endswith(suffix):
            return u[: -len(suffix)].rstrip("/")
    return u


def auth_json_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "artemis" / "auth.json"


def read_tui_api_keys() -> dict[str, str]:
    """Return env-var → value for API-type entries in TUI auth.json.

    Includes the API key plus any metadata fields (base URL, model id, project,
    location) the user set via /connect, so the backend solvers see them.
    """
    path = auth_json_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read TUI auth %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for provider_id, env_name in _PROVIDER_ENV.items():
        entry = data.get(provider_id)
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "api":
            continue
        key = (entry.get("key") or "").strip()
        if key:
            out[env_name] = key
        # Extract metadata fields (base URL, model id, project, location, …).
        meta = entry.get("metadata")
        if isinstance(meta, dict):
            for mkey, menv in _PROVIDER_METADATA_ENV.get(provider_id, {}).items():
                mval = (str(meta.get(mkey) or "")).strip()
                if menv == "ANTHROPIC_BASE_URL":
                    mval = normalize_anthropic_base_url(mval)
                if mval:
                    out[menv] = mval
    return out


def apply_tui_credentials(*, overwrite: bool = True) -> dict[str, str]:
    """Merge TUI auth keys into ``os.environ``.

    When ``overwrite`` is True (default), TUI keys win over existing env / ``.env``.
    Returns the keys that were applied.
    """
    applied: dict[str, str] = {}
    for env_name, key in read_tui_api_keys().items():
        if not overwrite and (os.environ.get(env_name) or "").strip():
            continue
        os.environ[env_name] = key
        applied[env_name] = key
    return applied


def credentials_into(env: dict[str, str], *, overwrite: bool = True) -> dict[str, str]:
    """Copy of ``env`` with TUI credentials merged (for ``execve`` / subprocess)."""
    merged = dict(env)
    for env_name, key in read_tui_api_keys().items():
        if not overwrite and (merged.get(env_name) or "").strip():
            continue
        merged[env_name] = key
    return merged
