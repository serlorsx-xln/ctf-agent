"""Model resolution — Bedrock, Azure OpenAI, Zen, Google AI Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import boto3
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from backend.config import Settings

# Default model specs — Cursor SDK is the primary backend (CURSOR_API_KEY).
# Override with --models for harder challenges or other backends.
DEFAULT_MODELS: list[str] = [
    "cursor/composer-2.5",
]

# Stronger options for hard crypto/rev (same Cursor key, or other backends).
# Example:
#   uv run ctf-solve --challenge ./challenges/X --models cursor/claude-4-sonnet -v
#   uv run ctf-solve --challenge ./challenges/X --models claude-sdk/claude-opus-4-6 -v
HARDER_MODELS: list[str] = [
    "cursor/claude-4-sonnet",
    "claude-sdk/claude-opus-4-6",
    "codex/gpt-5.4",
]

# Models that support vision
VISION_MODELS: set[str] = {
    "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3-flash-preview",
    "composer-2.5",
    "claude-4-sonnet",
    "auto",
}


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "bedrock":
            if settings.aws_bearer_token:
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(
                        api_key=settings.aws_bearer_token,
                        region_name=settings.aws_region,
                    ),
                )
            else:
                session = boto3.Session()
                client = session.client("bedrock-runtime", region_name=settings.aws_region)
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(bedrock_client=client),
                )
        case "azure":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_api_key,
                ),
            )
        case "zen":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url="https://opencode.ai/zen/v1",
                    api_key=settings.opencode_zen_api_key,
                ),
            )
        case "google":
            return GoogleModel(
                model_id,
                provider=GoogleProvider(api_key=settings.gemini_api_key),
            )
        case "cursor" | "claude-sdk" | "codex":
            raise ValueError(
                f"Provider '{provider}' uses its own solver backend, not Pydantic AI. "
                f"resolve_model() should not be called for {spec}."
            )
        case _:
            raise ValueError(f"Unknown provider: {provider}")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings with caching enabled."""
    provider = spec.split("/", 1)[0]
    match provider:
        case "bedrock":
            return BedrockModelSettings(
                max_tokens=128_000,
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )
        case "azure" | "zen":
            # Azure/Zen use OpenAI chat completions — server-side prompt caching
            # is automatic, no explicit config needed. Set max_tokens to avoid
            # reserving the full context window.
            return OpenAIChatModelSettings(
                max_tokens=128_000,
            )
        case "google":
            from google.genai.types import ThinkingLevel

            return GoogleModelSettings(
                max_tokens=64_000,
                google_thinking_config={
                    "thinking_level": ThinkingLevel.HIGH,
                    "include_thoughts": True,
                },
            )
        case _:
            return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def expand_model_cli_args(models: list[str] | tuple[str, ...]) -> list[str]:
    """Expand CLI model specs.

    Supports:
    - Comma-separated specs in one arg: ``claude-sdk/glm-5.2,cursor/grok-4.5``
    - Repeatable ``--models`` values (Click ``multiple=True``)
    - ``cursor/grok-4.5*3`` / ``cursor/grok-4.5x3`` multipliers
    """
    import re

    out: list[str] = []
    for raw in models:
        # One argv may hold several specs joined by commas (common shell habit).
        pieces = [p.strip() for p in (raw or "").split(",") if p.strip()]
        for m in pieces:
            # Prefer *N (shell-friendly when quoted). Also accept trailing xN.
            star = re.fullmatch(r"(.+)\*(\d+)$", m)
            if star:
                base, n_s = star.group(1), star.group(2)
                n = int(n_s)
                if n < 1:
                    raise ValueError(f"Invalid model repeat count in {m!r}")
                out.extend([base] * n)
                continue
            xmul = re.fullmatch(r"(.+?)x(\d+)$", m, flags=re.IGNORECASE)
            # Only treat as multiplier when base looks like a provider/model spec
            # (contains '/') so we don't mangle ids that legitimately end in x2.
            if xmul and "/" in xmul.group(1):
                base, n_s = xmul.group(1), xmul.group(2)
                n = int(n_s)
                if n < 1:
                    raise ValueError(f"Invalid model repeat count in {m!r}")
                out.extend([base] * n)
                continue
            out.append(m)
    return out


def assign_runner_ids(specs: list[str]) -> list[tuple[str, str]]:
    """Map possibly-duplicate model specs to unique runner ids.

    Returns ``(runner_id, model_spec)``. A single copy keeps the bare spec;
    duplicates become ``spec#1``, ``spec#2``, …
    """
    from collections import Counter

    totals = Counter(specs)
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for spec in specs:
        seen[spec] = seen.get(spec, 0) + 1
        if totals[spec] == 1:
            out.append((spec, spec))
        else:
            out.append((f"{spec}#{seen[spec]}", spec))
    return out


EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


def effort_from_spec(spec: str) -> EffortLevel | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) < 3:
        return None
    effort = parts[2]
    if effort == "low":
        return "low"
    if effort == "medium":
        return "medium"
    if effort == "high":
        return "high"
    if effort == "xhigh":
        return "xhigh"
    if effort == "max":
        return "max"
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS
