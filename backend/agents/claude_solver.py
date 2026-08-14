"""Claude Agent SDK solver — native tools with execution hooks.

Uses Claude's native Bash tool, but intercepts every command via a PreToolUse
hook and runs it through ``DockerSandbox.exec`` / ``do_bash`` (pack ensure,
host VPN proxy, nmap/hosts harden) — not a raw ``docker exec`` rewrite. Read,
Write, and Edit are blocked — the model uses bash for all file operations.

Flag submission is available both as an MCP tool (preferred) and via bash
``submit_flag …`` (including compound commands like ``cd … && submit_flag …``).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    create_sdk_mcp_server,
    tool,
)

from backend.agents.live_log import live as _live
from backend.agents.live_log import live_json
from backend.bash_intercept import (
    SUBMIT_EXPANSION_ERROR,
    SUBMIT_UNPARSED_ERROR,
    extract_notify_coordinator,
    parse_submit_flag,
    submit_flag_attempted,
    submit_flag_suffix,
)
from backend.continue_prompt import build_continue_prompt
from backend.cost_tracker import CostTracker, usage_from_provider
from backend.loop_detect import LoopDetector
from backend.models import model_id_from_spec
from backend.output_types import solver_output_json_schema
from backend.prompts import ChallengeMeta, build_prompt
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


class ClaudeSolver:
    """Claude Agent SDK solver using native tools redirected to Docker sandbox."""

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
        # Swarm picker wins. /connect model_id is only the fallback when the
        # spec has no id (custom Claude URL with an empty catalog).
        custom_model = (getattr(settings, "anthropic_model_id", "") or "").strip()
        spec_model = model_id_from_spec(model_spec)
        self.model_id = spec_model or custom_model
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

        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._accepted_flags: list[str] = []
        self._findings = ""
        self._cost_usd = 0.0
        self._cost_reported = False
        self._bump_insights: str | None = None

    @staticmethod
    def _bash_timeout_s(tool_input: dict) -> int:
        """Claude Bash may pass timeout in seconds or milliseconds."""
        for key in ("timeout", "timeout_ms"):
            raw = tool_input.get(key)
            if raw is None:
                continue
            try:
                val = int(raw)
            except (TypeError, ValueError):
                continue
            if val > 1000:  # treat as ms
                return max(5, min(val // 1000, 900))
            return max(5, min(val, 900))
        return 120

    def _host_cat_result(self, text: str, tool_input: dict) -> dict:
        """Surface sandbox output via a host-side cat (PreToolUse runs on host)."""
        fd, path = tempfile.mkstemp(prefix="ctf-claude-bash-", suffix=".txt")
        try:
            Path(path).write_text(text, encoding="utf-8", errors="replace")
        finally:
            import os

            os.close(fd)
        cmd = f"cat {shlex.quote(path)}; rm -f {shlex.quote(path)}"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {**tool_input, "command": cmd},
            }
        }

    async def _handle_submit(self, flag_val: str) -> str:
        flag_val = (flag_val or "").strip()
        if not flag_val:
            return "Empty flag — nothing to submit."
        if self.submit_fn:
            display, confirmed = await self.submit_fn(flag_val)
        else:
            from backend.flags import normalize_flags_required
            from backend.tools.core import do_submit_flag

            display, confirmed = await do_submit_flag(
                self.meta.name,
                flag_val,
                already_accepted=list(self._accepted_flags),
                required=normalize_flags_required(getattr(self.meta, "flags_required", 1)),
                challenge_dir=self.challenge_dir,
                auto_confirm=bool(getattr(self.settings, "auto_confirm_flags", False)),
            )
        if (
            display.startswith(("ACCEPTED", "CORRECT", "Already accepted"))
            and flag_val
            and flag_val not in self._accepted_flags
        ):
            self._accepted_flags.append(flag_val)
            try:
                from backend.shell.sandbox_session import sync_accepted_flags

                sync_accepted_flags(
                    self._accepted_flags,
                    flags_required=normalize_flags_required(getattr(self.meta, "flags_required", 1)),
                )
            except Exception:
                pass
        if confirmed:
            self._confirmed = True
            self._flag = " | ".join(self._accepted_flags) if self._accepted_flags else flag_val
            self.tracer.event("flag_confirmed", flag=self._flag, step=self._step_count)
        return display

    def _build_solver_mcp(self):
        solver = self

        @tool(
            "submit_flag",
            "Submit a recovered flag candidate (exact string). Human confirms; "
            "ACCEPTED = more needed; CORRECT = done.",
            {"flag": str},
        )
        async def submit_flag(args: dict) -> dict:
            display = await solver._handle_submit(args.get("flag", ""))
            return {"content": [{"type": "text", "text": display}]}

        @tool(
            "notify_coordinator",
            "Send a short strategic note to the competition coordinator.",
            {"message": str},
        )
        async def notify_coordinator(args: dict) -> dict:
            msg = (args.get("message") or "").strip()
            if solver.notify_coordinator and msg:
                await solver.notify_coordinator(msg)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Message sent to coordinator." if msg else "Empty message.",
                    }
                ]
            }

        return create_sdk_mcp_server(
            name="ctf",
            version="1.0.0",
            tools=[submit_flag, notify_coordinator],
        )

    async def start(self) -> None:
        from backend.agents.solver_control import start_sandbox_basics

        container_arch, distfile_names = await start_sandbox_basics(
            self.sandbox, self.meta, self.challenge_dir
        )
        sandbox_preamble = (
            "IMPORTANT: You are running inside a Docker sandbox. "
            "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
            "workspace at /challenge/workspace/. Do NOT use any paths outside /challenge/. "
            "All bash commands run inside the container via docker exec. "
            "Use bash for everything: cat/head to read files, tee/echo> to write, find/grep to search. "
            "Prefer the submit_flag tool when you recover a candidate "
            "(any format; human confirms; ACCEPTED = more needed; CORRECT = done). "
            "Bash `submit_flag 'FLAG'` also works (even after `cd … &&`). "
            "Use notify_coordinator to message the coordinator.\n\n"
        )
        # Claude MCP only exposes submit_flag / notify_coordinator — not view_image
        # / webhook_*. Keep prompt honest (bash-oriented image/web hints).
        system_prompt = sandbox_preamble + build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
            has_named_tools=False,
        )

        mcp_server = self._build_solver_mcp()
        mcp_submit = "mcp__ctf__submit_flag"
        mcp_notify = "mcp__ctf__notify_coordinator"

        # PreToolUse hook: rewrite Bash commands to run in the sandbox container.
        # Block Read/Write/Edit — model should use bash for file access.
        async def sandbox_redirect(input_data, tool_use_id, context):
            try:
                return await _sandbox_redirect_inner(input_data, tool_use_id, context)
            except Exception as e:
                logger.warning(f"[{self.agent_name}] PreToolUse hook error: {e}")
                return {}

        async def _sandbox_redirect_inner(input_data, tool_use_id, context):
            if input_data.get("hook_event_name") != "PreToolUse":
                return {}

            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {})

            # Step counting and loop detection for all tools
            self._step_count += 1
            self.tracer.tool_call(tool_name, tool_input, self._step_count)
            live_json(
                f"{self.agent_name} tool#{self._step_count} → {tool_name}",
                tool_input,
                limit=1500,
            )
            loop_status = self.loop_detector.check(tool_name, str(tool_input)[:200])
            if loop_status == "break":
                self.tracer.event("loop_break", tool=tool_name, step=self._step_count)
                msg = "Loop detected — change arguments or tool flags before repeating."
                _live(f"{self.agent_name} tool#{self._step_count} ✗ {tool_name}", msg)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": msg,
                    }
                }
            warn_msg = ""
            if loop_status == "warn":
                from backend.loop_detect import LOOP_WARNING_MESSAGE

                warn_msg = LOOP_WARNING_MESSAGE

            # MCP harness tools execute in-process — allow through.
            if tool_name in (mcp_submit, mcp_notify):
                return {"systemMessage": warn_msg} if warn_msg else {}

            if tool_name == "Bash":
                command = tool_input.get("command", "")

                # Intercept submit_flag anywhere in a compound command
                parsed_submit = parse_submit_flag(command)
                if parsed_submit is not None:
                    if parsed_submit.has_expansion:
                        result_msg = SUBMIT_EXPANSION_ERROR
                    else:
                        result_msg = await self._handle_submit(parsed_submit.value)
                        suffix = submit_flag_suffix(command, parsed_submit)
                        if suffix:
                            from backend.tools.core import do_bash

                            more = await do_bash(
                                self.sandbox,
                                suffix,
                                timeout_seconds=self._bash_timeout_s(tool_input),
                            )
                            result_msg = f"{result_msg}\n{more}"
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": {
                                **tool_input,
                                "command": f"echo {shlex.quote(result_msg)}",
                            },
                        }
                    }
                if submit_flag_attempted(command):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": {
                                **tool_input,
                                "command": f"echo {shlex.quote(SUBMIT_UNPARSED_ERROR)}",
                            },
                        }
                    }

                # Intercept notify_coordinator anywhere in a compound command
                notify_msg = extract_notify_coordinator(command)
                if notify_msg is not None and self.notify_coordinator:
                    await self.notify_coordinator(notify_msg)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": {
                                **tool_input,
                                "command": "echo 'Message sent to coordinator.'",
                            },
                        }
                    }

                # Run through sandbox.exec (pack ensure, SOCKS wrap, harden).
                from backend.tools.core import do_bash

                out = await do_bash(
                    self.sandbox,
                    command,
                    timeout_seconds=self._bash_timeout_s(tool_input),
                )
                fail_status = self.loop_detector.check_result("Bash", out)
                if fail_status == "oom_break":
                    from backend.loop_detect import OOM_STUCK_MESSAGE

                    out = f"{out}\n\n{OOM_STUCK_MESSAGE}"
                result = self._host_cat_result(out, tool_input)
                if warn_msg:
                    result["systemMessage"] = warn_msg
                return result

            if tool_name in ("WebFetch", "WebSearch"):
                return {"systemMessage": warn_msg} if warn_msg else {}

            # Everything else is denied — Glob/Grep/Read/Write/Edit/Agent/etc.
            # would run on the host filesystem, breaking sandbox isolation.
            # The model should use find/grep/cat/tee via bash instead.
            redirect_hint = ""
            if tool_name in ("Glob", "Grep"):
                redirect_hint = (
                    " Use `find` or `grep` via bash instead — those run in the container."
                )
            elif tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
                redirect_hint = " Use cat/head/tail to read, and tee/cat>file to write via bash."

            deny_reason = f"{tool_name} blocked — use bash for all operations inside the sandbox."
            _live(f"{self.agent_name} tool#{self._step_count} ✗ {tool_name}", deny_reason)
            return {
                "systemMessage": f"{tool_name} is not available — all work happens inside the Docker container.{redirect_hint}"
                if redirect_hint
                else "",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": deny_reason,
                },
            }

        async def trace_post_tool(input_data, tool_use_id, context):
            try:
                return await _trace_post_tool_inner(input_data, tool_use_id, context)
            except Exception as e:
                logger.warning(f"[{self.agent_name}] PostToolUse hook error: {e}")
                return {}

        async def _trace_post_tool_inner(input_data, tool_use_id, context):
            if input_data.get("hook_event_name") != "PostToolUse":
                return {}
            tool_name = input_data.get("tool_name", "?")
            response_str = str(input_data.get("tool_response", ""))
            self.tracer.tool_result(tool_name, response_str[:500], self._step_count)
            _live(
                f"{self.agent_name} tool#{self._step_count} ← {tool_name}",
                response_str,
                limit=2000,
            )

            if self._step_count % 5 == 0 and self.message_bus:
                from backend.tools.core import do_check_findings

                findings = await do_check_findings(
                    self.message_bus, getattr(self, "runner_id", None) or self.model_spec
                )
                if findings and "No new findings" not in findings:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": findings,
                        }
                    }
            return {}

        from backend.models import effort_from_spec

        effort = effort_from_spec(self.model_spec)

        # Clear CLAUDECODE to prevent nested-session rejection when run from coordinator.
        # Propagate a custom Anthropic base URL (/connect → auth.json metadata).
        env = {"CLAUDECODE": ""}
        api_key = getattr(self.settings, "anthropic_api_key", "") or ""
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        base_url = getattr(self.settings, "anthropic_base_url", "") or ""
        if base_url:
            from backend.shell.credentials import normalize_anthropic_base_url

            env["ANTHROPIC_BASE_URL"] = normalize_anthropic_base_url(base_url)
        options = ClaudeAgentOptions(
            model=self.model_id,
            system_prompt=system_prompt,
            effort=effort,
            env=env,
            mcp_servers={"ctf": mcp_server},
            allowed_tools=[
                "Bash",
                "WebFetch",
                "WebSearch",
                mcp_submit,
                mcp_notify,
            ],
            permission_mode="bypassPermissions",
            output_format={"type": "json_schema", "schema": solver_output_json_schema()},
            hooks={
                "PreToolUse": [
                    HookMatcher(hooks=[sandbox_redirect]),
                ],
                "PostToolUse": [
                    HookMatcher(hooks=[trace_post_tool]),
                ],
            },
        )

        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Claude SDK solver started")

    def _finish_findings(self) -> str:
        """Surface accepted flags + findings even when submit/race did not finish."""
        parts: list[str] = []
        if self._accepted_flags:
            parts.append("Accepted flag(s): " + " | ".join(self._accepted_flags))
        if self._findings:
            parts.append(self._findings)
        return "\n".join(parts)[:2000]

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._client:
            await self.start()
        assert self._client is not None

        t0 = time.monotonic()
        cost_before = self._cost_usd
        steps_before = self._step_count

        try:
            if self._bump_insights:
                prompt = build_continue_prompt(
                    accepted_flags=self._accepted_flags,
                    flags_required=getattr(self.meta, "flags_required", 1),
                    bump_insights=self._bump_insights,
                )
                self._bump_insights = None
            elif self._session_id:
                prompt = build_continue_prompt(
                    accepted_flags=self._accepted_flags,
                    flags_required=getattr(self.meta, "flags_required", 1),
                )
            else:
                prompt = "Solve this CTF challenge."

            _live(self.agent_name, "── turn start ──")
            await self._client.query(prompt)

            async for message in self._client.receive_response():
                if self.cancel_event.is_set():
                    break

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            think = (
                                getattr(block, "thinking", None) or getattr(block, "text", "") or ""
                            )
                            if str(think).strip():
                                _live(f"{self.agent_name} think", str(think))
                        elif isinstance(block, TextBlock):
                            self._findings = block.text[:2000]
                            _live(f"{self.agent_name} ai", block.text)
                    # Mid-turn usage preview (provider-reported only). Gateways
                    # may send Anthropic or OpenAI-shaped fields; normalize both.
                    parsed = usage_from_provider(message)
                    if parsed["input"] or parsed["output"] or parsed["cache_read"]:
                        self.cost_tracker.publish_with_pending(
                            input_tokens=parsed["input"],
                            output_tokens=parsed["output"],
                            cache_read_tokens=parsed["cache_read"],
                        )

                elif isinstance(message, ResultMessage):
                    self._session_id = message.session_id
                    parsed = usage_from_provider(message)
                    turn_cost = getattr(message, "total_cost_usd", None)
                    if turn_cost is None:
                        turn_cost = parsed["cost_usd"]
                    if turn_cost is not None:
                        self._cost_usd += float(turn_cost)
                        self._cost_reported = True
                    self.cost_tracker.record_tokens(
                        self.agent_name,
                        self.model_id,
                        input_tokens=parsed["input"],
                        output_tokens=parsed["output"],
                        cache_read_tokens=parsed["cache_read"],
                        provider_spec="claude-sdk",
                        duration_seconds=time.monotonic() - t0,
                        reported_cost_usd=float(turn_cost) if turn_cost is not None else None,
                    )

                    output = getattr(message, "structured_output", None)
                    if output and output.get("type") == "flag_found":
                        self._flag = output.get("flag")
                        self._findings = f"Flag found via {output.get('method', '?')}: {self._flag}"
                        # JSON alone does not confirm — only submit_flag does.

                # tool_progress / other SDK noise — ignore silently

            _live(self.agent_name, "── turn end ──")
            turn_event: dict = {"duration": round(time.monotonic() - t0, 1)}
            if self._cost_reported:
                turn_event["cost_usd_reported"] = round(self._cost_usd, 4)
            self.tracer.event("turn_complete", **turn_event)

            # Also check if flag was confirmed via submit_flag in bash
            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)
            # Report per-run metrics so broken-solver detection works
            run_steps = self._step_count - steps_before
            run_cost = self._cost_usd - cost_before
            return self._result(GAVE_UP, run_steps=run_steps, run_cost=run_cost)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as e:
            error_str = str(e)
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            from backend.agents.solver_control import classify_turn_error
            from backend.solver_base import QUOTA_ERROR

            status = classify_turn_error(error_str)
            if status == QUOTA_ERROR:
                _live(
                    self.agent_name,
                    "provider/auth error — check API key or billing (details suppressed)",
                )
            return self._result(status)

    def bump(self, insights: str) -> None:
        from backend.agents.solver_control import stash_bump

        stash_bump(self, insights)
        logger.info(f"[{self.agent_name}] Bumped with insights (session {self._session_id})")

    def _result(
        self, status: str, run_steps: int | None = None, run_cost: float | None = None
    ) -> SolverResult:
        finish_kw: dict = {
            "status": status,
            "flag": self._flag,
            "confirmed": self._confirmed,
        }
        if self._cost_reported:
            finish_kw["cost_usd_reported"] = round(self._cost_usd, 4)
        self.tracer.event("finish", **finish_kw)
        # Use per-run metrics if provided, so broken-solver detection works across bumps
        return SolverResult(
            flag=self._flag
            if self._confirmed
            else (" | ".join(self._accepted_flags) if self._accepted_flags else self._flag),
            status=status,
            findings_summary=self._finish_findings(),
            step_count=run_steps if run_steps is not None else self._step_count,
            # Provider-reported USD only (Claude SDK); 0 means unknown/not reported.
            cost_usd=run_cost if run_cost is not None else self._cost_usd,
            log_path=self.tracer.path,
        )

    async def produce_writeup(self, prompt: str | None = None) -> str:
        """One more turn: narrative writeup for the operator recap (no tools expected)."""
        from backend.writeup import WRITEUP_PROMPT

        if self._client is None:
            return ""
        parts: list[str] = []
        _live(self.agent_name, "── writeup ──")
        await self._client.query(prompt or WRITEUP_PROMPT)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and str(block.text or "").strip():
                        parts.append(block.text.strip())
                        # No per-token live stream — see cursor_solver.produce_writeup.
        from backend.writeup import join_streamed_text_parts

        return join_streamed_text_parts(parts)

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
        if self.sandbox:
            await self.sandbox.stop()
