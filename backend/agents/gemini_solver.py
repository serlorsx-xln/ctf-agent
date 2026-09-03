"""Gemini (google-genai) solver — native function calling routed to the sandbox.

Mirrors ``claude_solver`` shape but uses the google-genai SDK directly (not
pydantic-ai) so Gemini gets first-class tool-calling parity with Claude/Codex.

Auth modes (all via /connect → auth.json → env):
  - API key (GEMINI_API_KEY)
  - ADC (no key — google.auth.default(); ``gcloud auth application-default login``)
  - Vertex AI (GEMINI_PROJECT + GEMINI_LOCATION; optionally GEMINI_BASE_URL)

USD is never reported by Gemini → ``cost_usd=None``. Token usage is committed
when ``generate_content`` returns ``usage_metadata``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.agents.live_log import live as _live
from backend.continue_prompt import build_continue_prompt
from backend.cost_tracker import CostTracker, usage_from_provider
from backend.loop_detect import LoopDetector
from backend.models import model_id_from_spec
from backend.prompts import ChallengeMeta, build_prompt
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


# google-genai tool declarations (function calling schema).
def _tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": "bash",
            "description": "Run a bash command inside the Docker CTF sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a file from the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write a file inside the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "list_files",
            "description": "List files in the sandbox (default /challenge/distfiles).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
        {
            "name": "submit_flag",
            "description": "Submit a recovered CTF flag for confirmation.",
            "parameters": {
                "type": "object",
                "properties": {"flag": {"type": "string"}},
                "required": ["flag"],
            },
        },
    ]


class GeminiSolver:
    """Gemini (google-genai) solver with native function calling."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        submit_fn=None,
        message_bus=None,
        notify_coordinator=None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.submit_fn = submit_fn
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator

        self.sandbox = DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox-core"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            settings=settings,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._client = None
        self._flag: str | None = None
        self._confirmed = False
        self._accepted_flags: list[str] = []
        self._findings = ""
        self._step_count = 0
        self._bump_insights: str | None = None
        # Session continuity across bumps: keep the running contents list.
        self._contents: list[Any] = []
        self._distfile_names: list[str] = []
        self._container_arch: str = "unknown"
        self._force_followup: asyncio.Event = asyncio.Event()
        self._pending_soft_notes: list[str] = []
        self._gen_task: asyncio.Task | None = None
        self._gen_epoch: int = 0

    async def start(self) -> None:
        from google import genai
        from google.genai.types import HttpOptions

        from backend.agents.solver_control import start_sandbox_basics

        api_key = getattr(self.settings, "gemini_api_key", "") or None
        project = getattr(self.settings, "gemini_project", "") or None
        location = getattr(self.settings, "gemini_location", "") or None
        base_url = getattr(self.settings, "gemini_base_url", "") or None

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif project:
            # Vertex mode can use ADC (no api_key) when a project is set.
            kwargs["vertexai"] = True
            kwargs["project"] = project
            if location:
                kwargs["location"] = location
        else:
            # No api_key and no project → ADC-only (google.auth.default()).
            # genai.Client with vertexai=True + no project uses ADC.
            kwargs["vertexai"] = True
        if base_url:
            kwargs["http_options"] = HttpOptions(base_url=base_url)

        self._client = genai.Client(**kwargs)
        self._container_arch, self._distfile_names = await start_sandbox_basics(
            self.sandbox, self.meta, self.challenge_dir
        )
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Gemini solver started")

    async def _exec_tool(self, name: str, args: dict[str, Any]) -> str:
        from backend.flags import normalize_flags_required
        from backend.tools.core import (
            do_bash,
            do_list_files,
            do_read_file,
            do_submit_flag,
            do_write_file,
        )

        out: str
        try:
            if name == "bash":
                out = await do_bash(self.sandbox, str(args.get("command", "")))
            elif name == "read_file":
                out = await do_read_file(self.sandbox, str(args.get("path", "")))
            elif name == "write_file":
                out = await do_write_file(
                    self.sandbox, str(args.get("path", "")), str(args.get("content", ""))
                )
            elif name == "list_files":
                out = await do_list_files(
                    self.sandbox, str(args.get("path", "/challenge/distfiles"))
                )
            elif name == "submit_flag":
                flag_val = (args.get("flag") or "").strip()
                if not flag_val:
                    out = "ERROR: empty flag"
                elif self.submit_fn:
                    # submit_fn = swarm.try_submit_flag → (display, challenge_complete)
                    out, done = await self.submit_fn(flag_val)
                else:
                    display, done = await do_submit_flag(
                        self.meta.name,
                        flag_val,
                        already_accepted=list(self._accepted_flags),
                        required=normalize_flags_required(
                            getattr(self.meta, "flags_required", 1)
                        ),
                        challenge_dir=self.challenge_dir,
                        auto_confirm=bool(getattr(self.settings, "auto_confirm_flags", False)),
                    )
                    out = display
                # Track ACCEPTED flags; only challenge_complete (CORRECT) sets _confirmed.
                if (
                    out.startswith(("ACCEPTED", "CORRECT", "Already accepted"))
                    and flag_val
                    and flag_val not in self._accepted_flags
                ):
                    self._accepted_flags.append(flag_val)
                    try:
                        from backend.shell.sandbox_session import sync_accepted_flags

                        sync_accepted_flags(
                            self._accepted_flags,
                            flags_required=normalize_flags_required(
                                getattr(self.meta, "flags_required", 1)
                            ),
                        )
                    except Exception:
                        pass
                if done:
                    self._confirmed = True
                    self._flag = (
                        " | ".join(self._accepted_flags)
                        if self._accepted_flags
                        else flag_val
                    )
                    self.tracer.event("flag_confirmed", flag=self._flag, step=self._step_count)
            else:
                out = f"unknown tool: {name}"
        except Exception as e:
            out = f"ERROR: {e}"
        self._step_count += 1
        step = self._step_count
        err = out.startswith("ERROR:") or out.startswith("unknown tool:")
        _live(f"{self.agent_name} tool#{step} → {name}", str(args), limit=1500)
        if err:
            _live(f"{self.agent_name} tool#{step} ✗ {name}", out[:2000], limit=2000)
        else:
            _live(f"{self.agent_name} tool#{step} ← {name}", out[:4000], limit=2000)
        return out

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if self._client is None:
            await self.start()
        assert self._client is not None

        # Session continuity: seed contents once, reuse across bumps. Inject
        # bump insights if the coordinator retried.
        if not self._contents:
            self._contents = [
                build_prompt(
                    self.meta,
                    self._distfile_names,
                    container_arch=self._container_arch,
                    has_named_tools=False,
                )
            ]
        if self._bump_insights:
            self._contents.append(f"\n[previous attempt insights]\n{self._bump_insights}\n")
            self._bump_insights = None

        tools = _tool_declarations()
        max_turns = 60
        t0 = time.monotonic()

        for _turn in range(max_turns):
            if self.cancel_event.is_set():
                from backend.agents.soft_steer import restore_pending_soft_notes

                restore_pending_soft_notes(self)
                if self._confirmed:
                    return self._result(FLAG_FOUND, run_steps=self._step_count)
                return self._result(CANCELLED, run_steps=self._step_count)

            _live(self.agent_name, "── turn start ──")
            if self.message_bus:
                from backend.tools.core import soft_idle_operator_notes

                idle_notes = await soft_idle_operator_notes(
                    self.message_bus,
                    self.model_spec,
                    runner_id=getattr(self, "runner_id", None) or self.model_spec,
                    cancel_event=self.cancel_event,
                    confirmed=self._confirmed,
                )
                if idle_notes:
                    self._contents.append(idle_notes)

            self._force_followup.clear()
            try:
                client = self._client
                assert client is not None
                # google-genai TypedDicts are stricter than the runtime API; cast
                # the config so ty does not reject valid function declarations.
                from typing import cast

                gen_client = client
                gen_model = self.model_id
                gen_contents = cast(Any, self._contents)
                gen_config = cast(
                    Any,
                    {
                        "tools": [{"function_declarations": tools}],
                        "automatic_function_calling": {"disable": True},
                    },
                )

                def _generate(
                    c=gen_client,
                    m=gen_model,
                    contents=gen_contents,
                    cfg=gen_config,
                ):
                    return c.models.generate_content(
                        model=m,
                        contents=contents,
                        config=cfg,
                    )

                self._gen_epoch += 1
                epoch = self._gen_epoch
                self._gen_task = asyncio.create_task(asyncio.to_thread(_generate))
                watcher = asyncio.create_task(self._watch_soft_steer())
                try:
                    response = await self._gen_task
                    superseded = epoch != self._gen_epoch or (
                        self._force_followup.is_set() and not self.cancel_event.is_set()
                    )
                    if superseded and self._apply_soft_interrupt_notes():
                        _live(self.agent_name, "── turn end status=interrupted ──")
                        continue
                except asyncio.CancelledError:
                    if self._force_followup.is_set() and not self.cancel_event.is_set():
                        _live(self.agent_name, "── turn end status=interrupted ──")
                        self._apply_soft_interrupt_notes()
                        continue
                    from backend.agents.soft_steer import restore_pending_soft_notes

                    restore_pending_soft_notes(self)
                    if self._confirmed:
                        return self._result(FLAG_FOUND, run_steps=self._step_count)
                    if self.cancel_event.is_set():
                        return self._result(CANCELLED, run_steps=self._step_count)
                    raise
                finally:
                    self._gen_task = None
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass
            except Exception as e:
                logger.warning(f"[{self.agent_name}] generate_content error: {e}")
                _live(f"{self.agent_name} status", f" Gemini error: {e}")
                _live(self.agent_name, "── turn end ──")
                from backend.agents.soft_steer import restore_pending_soft_notes

                restore_pending_soft_notes(self)
                if self._confirmed:
                    return self._result(FLAG_FOUND, run_steps=self._step_count)
                return self._result(GAVE_UP, run_steps=self._step_count)

            # Commit provider-reported tokens only. Do not preview the same
            # snapshot after record — that doubled the sidebar totals.
            parsed = usage_from_provider(response)
            if parsed["input"] or parsed["output"] or parsed["cache_read"]:
                self.cost_tracker.record_tokens(
                    self.agent_name,
                    self.model_id,
                    input_tokens=parsed["input"],
                    output_tokens=parsed["output"],
                    cache_read_tokens=parsed["cache_read"],
                    duration_seconds=time.monotonic() - t0,
                    reported_cost_usd=None,  # Gemini does not report USD.
                )

            candidate = response.candidates[0] if response.candidates else None
            if candidate is None:
                _live(f"{self.agent_name} status", "Gemini returned no candidates")
                _live(self.agent_name, "── turn end ──")
                from backend.agents.soft_steer import restore_pending_soft_notes

                restore_pending_soft_notes(self)
                return self._result(GAVE_UP, run_steps=self._step_count)

            # Handle finish_reason: stop on safety/recursion/max-tokens stops.
            finish_reason = getattr(candidate, "finish_reason", None)
            fr_name = str(getattr(finish_reason, "name", finish_reason) or "").upper()
            if fr_name and fr_name not in ("STOP", "FINISH_REASON_STOP", "MAX_TOKENS", "NONE", ""):
                _live(
                    f"{self.agent_name} status",
                    f"Gemini stopped: {fr_name}",
                )
                _live(self.agent_name, "── turn end ──")
                from backend.agents.soft_steer import restore_pending_soft_notes

                restore_pending_soft_notes(self)
                if self._confirmed:
                    return self._result(FLAG_FOUND, run_steps=self._step_count)
                return self._result(GAVE_UP, run_steps=self._step_count)

            parts = getattr(candidate.content, "parts", []) or []
            function_calls = [p for p in parts if getattr(p, "function_call", None)]
            text_parts = [p for p in parts if getattr(p, "text", None)]

            for p in text_parts:
                txt = getattr(p, "text", "")
                if txt.strip():
                    self._findings = txt[:2000]
                    _live(f"{self.agent_name} ai", txt)

            if not function_calls:
                # No tool call → model is done talking. Check confirmed flag.
                _live(self.agent_name, "── turn end ──")
                if self._confirmed:
                    from backend.agents.soft_steer import restore_pending_soft_notes

                    restore_pending_soft_notes(self)
                    return self._result(FLAG_FOUND, run_steps=self._step_count)
                # Ask the model to continue / act.
                self._contents.append(response)
                self._contents.append(
                    build_continue_prompt(
                        accepted_flags=self._accepted_flags,
                        session_sync=bool(self.submit_fn),
                        flags_required=getattr(self.meta, "flags_required", 1),
                    )
                )
                continue

            self._contents.append(response)
            # Claim Send-now during tool batches (watcher only covered generate).
            await self._claim_soft_steer()
            for fc_part in function_calls:
                if self._force_followup.is_set() or self.cancel_event.is_set():
                    break
                fc = fc_part.function_call
                name = fc.name
                args = dict(fc.args or {})
                tool_watcher = asyncio.create_task(self._watch_soft_steer())
                try:
                    result_text = await self._exec_tool(name, args)
                    if self.message_bus:
                        from backend.tools.core import do_check_findings

                        findings = await do_check_findings(
                            self.message_bus,
                            self.model_spec,
                            runner_id=getattr(self, "runner_id", None) or self.model_spec,
                        )
                        if findings and "No new findings" not in findings:
                            result_text = f"{result_text}\n\n---\n{findings}"
                    # Append the function response for the next turn.
                    from google.genai.types import Part

                    self._contents.append(
                        Part.from_function_response(name=name, response={"result": result_text})
                    )
                finally:
                    tool_watcher.cancel()
                    try:
                        await tool_watcher
                    except asyncio.CancelledError:
                        pass

            _live(self.agent_name, "── turn end ──")
            if self._confirmed:
                from backend.agents.soft_steer import restore_pending_soft_notes

                restore_pending_soft_notes(self)
                return self._result(FLAG_FOUND, run_steps=self._step_count)

            if (
                self._force_followup.is_set()
                and not self.cancel_event.is_set()
                and self._apply_soft_interrupt_notes()
            ):
                continue

        from backend.agents.soft_steer import restore_pending_soft_notes

        restore_pending_soft_notes(self)
        return self._result(GAVE_UP, run_steps=self._step_count)

    def _apply_soft_interrupt_notes(self) -> bool:
        """Append pending Send-now notes into contents. Returns True when notes applied."""
        notes = [n for n in self._pending_soft_notes if str(n or "").strip()]
        self._pending_soft_notes.clear()
        self._force_followup.clear()
        if not notes:
            return False
        from backend.agents.soft_steer import operator_interrupt_prompt

        cont = build_continue_prompt(
            accepted_flags=self._accepted_flags,
            session_sync=bool(self.submit_fn),
            flags_required=getattr(self.meta, "flags_required", 1),
        )
        self._contents.append(operator_interrupt_prompt(notes, cont))
        return True

    async def _claim_soft_steer(self) -> list[str]:
        from backend.agents.live_log import emit_line
        from backend.agents.soft_steer import claim_soft_steer_notes

        notes = await claim_soft_steer_notes(self)
        if notes:
            self._pending_soft_notes.extend(notes)
            self._force_followup.set()
            preview = notes[0][:120]
            emit_line(
                "[artemis] followup — interrupting current turn"
                + (f" · {preview}" if preview else "")
            )
            task = self._gen_task
            if task is not None and not task.done():
                self._gen_epoch += 1
                task.cancel()
        return notes

    async def _watch_soft_steer(self) -> None:
        try:
            while not self.cancel_event.is_set() and not self._confirmed:
                if self._force_followup.is_set():
                    return
                await self._claim_soft_steer()
                if self._force_followup.is_set():
                    return
                try:
                    await asyncio.wait_for(self._force_followup.wait(), timeout=0.6)
                    return
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    def _result(self, status: str, run_steps: int | None = None) -> SolverResult:
        return SolverResult(
            flag=self._flag if self._confirmed else None,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count,
            # Gemini does not report USD — leave unknown (never estimated).
            cost_usd=None,
            log_path=self.tracer.path,
        )

    def bump(self, insights: str) -> None:
        """Stash bump insights + reset loop detector (mirrors Claude/Cursor/Codex)."""
        from backend.agents.solver_control import stash_bump

        stash_bump(self, insights)

    async def produce_writeup(self, prompt: str | None = None) -> str:
        """One more generate_content call without tools for the operator recap."""
        from backend.agents.live_log import quiet_live
        from backend.writeup import WRITEUP_PROMPT, WRITEUP_TIMEOUT_S

        if self._client is None:
            return ""
        with quiet_live():
            _live(self.agent_name, "── writeup ──")
            contents = list(self._contents) + [prompt or WRITEUP_PROMPT]
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.models.generate_content,
                        model=self.model_id,
                        contents=contents,
                        config={},
                    ),
                    timeout=WRITEUP_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "[%s] writeup/qa generate_content timed out after %.0fs",
                    self.agent_name,
                    WRITEUP_TIMEOUT_S,
                )
                return ""
            except Exception as e:
                logger.warning(f"[{self.agent_name}] writeup generate_content error: {e}")
                return ""
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None:
                return ""
            parts = getattr(candidate.content, "parts", []) or []
            texts = [
                str(getattr(p, "text", "") or "").strip()
                for p in parts
                if getattr(p, "text", None)
            ]
            from backend.writeup import join_streamed_text_parts

            # Recap reaches the TUI via ``[artemis] summary`` — skip live writeup spam.
            return join_streamed_text_parts(texts)

    async def qa_turn(self, question: str) -> str:
        """Post-solve follow-up with prior Gemini contents."""
        q = (question or "").strip()
        if not q:
            return ""
        prompt = (
            "The challenge is already solved. Answer the operator's follow-up "
            "using what you learned in this session. Be concise.\n\n"
            f"Operator: {q}"
        )
        return await self.produce_writeup(prompt)

    async def stop(self) -> None:
        try:
            await self.sandbox.stop()
        except Exception:
            logger.debug("gemini sandbox stop failed", exc_info=True)
