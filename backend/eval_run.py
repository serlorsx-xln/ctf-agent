"""Thin eval harness — wall/cost budgets and JSON summary artifacts."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.solver_base import ERROR, GAVE_UP, INFRA_ERROR, QUOTA_ERROR

logger = logging.getLogger(__name__)

EVAL_BUDGET = "eval_budget"


def _positive_limit(raw: Any) -> float | None:
    """Parse a >0 numeric limit; ignore bools / MagicMock / junk."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass
class EvalRunState:
    """Per-challenge eval clocks and counters (shared across swarm solvers)."""

    started_monotonic: float = field(default_factory=time.monotonic)
    infra_recoveries: int = 0
    status_override: str | None = None

    def wall_s(self) -> float:
        return time.monotonic() - self.started_monotonic

    def budget_exceeded(self, settings: Any, cost_usd: float | None) -> str | None:
        """Return ``eval_budget`` reason if wall or USD limit hit; else None.

        Unknown USD (``None``) is common (Cursor/Gemini/Codex). Treat it as
        fail-open **only when ``--eval-max-wall-s`` is also set** — the wall is
        then the unattended hard stop. USD-only + unknown cost is fail-closed
        so ``--eval-max-usd`` is not a silent no-op (audit M2).
        """
        max_wall = _positive_limit(getattr(settings, "eval_max_wall_s", None))
        if max_wall is not None and self.wall_s() >= max_wall:
            return EVAL_BUDGET
        max_usd = _positive_limit(getattr(settings, "eval_max_usd", None))
        if max_usd is None:
            return None
        if cost_usd is None:
            return None if max_wall is not None else EVAL_BUDGET
        try:
            if float(cost_usd) >= max_usd:
                return EVAL_BUDGET
        except (TypeError, ValueError):
            return None
        return None


def agent_failed_from_status(status: str, *, infra_only: bool = False) -> bool:
    """True when the *agent* failed — infra-only deaths are not agent failures."""
    if status in (INFRA_ERROR, EVAL_BUDGET):
        return False
    return status in (ERROR, GAVE_UP, QUOTA_ERROR)


def write_eval_summary(
    path: str,
    *,
    challenge: str,
    model: str,
    status: str,
    steps: int,
    infra_recoveries: int,
    preflight_ms: float,
    cost_usd: float | None,
    flag: str | None,
) -> None:
    """Write JSON eval artifact; ``agent_failed`` excludes infra-only deaths."""
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "challenge": challenge,
        "model": model,
        "status": status,
        "steps": steps,
        "infra_recoveries": infra_recoveries,
        "preflight_ms": round(preflight_ms, 1),
        "cost_usd": cost_usd,
        "flag": flag,
        "agent_failed": agent_failed_from_status(status),
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote eval summary → %s", out)
