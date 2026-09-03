"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import os
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
    # After CORRECT: winner stays alive for operator Q&A until release / stop.
    release_event: asyncio.Event = field(default_factory=asyncio.Event)
    hold_active: bool = False
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
    # True after interim emitted the "Writing recap…" placeholder (not a real How).
    _how_placeholder: bool = False
    _writeup_attempted: bool = False
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # One human y/n at a time — TUI has a single flagConfirm slot.
    _confirm_dialog_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _flag_inflight: set[str] = field(default_factory=set)  # flags awaiting human confirm
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

            # Soft check_findings must not steal the file inbox when Cursor
            # force-followup / idle-queue owns delivery.
            os.environ["ARTEMIS_CURSOR_OWNS_INBOX"] = "1"

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

    async def try_submit_flag(self, flag: str, runner_id: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, challenge_complete).

        The human confirm dialog must not hold ``_flag_lock`` — otherwise sibling
        agents block their entire ``submit_flag`` tool call until y/n returns.
        """
        from backend.flags import normalize_flags_required

        required = normalize_flags_required(getattr(self.meta, "flags_required", 1))
        normalized = flag.strip()
        async with self._flag_lock:
            if self.confirmed_flag:
                # Challenge already won by someone else — do NOT return
                # challenge_complete=True or the sibling sets ``_confirmed`` and
                # steals Hold via FLAG_FOUND.
                return (
                    f"ALREADY SOLVED — all flag(s) already confirmed: {self.confirmed_flag}",
                    False,
                )

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

            if normalized in self._flag_inflight:
                return (
                    "PENDING — another agent already has this flag awaiting confirmation. "
                    "Keep solving other angles; do not resubmit the same candidate.",
                    False,
                )

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
            wrong_count = self._submit_count.get(runner_id, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(runner_id, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._flag_inflight.add(normalized)
            auto = bool(getattr(self.settings, "auto_confirm_flags", False))

        try:
            from backend.tools.core import do_submit_flag

            # Serialize the TUI confirm dialog (single bar) without holding
            # ``_flag_lock`` during the y/n wait so siblings can keep working /
            # fail fast on dupes. Bookkeeping stays under the dialog lock so a
            # sibling cannot open another confirm before CORRECT/ACCEPTED lands.
            async with self._confirm_dialog_lock:
                # Fresh snapshot under the dialog lock — a sibling may have
                # finished another flag (or the whole challenge) while we waited.
                early: tuple[str, bool] | None = None
                already_accepted: list[str] = []
                async with self._flag_lock:
                    if self.confirmed_flag:
                        early = (
                            f"ALREADY SOLVED — all flag(s) already confirmed: {self.confirmed_flag}",
                            False,
                        )
                    elif len(self.confirmed_flags) >= required:
                        self.confirmed_flag = " | ".join(self.confirmed_flags)
                        early = (
                            f"ALREADY SOLVED — all flag(s) already confirmed: {self.confirmed_flag}",
                            False,
                        )
                    elif normalized in self.confirmed_flags:
                        n = len(self.confirmed_flags)
                        early = (
                            f"Already accepted this flag ({n}/{required}). "
                            "Continue and submit the remaining distinct flag(s).",
                            False,
                        )
                    else:
                        already_accepted = list(self.confirmed_flags)
                if early is not None:
                    display, is_complete = early
                else:
                    display, is_complete = await do_submit_flag(
                        self.meta.name,
                        flag,
                        already_accepted=already_accepted,
                        required=required,
                        challenge_dir=self.challenge_dir,
                        auto_confirm=auto,
                        by=runner_id,
                    )

                async with self._flag_lock:
                    self._flag_inflight.discard(normalized)
                    if self.confirmed_flag:
                        return (
                            f"ALREADY SOLVED — all flag(s) already confirmed: {self.confirmed_flag}",
                            False,
                        )
                    if display.startswith("ALREADY SOLVED") or display.startswith(
                        "Already accepted"
                    ):
                        return display, False
                    # Hard rejects still dedupe so agents do not re-prompt junk.
                    self._submitted_flags.add(normalized)

                    if display.startswith(("ACCEPTED", "CORRECT")):
                        if normalized not in self.confirmed_flags:
                            self.confirmed_flags.append(normalized)
                        self.flag_credits[normalized] = runner_id
                        from backend.action_log import notes_from_prose

                        solver = self.solvers.get(runner_id)
                        prose = str(getattr(solver, "_findings", "") or "").strip()
                        notes = notes_from_prose(prose)
                        if notes:
                            self.flag_notes[runner_id] = notes
                        logger.info(
                            "[%s] Flag progress %s/%s via %s",
                            self.meta.name,
                            len(self.confirmed_flags),
                            required,
                            runner_id,
                        )
                        try:
                            from backend.shell.sandbox_session import sync_accepted_flags

                            sync_accepted_flags(
                                list(self.confirmed_flags),
                                flags_required=required,
                            )
                        except Exception:
                            logger.debug("sync_accepted_flags failed", exc_info=True)
                        # Prefer live count over do_submit_flag's is_complete —
                        # concurrent multi-flag confirms can each see a stale
                        # already_accepted=[] and both return is_complete=False
                        # even when N/N is now satisfied.
                        if is_complete or len(self.confirmed_flags) >= required:
                            self.confirmed_flag = " | ".join(self.confirmed_flags)
                            self.winner_runner_id = runner_id
                            self.hold_active = True
                            self.cancel_event.set()
                            # Live-count upgrade: accept_flag only emitted ACCEPTED.
                            if not display.startswith("CORRECT"):
                                from backend.agents.live_log import emit_line
                                from backend.models import agent_display_key

                                via = agent_display_key(runner_id, runner_id)
                                emit_line(
                                    f"[artemis] outcome CORRECT — all {required} "
                                    f"flag(s) confirmed via {via}: {self.confirmed_flag}. "
                                    "Challenge complete for this run."
                                )
                            self._emit_correct_recap(runner_id)
                            self._emit_how_recap(interim=True)
                            return (
                                f"CORRECT — all {required} flag(s) confirmed: "
                                f"{self.confirmed_flag}",
                                True,
                            )
                        return display, False

                    if not display.startswith("Empty flag"):
                        self._submit_count[runner_id] = wrong_count + 1
                        self._last_submit_time[runner_id] = time.monotonic()
                    return display, False
        finally:
            async with self._flag_lock:
                self._flag_inflight.discard(normalized)

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
                infra = False
                try:
                    from backend.agents.cursor_runtime import is_infra_error_message

                    infra = is_infra_error_message(str(e))
                except Exception:
                    pass
                logger.error(
                    "[%s/%s] Fatal: %s",
                    self.meta.name,
                    runner_id,
                    e,
                    exc_info=not infra,
                )
                return None
            finally:
                # FLAG_FOUND path can be skipped (turn error/cancel after CORRECT).
                # Recap before stop() while the agent is still alive.
                if self.confirmed_flag and runner_id == self.winner_runner_id:
                    try:
                        await self._capture_and_emit_writeup(solver, runner_id)
                    except Exception:
                        logger.warning(
                            "[%s] writeup fallback failed for %s",
                            self.meta.name,
                            runner_id,
                            exc_info=True,
                        )
                        if not self._how_emitted:
                            self._emit_how_recap()
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

            # Operator notes: Cursor force-followup handles steer (interrupt) and
            # queue (after idle) inside the solver — do not soft-drain onto the bus.
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
                # Stop siblings BEFORE writeup (can take minutes) so they do not
                # keep racing/spending after CORRECT is already sticky on the TUI.
                # hold_active + winner_runner_id keep this winner alive for recap/Q&A.
                # Late FLAG_FOUND from a sibling (stale _confirmed / race) must not
                # overwrite the real winner or start a second Hold.
                if self.winner_runner_id and self.winner_runner_id != runner_id:
                    logger.info(
                        "[%s] Ignoring late FLAG_FOUND from %s (winner=%s)",
                        self.meta.name,
                        runner_id,
                        self.winner_runner_id,
                    )
                    return result, solver
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
                self.hold_active = True
                self.cancel_event.set()
                logger.info(f"[{self.meta.name}] Flag(s) found by {runner_id}: {result.flag}")
                await self._capture_and_emit_writeup(solver, runner_id)
                await self._winner_qa_hold(solver, runner_id)
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

            # Winner finished after CORRECT (cancel or provider error) — still Hold.
            # Only CANCELLED was promoted before; ERROR/QUOTA/GAVE_UP/INFRA after
            # CORRECT skipped writeup Hold entirely.
            if (
                result.status
                in (CANCELLED, ERROR, QUOTA_ERROR, GAVE_UP, INFRA_ERROR)
                and self.confirmed_flag
                and runner_id == self.winner_runner_id
                and getattr(solver, "_confirmed", False)
            ):
                prior_status = result.status
                result = SolverResult(
                    flag=self.confirmed_flag,
                    status=FLAG_FOUND,
                    findings_summary=result.findings_summary,
                    step_count=result.step_count,
                    cost_usd=result.cost_usd,
                    log_path=result.log_path,
                )
                self.winner = result
                self.hold_active = True
                self.cancel_event.set()
                logger.info(
                    "[%s] Flag(s) found by %s (promoted from %s): %s",
                    self.meta.name,
                    runner_id,
                    prior_status,
                    result.flag,
                )
                await self._capture_and_emit_writeup(solver, runner_id)
                await self._winner_qa_hold(solver, runner_id)
                return result, solver

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
                        infra = False
                        try:
                            from backend.agents.cursor_runtime import is_infra_error_message

                            infra = is_infra_error_message(str(e))
                        except Exception:
                            pass
                        logger.error(
                            "[%s/%s] Session recover failed: %s",
                            self.meta.name,
                            runner_id,
                            e,
                            exc_info=not infra,
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
                    # CORRECT: cancel siblings only — winner may be in QA hold.
                    winner_name = (
                        f"solver-{self.winner_runner_id}"
                        if self.confirmed_flag and self.winner_runner_id
                        else ""
                    )
                    to_cancel = [
                        p
                        for p in tasks
                        if not (winner_name and p.get_name() == winner_name)
                    ]
                    for p in to_cancel:
                        p.cancel()
                    if to_cancel:
                        await asyncio.gather(*to_cancel, return_exceptions=True)
                    remaining = [
                        p
                        for p in tasks
                        if winner_name and p.get_name() == winner_name and not p.done()
                    ]
                    if remaining:
                        done_hold = await asyncio.gather(
                            *remaining, return_exceptions=True
                        )
                        for item in done_hold:
                            if isinstance(item, SolverResult) and item.status == FLAG_FOUND:
                                self._write_eval_artifact(item)
                                return item
                        if self.winner:
                            self._write_eval_artifact(self.winner)
                            return self.winner
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
                    # Soft race: only kill siblings on full completion — keep the
                    # hold winner alive (same as the cancel_event waiter path).
                    if result and result.status == FLAG_FOUND and self.confirmed_flag:
                        self.cancel_event.set()
                        winner_name = (
                            f"solver-{self.winner_runner_id}"
                            if self.winner_runner_id
                            else ""
                        )
                        to_cancel = [
                            p
                            for p in pending
                            if not (winner_name and p.get_name() == winner_name)
                        ]
                        for p in to_cancel:
                            p.cancel()
                        if to_cancel:
                            await asyncio.gather(*to_cancel, return_exceptions=True)
                        remaining = [
                            p
                            for p in pending
                            if winner_name and p.get_name() == winner_name and not p.done()
                        ]
                        if remaining:
                            done_hold = await asyncio.gather(
                                *remaining, return_exceptions=True
                            )
                            for item in done_hold:
                                if (
                                    isinstance(item, SolverResult)
                                    and item.status == FLAG_FOUND
                                ):
                                    self._write_eval_artifact(item)
                                    return item
                            if self.winner:
                                self._write_eval_artifact(self.winner)
                                return self.winner
                        self._write_eval_artifact(result)
                        return result

                # Quota/stop may have been set while a task was completing.
                if self.cancel_event.is_set() and pending:
                    winner_name = (
                        f"solver-{self.winner_runner_id}"
                        if self.confirmed_flag and self.winner_runner_id
                        else ""
                    )
                    if winner_name and self.hold_active:
                        to_cancel = [
                            p for p in pending if p.get_name() != winner_name
                        ]
                        for p in to_cancel:
                            p.cancel()
                        if to_cancel:
                            await asyncio.gather(*to_cancel, return_exceptions=True)
                        hold_tasks = [
                            p for p in pending if p.get_name() == winner_name
                        ]
                        if hold_tasks:
                            await asyncio.gather(*hold_tasks, return_exceptions=True)
                        if self.winner:
                            self._write_eval_artifact(self.winner)
                            return self.winner
                        break
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
        finally:
            os.environ.pop("ARTEMIS_CURSOR_OWNS_INBOX", None)

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

    def _how_has_body(self, how_lines: list[str]) -> bool:
        from backend.writeup import is_detailed_writeup

        return is_detailed_writeup("\n".join(how_lines))

    def _emit_how_recap(self, *, interim: bool = False) -> None:
        """Stream narrative writeup once (no command trail).

        ``interim=True`` (last-flag accept): print notes now, or a pending line,
        without blocking the later writeup turn.
        """
        if self._how_emitted:
            return
        from backend.agents.live_log import emit_line

        lines = self.solve_writeup()
        # solve_writeup starts with Solved by — already emitted on accept.
        how_lines = [ln for ln in lines if not ln.startswith("Solved by ")]
        if interim and not self._how_has_body(how_lines):
            # No How: header here — the final emit owns that label. Printing it
            # now left the TUI with How: / How: once the recap arrived.
            emit_line("[artemis] summary   Writing recap from the winning solver…")
            self._how_placeholder = True
            return
        if not how_lines:
            return
        for line in how_lines:
            emit_line(f"[artemis] summary {line}")
        self._how_emitted = True
        self._how_placeholder = False

    async def _winner_qa_hold(self, solver: Any, runner_id: str) -> None:
        """Keep the winning solver session alive for operator follow-ups.

        Mid-solve inbox notes keep working (steer + queue). Esc / ``/stop`` /
        ``kill()`` sets ``release_event`` and ends the hold. Disable with
        ``ARTEMIS_SKIP_SOLVER_HOLD=1`` (tests / CI).
        """
        import os

        if (os.environ.get("ARTEMIS_SKIP_SOLVER_HOLD") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            self.hold_active = False
            return
        from backend.agents.live_log import emit_line
        from backend.models import agent_display_key
        from backend.operator_inbox import drain_operator_notes_to_bus

        # hold_active may already be True (set before cancel_event).
        self.hold_active = True
        # /stop during writeup sets release_event — honor it; do not clear.
        if self.release_event.is_set():
            self.hold_active = False
            return
        emit_line(
            "[artemis] hold — ask follow-ups on this agent "
            "(Enter sends · Esc or /stop releases)"
        )
        winner_key = None
        try:
            rid = getattr(solver, "runner_id", None) or runner_id
            spec = getattr(solver, "model_spec", None) or rid
            if rid and spec:
                winner_key = agent_display_key(str(rid), str(spec))
        except Exception:
            winner_key = None
        if not winner_key and runner_id:
            try:
                winner_key = agent_display_key(str(runner_id), str(runner_id))
            except Exception:
                winner_key = str(runner_id).rsplit("/", 1)[-1] or None
        try:
            while not self.release_event.is_set():
                try:
                    # Never claim with claimer=None on a multi-agent hold — that
                    # would vacuum sibling-scoped queue leftovers.
                    if winner_key is None and len(self.model_specs) > 1:
                        notes = []
                    else:
                        notes = await drain_operator_notes_to_bus(
                            self.message_bus,
                            delivery=None,
                            broadcast=False,
                            claimer=winner_key,
                        )
                except Exception:
                    notes = []
                for i, text in enumerate(notes):
                    reply = ""
                    preview = text[:120].replace("\n", " ")
                    emit_line(
                        "[artemis] qa-wait"
                        + (f" · {preview}" if preview else "")
                    )
                    if self.release_event.is_set():
                        # Re-queue unanswered drained notes (incl. current).
                        self._requeue_hold_notes(notes[i:], winner_key)
                        break
                    qa = getattr(solver, "qa_turn", None)
                    if callable(qa):
                        from backend.writeup import WRITEUP_TIMEOUT_S

                        qa_task = asyncio.create_task(qa(text))
                        release_task = asyncio.create_task(self.release_event.wait())
                        try:
                            done, pending = await asyncio.wait(
                                {qa_task, release_task},
                                timeout=WRITEUP_TIMEOUT_S,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for t in pending:
                                t.cancel()
                                try:
                                    await t
                                except asyncio.CancelledError:
                                    pass
                            if qa_task in done:
                                try:
                                    reply = (qa_task.result() or "").strip()
                                except Exception as e:
                                    reply = f"(qa failed: {e})"
                            elif self.release_event.is_set():
                                # Prefer a finished answer over Esc when both
                                # complete in the same wait — otherwise we throw
                                # away a landed reply and re-ask the same note.
                                reply = "(hold released)"
                            else:
                                reply = "(qa timed out)"
                        except Exception as e:
                            reply = f"(qa failed: {e})"
                    else:
                        reply = "(this solver cannot answer follow-ups)"
                    # Tag replies with roster key so the TUI can show the same
                    # agent/model footer style as normal assistant messages.
                    prefix = f"→{winner_key}: " if winner_key else ""
                    if reply:
                        for line in reply.splitlines()[:80]:
                            emit_line(f"[artemis] qa {prefix}{line}")
                    else:
                        emit_line(f"[artemis] qa {prefix}(no reply)")
                    # End the answering turn so the footer spinner clears.
                    emit_line("[artemis] qa-done")
                    if self.release_event.is_set():
                        # Abort mid-answer → requeue current; successful answer
                        # then Esc → only requeue remaining unanswered notes.
                        abort = (
                            reply in ("(hold released)", "(qa timed out)")
                            or reply.startswith("(qa failed:")
                        )
                        remaining = notes[i:] if abort else notes[i + 1 :]
                        self._requeue_hold_notes(remaining, winner_key)
                        break
                try:
                    await asyncio.wait_for(self.release_event.wait(), timeout=0.75)
                except TimeoutError:
                    pass
        finally:
            self.hold_active = False
            emit_line("[artemis] hold released")

    def _requeue_hold_notes(self, texts: list[str], winner_key: str | None) -> None:
        """Put unanswered Hold notes back so a later Hold / restart can claim them."""
        from backend.agents.live_log import emit_line
        from backend.operator_inbox import append_operator_note

        for text in texts:
            body = str(text or "").strip()
            if not body:
                continue
            try:
                # Queue (not steer): Hold is ending; sticky pending tracks queue crumbs.
                append_operator_note(body, delivery="queue", target=winner_key)
                scope = f"→{winner_key}" if winner_key else ""
                emit_line(f"[artemis] you (queue{scope}): {body[:2000]}")
            except Exception:
                logger.debug("hold requeue failed", exc_info=True)

    async def _capture_and_emit_writeup(self, solver: Any, runner_id: str) -> None:
        """Ask the winning solver for a recap; always leave How: on the main page."""
        if self._writeup_attempted:
            if not self._how_emitted:
                self._emit_how_recap()
            return
        self._writeup_attempted = True
        from backend.writeup import (
            capture_solver_writeup,
            clean_how_lines,
            is_detailed_writeup,
        )

        existing = (self.flag_notes.get(runner_id) or "").strip()
        late = str(getattr(solver, "_findings", "") or "").strip()
        # Last-turn chatter is not a writeup — only promote structured recaps.
        if is_detailed_writeup(late) and (
            not is_detailed_writeup(existing) or len(late) > len(existing) + 40
        ):
            existing = "\n".join(clean_how_lines(late)).strip()
            self.flag_notes[runner_id] = existing

        writeup = await capture_solver_writeup(solver)
        # A one-paragraph teaser is usable prose but not the operator recap.
        if writeup and is_detailed_writeup(writeup):
            prev = (self.flag_notes.get(runner_id) or "").strip()
            self.flag_notes[runner_id] = writeup
            self.findings[runner_id] = writeup[:800]
            # Re-open emit for placeholder interim, or when the real writeup is
            # clearly richer than the interim How already on the main page.
            if self._how_placeholder or (
                self._how_emitted and len(writeup) > len(prev) + 80
            ):
                self._how_emitted = False
                self._how_placeholder = False
        if not self._how_emitted:
            self._emit_how_recap()

    def solve_writeup(self) -> list[str]:
        """Operator recap: who solved it and a narrative writeup only."""
        flags = list(self.confirmed_flags)
        if not flags:
            return []
        from backend.writeup import clean_how_lines, is_detailed_writeup

        specs = dict(assign_runner_ids(self.model_specs))
        winners = self.solved_by()
        key = {rid: agent_display_key(rid, specs.get(rid, rid)) for rid in winners}
        who = ", ".join(key[rid] for rid in winners) if winners else "the swarm"
        lines = [f"Solved by {who}"]
        shared = len(winners) > 1
        for rid in winners:
            note = (self.flag_notes.get(rid) or "").strip()
            finding = (self.findings.get(rid) or "").strip()
            if not is_detailed_writeup(note) and is_detailed_writeup(finding):
                note = finding
            if shared:
                lines.append(f"How ({key[rid]}):")
            else:
                lines.append("How:")
            if note and is_detailed_writeup(note):
                for piece in clean_how_lines(note):
                    if not piece.strip():
                        lines.append("")
                        continue
                    lines.append(f"  {piece}")
                continue
            lines.append("  (no writeup recorded)")
        while lines and not lines[-1].strip():
            lines.pop()
        return lines[:400]

    def _summary_findings(self) -> str:
        parts = [f"[{m}]: {f}" for m, f in self.findings.items() if f]
        return "\n\n".join(parts)

    def kill(self) -> None:
        """Cancel all agents for this challenge (also ends post-solve QA hold)."""
        try:
            from backend.flags import cancel_flag_confirmation

            cancel_flag_confirmation()
        except Exception:
            pass
        self.release_event.set()
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
                    "status": (
                        "holding"
                        if (
                            self.hold_active
                            and self.winner_runner_id
                            and rid == self.winner_runner_id
                        )
                        else "running"
                        if rid in self.solvers and not self.cancel_event.is_set()
                        else ("won" if self.winner and self.winner.flag else "finished")
                    ),
                }
                for rid, spec in assign_runner_ids(self.model_specs)
            },
        }
