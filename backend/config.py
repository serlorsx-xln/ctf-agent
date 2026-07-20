"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys
    cursor_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox-core"
    # True when user passed --image (router must not override).
    sandbox_image_locked: bool = False
    detected_packs: list[str] = Field(default_factory=list)
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
