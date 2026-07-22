"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.config import Settings
from backend.models import DEFAULT_MODELS

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X")
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command()
@click.option(
    "--image",
    default=None,
    help="Force sandbox image (optional). Default: auto-select from challenge files (L0 + packs).",
)
@click.option(
    "--models",
    multiple=True,
    help="Model specs (repeatable, or comma-separated in one arg). "
    "Same model multiple times = parallel instances. "
    "Shorthand: cursor/grok-4.5*3",
)
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option(
    "--coordinator-model",
    default=None,
    help="Model for coordinator (default: composer-2.5 for cursor)",
)
@click.option(
    "--coordinator",
    default="cursor",
    type=click.Choice(["cursor", "claude", "codex"]),
    help="Coordinator backend (default: cursor)",
)
@click.option("--max-challenges", default=10, type=int, help="Max challenges solved concurrently")
@click.option("--msg-port", default=9400, type=int, help="Operator message port (default 9400)")
@click.option(
    "--auto-confirm-flags",
    is_flag=True,
    help="Skip interactive flag confirmation (also: CTF_AUTO_CONFIRM_FLAGS=1)",
)
@click.option(
    "--pack",
    "packs",
    multiple=True,
    help="Force prefetch pack id(s); repeatable (overrides auto-detect)",
)
@click.option(
    "--eval-out",
    default="",
    help="Write JSON eval summary to PATH after a single-challenge run",
)
@click.option(
    "--eval-max-wall-s",
    default=None,
    type=float,
    help="Cancel single-challenge run after this many wall-clock seconds",
)
@click.option(
    "--eval-max-usd",
    default=None,
    type=float,
    help="Cancel single-challenge run after this many reported USD",
)
@click.option(
    "--eval-strict-packs",
    is_flag=True,
    help="Fail closed if pack preflight/bootstrap errors (default: fail-soft)",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    image: str | None,
    models: tuple[str, ...],
    challenge: str | None,
    challenges_dir: str,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    msg_port: int,
    auto_confirm_flags: bool,
    packs: tuple[str, ...],
    eval_out: str,
    eval_max_wall_s: float | None,
    eval_max_usd: float | None,
    eval_strict_packs: bool,
    verbose: bool,
) -> None:
    """Artemis — multi-model CTF solver swarm.

    Flag candidates are confirmed by you locally (no external scoreboard).
    Run without --challenge to start the full coordinator over challenges/
    (Ctrl+C to stop).
    """
    _setup_logging(verbose)

    settings = Settings()
    if image:
        settings.sandbox_image = image
        settings.sandbox_image_locked = True
    else:
        settings.sandbox_image_locked = False
    settings.max_concurrent_challenges = max_challenges
    settings.auto_confirm_flags = auto_confirm_flags or settings.auto_confirm_flags
    if packs:
        settings.force_packs = list(packs)
    if eval_out:
        settings.eval_out = eval_out
    if eval_max_wall_s is not None:
        settings.eval_max_wall_s = eval_max_wall_s
    if eval_max_usd is not None:
        settings.eval_max_usd = eval_max_usd
    if eval_strict_packs:
        settings.eval_strict_packs = True

    model_specs = list(models) if models else list(DEFAULT_MODELS)
    if models:
        from backend.models import expand_model_cli_args

        model_specs = expand_model_cli_args(list(models))

    console.print("[bold]Artemis[/bold]")
    console.print(f"  Models: {', '.join(model_specs)}")
    if image:
        console.print(f"  Image: {settings.sandbox_image} (forced via --image)")
    elif challenge:
        from backend.tool_router import resolve_sandbox_image

        auto_image, detected = resolve_sandbox_image(
            challenge,
            default_image=settings.sandbox_image,
        )
        show_packs = settings.force_packs or detected
        pack_note = f"; prefetch packs={','.join(show_packs)}" if show_packs else ""
        forced = " (forced)" if settings.force_packs else ""
        console.print(f"  Image: {auto_image} (L0{pack_note}{forced})")
    else:
        console.print(
            f"  Image: L0 default={settings.sandbox_image} (packs loaded additively per challenge)"
        )
    console.print(f"  Max challenges: {max_challenges}")
    if settings.eval_out or settings.eval_max_wall_s or settings.eval_max_usd:
        bits = []
        if settings.eval_max_wall_s:
            bits.append(f"wall≤{settings.eval_max_wall_s}s")
        if settings.eval_max_usd:
            bits.append(f"usd≤{settings.eval_max_usd}")
        if settings.eval_out:
            bits.append(f"out={settings.eval_out}")
        if settings.eval_strict_packs:
            bits.append("strict-packs")
        console.print(f"  Eval: {', '.join(bits)}")
    if settings.auto_confirm_flags:
        console.print("  Flag submit: local + auto-confirm (no human prompt)")
    else:
        console.print("  Flag submit: local + human confirm (y/N on each candidate)")
    console.print()

    if challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, max_challenges))
    else:
        asyncio.run(
            _run_coordinator(
                settings,
                model_specs,
                challenges_dir,
                coordinator_model,
                coordinator,
                max_challenges,
                msg_port,
            )
        )


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    max_challenges: int,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.challenge import load_challenge
    from backend.cost_tracker import CostTracker
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    if not challenge_path.is_dir():
        console.print(f"[red]Not a directory: {challenge_dir}[/red]")
        sys.exit(1)

    try:
        meta = load_challenge(challenge_path)
    except Exception as e:
        console.print(f"[red]Failed to load challenge: {e}[/red]")
        sys.exit(1)

    console.print(f"[bold]Challenge:[/bold] {meta.name}")
    if meta.connection_info:
        console.print(f"  Endpoint: {meta.connection_info}")

    cost_tracker = CostTracker()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path.resolve()),
        meta=meta,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
    )

    result = await swarm.run()
    from backend.solver_base import FLAG_FOUND, GAVE_UP

    if result and result.status == FLAG_FOUND:
        console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
    elif result and result.flag and result.status == GAVE_UP:
        console.print(f"\n[bold yellow]Partial progress:[/bold yellow] {result.flag}")
        if result.findings_summary:
            console.print(result.findings_summary[:1500])
    elif result and result.findings_summary:
        console.print("\n[bold red]No flag accepted.[/bold red]")
        console.print("[dim]Last findings:[/dim]")
        console.print(result.findings_summary[:1500])
    else:
        console.print("\n[bold red]No flag found.[/bold red]")

    if swarm.confirmed_flags and not (result and result.status == FLAG_FOUND):
        console.print(f"[dim]Accepted this run: {' | '.join(swarm.confirmed_flags)}[/dim]")

    console.print("\n[bold]Usage Summary:[/bold]")
    for agent_name in cost_tracker.by_agent:
        console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
    console.print(f"  [bold]Total: {cost_tracker.format_total()}[/bold]")


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    msg_port: int = 0,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    console.print(f"[bold]Starting coordinator ({coordinator_backend}, Ctrl+C to stop)...[/bold]\n")

    if coordinator_backend == "cursor":
        from backend.agents.cursor_coordinator import run_cursor_coordinator

        results = await run_cursor_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )
    elif coordinator_backend == "codex":
        from backend.agents.codex_coordinator import run_codex_coordinator

        results = await run_codex_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )
    else:
        from backend.agents.claude_coordinator import run_claude_coordinator

        results = await run_claude_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total usage: {results.get('usage_summary', 'n/a')}[/bold]")


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    import json
    import urllib.request

    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
