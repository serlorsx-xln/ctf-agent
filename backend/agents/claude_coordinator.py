"""Claude Agent SDK coordinator — uses the shared event loop with a Claude SDK client."""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.coordinator_core import (
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.deps import CoordinatorDeps

try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        ResultMessage,
        create_sdk_mcp_server,
        tool,
    )
except ImportError as _claude_sdk_err:
    _CLAUDE_SDK_IMPORT_ERROR: ImportError | None = _claude_sdk_err
    ClaudeAgentOptions = object  # type: ignore[misc,assignment]
    ClaudeSDKClient = object  # type: ignore[misc,assignment]
    HookMatcher = object  # type: ignore[misc,assignment]
    ResultMessage = object  # type: ignore[misc,assignment]

    def create_sdk_mcp_server(*_a, **_k):  # type: ignore[no-redef]
        raise ImportError(_CLAUDE_EXTRA_HINT) from _CLAUDE_SDK_IMPORT_ERROR

    def tool(*_a, **_k):  # type: ignore[no-redef]
        raise ImportError(_CLAUDE_EXTRA_HINT) from _CLAUDE_SDK_IMPORT_ERROR
else:
    _CLAUDE_SDK_IMPORT_ERROR = None

_CLAUDE_EXTRA_HINT = (
    "Claude coordinator requires the 'claude' extra. Install with: uv sync --extra claude"
)

logger = logging.getLogger(__name__)

COORDINATOR_PROMPT = """\
You are a CTF competition coordinator running for the ENTIRE duration of a live competition.
Your job is to maximize the number of challenges solved.

Strategy:
- Spawn swarms for unsolved local challenges (description + status)
- Use read_solver_trace to monitor what each solver is doing and where it's stuck
- When agents are stuck, read their traces, then craft targeted bumps with specific technical guidance
- Use broadcast to share cross-solver insights (e.g. flag format discovery, shared vulnerabilities)

CRITICAL RULES:
- NEVER kill a swarm. Solvers will keep trying indefinitely with different approaches.
  Even when stuck, they often unstick themselves after several bumps. Your job is to
  HELP them, not give up on them. The only time a swarm should die is when the flag
  is confirmed correct.
- When a solver seems stuck, bump it with very specific technical guidance based on
  its trace. Tell it exactly what to try next — specific tools, techniques, approaches.
- Cost is not a concern. Keep all swarms running.

You will receive event messages. Respond with tool calls to manage the competition.
"""


def _text(s: str) -> dict:
    """Wrap a string in the Claude SDK MCP tool return format."""
    return {"content": [{"type": "text", "text": s}]}


