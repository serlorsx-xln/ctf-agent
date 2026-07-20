"""Cursor SDK coordinator — manages the competition via custom tools."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from cursor_sdk import AsyncAgent, CustomTool, CustomToolContext, LocalAgentOptions

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
from backend.agents.cursor_runtime import acquire_client, release_client, resolve_api_key
from backend.config import Settings
from backend.deps import CoordinatorDeps

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
- Use ONLY the custom coordinator tools. Do not use host Shell/Read/Write.

You will receive event messages. Respond with tool calls to manage the competition.
"""


def _build_coordinator_tools(deps: CoordinatorDeps) -> dict[str, CustomTool]:
    async def fetch_challenges(_args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_fetch_challenges(deps)

    async def get_solve_status(_args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_get_solve_status(deps)

    async def spawn_swarm(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_spawn_swarm(deps, args["challenge_name"])

    async def check_swarm_status(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_check_swarm_status(deps, args["challenge_name"])

    async def submit_flag(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_submit_flag(deps, args["challenge_name"], args["flag"])

    async def kill_swarm(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_kill_swarm(deps, args["challenge_name"])

    async def bump_agent(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_bump_agent(
            deps, args["challenge_name"], args["model_spec"], args["insights"]
        )

    async def broadcast(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_broadcast(deps, args["challenge_name"], args["message"])

    async def read_solver_trace(args: Mapping[str, Any], _ctx: CustomToolContext) -> str:
        return await do_read_solver_trace(
            deps,
            args["challenge_name"],
            args["model_spec"],
            int(args.get("last_n", 20) or 20),
        )

    return {
        "fetch_challenges": CustomTool(
            description="List local challenges with name, status, and description.",
            input_schema={"type": "object", "properties": {}},
            execute=fetch_challenges,
        ),
        "get_solve_status": CustomTool(
            description="Check which challenges are solved and which swarms are running.",
            input_schema={"type": "object", "properties": {}},
            execute=get_solve_status,
        ),
        "spawn_swarm": CustomTool(
            description="Launch all solver models on a challenge.",
            input_schema={
                "type": "object",
                "properties": {"challenge_name": {"type": "string"}},
                "required": ["challenge_name"],
            },
            execute=spawn_swarm,
        ),
        "check_swarm_status": CustomTool(
            description="Get per-agent progress for a swarm.",
            input_schema={
                "type": "object",
                "properties": {"challenge_name": {"type": "string"}},
                "required": ["challenge_name"],
            },
            execute=check_swarm_status,
        ),
        "submit_flag": CustomTool(
            description="Accept a recovered flag. Ends the run only when all required flags are accepted.",
            input_schema={
                "type": "object",
                "properties": {
                    "challenge_name": {"type": "string"},
                    "flag": {"type": "string"},
                },
                "required": ["challenge_name", "flag"],
            },
            execute=submit_flag,
        ),
        "kill_swarm": CustomTool(
            description="Cancel all agents for a challenge.",
            input_schema={
                "type": "object",
                "properties": {"challenge_name": {"type": "string"}},
                "required": ["challenge_name"],
            },
            execute=kill_swarm,
        ),
        "bump_agent": CustomTool(
            description="Send targeted insights to a stuck agent.",
            input_schema={
                "type": "object",
                "properties": {
                    "challenge_name": {"type": "string"},
                    "model_spec": {"type": "string"},
                    "insights": {"type": "string"},
                },
                "required": ["challenge_name", "model_spec", "insights"],
            },
            execute=bump_agent,
        ),
        "broadcast": CustomTool(
            description="Broadcast a strategic hint to ALL solvers on a challenge.",
            input_schema={
                "type": "object",
                "properties": {
                    "challenge_name": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["challenge_name", "message"],
            },
            execute=broadcast,
        ),
        "read_solver_trace": CustomTool(
            description="Read recent trace events from a specific solver.",
            input_schema={
                "type": "object",
                "properties": {
                    "challenge_name": {"type": "string"},
                    "model_spec": {"type": "string"},
                    "last_n": {"type": "integer", "default": 20},
                },
                "required": ["challenge_name", "model_spec"],
            },
            execute=read_solver_trace,
        ),
    }


async def run_cursor_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Run the Cursor SDK coordinator with the shared event loop."""
    cost_tracker, deps = build_deps(settings, model_specs, challenges_root)
    deps.msg_port = msg_port

    api_key = resolve_api_key(settings)
    resolved_model = coordinator_model or "composer-2.5"
    # Allow cursor/composer-2.5 style specs
    if "/" in resolved_model:
        resolved_model = resolved_model.split("/", 1)[1]

    client = await acquire_client(workspace=".")
    agent: AsyncAgent | None = None
    try:
        agent = await AsyncAgent.create(
            client=client,
            model=resolved_model,
            api_key=api_key,
            name="ctf-coordinator",
            local=LocalAgentOptions(
                cwd=".",
                setting_sources=[],
                custom_tools=_build_coordinator_tools(deps),
            ),
        )
        logger.info(
            "Cursor coordinator started (agent=%s, model=%s)",
            agent.agent_id,
            resolved_model,
        )

        async def turn_fn(msg: str) -> None:
            assert agent is not None
            prompt = f"{COORDINATOR_PROMPT}\n\n---\n\n{msg}"
            logger.debug("Coordinator query: %s", msg[:200])
            run = await agent.send(prompt)
            result = await run.wait()
            logger.info(
                "Cursor coordinator turn done (status=%s, agent=%s)",
                result.status,
                agent.agent_id,
            )

        return await run_event_loop(deps, cost_tracker, turn_fn)
    finally:
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                pass
        await release_client()
