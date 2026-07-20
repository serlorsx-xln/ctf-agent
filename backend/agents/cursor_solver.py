"""Cursor SDK solver — local agent with custom tools bound to the Docker sandbox.

Uses Cursor's AsyncAgent + LocalAgentOptions.custom_tools so every tool call
executes against the isolated CTF sandbox (same surface as the Codex solver).
Requires CURSOR_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cursor_sdk import (
    AsyncAgent,
    CursorAgentError,
    CustomTool,
    CustomToolContext,
    LocalAgentOptions,
    SDKAssistantMessage,
    SDKThinkingMessage,
    SDKToolUseMessage,
)

from backend.agents.cursor_runtime import acquire_client, release_client, resolve_api_key
from backend.agents.live_log import live as _live
from backend.continue_prompt import build_continue_prompt
from backend.cost_tracker import CostTracker
from backend.flags import is_decoy_flag
from backend.loop_detect import LoopDetector
from backend.models import model_id_from_spec, supports_vision
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, ERROR, FLAG_FOUND, GAVE_UP, QUOTA_ERROR, SolverResult
from backend.tools.core import (
    do_bash,
    do_list_files,
    do_read_file,
    do_view_image,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


SOLVER_PREAMBLE = """\
IMPORTANT: You are solving a CTF challenge. Challenge files and installed tools
live inside a Docker sandbox. You MUST use the custom tools listed below for every
operation — do NOT use the built-in Shell, Read, Write, Edit, Glob, or Grep tools
(those run on the host and will not see challenge files).

Available tools:
- bash — run a command in the sandbox
- read_file / write_file / list_files — file I/O in the sandbox
- submit_flag — submit a recovered flag (ACCEPTED = more needed; CORRECT = done)
- webhook_create / webhook_get_requests — out-of-band HTTP callbacks
- view_image — inspect an image file in the sandbox
- notify_coordinator — send a strategic note to the coordinator

Paths:
- Challenge: /challenge/distfiles (read-only), /challenge/workspace (writable)
- Tool inventory: /challenge/TOOLS.txt (same as /tools.txt) — read this early
- If a tool is missing, just run it — the sandbox may install it and retry.
  Then re-read /tools.txt.

Start order: if the prompt requires connecting to a live service first, do that;
otherwise `cat /challenge/TOOLS.txt`, then inspect challenge files and solve.
Prefer installed tools over guessing. Do not search writeups.
Packages are per interpreter (`python3` ≠ `sage`); follow TOOLS.txt.

When you recover a real flag, call submit_flag. CORRECT ends the run
(ACCEPTED means more distinct flags are still required).
Ignore decoys (*fake_flag*, CTF{flag}, CTF{placeholder}, TRYHARDER).

