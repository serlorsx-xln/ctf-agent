"""Pydantic Settings — credentials from TUI /connect, then .env / environment."""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys (TUI /connect → auth.json preferred; .env still works for CI)
    cursor_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_model_id: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    gemini_project: str = ""
    gemini_location: str = ""
    gemini_base_url: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox-core"
    # True when user passed --image (router must not override).
    sandbox_image_locked: bool = False
    detected_packs: list[str] = Field(default_factory=list)
    # CLI ``--pack`` forces prefetch list (wins over detect / detected_packs).
    force_packs: list[str] = Field(default_factory=list)
    max_concurrent_challenges: int = 10
    container_memory_limit: str = "16g"
    # When True, skip interactive flag confirmation (tests / unattended).
    # Also honored via env CTF_AUTO_CONFIRM_FLAGS=1.
    auto_confirm_flags: bool = False
    # Eval harness (optional budgets + JSON summary path).
    eval_max_wall_s: float | None = None
    eval_max_usd: float | None = None
    eval_strict_packs: bool = False
    eval_out: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _merge_tui_credentials(self) -> Settings:
        from backend.shell.credentials import apply_tui_credentials

        apply_tui_credentials(overwrite=True)
        import os

        # Refresh fields after env injection (pydantic already bound once).
        if (k := os.environ.get("CURSOR_API_KEY", "").strip()):
            self.cursor_api_key = k
        if (k := os.environ.get("ANTHROPIC_API_KEY", "").strip()):
            self.anthropic_api_key = k
        if (k := os.environ.get("ANTHROPIC_BASE_URL", "").strip()):
            self.anthropic_base_url = k
        if (k := os.environ.get("ANTHROPIC_MODEL_ID", "").strip()):
            self.anthropic_model_id = k
        if (k := os.environ.get("OPENAI_API_KEY", "").strip()):
            self.openai_api_key = k
        if (k := os.environ.get("GEMINI_API_KEY", "").strip()):
            self.gemini_api_key = k
        if (k := os.environ.get("GEMINI_PROJECT", "").strip()):
            self.gemini_project = k
        if (k := os.environ.get("GEMINI_LOCATION", "").strip()):
            self.gemini_location = k
        if (k := os.environ.get("GEMINI_BASE_URL", "").strip()):
            self.gemini_base_url = k
        return self
