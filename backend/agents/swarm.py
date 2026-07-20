"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


# Quota fallback: map subscription-backed providers to API-backed equivalents
QUOTA_FALLBACK: dict[str, str] = {
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def _quota_fallback_spec(model_spec: str) -> str | None:
    return QUOTA_FALLBACK.get(model_spec)


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None  # joined flags when challenge complete
    confirmed_flags: list[str] = field(default_factory=list)
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(
        default_factory=dict
    )  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)

    def __post_init__(self) -> None:
        # Transparent L1: stay on L0; prefetch packs into the same container.
        from backend.tool_router import apply_router_to_settings

        image, packs = apply_router_to_settings(self.settings, self.challenge_dir)
        if packs:
            logger.info(
                "[%s] L0 image=%s; prefetch packs=%s",
                self.meta.name,
                image,
                ",".join(packs),
            )
        else:
            logger.info("[%s] L0 image=%s", self.meta.name, image)

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider.

        - cursor/* → CursorSolver (Cursor SDK + CURSOR_API_KEY)
        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag):
            return self.try_submit_flag(flag, model_spec)

        _notify = self._make_notify_fn(model_spec)

        if provider == "cursor":
            from backend.agents.cursor_solver import CursorSolver

            return CursorSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver

            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver

            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""

        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(f"[{self.meta.name}/{model_spec}] {message}")

        return _notify

    def _create_pydantic_solver(
        self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None
    ) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        from backend.flags import normalize_flags_required

        solver.deps.flags_required = normalize_flags_required(
            getattr(self.meta, "flags_required", 1)
        )
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, challenge_complete)."""
        from backend.flags import normalize_flags_required

        required = normalize_flags_required(getattr(self.meta, "flags_required", 1))
        async with self._flag_lock:
            if self.confirmed_flag:
                return (
                    f"ALREADY SOLVED — all flag(s) already confirmed: {self.confirmed_flag}",
                    True,
                )

            normalized = flag.strip()

            if normalized in self.confirmed_flags:
                n = len(self.confirmed_flags)
                return (
                    f"Already accepted this flag ({n}/{required}). "
                    "Continue and submit the remaining distinct flag(s).",
                    False,
                )

            # Dedup exact flags across all models (rejected or accepted)
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            from backend.tools.core import do_submit_flag

            display, is_complete = await do_submit_flag(
                self.meta.name,
                flag,
                already_accepted=list(self.confirmed_flags),
                required=required,
            )
            self._submitted_flags.add(normalized)

            if display.startswith(("ACCEPTED", "CORRECT")):
                self.confirmed_flags.append(normalized)
                logger.info(
                    "[%s] Flag progress %s/%s via %s",
                    self.meta.name,
                    len(self.confirmed_flags),
                    required,
                    model_spec,
                )
                if is_complete:
                    self.confirmed_flag = " | ".join(self.confirmed_flags)
                return display, is_complete

            # Rejected / not counted
            self._submit_count[model_spec] = wrong_count + 1
            self._last_submit_time[model_spec] = time.monotonic()
            return display, False

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            await solver.stop()

    async def _run_solver_loop(
        self, solver, model_spec: str
    ) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        result = SolverResult(
            flag=None,
            status=CANCELLED,
            findings_summary="",
            step_count=0,
            cost_usd=0.0,
            log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            result = await solver.run_until_done_or_gave_up()

            # Only broadcast useful findings — skip errors and broken solvers
            if (
                result.status not in (ERROR, QUOTA_ERROR)
                and not (result.step_count == 0 and result.cost_usd == 0)
                and result.findings_summary
                and not result.findings_summary.startswith(("Error:", "Turn failed:"))
            ):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND and self.confirmed_flag:
                # Soft race: cancel siblings only when all required flags are in.
                self.cancel_event.set()
                if result.flag != self.confirmed_flag:
                    result = SolverResult(
                        flag=self.confirmed_flag,
                        status=result.status,
                        findings_summary=result.findings_summary,
                        step_count=result.step_count,
                        cost_usd=result.cost_usd,
                        log_path=result.log_path,
                    )
                self.winner = result
                logger.info(f"[{self.meta.name}] Flag(s) found by {model_spec}: {result.flag}")
                return result, solver

            if result.status == FLAG_FOUND and not self.confirmed_flag:
                from backend.flags import normalize_flags_required

                logger.warning(
                    "[%s] %s reported FLAG_FOUND but challenge incomplete "
                    "(%s/%s) — soft race continues",
                    self.meta.name,
                    model_spec,
                    len(self.confirmed_flags),
                    normalize_flags_required(getattr(self.meta, "flags_required", 1)),
                )
                # Clear local confirm so the next turn does not re-emit FLAG_FOUND.
                if hasattr(solver, "_confirmed"):
                    solver._confirmed = False
                # Treat as a normal bump cycle so we do not spin on FLAG_FOUND.
                result = SolverResult(
                    flag=result.flag,
                    status=GAVE_UP,
                    findings_summary=result.findings_summary,
                    step_count=result.step_count,
                    cost_usd=result.cost_usd,
                    log_path=result.log_path,
                )

            if result.status == CANCELLED:
                break

            # Quota exhaustion: fall back to API-backed Pydantic AI solver
            if result.status == QUOTA_ERROR:
                fallback_spec = _quota_fallback_spec(model_spec)
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    solver.sandbox = None  # type: ignore[assignment]
                    await solver.stop()
                    solver = self._create_pydantic_solver(
                        fallback_spec, sandbox=existing_sandbox, owns_sandbox=True
                    )
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming")
                continue

        return result, solver

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        ]

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    # Soft race: only kill siblings on full completion.
                    if result and result.status == FLAG_FOUND and self.confirmed_flag:
                        self.cancel_event.set()
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self.cancel_event.set()
            return self._end_summary()
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None

    def _end_summary(self) -> SolverResult | None:
        """Return winner, or a partial summary when flags were found but race unfinished."""
        if self.winner:
            return self.winner
        if self.confirmed_flag:
            return SolverResult(
                flag=self.confirmed_flag,
                status=FLAG_FOUND,
                findings_summary=self._summary_findings(),
                step_count=0,
                cost_usd=0.0,
                log_path="",
            )
        if self.confirmed_flags:
            from backend.flags import normalize_flags_required

            req = normalize_flags_required(getattr(self.meta, "flags_required", 1))
            joined = " | ".join(self.confirmed_flags)
            return SolverResult(
                flag=joined,
                status=GAVE_UP,
                findings_summary=(
                    f"Partial flags accepted ({len(self.confirmed_flags)}/{req}): {joined}\n"
                    + self._summary_findings()
                )[:2000],
                step_count=0,
                cost_usd=0.0,
                log_path="",
            )
        # Best-effort: surface any solver findings even without accepts
        summary = self._summary_findings()
        if summary:
            return SolverResult(
                flag=None,
                status=GAVE_UP,
                findings_summary=summary[:2000],
                step_count=0,
                cost_usd=0.0,
                log_path="",
            )
        return None

    def _summary_findings(self) -> str:
        parts = [f"[{m}]: {f}" for m, f in self.findings.items() if f]
        return "\n\n".join(parts)

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
        self.cancel_event.set()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        from backend.flags import normalize_flags_required

        required = normalize_flags_required(getattr(self.meta, "flags_required", 1))
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "flags_required": required,
            "flags_accepted": list(self.confirmed_flags),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": "running"
                    if spec in self.solvers and not self.cancel_event.is_set()
                    else ("won" if self.winner and self.winner.flag else "finished"),
                }
                for spec in self.model_specs
            },
        }
