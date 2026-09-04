"""Artemis CLI — interactive TUI (default) + swarm harness."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.config import Settings
from backend.models import DEFAULT_MODELS

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    from backend.log_context import AgentTagFilter

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
    # Parallel solvers share this stream; without the tag their sandbox/boot
    # lines are indistinguishable in the TUI.
    handler.addFilter(AgentTagFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _apply_common_settings(
    settings: Settings,
    *,
    image: str | None,
    auto_confirm_flags: bool,
    packs: tuple[str, ...],
    eval_out: str,
    eval_max_wall_s: float | None,
    eval_max_usd: float | None,
    eval_strict_packs: bool,
    max_challenges: int,
) -> None:
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


def _expand_models(models: tuple[str, ...]) -> list[str]:
    if not models:
        return list(DEFAULT_MODELS)
    from backend.models import normalize_swarm_specs

    return normalize_swarm_specs(list(models))


@click.group(invoke_without_command=True)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Artemis — interactive TUI + Python CTF swarm/sandbox.

    \b
      artemis                  Interactive TUI (requires Bun)
      artemis swarm …          Multi-model swarm harness
      artemis chassis …        Pass-through args to TUI

    Paste a challenge (text and/or path) — Artemis loads it and runs the swarm.
    API keys: /connect in the TUI — do not edit .env by hand.

    \b
      artemis setup            Warm L0 + full Jeopardy packs (Sage, pwn, …)
      artemis setup --lite     Bake only web / steg / forensics
    """
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    if ctx.invoked_subcommand is not None:
        return

    from backend.shell.chassis_launch import (
        bun_missing_message,
        chassis_paths,
        find_bun,
        try_launch_chassis,
    )

    _repo, _chassis, launcher = chassis_paths()
    if not launcher.is_file():
        console.print("[red]Chassis missing:[/red] chassis/bin/artemis not found in this repo.")
        sys.exit(1)
    if find_bun() is None:
        console.print(f"[red]{bun_missing_message()}[/red]")
        sys.exit(1)

    try_launch_chassis([])
    console.print("[red]Failed to exec Artemis TUI.[/red]")
    sys.exit(1)


@main.command("swarm")
@click.option(
    "--image",
    default=None,
    help="Force sandbox image (optional). Default: L0 + packs.",
)
@click.option(
    "--models",
    multiple=True,
    help="Model specs (repeatable). Shorthand: cursor/grok-4.5*3",
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
    help=(
        "Cancel after this many provider-reported USD. Unknown cost "
        "(Cursor often omits it) fails open only when --eval-max-wall-s "
        "is also set; USD-only + unknown cost trips the budget."
    ),
)
@click.option(
    "--eval-strict-packs",
    is_flag=True,
    help="Fail closed if pack preflight/bootstrap errors (default: fail-soft)",
)
@click.option(
    "--flags-required",
    default=None,
    type=int,
    help="Distinct accepted flags needed (default from challenge or 1)",
)
@click.pass_context
def swarm_cmd(
    ctx: click.Context,
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
    flags_required: int | None,
) -> None:
    """Multi-model swarm (CORRECT kills siblings)."""
    verbose = bool(ctx.obj.get("verbose")) if ctx.obj else False
    _setup_logging(verbose)

    settings = Settings()
    _apply_common_settings(
        settings,
        image=image,
        auto_confirm_flags=auto_confirm_flags,
        packs=packs,
        eval_out=eval_out,
        eval_max_wall_s=eval_max_wall_s,
        eval_max_usd=eval_max_usd,
        eval_strict_packs=eval_strict_packs,
        max_challenges=max_challenges,
    )
    model_specs = _expand_models(models)

    from backend.models import missing_credentials_error, missing_swarm_credentials

    missing = missing_swarm_credentials(model_specs, settings)
    if missing:
        console.print(f"[red]{missing_credentials_error(missing)}[/red]")
        sys.exit(1)

    console.print("[bold]Artemis Swarm[/bold]")
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
    elif os.environ.get("ARTEMIS_FLAG_CONFIRM", "").strip() in ("1", "true", "yes"):
        console.print("  Flag submit: local + TUI dialog confirm")
    else:
        console.print("  Flag submit: local + human confirm (y/N on each candidate)")
    console.print()

    if challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, max_challenges, flags_required))
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


@main.command("chassis")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def chassis_cmd(args: tuple[str, ...]) -> None:
    """Pass-through to Artemis TUI with extra args."""
    from backend.shell.chassis_launch import bun_missing_message, find_bun, try_launch_chassis

    if find_bun() is None:
        console.print(f"[red]{bun_missing_message()}[/red]")
        sys.exit(1)
    if not try_launch_chassis(list(args)):
        console.print("[red]Chassis launch failed (missing chassis/bin/artemis).[/red]")
        sys.exit(1)



# Legacy alias for older scripts/docs (``artemis race`` → same as ``swarm``)
main.add_command(swarm_cmd, "race")


def race_main() -> None:
    """Entry for ``ctf-solve`` — flat swarm CLI (backward compatible)."""
    swarm_cmd.main(args=sys.argv[1:], prog_name="ctf-solve", standalone_mode=True)


