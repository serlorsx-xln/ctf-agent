"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from backend.cost_tracker import CostTracker
from backend.log_context import log_agent
from backend.message_bus import ChallengeMessageBus
from backend.models import (
    DEFAULT_MODELS,
    SUPPORTED_PROVIDERS,
    agent_display_key,
    assign_runner_ids,
    provider_from_spec,
)
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


def _tracker_cost_usd(tracker: Any) -> float | None:
    """Read CostTracker.total_reported_cost_usd; tolerate MagicMock callables.

    Returns ``None`` when no provider has reported cost (never estimated).
    """
    raw: Any = getattr(tracker, "total_reported_cost_usd", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# After this many bridge recoveries in one challenge, stop (avoid infinite spin).
MAX_INFRA_RECOVERIES = 20
# Short cooldown before recreating a poisoned Cursor agent.
INFRA_RECOVERY_COOLDOWN_S = 5


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
    winner_runner_id: str = ""
    confirmed_flag: str | None = None  # joined flags when challenge complete
    confirmed_flags: list[str] = field(default_factory=list)
    # Who got each accepted flag, and what they said they did — the swarm card
    # otherwise ends on a bare flag with no author and no method.
    flag_credits: dict[str, str] = field(default_factory=dict)
    flag_notes: dict[str, str] = field(default_factory=dict)
    _steps_by_runner: dict[str, int] = field(default_factory=dict)
    # True after How: was streamed — print_swarm_outcome must not dump it again.
    _how_emitted: bool = False
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
        pack_note = f"; packs={','.join(packs)}" if packs else ""
        # stdout (not just logger) so the TUI boot panel sees pack/image status
        print(f"[artemis] boot L0 image={image}{pack_note}", flush=True)
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
        - gemini-sdk/* → GeminiSolver (google-genai, API key/ADC/Vertex)
        """
        rid = runner_id or model_spec
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag):
            return self.try_submit_flag(flag, rid)

        _notify = self._make_notify_fn(rid)

        if provider == "cursor":
            from backend.agents.cursor_solver import CursorSolver

            all_cursor = all(
                provider_from_spec(s) == "cursor" for s in self.model_specs
            )
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
                emit_global_quota=all_cursor or len(self.model_specs) <= 1,
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

        if provider == "gemini-sdk":
            from backend.agents.gemini_solver import GeminiSolver

            solver = GeminiSolver(
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

        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(
            f"Unsupported provider {provider!r} in {model_spec!r}. "
            f"Supported: {supported} "
            f"(legacy Bedrock/Azure/Zen provider shims removed)."
        )

    def _make_notify_fn(self, runner_id: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""

        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(f"[{self.meta.name}/{runner_id}] {message}")

        return _notify

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
                by=model_spec,
            )
            # Hard rejects (decoy/artifact/rewrap-style) still dedupe so agents
            # do not re-prompt the operator with the same junk.
            self._submitted_flags.add(normalized)

            if display.startswith(("ACCEPTED", "CORRECT")):
                self.confirmed_flags.append(normalized)
                self.flag_credits[normalized] = model_spec
                # Snapshot now: after the race is cut short the solver may be
                # cancelled before it writes a prose summary. Prefer the model's
                # own note when it wrote one; always fall back to the tool trail
                # so "How the flag was found" is never just a name and a step count.
                from backend.action_log import notes_from_actions

                solver = self.solvers.get(model_spec)
                prose = str(getattr(solver, "_findings", "") or "").strip()
                actions = list(getattr(solver, "_action_log", []) or [])
                notes = notes_from_actions(actions, prose=prose)
                if notes:
                    self.flag_notes[model_spec] = notes
                logger.info(
                    "[%s] Flag progress %s/%s via %s",
                    self.meta.name,
                    len(self.confirmed_flags),
                    required,
                    model_spec,
                )
                # Persist so TUI sidebar / flowCompleted / restart-confirm work.
                try:
                    from backend.shell.sandbox_session import sync_accepted_flags

                    sync_accepted_flags(
                        list(self.confirmed_flags),
                        flags_required=required,
                    )
                except Exception:
                    logger.debug("sync_accepted_flags failed", exc_info=True)
                if is_complete:
                    self.confirmed_flag = " | ".join(self.confirmed_flags)
                    # Sticky main-page recap NOW — do not wait for writeup / teardown.
                    # Writeup can hang for minutes; without these lines the TUI goes
                    # empty while sidebar already shows n/n flags.
                    self._emit_correct_recap(model_spec)
                return display, is_complete

            # Rejected / not counted — escalate cooldown only for attempts that
            # reached the operator (or incorrect), not pure parse empties.
            if not display.startswith("Empty flag"):
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, False

    async def _run_solver(self, runner_id: str, model_spec: str) -> SolverResult | None:
        # Each solver runs in its own asyncio task, so the bound tag stays local
        # and sandbox/pack logs from this runner are attributable in the TUI.
        with log_agent(f"{self.meta.name}/{runner_id}"):
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
            cost_usd=None,
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
            self._steps_by_runner[runner_id] = result.step_count

            # Only broadcast useful findings — skip broken solvers, but DO keep
            # mid-run notes even on infra_error so sibling recover gets context.
            live_notes = (result.findings_summary or "").strip()
            solver_notes = str(getattr(solver, "_findings", "") or "").strip()
            note = live_notes if live_notes else solver_notes
            if note and result.status == QUOTA_ERROR:
                self.findings[runner_id] = note[:500]
            elif (
                result.step_count > 0
                and note
                and not note.startswith(("Error:", "Turn failed:", "Infra:"))
            ):
                self.findings[runner_id] = note
                if result.status not in (ERROR, QUOTA_ERROR, INFRA_ERROR):
                    await self.message_bus.post(runner_id, note[:500])

            if result.status == FLAG_FOUND and self.confirmed_flag:
                # Solved-by already emitted on the completing submit. Try a short
                # narrative writeup, but NEVER block teardown on Cursor SDK —
                # a hung writeup left Stopping + empty main for hours.
                from backend.writeup import capture_solver_writeup, is_usable_narrative

                writeup = ""
                existing = (self.flag_notes.get(runner_id) or "").strip()
                # Action-log notes from submit are enough for How:; skip the
                # extra Cursor turn when we already have a usable trail.
                if not is_usable_narrative(existing) and len(existing) < 80:
                    writeup_task = asyncio.create_task(capture_solver_writeup(solver))
                    done, _pending = await asyncio.wait({writeup_task}, timeout=12.0)
                    if writeup_task in done:
                        try:
                            writeup = writeup_task.result() or ""
                        except Exception:
                            writeup = ""
                    else:
                        writeup_task.cancel()
                        try:
                            await asyncio.wait_for(writeup_task, timeout=0.5)
                        except (TimeoutError, asyncio.CancelledError, Exception):
                            pass
                self.cancel_event.set()
                if writeup and is_usable_narrative(writeup):
                    self.flag_notes[runner_id] = writeup
                    self.findings[runner_id] = writeup[:500]
                # One How: block now so a hung exit path still leaves a trail.
                self._emit_how_recap()
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
                self.winner_runner_id = runner_id
                logger.info(f"[{self.meta.name}] Flag(s) found by {runner_id}: {result.flag}")
                return result, solver

            if result.status == FLAG_FOUND and not self.confirmed_flag:
                from backend.flags import normalize_flags_required

                logger.warning(
                    "[%s] %s reported FLAG_FOUND but challenge incomplete "
                    "(%s/%s) — soft swarm continues",
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

            # Quota exhaustion: stop this solver (no Bedrock/Azure API fallback).
            # All-Cursor swarms share one account — cancel siblings so Solving
            # does not spin waiting on models that will hit the same limit.
            # Mixed providers: only stop this runner (soft race continues).
            if result.status == QUOTA_ERROR:
                from backend.agents.cursor_runtime import humanize_cursor_error

                short = humanize_cursor_error(result.findings_summary) or (
                    "Cursor usage limit reached — switch model or wait for reset"
                )
                logger.warning("[%s/%s] %s", self.meta.name, runner_id, short)
                # Per-runner line so TUI agent boxes show the failure (not endless starting).
                print(f"[{self.meta.name}/{runner_id}] {short}", flush=True)
                all_cursor = all(
                    provider_from_spec(s) == "cursor" for s in self.model_specs
                )
                if all_cursor:
                    # May already be printed live by cursor_solver — keep one global line.
                    from backend.agents.quota_dedupe import claim_quota_outcome_print

                    if claim_quota_outcome_print(self.cancel_event):
                        self._quota_outcome_printed = True
                        from backend.agents.live_log import emit_line

                        emit_line(f"[artemis] outcome ERROR — {short}")
                    try:
                        from backend.flags import cancel_flag_confirmation

                        cancel_flag_confirmation()
                    except Exception:
                        pass
                    self.cancel_event.set()
                else:
                    # Mixed providers: siblings keep racing, but the main chat must
                    # still see the failure. Per-agent outcomes alone are filtered
                    # off the main page, which made quota look like a silent hang.
                    from backend.agents.live_log import emit_line

                    emit_line(
                        f"[artemis] outcome WARN — {runner_id} hit a usage limit "
                        f"(siblings continue): {short}"
                    )
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
                cancel_waiter = asyncio.create_task(self.cancel_event.wait())
                tasks_waiter = asyncio.create_task(
                    asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                )
                finished, _ = await asyncio.wait(
                    {cancel_waiter, tasks_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_waiter in finished and self.cancel_event.is_set():
                    tasks_waiter.cancel()
                    try:
                        await tasks_waiter
                    except (asyncio.CancelledError, Exception):
                        pass
                    for p in tasks:
                        p.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

                cancel_waiter.cancel()
                try:
                    await cancel_waiter
                except (asyncio.CancelledError, Exception):
                    pass
                done, pending = tasks_waiter.result()

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

                # Quota/stop may have been set while a task was completing.
                if self.cancel_event.is_set() and pending:
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    break

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
        cost_usd: float | None = None
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
        # Real provider-reported cost (None when no provider reported — never estimated).
        cost = _tracker_cost_usd(self.cost_tracker) if self.cost_tracker is not None else None
        if self.confirmed_flag:
            return SolverResult(
                flag=self.confirmed_flag,
                status=FLAG_FOUND,
                findings_summary=self._summary_findings(),
                step_count=0,
                cost_usd=cost,
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
                cost_usd=cost,
                log_path="",
            )
        # Best-effort: surface any solver findings even without accepts
        summary = self._summary_findings()
        if summary:
            # Prefer QUOTA_ERROR when every useful note is a usage-limit stop.
            from backend.agents.cursor_runtime import is_quota_error_message

            status = GAVE_UP
            if is_quota_error_message(summary) and not self.confirmed_flags:
                status = QUOTA_ERROR
            return SolverResult(
                flag=None,
                status=status,
                findings_summary=summary[:2000],
                step_count=0,
                cost_usd=cost,
                log_path="",
            )
        return None

    def solved_by(self) -> list[str]:
        """Runner ids credited with an accepted flag, in accept order."""
        credited = [self.flag_credits[f] for f in self.confirmed_flags if f in self.flag_credits]
        if self.winner_runner_id and self.winner_runner_id not in credited:
            credited.append(self.winner_runner_id)
        return list(dict.fromkeys(credited))

    def solved_by_labels(self) -> list[str]:
        """Credited runners as the labels the TUI puts on their agent boxes."""
        specs = dict(assign_runner_ids(self.model_specs))
        return [agent_display_key(rid, specs.get(rid, rid)) for rid in self.solved_by()]

    def _emit_correct_recap(self, credited_spec: str) -> None:
        """Print sticky Solved-by as soon as the last flag is accepted.

        CORRECT itself is already emitted by ``accept_flag`` — do not repeat
        the flag list here. How: comes later via ``_emit_how_recap``.
        """
        from backend.agents.live_log import emit_line

        if not self.confirmed_flags:
            return
        labels = self.solved_by_labels()
        who = ", ".join(labels) if labels else agent_display_key(credited_spec, credited_spec)
        emit_line(f"[artemis] summary Solved by {who}")

    def _emit_how_recap(self) -> None:
        """Stream How: once (narrative if usable, else command trail)."""
        if self._how_emitted:
            return
        from backend.agents.live_log import emit_line

        lines = self.solve_writeup()
        # solve_writeup starts with Solved by — already emitted on accept.
        how_lines = [ln for ln in lines if not ln.startswith("Solved by ")]
        if not how_lines:
            return
        for line in how_lines:
            emit_line(f"[artemis] summary {line}")
        self._how_emitted = True

    def solve_writeup(self) -> list[str]:
        """Operator recap: who solved it, and how (narrative XOR commands).

        Flags are only on the CORRECT outcome — repeating them here cluttered
        the TUI. Facts only — recorded state, never guessed.
        """
        flags = list(self.confirmed_flags)
        if not flags:
            return []
        from backend.action_log import command_how_lines
        from backend.writeup import clean_how_lines, is_usable_narrative

        specs = dict(assign_runner_ids(self.model_specs))
        winners = self.solved_by()
        key = {rid: agent_display_key(rid, specs.get(rid, rid)) for rid in winners}
        who = ", ".join(key[rid] for rid in winners) if winners else "the swarm"
        lines = [f"Solved by {who}"]
        shared = len(winners) > 1
        for rid in winners:
            note = (self.flag_notes.get(rid) or self.findings.get(rid) or "").strip()
            if shared:
                lines.append(f"How ({key[rid]}):")
            else:
                lines.append("How:")
            if note and is_usable_narrative(note):
                for piece in clean_how_lines(note):
                    if not piece.strip():
                        lines.append("")
                        continue
                    lines.append(f"  {piece}")
                continue
            # Unusable / missing narrative → command trail only.
            solver = self.solvers.get(rid)
            actions = list(getattr(solver, "_action_log", []) or []) if solver else []
            cmds = command_how_lines(actions)
            if not cmds and note:
                import re as _re

                for raw in note.splitlines():
                    text = raw.strip()
                    if _re.match(r"^\d+\.\s", text) and "submit_flag:" not in text:
                        cmds.append(text)
            if cmds:
                for cmd in cmds:
                    lines.append(f"  {cmd}")
                continue
            short = clean_how_lines(note) if note else []
            if short:
                for piece in short:
                    if piece.strip():
                        lines.append(f"  {piece}")
            else:
                lines.append("  (no writeup or command trail recorded)")
        while lines and not lines[-1].strip():
            lines.pop()
        return lines[:120]

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
