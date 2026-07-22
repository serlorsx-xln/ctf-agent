"""Per-agent token usage tracking.

USD is recorded only when a provider/SDK reports it (e.g. Claude Agent SDK
``total_cost_usd``). There is no local price table and no estimated billing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.usage import RunUsage

logger = logging.getLogger(__name__)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _cache_rate(usage: RunUsage) -> str:
    if usage.input_tokens == 0:
        return "n/a"
    rate = (usage.cache_read_tokens / usage.input_tokens) * 100
    return f"{rate:.0f}%"


@dataclass
class AgentUsage:
    usage: RunUsage = field(default_factory=RunUsage)
    model_name: str = ""
    provider_spec: str = ""
    duration_seconds: float = 0.0
    # Sum of provider-reported USD only. None until a provider reports a value.
    reported_cost_usd: float | None = None

    @property
    def cost_usd(self) -> float:
        """Provider-reported USD, or 0.0 when none was reported."""
        return self.reported_cost_usd if self.reported_cost_usd is not None else 0.0

    @property
    def has_reported_cost(self) -> bool:
        return self.reported_cost_usd is not None


@dataclass
class CostTracker:
    by_agent: dict[str, AgentUsage] = field(default_factory=dict)

    def record_tokens(
        self,
        agent_name: str,
        model_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        provider_spec: str = "",
        duration_seconds: float = 0.0,
        reported_cost_usd: float | None = None,
    ) -> None:
        """Record token usage; optionally attach provider-reported USD."""
        usage = RunUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        self.record(
            agent_name,
            usage,
            model_name,
            provider_spec=provider_spec,
            duration_seconds=duration_seconds,
            reported_cost_usd=reported_cost_usd,
        )

    def record(
        self,
        agent_name: str,
        usage: RunUsage,
        model_name: str,
        provider_spec: str = "",
        duration_seconds: float = 0.0,
        reported_cost_usd: float | None = None,
    ) -> None:
        if agent_name not in self.by_agent:
            self.by_agent[agent_name] = AgentUsage(
                model_name=model_name, provider_spec=provider_spec
            )

        agent = self.by_agent[agent_name]
        agent.usage += usage
        agent.duration_seconds += duration_seconds
        if reported_cost_usd is not None:
            prev = agent.reported_cost_usd or 0.0
            agent.reported_cost_usd = prev + float(reported_cost_usd)

        cost_note = f" | ${reported_cost_usd:.4f} reported" if reported_cost_usd is not None else ""
        logger.debug(
            f"{agent_name}: {_fmt_tokens(usage.input_tokens)} in / "
            f"{_fmt_tokens(usage.cache_read_tokens)} cached ({_cache_rate(usage)} hit) / "
            f"{_fmt_tokens(usage.output_tokens)} out | {duration_seconds:.1f}s{cost_note}"
        )

    @property
    def total_reported_cost_usd(self) -> float | None:
        """Sum of provider-reported USD, or None if no provider reported cost."""
        total = 0.0
        any_reported = False
        for agent in self.by_agent.values():
            if agent.reported_cost_usd is not None:
                total += agent.reported_cost_usd
                any_reported = True
        return total if any_reported else None

    @property
    def total_cost_usd(self) -> float:
        """Backward-compatible: reported USD sum, else 0.0 (unknown ≠ free)."""
        reported = self.total_reported_cost_usd
        return reported if reported is not None else 0.0

    @property
    def total_tokens(self) -> int:
        total = RunUsage()
        for a in self.by_agent.values():
            total += a.usage
        return total.total_tokens

    @property
    def total_duration_seconds(self) -> float:
        return sum(a.duration_seconds for a in self.by_agent.values())

    def format_usage(self, agent_name: str) -> str:
        """Format: '45k in / 32k cached (71% hit) / 2.1k out | 28.3s' (+ reported $)."""
        agent = self.by_agent.get(agent_name)
        if not agent:
            return ""
        u = agent.usage
        parts = [
            f"{_fmt_tokens(u.input_tokens)} in / "
            f"{_fmt_tokens(u.cache_read_tokens)} cached ({_cache_rate(u)} hit) / "
            f"{_fmt_tokens(u.output_tokens)} out",
            f"{agent.duration_seconds:.1f}s",
        ]
        if agent.has_reported_cost:
            parts.append(f"${agent.cost_usd:.2f} reported")
        return " | ".join(parts)

    def format_total(self) -> str:
        """One-line total for CLI / coordinator status."""
        parts = [
            f"{_fmt_tokens(self.total_tokens)} tokens",
            f"{self.total_duration_seconds:.1f}s",
        ]
        reported = self.total_reported_cost_usd
        if reported is not None:
            parts.append(f"${reported:.2f} reported")
        return " | ".join(parts)

    def get_usage_by_model(self) -> dict[str, dict[str, Any]]:
        by_model: dict[str, dict[str, Any]] = {}
        for agent in self.by_agent.values():
            model = agent.model_name
            if model not in by_model:
                by_model[model] = {
                    "input": 0,
                    "cached": 0,
                    "output": 0,
                    "duration": 0.0,
                    "reported_cost": None,
                }
            row = by_model[model]
            row["input"] += agent.usage.input_tokens
            row["cached"] += agent.usage.cache_read_tokens
            row["output"] += agent.usage.output_tokens
            row["duration"] += agent.duration_seconds
            if agent.reported_cost_usd is not None:
                prev = row["reported_cost"] or 0.0
                row["reported_cost"] = prev + agent.reported_cost_usd
        return by_model

    def log_summary(self) -> None:
        """Log summary grouped by model with cache hit rates."""
        by_model = self.get_usage_by_model()
        for model, s in by_model.items():
            hit_rate = f"{(s['cached'] / s['input'] * 100):.0f}%" if s["input"] > 0 else "n/a"
            cost_note = (
                f" | ${s['reported_cost']:.2f} reported" if s["reported_cost"] is not None else ""
            )
            logger.info(
                "  %s: %s in / %s cached (%s hit) / %s out | %.1fs%s",
                model,
                _fmt_tokens(s["input"]),
                _fmt_tokens(s["cached"]),
                hit_rate,
                _fmt_tokens(s["output"]),
                s["duration"],
                cost_note,
            )
        total = sum(s["input"] for s in by_model.values())
        total_cached = sum(s["cached"] for s in by_model.values())
        overall_hit = f"{(total_cached / total * 100):.0f}%" if total > 0 else "n/a"
        logger.info(
            "  Total: %s | %s overall cache hit rate",
            self.format_total(),
            overall_hit,
        )