def _build_coordinator_mcp(deps: CoordinatorDeps):
    """Build MCP server — thin wrappers around coordinator_core functions."""

    @tool("fetch_challenges", "List local challenges with name, status, and description.", {})
    async def fetch_challenges(args: dict) -> dict:
        return _text(await do_fetch_challenges(deps))

    @tool("get_solve_status", "Check which challenges are solved and which swarms are running.", {})
    async def get_solve_status(args: dict) -> dict:
        return _text(await do_get_solve_status(deps))

    @tool("spawn_swarm", "Launch all solver models on a challenge.", {"challenge_name": str})
    async def spawn_swarm(args: dict) -> dict:
        return _text(await do_spawn_swarm(deps, args["challenge_name"]))

    @tool("check_swarm_status", "Get per-agent progress for a swarm.", {"challenge_name": str})
    async def check_swarm_status(args: dict) -> dict:
        return _text(await do_check_swarm_status(deps, args["challenge_name"]))

    @tool(
        "submit_flag",
        "Accept a recovered flag. Ends the run only when all required flags are accepted.",
        {"challenge_name": str, "flag": str},
    )
    async def submit_flag(args: dict) -> dict:
        return _text(await do_submit_flag(deps, args["challenge_name"], args["flag"]))

    @tool("kill_swarm", "Cancel all agents for a challenge.", {"challenge_name": str})
    async def kill_swarm(args: dict) -> dict:
        return _text(await do_kill_swarm(deps, args["challenge_name"]))

    @tool(
        "bump_agent",
        "Send targeted insights to a stuck agent.",
        {"challenge_name": str, "model_spec": str, "insights": str},
    )
    async def bump_agent(args: dict) -> dict:
        return _text(
            await do_bump_agent(deps, args["challenge_name"], args["model_spec"], args["insights"])
        )

    @tool(
        "broadcast",
        "Broadcast a strategic hint to ALL solvers on a challenge.",
        {"challenge_name": str, "message": str},
    )
    async def broadcast(args: dict) -> dict:
        return _text(await do_broadcast(deps, args["challenge_name"], args["message"]))

    @tool(
        "read_solver_trace",
        "Read recent trace events from a specific solver. Use this to understand what a solver is doing, what it tried, and where it's stuck.",
        {"challenge_name": str, "model_spec": str, "last_n": int},
    )
    async def read_solver_trace(args: dict) -> dict:
        return _text(
            await do_read_solver_trace(
                deps, args["challenge_name"], args["model_spec"], args.get("last_n", 20)
            )
        )

    return create_sdk_mcp_server(
        name="coordinator",
        version="1.0.0",
        tools=[
            fetch_challenges,
            get_solve_status,
            spawn_swarm,
            check_swarm_status,
            submit_flag,
            kill_swarm,
            bump_agent,
            broadcast,
            read_solver_trace,
        ],
    )


async def run_claude_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Run the Claude Agent SDK coordinator with the shared event loop."""
    if _CLAUDE_SDK_IMPORT_ERROR is not None:
        raise ImportError(_CLAUDE_EXTRA_HINT) from _CLAUDE_SDK_IMPORT_ERROR
    cost_tracker, deps = build_deps(settings, model_specs, challenges_root)
    deps.msg_port = msg_port

    mcp_server = _build_coordinator_mcp(deps)
    resolved_model = coordinator_model or "claude-opus-4-6"

    allowed = {
        "mcp__coordinator__fetch_challenges",
        "mcp__coordinator__get_solve_status",
        "mcp__coordinator__spawn_swarm",
        "mcp__coordinator__check_swarm_status",
        "mcp__coordinator__submit_flag",
        "mcp__coordinator__kill_swarm",
        "mcp__coordinator__bump_agent",
        "mcp__coordinator__broadcast",
        "mcp__coordinator__read_solver_trace",
        "ToolSearch",
        "TaskCreate",
        "TaskUpdate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
    }

    async def enforce_allowlist(input_data, tool_use_id, context):
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        tool = input_data.get("tool_name", "")
        if tool in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{tool} not available to coordinator.",
            }
        }

    options = ClaudeAgentOptions(
        model=resolved_model,
        system_prompt=COORDINATOR_PROMPT,
        env={"CLAUDECODE": ""},
        mcp_servers={"coordinator": mcp_server},
        allowed_tools=list(allowed),
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [HookMatcher(hooks=[enforce_allowlist])],
        },
    )

    async with ClaudeSDKClient(options=options) as client:

        async def turn_fn(msg: str) -> None:
            logger.debug(f"Coordinator query: {msg[:200]}")
            await client.query(msg)
            msg_count = 0
            async for message in client.receive_response():
                msg_count += 1
                msg_type = type(message).__name__
                logger.debug(f"Coordinator received: {msg_type}")
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", None)
                    session = getattr(message, "session_id", None)
                    if cost is not None:
                        logger.info(
                            "Claude coordinator turn done "
                            "(messages=%s, cost=$%.4f reported, session=%s)",
                            msg_count,
                            float(cost),
                            session,
                        )
                    else:
                        logger.info(
                            "Claude coordinator turn done (messages=%s, session=%s)",
                            msg_count,
                            session,
                        )
            if msg_count == 0:
                logger.warning("Coordinator turn produced no messages!")

        return await run_event_loop(deps, cost_tracker, turn_fn)