def _harden_supervised_swarm() -> None:
    """When spawned by the daemon (ARTEMIS_DAEMON_SOCK set), make the swarm
    survive a broken stdout pipe.

    The supervising daemon holds the swarm's stdout pipe; if the daemon dies,
    the pipe breaks. Without this, writes (``print``) raise BrokenPipeError /
    SIGPIPE and can kill the swarm mid-solve. Ignore SIGPIPE and let
    ``live_log._emit`` swallow pipe errors so the swarm keeps running detached
    and keeps teeing to the disk log for adopt-on-restart.
    """
    from backend.stdio_platform import ensure_standard_streams

    ensure_standard_streams()

    from backend.daemon.transport import daemon_configured_in_env

    if not daemon_configured_in_env():
        return
    import signal

    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass


def _ask_flags_via_daemon(challenge_name: str) -> int | None:
    """Ask the TUI (via the daemon socket) how many flags are required.

    Returns the count, or None if the daemon is unreachable (caller falls back
    to the challenge default). Synchronous blocking socket — runs before the
    swarm event loop starts.
    """
    import json as _json
    import select
    import time
    import uuid

    from backend.daemon.auth import with_hello_token
    from backend.daemon.transport import daemon_configured_in_env, sync_connect
    from backend.flags import normalize_flags_required

    if not daemon_configured_in_env():
        return None
    session = os.environ.get("ARTEMIS_SESSION_ID")
    req_id = uuid.uuid4().hex[:12]
    deadline = time.monotonic() + 1800

    from backend.agents.live_log import live

    live("artemis", f"FLAGS_ASK id={req_id} default=1 challenge={challenge_name or 'loaded'}")

    try:
        s = sync_connect(timeout=0.25)
    except OSError:
        return None

    try:
        s.sendall(
            _json.dumps(
                with_hello_token(
                    {"v": 1, "id": None, "type": "hello", "role": "swarm", "session": session}
                )
            ).encode()
            + b"\n"
        )
        s.sendall(
            _json.dumps(
                {
                    "v": 1,
                    "id": req_id,
                    "type": "flags_ask_request",
                    "request_id": req_id,
                    "default": 1,
                    "challenge": challenge_name,
                    "session": session,
                }
            ).encode()
            + b"\n"
        )
        buf = b""
        while time.monotonic() < deadline:
            r, _, _ = select.select([s], [], [], 0.25)
            if not r:
                continue
            try:
                chunk = s.recv(4096)
            except (TimeoutError, OSError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    msg = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id and msg.get("type") in (
                    "flags_ask_request",
                    "error",
                ):
                    if msg.get("type") == "error" or msg.get("ok") is False:
                        return None
                    n = msg.get("n")
                    if n is not None:
                        return normalize_flags_required(int(n))
                    return None
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _print_writeup(lines: list[str]) -> None:
    """Emit the solve recap on a channel the TUI renders in the main chat.

    Uses ``emit_line`` so lines are teed to the swarm disk log. The TUI colors
    Solved-by / headers / steps; keep this channel plain for reliable parsing.
    """
    from backend.agents.live_log import emit_line

    for line in lines:
        emit_line(f"[artemis] summary {line}")


def print_swarm_outcome(swarm, result, *, out: Console | None = None) -> None:
    """Report how the run ended, on lines the TUI can parse.

    ``soft_wrap`` throughout: rich's 80-column wrap used to push the flag onto
    its own line, leaving the TUI showing a bare ``FLAG FOUND:``.
    """

    from backend.daemon.transport import daemon_configured_in_env
    from backend.solver_base import FLAG_FOUND, GAVE_UP, QUOTA_ERROR

    con = out or console
    under_daemon = daemon_configured_in_env()
    if result and result.status == FLAG_FOUND:
        # Under the TUI daemon, CORRECT already carries the flag — another
        # FLAG FOUND line just repeats it. Keep the banner for bare CLI runs.
        if not under_daemon:
            flag = result.flag or " | ".join(swarm.confirmed_flags)
            winners = ", ".join(swarm.solved_by_labels())
            con.print(
                f"\n[bold green]FLAG FOUND:[/bold green] {flag}"
                + (f" [dim](solved by {winners})[/dim]" if winners else ""),
                soft_wrap=True,
            )
        if not getattr(swarm, "_how_emitted", False):
            _print_writeup(swarm.solve_writeup())
            swarm._how_emitted = True
    elif result and result.flag and result.status == GAVE_UP:
        con.print(f"\n[bold yellow]Partial progress:[/bold yellow] {result.flag}", soft_wrap=True)
        if result.findings_summary:
            con.print(result.findings_summary[:1500])
        if not getattr(swarm, "_how_emitted", False):
            _print_writeup(swarm.solve_writeup())
            swarm._how_emitted = True
    elif result and result.status == QUOTA_ERROR:
        from backend.agents.cursor_runtime import humanize_cursor_error

        msg = humanize_cursor_error(result.findings_summary) or (
            "Cursor usage limit reached — switch model or wait for reset"
        )
        con.print(f"\n[bold yellow]Stopped:[/bold yellow] {msg}")
    elif result and result.findings_summary:
        from backend.agents.cursor_runtime import humanize_cursor_error, is_quota_error_message

        summary = result.findings_summary.strip()
        if is_quota_error_message(summary):
            con.print(f"\n[bold yellow]Stopped:[/bold yellow] {humanize_cursor_error(summary)}")
        else:
            con.print("\n[bold red]No flag accepted.[/bold red]")
            con.print("[dim]Last findings:[/dim]")
            con.print(summary[:1500])
    else:
        con.print("\n[bold red]No flag found.[/bold red]")

    if swarm.confirmed_flags and not (result and result.status == FLAG_FOUND):
        con.print(
            f"[dim]Accepted this run: {' | '.join(swarm.confirmed_flags)}[/dim]", soft_wrap=True
        )


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    max_challenges: int,
    flags_required: int | None = None,
) -> None:
    """Run a single challenge with a swarm."""
    _harden_supervised_swarm()
    from backend.agents.swarm import ChallengeSwarm
    from backend.challenge import load_challenge
    from backend.cost_tracker import CostTracker
    from backend.flags import normalize_flags_required
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

    from backend.daemon.transport import daemon_configured_in_env

    if flags_required is not None:
        meta.flags_required = normalize_flags_required(flags_required)
    elif daemon_configured_in_env():
        # Daemon-supervised swarm with unknown flags_required: ask the TUI via a
        # digits dialog pushed through the daemon, then proceed.
        asked = _ask_flags_via_daemon(meta.name)
        if asked is not None:
            meta.flags_required = asked
            flags_required = asked

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

    import signal

    def _on_stop(_signum=None, _frame=None) -> None:
        swarm.kill()

    try:
        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)
    except (ValueError, OSError):
        pass

    result = await swarm.run()
    print_swarm_outcome(swarm, result, out=console)

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