"""


class CursorSolver:
    """Cursor SDK local-agent solver with sandbox custom tools."""

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
        )
        self.use_vision = supports_vision(model_spec)
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"

        self._client = None
        self._agent: AsyncAgent | None = None
        self._workdir: tempfile.TemporaryDirectory[str] | None = None
        self._system_prompt = ""
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._accepted_flags: list[str] = []
        self._findings = ""
        self._cost_usd = 0.0
        self._bump_insights: str | None = None
        self._started = False
        self._api_key = ""

    async def start(self) -> None:
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"
        distfile_names = list_distfiles(self.challenge_dir)
        self._system_prompt = SOLVER_PREAMBLE + build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
            has_named_tools=True,
        )

        self._api_key = resolve_api_key(self.settings)
        self._workdir = tempfile.TemporaryDirectory(prefix="ctf-cursor-")
        workdir = Path(self._workdir.name)
        skill_path = (
            Path(__file__).resolve().parents[2]
            / ".cursor"
            / "skills"
            / "ctf-tools-first"
            / "SKILL.md"
        )
        skill_body = ""
        if skill_path.is_file():
            # Strip YAML frontmatter for AGENTS.md
            raw = skill_path.read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                skill_body = parts[2].strip() if len(parts) >= 3 else raw
            else:
                skill_body = raw.strip()
        agents_md = (
            "# CTF Solver Workspace\n\n"
            "Use only the custom sandbox tools. Do not use host Shell/Read/Write.\n\n"
            "Start with: bash `cat /challenge/TOOLS.txt`\n"
            "If a tool is missing, run it anyway — the sandbox may install it "
            "automatically. Then re-read /tools.txt.\n"
        )
        if skill_body:
            agents_md += "\n" + skill_body + "\n"
        (workdir / "AGENTS.md").write_text(agents_md, encoding="utf-8")

        self._client = await acquire_client(workspace=str(workdir))
        try:
            custom_tools = self._build_custom_tools()
            self._agent = await AsyncAgent.create(
                client=self._client,
                model=self.model_id,
                api_key=self._api_key,
                name=f"ctf-solver-{self.meta.name}-{self.model_id}",
                local=LocalAgentOptions(
                    cwd=str(workdir),
                    setting_sources=[],
                    custom_tools=custom_tools,
                ),
            )
        except Exception:
            await release_client()
            self._client = None
            raise
        self._started = True
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info("[%s] Cursor solver started (agent=%s)", self.agent_name, self._agent.agent_id)

    def _build_custom_tools(self) -> dict[str, CustomTool]:
        async def _wrap(name: str, args: Mapping[str, Any], runner) -> str:
            self._step_count += 1
            self.tracer.tool_call(name, args, self._step_count)
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
            _live(f"{self.agent_name} tool#{self._step_count} → {name}", args_preview, limit=1500)

            loop_status = self.loop_detector.check(name, args)
            if loop_status == "break":
                self.tracer.event("loop_break", tool=name, step=self._step_count)
                msg = "Loop detected — change arguments or tool flags before repeating."
                _live(f"{self.agent_name} tool#{self._step_count} ✗ {name}", msg)
                return msg

            result = await runner()
            if isinstance(result, tuple):
                # view_image binary — describe instead of embedding
                image_bytes, mime_type = result
                text = f"[image {mime_type}, {len(image_bytes)} bytes — analyze via bash tools]"
            else:
                text = str(result)

            if loop_status == "warn":
                from backend.loop_detect import LOOP_WARNING_MESSAGE

                text = f"{text}\n\n{LOOP_WARNING_MESSAGE}"

            fail_status = self.loop_detector.check_result(name, text)
            if fail_status in ("oom_break", "fail_break"):
                from backend.loop_detect import OOM_STUCK_MESSAGE

                self.tracer.event("resource_loop", tool=name, step=self._step_count)
                text = f"{text}\n\n{OOM_STUCK_MESSAGE}"

            self.tracer.tool_result(name, text[:500], self._step_count)
            _live(f"{self.agent_name} tool#{self._step_count} ← {name}", text, limit=2000)

            if self._step_count % 5 == 0 and self.message_bus:
                from backend.tools.core import do_check_findings

                findings = await do_check_findings(self.message_bus, self.model_spec)
                if findings and "No new findings" not in findings:
                    text = f"{text}\n\n---\n{findings}"
            return text

        async def bash(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "bash",
                args,
                lambda: do_bash(
                    self.sandbox,
                    args.get("command", ""),
                    int(args.get("timeout_seconds", 60) or 60),
                ),
            )

        async def read_file(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "read_file",
                args,
                lambda: do_read_file(self.sandbox, args.get("path", "")),
            )

        async def write_file(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "write_file",
                args,
                lambda: do_write_file(self.sandbox, args.get("path", ""), args.get("content", "")),
            )

        async def list_files(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "list_files",
                args,
                lambda: do_list_files(self.sandbox, args.get("path", "/challenge/distfiles")),
            )

        async def submit_flag(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            async def _run() -> str:
                flag = str(args.get("flag", "")).strip()
                if self.submit_fn:
                    display, is_confirmed = await self.submit_fn(flag)
                else:
                    from backend.flags import normalize_flags_required
                    from backend.tools.core import do_submit_flag

                    display, is_confirmed = await do_submit_flag(
                        self.meta.name,
                        flag,
                        already_accepted=list(self._accepted_flags),
                        required=normalize_flags_required(getattr(self.meta, "flags_required", 1)),
                        challenge_dir=self.challenge_dir,
                    )
                if (
                    display.startswith(("ACCEPTED", "CORRECT", "Already accepted"))
                    and flag
                    and flag not in self._accepted_flags
                ):
                    self._accepted_flags.append(flag)
                if is_confirmed:
                    self._confirmed = True
                    self._flag = " | ".join(self._accepted_flags) if self._accepted_flags else flag
                    self.tracer.event(
                        "flag_confirmed",
                        flag=self._flag,
                        step=self._step_count,
                    )
                elif is_decoy_flag(flag):
                    self.tracer.event(
                        "flag_rejected_decoy",
                        flag=flag,
                        step=self._step_count,
                    )
                return display

            return await _wrap("submit_flag", args, _run)

        async def webhook_create(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap("webhook_create", args, do_webhook_create)

        async def webhook_get_requests(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "webhook_get_requests",
                args,
                lambda: do_webhook_get_requests(args.get("uuid", "")),
            )

        async def view_image(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            return await _wrap(
                "view_image",
                args,
                lambda: do_view_image(
                    self.sandbox, args.get("filename", ""), use_vision=self.use_vision
                ),
            )

        async def notify_coordinator(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
            async def _run() -> str:
                if self.notify_coordinator:
                    await self.notify_coordinator(args.get("message", ""))
                    return "Message sent to coordinator."
                return "No coordinator connected."

            return await _wrap("notify_coordinator", args, _run)

        return {
            "bash": CustomTool(
                description="Execute a bash command in the Docker sandbox.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 60},
                    },
                    "required": ["command"],
                },
                execute=bash,
            ),
            "read_file": CustomTool(
                description="Read a file from the sandbox container.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                execute=read_file,
            ),
            "write_file": CustomTool(
                description="Write a file into the sandbox container.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                execute=write_file,
            ),
            "list_files": CustomTool(
                description="List files in a directory in the sandbox.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "default": "/challenge/distfiles",
                        }
                    },
                },
                execute=list_files,
            ),
            "submit_flag": CustomTool(
                description=(
                    "Submit a recovered flag. Returns ACCEPTED (n/m) if more flags "
                    "are needed, CORRECT when all required flags are accepted, or "
                    "REJECTED. Do not submit decoys."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"flag": {"type": "string"}},
                    "required": ["flag"],
                },
                execute=submit_flag,
            ),
            "webhook_create": CustomTool(
                description="Create a webhook.site token for out-of-band HTTP callbacks.",
                input_schema={"type": "object", "properties": {}},
                execute=webhook_create,
            ),
            "webhook_get_requests": CustomTool(
                description="Retrieve HTTP requests received by a webhook.site token.",
                input_schema={
                    "type": "object",
                    "properties": {"uuid": {"type": "string"}},
                    "required": ["uuid"],
                },
                execute=webhook_get_requests,
            ),
            "view_image": CustomTool(
                description="View an image file from the sandbox for visual/steg analysis.",
                input_schema={
                    "type": "object",
                    "properties": {"filename": {"type": "string"}},
                    "required": ["filename"],
                },
                execute=view_image,
            ),
            "notify_coordinator": CustomTool(
                description="Send a strategic message to the coordinator.",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                execute=notify_coordinator,
            ),
        }

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._started:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        steps_before = self._step_count
        cost_before = self._cost_usd

        if self._bump_insights:
            cont = build_continue_prompt(
                accepted_flags=self._accepted_flags,
                flags_required=getattr(self.meta, "flags_required", 1),
                bump_insights=self._bump_insights,
            )
            prompt = f"{self._system_prompt}\n\n{cont}"
            self._bump_insights = None
        elif self._step_count == 0:
            prompt = f"{self._system_prompt}\n\nSolve this CTF challenge."
        else:
            cont = build_continue_prompt(
                accepted_flags=self._accepted_flags,
                flags_required=getattr(self.meta, "flags_required", 1),
            )
            prompt = f"{self._system_prompt}\n\n{cont}"

        try:
            _live(self.agent_name, "── turn start ──")
            run = await self._agent.send(prompt)
            async for message in run.stream():
                if self.cancel_event.is_set():
                    if run.supports("cancel"):
                        await run.cancel()
                    break

                if isinstance(message, SDKThinkingMessage):
                    if message.text.strip():
                        _live(f"{self.agent_name} think", message.text)

                elif isinstance(message, SDKAssistantMessage):
                    for block in message.message.content:
                        text = getattr(block, "text", None)
                        if text:
                            self._findings = text[:2000]
                            self._maybe_parse_flag_json(text)
                            _live(f"{self.agent_name} ai", text)

                elif isinstance(message, SDKToolUseMessage):
                    # Custom tools also log in _wrap; this catches built-in tool events.
                    if message.status == "running":
                        _live(
                            f"{self.agent_name} cursor-tool → {message.name}",
                            json.dumps(message.args, ensure_ascii=False, default=str),
                            limit=1200,
                        )
                    elif message.status in ("completed", "error") and message.result is not None:
                        _live(
                            f"{self.agent_name} cursor-tool ← {message.name} ({message.status})",
                            str(message.result),
                            limit=1500,
                        )

            result = await run.wait()
            _live(self.agent_name, f"── turn end status={result.status} ──")
            duration = time.monotonic() - t0
            self.tracer.event("turn_complete", duration=round(duration, 1), steps=self._step_count)

            if result.usage is not None:
                self.cost_tracker.record_tokens(
                    self.agent_name,
                    self.model_id,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cache_read_tokens=result.usage.cache_read_tokens,
                    provider_spec="cursor",
                    duration_seconds=duration,
                )
                agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
                self._cost_usd = agent_usage.cost_usd if agent_usage else self._cost_usd

            if result.result:
                self._findings = (result.result or self._findings)[:2000]
                self._maybe_parse_flag_json(result.result)

            status = str(result.status)
            if status == "error":
                err = (result.result or "run error").lower()
                self.tracer.event("error", error=result.result or status)
                if any(k in err for k in ("quota", "rate", "capacity", "usage", "billing")):
                    return self._result(QUOTA_ERROR)
                return self._result(ERROR)

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)

            run_steps = self._step_count - steps_before
            run_cost = self._cost_usd - cost_before
            return self._result(GAVE_UP, run_steps=run_steps, run_cost=run_cost)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except CursorAgentError as e:
            error_str = str(e)
            logger.error("[%s] Cursor startup/API error: %s", self.agent_name, e)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            if any(k in error_str.lower() for k in ("quota", "rate", "401", "403", "billing")):
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)
        except Exception as e:
            error_str = str(e)
            logger.error("[%s] Error: %s", self.agent_name, e, exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            if any(k in error_str.lower() for k in ("quota", "rate", "overloaded")):
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)

    def _maybe_parse_flag_json(self, text: str) -> None:
        stripped = text.strip()
        if not stripped.startswith("{"):
            # Try to find a JSON object in the text
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                return
            stripped = stripped[start : end + 1]
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError, ValueError:
            return
        if isinstance(parsed, dict) and parsed.get("type") == "flag_found":
            flag = parsed.get("flag")
            if flag:
                flag_s = str(flag)
                if is_decoy_flag(flag_s):
                    self._findings = f"Rejected decoy flag from model JSON: {flag_s}"
                    return
                self._flag = flag_s
                self._findings = f"Flag found via {parsed.get('method', '?')}: {self._flag}"
                # JSON alone does not confirm — only submit_flag does.

    def bump(self, insights: str) -> None:
        self._bump_insights = insights
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        logger.info("[%s] Bumped with insights", self.agent_name)

    def _result(
        self,
        status: str,
        run_steps: int | None = None,
        run_cost: float | None = None,
    ) -> SolverResult:
        self.tracer.event(
            "finish",
            status=status,
            flag=self._flag,
            confirmed=self._confirmed,
            cost_usd=round(self._cost_usd, 4),
        )
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count,
            cost_usd=run_cost if run_cost is not None else self._cost_usd,
            log_path=self.tracer.path,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._agent is not None:
            try:
                await self._agent.close()
            except Exception:
                pass
            self._agent = None
        if self._client is not None:
            await release_client()
            self._client = None
        if self._workdir is not None:
            try:
                self._workdir.cleanup()
            except Exception:
                pass
            self._workdir = None
        if self.sandbox:
            await self.sandbox.stop()
