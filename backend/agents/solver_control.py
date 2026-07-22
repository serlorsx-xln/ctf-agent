"""Shared solver control helpers — turn errors, bumps, sandbox start basics."""

from __future__ import annotations

from typing import Any

from backend.agents.cursor_runtime import is_infra_error_message
from backend.prompts import ChallengeMeta, list_distfiles
from backend.solver_base import ERROR, INFRA_ERROR, QUOTA_ERROR


def classify_turn_error(message: str | None) -> str:
    """Map an error string to QUOTA_ERROR | INFRA_ERROR | ERROR."""
    if not message:
        return ERROR
    err = message.lower()
    quota_needles = (
        "quota",
        "rate",
        "capacity",
        "usage",
        "billing",
        "overloaded",
        "401",
        "403",
    )
    if any(k in err for k in quota_needles):
        return QUOTA_ERROR
    if is_infra_error_message(message):
        return INFRA_ERROR
    return ERROR


def stash_bump(solver: Any, insights: str) -> None:
    """Stash bump insights and reset loop detector (Cursor/Claude/Codex)."""
    solver._bump_insights = insights
    loop = getattr(solver, "loop_detector", None)
    if loop is not None and hasattr(loop, "reset"):
        loop.reset()
    tracer = getattr(solver, "tracer", None)
    if tracer is not None and hasattr(tracer, "event"):
        tracer.event("bump", insights=(insights or "")[:500])


async def start_sandbox_basics(
    sandbox: Any,
    meta: ChallengeMeta,
    challenge_dir: str,
) -> tuple[str, list[str]]:
    """Start sandbox (if needed), probe arch, list distfiles — shared by all solvers.

    Returns ``(container_arch, distfile_names)``.
    """
    # Quota fallback reuses a live container — do not recreate.
    if not getattr(sandbox, "_container", None):
        await sandbox.start()
    arch_result = await sandbox.exec("uname -m", timeout_s=10)
    container_arch = arch_result.stdout.strip() or "unknown"
    distfile_names = list_distfiles(challenge_dir)
    return container_arch, distfile_names
