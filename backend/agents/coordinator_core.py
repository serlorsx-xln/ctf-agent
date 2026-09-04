"""Shared coordinator tool logic — called by Cursor / Claude / Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.flags import is_counted_accept_message, normalize_flags_required
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)


def resolve_swarm_solver(swarm, key: str):
    """Find a solver by ``runner_id``, then by unique ``model_spec``."""
    solvers = getattr(swarm, "solvers", None) or {}
    if key in solvers:
        return solvers[key]
    matches = []
    for rid, solver in solvers.items():
        spec = getattr(solver, "model_spec", None)
        if spec == key or str(rid).split("#", 1)[0] == key:
            matches.append(solver)
    if len(matches) == 1:
        return matches[0]
    return None


def _solved_names(deps: CoordinatorDeps) -> set[str]:
    """Challenge names that are fully complete (not partial ACCEPTED progress)."""
    solved: set[str] = set()
    for name, entry in deps.results.items():
        if isinstance(entry, dict) and entry.get("complete") is False:
            continue
        solved.add(name)
    return solved


def _scan_local_challenges(deps: CoordinatorDeps) -> list[dict]:
    """List challenges from the local challenges_root directory."""
    from backend.challenge import is_challenge_dir, load_challenge

    root = Path(deps.challenges_root)
    solved = _solved_names(deps)
    result: list[dict] = []
    if not root.is_dir():
        return result
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not is_challenge_dir(d):
            continue
        try:
            meta = load_challenge(d)
        except Exception:
            continue
        deps.challenge_dirs.setdefault(meta.name, str(d))
        deps.challenge_metas.setdefault(meta.name, meta)
        result.append(
            {
                "name": meta.name,
                "status": "SOLVED" if meta.name in solved else "unsolved",
                "description": (meta.description or "")[:200],
                "path": str(d),
            }
        )
    return result


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    return json.dumps(_scan_local_challenges(deps), indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = sorted(_solved_names(deps))
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps({"solved": solved, "active_swarms": swarm_status}, indent=2)


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    # Retire ALL finished swarms before checking capacity
    finished = [
        name
        for name, swarm in deps.swarms.items()
        if swarm.cancel_event.is_set()
        or (name in deps.swarm_tasks and deps.swarm_tasks[name].done())
    ]
    for name in finished:
        del deps.swarms[name]
        deps.swarm_tasks.pop(name, None)

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return (
            f"At capacity ({active_count}/{deps.max_concurrent_challenges} "
            "challenges running). Wait for one to finish."
        )

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Resolve local challenge directory
    if challenge_name not in deps.challenge_dirs:
        _scan_local_challenges(deps)
    if challenge_name not in deps.challenge_dirs:
        from backend.challenge import is_challenge_dir, load_challenge

        root = Path(deps.challenges_root)
        candidate = root / challenge_name
        if candidate.is_dir() and is_challenge_dir(candidate):
            meta = load_challenge(candidate)
            deps.challenge_dirs[challenge_name] = str(candidate)
            deps.challenge_metas[challenge_name] = meta
            if meta.name != challenge_name:
                deps.challenge_dirs[meta.name] = str(candidate)
                deps.challenge_metas[meta.name] = meta
                challenge_name = meta.name
        else:
            return (
                f"Challenge '{challenge_name}' not found under "
                f"{deps.challenges_root}/ (drop a folder with challenge.txt "
                "and/or files)"
            )

    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        coordinator_inbox=deps.coordinator_inbox,
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "accepted locally",
                "complete": True,
            }

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    """Coordinator flag submit. Only marks SOLVED when the challenge is complete.

    When a swarm is running, go through ``try_submit_flag`` so lock/dedup/cooldown
    match solver submits. Partial progress may be stored with ``complete: False``
    (ignored by the poller); only ``complete: True`` counts as solved.
    """
    swarm = deps.swarms.get(challenge_name)
    normalized = flag.strip()

    if swarm:
        display, complete = await swarm.try_submit_flag(flag, "coordinator")
        accepted = list(swarm.confirmed_flags)
        if complete:
            flag_str = swarm.confirmed_flag or " | ".join(accepted) or normalized
            deps.results[challenge_name] = {
                "flag": flag_str,
                "flags": accepted,
                "submit": "accepted locally (coordinator)",
                "complete": True,
            }
            if not swarm.cancel_event.is_set():
                swarm.kill()
        return display

    from backend.tools.core import do_submit_flag as local_submit

    meta = deps.challenge_metas.get(challenge_name)
    required = normalize_flags_required(getattr(meta, "flags_required", 1) if meta else 1)
    prior = deps.results.get(challenge_name) or {}
    already = list(prior.get("flags") or [])
    auto = bool(getattr(deps.settings, "auto_confirm_flags", False))
    display, complete = await local_submit(
        challenge_name,
        flag,
        already_accepted=already,
        required=required,
        challenge_dir=deps.challenge_dirs.get(challenge_name),
        auto_confirm=auto,
    )
    if complete or is_counted_accept_message(display):
        if is_counted_accept_message(display) and normalized and normalized not in already:
            already = [*already, normalized]
        deps.results[challenge_name] = {
            "flag": " | ".join(already) if already else normalized,
            "flags": already,
            "submit": "accepted locally (coordinator)",
            "complete": complete,
        }
    return display


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(
    deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str
) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = resolve_swarm_solver(swarm, model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(
    deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20
) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = resolve_swarm_solver(swarm, model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(
                        f"step {d.get('step', '?')} CALL {d.get('tool', '?')}: {args_str}"
                    )
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(
                        f"step {d.get('step', '?')} RESULT {d.get('tool', '?')}: {result_str}"
                    )
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(
                        f"** {t}: {json.dumps({k: v for k, v in d.items() if k != 'ts'})}"
                    )
                elif t == "usage":
                    line = f"usage: in={d.get('input_tokens', 0)} out={d.get('output_tokens', 0)}"
                    if d.get("cost_usd") is not None:
                        line += f" cost=${d['cost_usd']:.4f} reported"
                    summary.append(line)
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
