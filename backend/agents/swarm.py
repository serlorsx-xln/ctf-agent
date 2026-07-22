"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, assign_runner_ids, provider_from_spec
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    INFRA_ERROR,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)

_RecoverSession = Callable[[str | None], Awaitable[None]]


def _tracker_cost_usd(tracker: Any) -> float:
    """Read CostTracker.total_cost_usd; tolerate MagicMock callables in tests."""
    raw: Any = getattr(tracker, "total_cost_usd", 0.0)
    if callable(raw):
        raw = raw()
    try:
        return float(raw)
    except TypeError, ValueError:
        return 0.0


# After this many bridge recoveries in one challenge, stop (avoid infinite spin).
MAX_INFRA_RECOVERIES = 20
# Short cooldown before recreating a poisoned Cursor agent.
INFRA_RECOVERY_COOLDOWN_S = 5

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
    # Eval harness counters (infra recoveries + wall clock).
    _eval: Any = field(default=None, repr=False)
    _infra_recoveries_total: int = 0
    _last_model_spec: str = ""
    _last_preflight_ms: float = 0.0
    _last_steps: int = 0
    _last_status: str = ""
    _last_flag: str | None = None

    def __post_init__(self) -> None:
        # Transparent L1: stay on L0; prefetch packs into the same container.
        from backend.eval_run import EvalRunState
        from backend.tool_router import apply_router_to_settings

        self._eval = EvalRunState()
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

    def _tag_solver(self, solver, runner_id: str, model_spec: str):
        """Give duplicate-model runners distinct names / logs / bus identity."""
        solver.runner_id = runner_id
        if runner_id == model_spec:
            return
        # e.g. cursor/grok-4.5#2 → grok-4.5#2
        label = runner_id.split("/", 1)[-1]
        mid = getattr(solver, "model_id", label)
        if "#" in label:
            mid = label
        solver.agent_name = f"{self.meta.name}/{mid}"
        tracer = getattr(solver, "tracer", None)
        if tracer is not None:
            from backend.tracing import SolverTracer

            old_path = getattr(tracer, "path", None)
            try:
                tracer.close()
            except Exception:
                pass
            solver.tracer = SolverTracer(self.meta.name, mid)
            # Drop the unused pre-tag stub file (parallel #N left empty traces).
            if old_path:
                try:
                    from pathlib import Path

                    p = Path(old_path)
                    if p.is_file() and p.stat().st_size == 0:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        # Pydantic solver bus identity
        deps = getattr(solver, "deps", None)
        if deps is not None and hasattr(deps, "model_spec"):
            deps.model_spec = runner_id

    def _create_solver(self, model_spec: str, runner_id: str | None = None):
        """Create the right solver type based on provider.

        - cursor/* → CursorSolver (Cursor SDK + CURSOR_API_KEY)
        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        rid = runner_id or model_spec
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag):
            return self.try_submit_flag(flag, rid)

        _notify = self._make_notify_fn(rid)

        if provider == "cursor":
            from backend.agents.cursor_solver import CursorSolver

            solver = CursorSolver(
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
            self._tag_solver(solver, rid, model_spec)
            return solver

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver

            solver = ClaudeSolver(
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
            self._tag_solver(solver, rid, model_spec)
            return solver

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver

            solver = CodexSolver(
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
            self._tag_solver(solver, rid, model_spec)
            return solver

        solver = self._create_pydantic_solver(model_spec, runner_id=rid)
        self._tag_solver(solver, rid, model_spec)
        return solver

    def _make_notify_fn(self, runner_id: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""

        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(f"[{self.meta.name}/{runner_id}] {message}")

        return _notify

    def _create_pydantic_solver(
        self,
        model_spec: str,
        sandbox=None,
        owns_sandbox: bool | None = None,
        runner_id: str | None = None,
    ) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        rid = runner_id or model_spec
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
        solver.deps.model_spec = rid
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, rid)
        solver.deps.notify_coordinator = self._make_notify_fn(rid)
        from backend.flags import normalize_flags_required

        solver.deps.flags_required = normalize_flags_required(
            getattr(self.meta, "flags_required", 1)
        )
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()

        def _add(label: str, text: str) -> None:
            t = (text or "").strip()
            if not t or t.startswith(("Error:", "Turn failed:", "Infra:")):
                return
            key = f"{label}:{t[:200]}"
            if key in seen:
                return
            seen.add(key)
            parts.append(f"[{label}]: {t}")

        for model, finding in self.findings.items():
            if model != exclude_model:
                _add(model, finding)

        # Live mid-turn notes from siblings still running
        for rid, solver in self.solvers.items():
            if rid == exclude_model:
                continue
            live = getattr(solver, "_findings", None) or ""
            _add(rid, str(live))

        if self.confirmed_flags:
            parts.append("Accepted flag(s) so far: " + " | ".join(self.confirmed_flags))
        if self._submitted_flags:
            # Help siblings avoid junk rewrap loops
            sample = sorted(self._submitted_flags)
            if len(sample) > 24:
                sample = sample[:24] + ["…"]
            parts.append(
                "Already-tried candidates (do not resubmit or wrap these): " + ", ".join(sample)
            )

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

            from backend.flags import is_rewrap_of_tried

            prior_try = is_rewrap_of_tried(normalized, list(self._submitted_flags))
            if prior_try is not None:
                return (
                    f'REJECTED — "{normalized}" is a wrap/unwrap of already-tried '
                    f'"{prior_try}". Do not wrap intermediate tokens, digests, EXIF, '
                    "or firmware codenames. Recover the real awarded flag.",
                    False,
                )

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

            auto = bool(getattr(self.settings, "auto_confirm_flags", False))
            display, is_complete = await do_submit_flag(
                self.meta.name,
                flag,
                already_accepted=list(self.confirmed_flags),
                required=required,
                challenge_dir=self.challenge_dir,
                auto_confirm=auto,
            )
            # Hard rejects (decoy/artifact/rewrap-style) still dedupe so agents
            # do not re-prompt the operator with the same junk.
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

            # Rejected / not counted — escalate cooldown only for attempts that
            # reached the operator (or incorrect), not pure parse empties.
            if not display.startswith("Empty flag"):
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, False

    async def _run_solver(self, runner_id: str, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec, runner_id=runner_id)
        self.solvers[runner_id] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, runner_id, model_spec)
            solver = final_solver
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{runner_id}] Fatal: {e}", exc_info=True)
            return None
        finally:
            await solver.stop()

    async def _run_solver_loop(
        self, solver, runner_id: str, model_spec: str
    ) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ...

        ``runner_id`` keys solvers/findings/bus; ``model_spec`` is the real API model.
        """
        bump_count = 0
        consecutive_errors = 0
        infra_recoveries = 0
        result = SolverResult(
            flag=None,
            status=CANCELLED,
            findings_summary="",
            step_count=0,
            cost_usd=0.0,
            log_path="",
        )
        await solver.start()
        sb = getattr(solver, "sandbox", None)
        if sb is not None:
            self._last_preflight_ms = float(getattr(sb, "preflight_ms", 0.0) or 0.0)
        self._last_model_spec = runner_id

        while not self.cancel_event.is_set():
            # Eval wall / USD budgets (thin harness).
            cost_now = 0.0
            if self.cost_tracker is not None:
                cost_now = _tracker_cost_usd(self.cost_tracker)
            budget = self._eval.budget_exceeded(self.settings, cost_now) if self._eval else None
            if budget:
                from backend.eval_run import EVAL_BUDGET

                logger.warning(
                    "[%s/%s] Eval budget hit (%s) — cancelling",
                    self.meta.name,
                    runner_id,
                    budget,
                )
                self.cancel_event.set()
                result = SolverResult(
                    flag=self.confirmed_flag,
                    status=EVAL_BUDGET,
                    findings_summary=f"Eval budget exceeded ({budget})",
                    step_count=result.step_count,
                    cost_usd=result.cost_usd,
                    log_path=result.log_path,
                )
                self._last_status = EVAL_BUDGET
                self._last_steps = result.step_count
                self._last_flag = result.flag
                return result, solver

            result = await solver.run_until_done_or_gave_up()
            self._last_status = result.status
            self._last_steps = result.step_count
            self._last_flag = result.flag

            # Only broadcast useful findings — skip broken solvers, but DO keep
            # mid-run notes even on infra_error so sibling recover gets context.
            live_notes = (result.findings_summary or "").strip()
            solver_notes = str(getattr(solver, "_findings", "") or "").strip()
            note = live_notes if live_notes else solver_notes
            if (
                result.step_count > 0
                and note
                and not note.startswith(("Error:", "Turn failed:", "Infra:"))
            ):
                self.findings[runner_id] = note
                if result.status not in (ERROR, QUOTA_ERROR, INFRA_ERROR):
                    await self.message_bus.post(runner_id, note[:500])

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
                logger.info(f"[{self.meta.name}] Flag(s) found by {runner_id}: {result.flag}")
                return result, solver

            if result.status == FLAG_FOUND and not self.confirmed_flag:
                from backend.flags import normalize_flags_required

                logger.warning(
                    "[%s] %s reported FLAG_FOUND but challenge incomplete "
                    "(%s/%s) — soft race continues",
                    self.meta.name,
                    runner_id,
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
                        f"[{self.meta.name}/{runner_id}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    solver.sandbox = None  # type: ignore[assignment]
                    await solver.stop()
                    solver = self._create_pydantic_solver(
                        fallback_spec,
                        sandbox=existing_sandbox,
                        owns_sandbox=True,
                        runner_id=runner_id,
                    )
                    self._tag_solver(solver, runner_id, fallback_spec)
                    self.solvers[runner_id] = solver
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            # Cursor bridge / transport failures: recreate agent, do not count
            # toward the consecutive-ERROR give-up limit (session is poisoned).
            if result.status == INFRA_ERROR:
                infra_recoveries += 1
                self._infra_recoveries_total += 1
                if self._eval is not None:
                    self._eval.infra_recoveries = self._infra_recoveries_total
                consecutive_errors = 0
                # Unblock any operator confirm stuck under the flag lock.
                try:
                    from backend.flags import cancel_flag_confirmation

                    cancel_flag_confirmation()
                except Exception:
                    pass
                if infra_recoveries > MAX_INFRA_RECOVERIES:
                    logger.warning(
                        "[%s/%s] %s infra recoveries — giving up",
                        self.meta.name,
                        runner_id,
                        infra_recoveries,
                    )
                    break
                if result.step_count == 0 and infra_recoveries >= 3:
                    logger.warning(
                        "[%s/%s] Infra errors before any progress — giving up",
                        self.meta.name,
                        runner_id,
                    )
                    break
                logger.warning(
                    "[%s/%s] Infra error (%s/%s): %s — recovering session",
                    self.meta.name,
                    runner_id,
                    infra_recoveries,
                    MAX_INFRA_RECOVERIES,
                    (result.findings_summary or "")[:160],
                )
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=INFRA_RECOVERY_COOLDOWN_S,
                    )
                    break
                except TimeoutError:
                    pass
                insights = self._gather_sibling_insights(runner_id)
                recover = getattr(solver, "recover_session", None)
                if callable(recover):
                    try:
                        await cast(_RecoverSession, recover)(insights)
                    except Exception as e:
                        logger.error(
                            "[%s/%s] Session recover failed: %s",
                            self.meta.name,
                            runner_id,
                            e,
                            exc_info=True,
                        )
                        break
                else:
                    solver.bump(insights)
                continue

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0:
                    logger.warning(f"[{self.meta.name}/{runner_id}] Broken (0 steps) — not bumping")
                    break

                # Track consecutive non-infra errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{runner_id}] {consecutive_errors} consecutive errors — giving up"
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
                insights = self._gather_sibling_insights(runner_id)
                solver.bump(insights)
                logger.info(f"[{self.meta.name}/{runner_id}] Bumped ({bump_count}), resuming")
                continue

        return result, solver

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        slots = assign_runner_ids(self.model_specs)
        tasks = [
            asyncio.create_task(
                self._run_solver(runner_id, model_spec),
                name=f"solver-{runner_id}",
            )
            for runner_id, model_spec in slots
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
                        self._write_eval_artifact(result)
                        return result

                tasks = list(pending)

            self.cancel_event.set()
            summary = self._end_summary()
            self._write_eval_artifact(summary)
            return summary
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._write_eval_artifact(None)
            return None

    def _write_eval_artifact(self, result: SolverResult | None) -> None:
        out = (getattr(self.settings, "eval_out", "") or "").strip()
        if not out:
            return
        from backend.eval_run import write_eval_summary

        status = result.status if result else (self._last_status or ERROR)
        cost_usd = 0.0
        if self.cost_tracker is not None:
            cost_usd = _tracker_cost_usd(self.cost_tracker)
        write_eval_summary(
            out,
            challenge=self.meta.name,
            model=self._last_model_spec or (",".join(self.model_specs)),
            status=status,
            steps=result.step_count if result else self._last_steps,
            infra_recoveries=self._infra_recoveries_total,
            preflight_ms=self._last_preflight_ms,
            cost_usd=cost_usd,
            flag=result.flag if result else self._last_flag,
        )

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
        try:
            from backend.flags import cancel_flag_confirmation

            cancel_flag_confirmation()
        except Exception:
            pass
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
                rid: {
                    "model": spec,
                    "findings": self.findings.get(rid, ""),
                    "status": "running"
                    if rid in self.solvers and not self.cancel_event.is_set()
                    else ("won" if self.winner and self.winner.flag else "finished"),
                }
                for rid, spec in assign_runner_ids(self.model_specs)
            },
        }
