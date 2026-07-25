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
    SDKStatusMessage,
    SDKThinkingMessage,
    SDKToolUseMessage,
    SDKUsageMessage,
)

from backend.agents.cursor_runtime import (
    acquire_client,
    current_client,
    force_recreate_client,
    format_cursor_run_error,
    humanize_cursor_error,
    release_client,
    resolve_api_key,
)
from backend.agents.live_log import live as _live
from backend.bash_intercept import (
    SUBMIT_EXPANSION_ERROR,
    SUBMIT_UNPARSED_ERROR,
    extract_notify_coordinator,
    parse_submit_flag,
    submit_flag_attempted,
    submit_flag_suffix,
)
from backend.continue_prompt import build_continue_prompt
from backend.cost_tracker import CostTracker
from backend.flags import is_decoy_flag
from backend.loop_detect import LoopDetector
from backend.models import model_id_from_spec, supports_vision
from backend.prompts import ChallengeMeta, build_prompt
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    INFRA_ERROR,
    QUOTA_ERROR,
    SolverResult,
)
from backend.tools.core import (
    VISION_BASH_FALLBACK,
    do_bash,
    do_list_files,
    do_read_file,
    do_view_image,
    do_web_fetch,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


def _tool_args_preview(name: str, args: Mapping[str, Any]) -> str:
    """Human-readable tool args for live chat (not raw Cursor SDK dumps)."""
    if name == "bash":
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    if name in ("read_file", "write_file", "list_files"):
        path = args.get("path")
        if path:
            return str(path)
    if name == "submit_flag":
        flag = args.get("flag")
        if flag:
            return str(flag)
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def _flag_candidate_hint(output: str) -> str | None:
    """If bash output looks like a lone flag, return it so we can nudge submit_flag."""
    import re

    text = (output or "").strip()
    if not text or len(text) > 200:
        return None
    # Prefer flag{…} / CTF{…} forms
    m = re.search(r"\b([A-Za-z0-9_]+\{[^\s|]{4,120}\})", text)
    if m:
        return m.group(1)
    # Single-line hex / token
    if re.fullmatch(r"[A-Za-z0-9_\-]{16,80}", text):
        return text
    return None


def _image_tool_result(image_bytes: bytes, mime_type: str, *, label: str = "") -> dict:
    """MCP-style content so the Cursor bridge can surface the image to the model."""
    import base64

    note = label or f"Image ({mime_type}, {len(image_bytes)} bytes)."
    return {
        "content": [
            {
                "type": "text",
                "text": (f"{note} Inspect visually; also use sandbox stego/exif tools as needed."),
            },
            {
                "type": "image",
                "data": base64.b64encode(image_bytes).decode("ascii"),
                "mimeType": mime_type,
            },
        ]
    }


SOLVER_PREAMBLE = """\
IMPORTANT: You are solving a CTF challenge. Challenge files and installed tools
live inside a Docker sandbox. You MUST use the custom tools listed below for every
operation — do NOT use the built-in Shell, Read, Write, Edit, Glob, or Grep tools
(those run on the host and will not see challenge files).

Available tools:
- bash — run a command in the sandbox
- read_file / write_file / list_files — file I/O in the sandbox
- submit_flag — submit a recovered flag (operator confirms; CORRECT = done)
- web_fetch — fetch a URL from the host network
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
`pip3 install pkg` works as-is (sandbox adds --break-system-packages).

Long-running bash (factoring, scans, compiles, remote loops): always set
timeout_seconds explicitly (300–900+) and print progress so the session stays
healthy. Prefer writing a script to /challenge/workspace and running it once
over many interactive one-liners.
If a command exits 124 (timeout) or 137 (OOM), do not immediately retry the
same heavy command — shrink the work or change approach.
When you already recovered a concrete flag/candidate, call submit_flag before
starting unrelated heavy jobs (e.g. sage factor after a stereotypic decrypt).

When you recover a candidate answer, call submit_flag with the exact string
(any format the challenge awards — do not rewrite to fit a pattern).
A human confirms; CORRECT ends the run (ACCEPTED = more flags still required).
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
        *,
        emit_global_quota: bool = True,
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
        # True for single-agent or all-Cursor swarms — mixed soft-race must not
        # emit a global [artemis] outcome (would unlock TUI while Claude continues).
        self.emit_global_quota = emit_global_quota

        self.sandbox = DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox-core"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            settings=settings,
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
        self._action_log: list[str] = []
        self._bump_insights: str | None = None
        self._infra_recovery = False
        self._started = False
        self._api_key = ""
        # In-turn usage from SDKUsageMessage (absolute for this turn; not yet committed).
        self._turn_usage_pending: Any | None = None

    async def start(self) -> None:
        from backend.agents.solver_control import start_sandbox_basics

        container_arch, distfile_names = await start_sandbox_basics(
            self.sandbox, self.meta, self.challenge_dir
        )
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
            self._agent = await self._create_agent()
        except Exception:
            await release_client()
            self._client = None
            raise
        self._started = True
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info("[%s] Cursor solver started (agent=%s)", self.agent_name, self._agent.agent_id)

    async def _create_agent(self) -> AsyncAgent:
        assert self._client is not None
        assert self._workdir is not None
        workdir = Path(self._workdir.name)
        custom_tools = self._build_custom_tools()
        return await AsyncAgent.create(
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

    async def recover_session(self, insights: str | None = None) -> None:
        """Replace a poisoned Cursor agent; keep sandbox + workspace files."""
        if self._workdir is None:
            raise RuntimeError("Cannot recover session before start()")

        logger.warning("[%s] Recovering Cursor agent session after infra error", self.agent_name)
        if self._agent is not None:
            try:
                await self._agent.close()
            except Exception:
                pass
            self._agent = None

        workdir = str(self._workdir.name)
        try:
            # Prefer the live shared view (sibling may have force-recreated).
            live = await current_client()
            if live is not None:
                self._client = live
            elif self._client is None:
                self._client = await acquire_client(workspace=workdir)
            self._agent = await self._create_agent()
        except Exception as e:
            logger.warning(
                "[%s] Agent recreate failed (%s) — force-relaunching bridge",
                self.agent_name,
                e,
            )
            # Keep our ref count; replace the underlying shared bridge process.
            self._client = await force_recreate_client(workspace=workdir)
            self._agent = await self._create_agent()

        self._infra_recovery = True
        self.loop_detector.reset()
        if insights:
            self._bump_insights = insights
        self.tracer.event("infra_recover", insights=(insights or "")[:500])
        logger.info(
            "[%s] Cursor agent recovered (agent=%s)",
            self.agent_name,
            self._agent.agent_id,
        )

    def _build_custom_tools(self) -> dict[str, CustomTool]:
        async def _wrap(name: str, args: Mapping[str, Any], runner) -> str | dict:
            self._step_count += 1
            step = self._step_count
            self.tracer.tool_call(name, args, step)
            from backend.action_log import append_action

            append_action(self._action_log, name, args)
            args_preview = _tool_args_preview(name, args)
            _live(f"{self.agent_name} tool#{step} → {name}", args_preview, limit=1500)

            loop_status = self.loop_detector.check(name, args)
            if loop_status == "break":
                self.tracer.event("loop_break", tool=name, step=step)
                msg = "Loop detected — change arguments or tool flags before repeating."
                self.tracer.tool_result(name, msg, step)
                _live(f"{self.agent_name} tool#{step} ✗ {name}", msg)
                return msg

            try:
                result = await runner()
                if isinstance(result, tuple):
                    image_bytes, mime_type = result
                    try:
                        text = _image_tool_result(
                            image_bytes,
                            mime_type,
                            label=f"view_image {args.get('filename', '')}".strip(),
                        )
                        preview = f"image:{mime_type}:{len(image_bytes)}b"
                    except Exception as img_err:
                        logger.warning(
                            "[%s] view_image payload failed: %s", self.agent_name, img_err
                        )
                        text = (
                            f"Loaded {mime_type} ({len(image_bytes)} bytes) but could not "
                            f"attach pixels to the model. {VISION_BASH_FALLBACK}"
                        )
                        preview = text
                elif isinstance(result, dict) and "content" in result:
                    text = result
                    preview = json.dumps(result, ensure_ascii=False, default=str)[:500]
                else:
                    text = str(result)
                    preview = text

                if loop_status == "warn":
                    from backend.loop_detect import LOOP_WARNING_MESSAGE

                    warn = LOOP_WARNING_MESSAGE
                    if isinstance(text, dict):
                        content = list(text.get("content") or [])
                        content.append({"type": "text", "text": warn})
                        text = {**text, "content": content}
                        preview = f"{preview}\n\n{warn}"
                    else:
                        text = f"{text}\n\n{warn}"
                        preview = text

                fail_status = self.loop_detector.check_result(name, str(preview))
                if fail_status in ("oom_break", "fail_break"):
                    from backend.loop_detect import OOM_STUCK_MESSAGE

                    self.tracer.event("resource_loop", tool=name, step=step)
                    if isinstance(text, dict):
                        content = list(text.get("content") or [])
                        content.append({"type": "text", "text": OOM_STUCK_MESSAGE})
                        text = {**text, "content": content}
                        preview = f"{preview}\n\n{OOM_STUCK_MESSAGE}"
                    else:
                        text = f"{text}\n\n{OOM_STUCK_MESSAGE}"
                        preview = text

                self.tracer.tool_result(name, str(preview)[:500], step)
                _live(f"{self.agent_name} tool#{step} ← {name}", str(preview), limit=2000)

                if step % 5 == 0 and self.message_bus:
                    from backend.tools.core import do_check_findings

                    findings = await do_check_findings(
                        self.message_bus,
                        getattr(self, "runner_id", None) or self.model_spec,
                    )
                    if findings and "No new findings" not in findings:
                        if isinstance(text, dict):
                            content = list(text.get("content") or [])
                            content.append({"type": "text", "text": f"---\n{findings}"})
                            text = {**text, "content": content}
                        else:
                            text = f"{text}\n\n---\n{findings}"
                return text
            except Exception as e:
                err = f"Tool error: {e}"
                self.tracer.tool_result(name, err[:500], step)
                self.tracer.event("error", error=err[:300], tool=name, step=step)
                _live(f"{self.agent_name} tool#{step} ✗ {name}", err, limit=2000)
                raise

        async def bash(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            async def _run() -> str:
                command = str(args.get("command", "") or "")
                timeout = int(args.get("timeout_seconds", 300) or 300)
                parsed = parse_submit_flag(command)
                if parsed is not None:
                    if parsed.has_expansion:
                        return SUBMIT_EXPANSION_ERROR
                    if self.submit_fn:
                        display, is_confirmed = await self.submit_fn(parsed.value)
                    else:
                        from backend.flags import normalize_flags_required
                        from backend.tools.core import do_submit_flag

                        display, is_confirmed = await do_submit_flag(
                            self.meta.name,
                            parsed.value,
                            already_accepted=list(self._accepted_flags),
                            required=normalize_flags_required(
                                getattr(self.meta, "flags_required", 1)
                            ),
                            challenge_dir=self.challenge_dir,
                            auto_confirm=bool(getattr(self.settings, "auto_confirm_flags", False)),
                        )
                    flag_val = (parsed.value or "").strip()
                    if (
                        display.startswith(("ACCEPTED", "CORRECT", "Already accepted"))
                        and flag_val
                        and flag_val not in self._accepted_flags
                    ):
                        self._accepted_flags.append(flag_val)
                        try:
                            from backend.flags import normalize_flags_required
                            from backend.shell.sandbox_session import sync_accepted_flags

                            sync_accepted_flags(
                                self._accepted_flags,
                                flags_required=normalize_flags_required(
                                    getattr(self.meta, "flags_required", 1)
                                ),
                            )
                        except Exception:
                            pass
                    if is_confirmed:
                        self._confirmed = True
                        self._flag = (
                            " | ".join(self._accepted_flags) if self._accepted_flags else flag_val
                        )
                    suffix = submit_flag_suffix(command, parsed)
                    if suffix:
                        more = await do_bash(self.sandbox, suffix, timeout)
                        return f"{display}\n{more}"
                    return display
                if submit_flag_attempted(command):
                    return SUBMIT_UNPARSED_ERROR
                notify_msg = extract_notify_coordinator(command)
                if notify_msg is not None and self.notify_coordinator:
                    await self.notify_coordinator(notify_msg)
                    return "Message sent to coordinator."
                out = await do_bash(self.sandbox, command, timeout)
                # Hint: recovered flag-looking text must go through submit_flag for TUI confirm
                hint = _flag_candidate_hint(out)
                if hint:
                    from backend.agents.live_log import live as _live_hint

                    _live_hint(
                        f"{self.agent_name} ai",
                        f"Possible flag in command output — call submit_flag({hint!r}) so the TUI can confirm.",
                    )
                return out

            return await _wrap("bash", args, _run)

        async def read_file(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap(
                "read_file",
                args,
                lambda: do_read_file(self.sandbox, args.get("path", "")),
            )

        async def write_file(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap(
                "write_file",
                args,
                lambda: do_write_file(self.sandbox, args.get("path", ""), args.get("content", "")),
            )

        async def list_files(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap(
                "list_files",
                args,
                lambda: do_list_files(self.sandbox, args.get("path", "/challenge/distfiles")),
            )

        async def submit_flag(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
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
                        auto_confirm=bool(getattr(self.settings, "auto_confirm_flags", False)),
                    )
                if (
                    display.startswith(("ACCEPTED", "CORRECT", "Already accepted"))
                    and flag
                    and flag not in self._accepted_flags
                ):
                    self._accepted_flags.append(flag)
                    try:
                        from backend.flags import normalize_flags_required
                        from backend.shell.sandbox_session import sync_accepted_flags

                        sync_accepted_flags(
                            self._accepted_flags,
                            flags_required=normalize_flags_required(
                                getattr(self.meta, "flags_required", 1)
                            ),
                        )
                    except Exception:
                        pass
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

        async def web_fetch(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap(
                "web_fetch",
                args,
                lambda: do_web_fetch(
                    args.get("url", ""),
                    args.get("method", "GET"),
                    args.get("body", ""),
                ),
            )

        async def webhook_create(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap("webhook_create", args, do_webhook_create)

        async def webhook_get_requests(
            args: Mapping[str, Any], _ctx: CustomToolContext
        ) -> str | dict:
            return await _wrap(
                "webhook_get_requests",
                args,
                lambda: do_webhook_get_requests(args.get("uuid", "")),
            )

        async def view_image(args: Mapping[str, Any], _ctx: CustomToolContext) -> str | dict:
            return await _wrap(
                "view_image",
                args,
                lambda: do_view_image(
                    self.sandbox, args.get("filename", ""), use_vision=self.use_vision
                ),
            )

        async def notify_coordinator(
            args: Mapping[str, Any], _ctx: CustomToolContext
        ) -> str | dict:
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
                        "timeout_seconds": {
                            "type": "integer",
                            "default": 300,
                            "description": (
                                "Seconds before the sandbox kills the command. "
                                "Use 300–900+ for factoring, scans, and long remotes."
                            ),
                        },
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
                    "Submit a recovered flag candidate (exact string from the challenge). "
                    "A human confirms. Returns ACCEPTED (n/m), CORRECT when done, "
                    "or REJECTED. Do not submit decoys."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"flag": {"type": "string"}},
                    "required": ["flag"],
                },
                execute=submit_flag,
            ),
            "web_fetch": CustomTool(
                description="Fetch a URL from the host network.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "method": {"type": "string", "default": "GET"},
                        "body": {"type": "string", "default": ""},
                    },
                    "required": ["url"],
                },
                execute=web_fetch,
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

        if self._bump_insights:
            cont = build_continue_prompt(
                accepted_flags=self._accepted_flags,
                flags_required=getattr(self.meta, "flags_required", 1),
                bump_insights=self._bump_insights,
                infra_recovery=self._infra_recovery,
            )
            prompt = f"{self._system_prompt}\n\n{cont}"
            self._bump_insights = None
            self._infra_recovery = False
        elif self._infra_recovery:
            cont = build_continue_prompt(
                accepted_flags=self._accepted_flags,
                flags_required=getattr(self.meta, "flags_required", 1),
                infra_recovery=True,
            )
            prompt = f"{self._system_prompt}\n\n{cont}"
            self._infra_recovery = False
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
            self._turn_usage_pending = None
            run = await self._agent.send(prompt)
            status_detail = ""
            async for message in run.stream():
                if self.cancel_event.is_set():
                    if run.supports("cancel"):
                        await run.cancel()
                    break

                if isinstance(message, SDKStatusMessage):
                    if (message.message or "").strip():
                        status_detail = humanize_cursor_error(message.message.strip())
                        from backend.agents.cursor_runtime import is_quota_error_message

                        if is_quota_error_message(message.message):
                            # Print immediately — waiting for turn teardown made the
                            # TUI sit on "waiting · 0 events" for a long time.
                            if not getattr(self, "_quota_live_printed", False):
                                self._quota_live_printed = True
                                short = status_detail or (
                                    "Cursor usage limit reached — switch model or wait for reset"
                                )
                                print(
                                    f"[{self.agent_name}] {short}",
                                    flush=True,
                                )
                                if self.emit_global_quota:
                                    from backend.agents.quota_dedupe import (
                                        claim_quota_outcome_print,
                                    )

                                    if claim_quota_outcome_print(self.cancel_event):
                                        print(
                                            f"[artemis] outcome ERROR — {short}",
                                            flush=True,
                                        )
                                    try:
                                        from backend.flags import cancel_flag_confirmation

                                        cancel_flag_confirmation()
                                    except Exception:
                                        pass
                                    self.cancel_event.set()
                                    if run.supports("cancel"):
                                        await run.cancel()
                                    break
                        else:
                            _live(
                                f"{self.agent_name} status",
                                f"{message.status}: {status_detail}",
                                limit=240,
                            )

                elif isinstance(message, SDKThinkingMessage):
                    if message.text.strip():
                        _live(f"{self.agent_name} think", message.text)

                elif isinstance(message, SDKAssistantMessage):
                    for block in message.message.content:
                        text = getattr(block, "text", None)
                        if text:
                            self._findings = text[:2000]
                            self._maybe_parse_flag_json(text)
                            _live(f"{self.agent_name} ai", text)

                elif isinstance(message, SDKUsageMessage):
                    # Provider-reported only — preview sidebar mid-turn (commit at turn end).
                    u = message.usage
                    if u is not None:
                        self._turn_usage_pending = u
                        self.cost_tracker.publish_with_pending(
                            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                            cache_read_tokens=int(getattr(u, "cache_read_tokens", 0) or 0),
                        )

                elif isinstance(message, SDKToolUseMessage):
                    # Custom tools already log in _wrap — skip SDK duplicate dumps.
                    if message.name and "custom" in str(message.name).lower():
                        continue
                    if message.status == "running":
                        preview = _tool_args_preview(
                            str(message.name),
                            message.args if isinstance(message.args, Mapping) else {},
                        )
                        _live(
                            f"{self.agent_name} tool → {message.name}",
                            preview,
                            limit=1200,
                        )
                    elif message.status in ("completed", "error") and message.result is not None:
                        _live(
                            f"{self.agent_name} tool ← {message.name} ({message.status})",
                            str(message.result),
                            limit=1500,
                        )

            result = await run.wait()
            _live(self.agent_name, f"── turn end status={result.status} ──")
            duration = time.monotonic() - t0
            self.tracer.event("turn_complete", duration=round(duration, 1), steps=self._step_count)

            usage = result.usage if result.usage is not None else self._turn_usage_pending
            self._turn_usage_pending = None
            if usage is not None:
                self.cost_tracker.record_tokens(
                    self.agent_name,
                    self.model_id,
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                    provider_spec="cursor",
                    duration_seconds=duration,
                )
                self.tracer.usage(
                    int(getattr(usage, "input_tokens", 0) or 0),
                    int(getattr(usage, "output_tokens", 0) or 0),
                    int(getattr(usage, "cache_read_tokens", 0) or 0),
                )

            if result.result:
                self._findings = (result.result or self._findings)[:2000]
                self._maybe_parse_flag_json(result.result)

            status = str(result.status)
            if status == "error":
                err = format_cursor_run_error(
                    result_text=result.result,
                    status_message=status_detail,
                )
                self.tracer.event("error", error=err)
                from backend.agents.solver_control import classify_turn_error

                classified = classify_turn_error(err)
                if classified == QUOTA_ERROR:
                    self._findings = status_detail or humanize_cursor_error(err)
                    return self._result(QUOTA_ERROR)
                if classified == INFRA_ERROR:
                    self._findings = f"Infra: {err}"
                    return self._result(INFRA_ERROR)
                self._findings = f"Error: {err}"
                return self._result(ERROR)

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)

            run_steps = self._step_count - steps_before
            return self._result(GAVE_UP, run_steps=run_steps)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except CursorAgentError as e:
            error_str = str(e)
            logger.error("[%s] Cursor startup/API error: %s", self.agent_name, e)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            from backend.agents.solver_control import classify_turn_error

            classified = classify_turn_error(error_str)
            if classified == QUOTA_ERROR:
                return self._result(QUOTA_ERROR)
            if classified == INFRA_ERROR or getattr(e, "is_retryable", False):
                return self._result(INFRA_ERROR)
            return self._result(ERROR)
        except Exception as e:
            error_str = str(e)
            logger.error("[%s] Error: %s", self.agent_name, e, exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            from backend.agents.solver_control import classify_turn_error

            classified = classify_turn_error(error_str)
            if classified == QUOTA_ERROR:
                return self._result(QUOTA_ERROR)
            if classified == INFRA_ERROR:
                return self._result(INFRA_ERROR)
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
        except (json.JSONDecodeError, ValueError):
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
        from backend.agents.solver_control import stash_bump

        stash_bump(self, insights)
        logger.info("[%s] Bumped with insights", self.agent_name)

    def _result(
        self,
        status: str,
        run_steps: int | None = None,
    ) -> SolverResult:
        self.tracer.event(
            "finish",
            status=status,
            flag=self._flag,
            confirmed=self._confirmed,
        )
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count,
            # Cursor does not report USD; leave unknown.
            cost_usd=None,
            log_path=self.tracer.path,
        )

    async def produce_writeup(self) -> str:
        """One more turn: narrative writeup for the operator recap (no tools expected)."""
        from backend.writeup import WRITEUP_PROMPT

        if self._agent is None:
            return ""
        # Swarm may have cancelled siblings; if we are already cancelled, skip the
        # extra Cursor turn — it hangs easily and the sticky summary already ran.
        if self.cancel_event is not None and self.cancel_event.is_set():
            return ""
        parts: list[str] = []
        _live(self.agent_name, "── writeup ──")
        run = await self._agent.send(WRITEUP_PROMPT)
        async for message in run.stream():
            if self.cancel_event is not None and self.cancel_event.is_set():
                break
            if isinstance(message, SDKAssistantMessage):
                for block in message.message.content:
                    text = getattr(block, "text", None)
                    if text and str(text).strip():
                        parts.append(str(text).strip())
                        # Do not stream token deltas to the live log — hundreds of
                        # ``[agent writeup] x`` lines drown CORRECT / summary and
                        # invent phantom status on the main page.
        try:
            await asyncio.wait_for(run.wait(), timeout=5.0)
        except TimeoutError:
            pass
        from backend.writeup import join_streamed_text_parts

        return join_streamed_text_parts(parts)

    async def stop(self) -> None:
        if self._step_count == 0 and not getattr(self, "_started", False):
            self.tracer.event(
                "error",
                error="stopped before start completed (0 steps)",
            )
        elif self._step_count == 0:
            self.tracer.event(
                "error",
                error="stopped with 0 tool steps",
            )
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._agent is not None:
            try:
                # Hung writeup/close left Stopping for hours — hard-cap teardown.
                await asyncio.wait_for(self._agent.close(), timeout=8.0)
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
            try:
                await asyncio.wait_for(self.sandbox.stop(), timeout=15.0)
            except Exception:
                logger.warning(
                    "[%s] sandbox.stop timed out or failed during cleanup",
                    self.agent_name,
                )
