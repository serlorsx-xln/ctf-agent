"""Model spec helpers — Cursor / Claude SDK / Codex / Gemini."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from backend.config import Settings

# Default model specs — Cursor SDK is the primary backend (CURSOR_API_KEY).
# Override with --models for harder challenges or other backends.
DEFAULT_MODELS: list[str] = [
    "cursor/composer-2.5",
]

# Product swarm providers (TUI /connect → cursor, anthropic, openai, google).
SUPPORTED_PROVIDERS = frozenset({"cursor", "claude-sdk", "codex", "gemini-sdk"})


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def agent_display_key(runner_id: str, spec: str) -> str:
    """Label the TUI puts on this runner's agent box (live-log ``shortAgent``).

    Duplicate runners keep their ``#N`` suffix; unique ones show the model id.
    """
    label = runner_id.split("/", 1)[-1]
    return label if "#" in label else model_id_from_spec(spec)


# TUI /connect provider ids → swarm solver prefixes
_TUI_PROVIDER_ALIASES: dict[str, str] = {
    "anthropic": "claude-sdk",
    "claude": "claude-sdk",
    "openai": "codex",
    "codex": "codex",
    "cursor": "cursor",
    "google": "gemini-sdk",
    "gemini": "gemini-sdk",
}


def normalize_swarm_spec(spec: str) -> str:
    """Normalize a model spec for swarm (TUI ids → solver prefixes).

    Accepts flexible forms:
    - ``cursor/composer-2`` / ``cursor/auto``
    - ``anthropic/claude-sonnet-4-6`` → ``claude-sdk/claude-sonnet-4-6``
    - ``openai/gpt-5.4`` → ``codex/gpt-5.4``
    - ``claude-sdk/...`` / ``codex/...`` unchanged
    - Bare model id with no provider → ``cursor/<id>`` (Cursor is primary)
    """
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Empty model spec")
    if "/" not in raw:
        return f"cursor/{raw}"
    provider, rest = raw.split("/", 1)
    provider = _TUI_PROVIDER_ALIASES.get(provider, provider)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown swarm provider {provider!r} in {spec!r}. "
            f"Use cursor/, claude-sdk/ (or anthropic/), codex/ (or openai/), gemini-sdk/ (or google/)…"
        )
    return f"{provider}/{rest}"


def normalize_swarm_specs(specs: list[str] | tuple[str, ...] | None) -> list[str]:
    """Expand CLI args then normalize each spec for swarm."""
    expanded = expand_model_cli_args(list(specs or []))
    return [normalize_swarm_spec(s) for s in expanded]


def missing_swarm_credentials(specs: list[str], settings: Settings | None = None) -> list[str]:
    """Return human-readable missing-key messages for the given swarm specs."""
    import os

    from backend.shell.credentials import apply_tui_credentials

    apply_tui_credentials(overwrite=True)
    needed: set[str] = set()
    for spec in specs:
        needed.add(provider_from_spec(normalize_swarm_spec(spec)))

    def _has(env_name: str, field: str) -> bool:
        if (os.environ.get(env_name) or "").strip():
            return True
        return settings is not None and bool((getattr(settings, field, "") or "").strip())

    missing: list[str] = []
    if "cursor" in needed and not _has("CURSOR_API_KEY", "cursor_api_key"):
        missing.append("Cursor: /connect → Cursor API key (CURSOR_API_KEY)")
    if "claude-sdk" in needed and not _has("ANTHROPIC_API_KEY", "anthropic_api_key"):
        missing.append("Claude: /connect → Anthropic API key (ANTHROPIC_API_KEY)")
    if "codex" in needed and not _has("OPENAI_API_KEY", "openai_api_key"):
        missing.append("Codex: /connect → OpenAI / ChatGPT (OPENAI_API_KEY)")
    if "gemini-sdk" in needed:
        # API key optional — ADC fallback. Only flag if neither key nor ADC.
        has_key = _has("GEMINI_API_KEY", "gemini_api_key")
        has_adc = (Path.home() / ".config/gcloud/application_default_credentials.json").exists()
        if not has_key and not has_adc:
            missing.append(
                "Gemini: /connect → Google API key (GEMINI_API_KEY), "
                "or run `gcloud auth application-default login` for ADC"
            )
    return missing


# Legacy aliases — prefer normalize_swarm_* / missing_swarm_credentials.
normalize_race_spec = normalize_swarm_spec
normalize_race_specs = normalize_swarm_specs
missing_race_credentials = missing_swarm_credentials


def missing_models_error() -> str:
    """Shared error string for 'no models passed' (bridge + daemon parity)."""
    return (
        "ERROR: pass models e.g. cursor/composer-2, anthropic/claude-sonnet-4-6, "
        "openai/gpt-5.4, google/gemini-2.5-flash (TUI provider ids are accepted)"
    )


def missing_credentials_error(missing: list[str]) -> str:
    """Shared error string for missing credentials (bridge + daemon parity)."""
    return "ERROR: missing credentials for swarm:\n- " + "\n- ".join(missing)


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
    """Always attempt multimodal view_image; fall back to bash tools if needed."""
    _ = spec
    return True