@main.command("setup")
@click.option(
    "--pack",
    "packs",
    multiple=True,
    help="Pack id to bake (repeatable). Default: full Jeopardy set.",
)
@click.option(
    "--full",
    "full",
    is_flag=True,
    help="Bake the full Jeopardy set (default).",
)
@click.option(
    "--lite",
    "lite",
    is_flag=True,
    help="Bake only web / steg / forensics.",
)
@click.option("--skip-core", is_flag=True, help="Do not build/check L0 core image")
@click.option(
    "--skip-warm-runtime",
    is_flag=True,
    help="Skip committing ctf-sandbox-warm-* images (host cache only)",
)
@click.option(
    "--skip-blutter-vm",
    is_flag=True,
    help="Skip prebuilding the blutter Dart VM (needs a sample Flutter APK)",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def setup_cmd(
    packs: tuple[str, ...],
    full: bool,
    lite: bool,
    skip_core: bool,
    skip_warm_runtime: bool,
    skip_blutter_vm: bool,
    verbose: bool,
) -> None:
    """Phase 3: warm L0 + selected tool packs on this machine (once).

    \b
      artemis setup              full Jeopardy set
      artemis setup --lite       web / steg / forensics only
      artemis setup --pack pwn --pack crypto

    Extracts donor trees into ~/.cache/ctf-agent/packs and commits warm L0
    runtimes so the first solve skips cold apt/pip. When a sample Flutter APK
    is available (``ARTEMIS_BLUTTER_WARM_APK`` or under challenges/), also
    prebuilds the shared blutter Dart VM so the first Flutter solve is fast.
    """
    _setup_logging(verbose)
    from backend.sandbox.setup_bake import FULL_BAKE_PACKS, LITE_BAKE_PACKS, run_setup

    if lite and full:
        raise click.UsageError("Use --lite or --full, not both.")

    if packs:
        chosen: list[str] | None = list(packs)
    elif lite:
        chosen = list(LITE_BAKE_PACKS)
    elif full:
        chosen = list(FULL_BAKE_PACKS)
    else:
        chosen = None

    lines = asyncio.run(
        run_setup(
            packs=chosen,
            skip_core=skip_core,
            skip_warm_runtime=skip_warm_runtime,
            skip_blutter_vm=skip_blutter_vm,
        )
    )
    failed = False
    for line in lines:
        if line.startswith("FAIL"):
            failed = True
            console.print(f"[red]{line}[/red]")
        elif line.startswith("INFO "):
            console.print(f"[cyan]{line[5:]}[/cyan]")
        else:
            console.print(f"[green]{line}[/green]")
    if failed:
        sys.exit(1)
    console.print(
        "[bold]Setup done.[/bold] Start Artemis as usual; packs load from cache/warm runtimes."
    )


@main.command("msg")
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the CLI coordinator HTTP inbox (not the TUI swarm).

    For mid-solve notes in the interactive TUI, type in the session prompt —
    that path uses the daemon operator inbox. This command only hits
    ``http://HOST:PORT/msg`` used by ``artemis swarm`` coordinator mode.
    """
    import json
    import urllib.request

    headers = {"Content-Type": "application/json"}
    tok = (os.environ.get("ARTEMIS_MSG_TOKEN") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers=headers,
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
