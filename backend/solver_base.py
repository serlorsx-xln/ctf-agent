"""Solver result type, status constants, and solver protocol — shared across all backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# Status constants
FLAG_FOUND = "flag_found"
GAVE_UP = "gave_up"
CANCELLED = "cancelled"
ERROR = "error"
QUOTA_ERROR = "quota_error"
# Transport / Cursor bridge failure — recoverable by recreating the agent session.
INFRA_ERROR = "infra_error"


@dataclass
class SolverResult:
    flag: str | None
    status: str
    findings_summary: str
    step_count: int
    # Provider-reported USD only (e.g. Claude SDK). 0.0 means unknown / not reported.
    cost_usd: float
    log_path: str


class SolverProtocol(Protocol):
    """Common interface for all solver backends (Cursor, Claude SDK, Codex, Pydantic AI)."""

    model_spec: str
    agent_name: str
    # Mutable: quota fallback may detach/clear the sandbox before stop().
    sandbox: Any

    async def start(self) -> None: ...
    async def run_until_done_or_gave_up(self) -> SolverResult: ...
    def bump(self, insights: str) -> None: ...
    async def stop(self) -> None: ...

    # Optional: Cursor (and future backends) recreate a poisoned session after
    # INFRA_ERROR. Swarm gates with ``hasattr(solver, "recover_session")``.
    # async def recover_session(self, insights: str) -> None: ...
