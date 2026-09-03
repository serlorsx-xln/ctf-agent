"""Sandbox setup readiness — Docker L0 + baked pack caches.

Used by the daemon/TUI first-run gate so operators cannot solve until
``ctf-sandbox-core`` exists and common packs are materialized.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SetupStatus:
    ready: bool
    docker_ok: bool
    core_image: bool
    packs_ready: list[str] = field(default_factory=list)
    packs_missing: list[str] = field(default_factory=list)
    message: str = ""
    global_cli: str = ""
    path_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "docker_ok": self.docker_ok,
            "core_image": self.core_image,
            "packs_ready": list(self.packs_ready),
            "packs_missing": list(self.packs_missing),
            "message": self.message,
            "global_cli": self.global_cli,
            "path_hint": self.path_hint,
        }


def _docker_image_exists(tag: str) -> bool:
    import subprocess

    try:
        from backend.subprocess_platform import resolve_docker_exe

        exe = resolve_docker_exe()
        proc = subprocess.run(
            [exe, "image", "inspect", tag],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_ok() -> bool:
    import subprocess

    try:
        from backend.subprocess_platform import resolve_docker_exe

        exe = resolve_docker_exe()
        # Short timeout — gate UI must not stall waiting on a wedged daemon.
        proc = subprocess.run(
            [exe, "info"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _path_hint() -> tuple[str, str]:
    """Return (global_cli_path, hint if ~/.local/bin missing from PATH)."""
    from pathlib import Path

    from backend.install_path import global_cli_path

    cli = str(global_cli_path())
    path_env = os.environ.get("PATH") or ""
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin in path_env.split(os.pathsep):
        return cli, ""
    return (
        cli,
        f"Add {local_bin} to PATH (or open a new terminal) so `artemis` works from anywhere.",
    )


def probe_setup_status(*, required_packs: list[str] | None = None) -> SetupStatus:
    """Sync probe — suitable for daemon RPC and unit tests."""
    # Tests / CI without Docker images: set ARTEMIS_SKIP_SETUP_GATE=1.
    if (os.environ.get("ARTEMIS_SKIP_SETUP_GATE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        cli, hint = _path_hint()
        return SetupStatus(
            ready=True,
            docker_ok=True,
            core_image=True,
            packs_ready=list(required_packs or []),
            packs_missing=[],
            message="Setup gate skipped (ARTEMIS_SKIP_SETUP_GATE=1).",
            global_cli=cli,
            path_hint=hint,
        )

    from backend.sandbox.packs import _pack_cache_is_ready
    from backend.sandbox.setup_bake import DEFAULT_BAKE_PACKS

    packs = list(required_packs) if required_packs is not None else list(DEFAULT_BAKE_PACKS)
    docker_ok = _docker_ok()
    core = _docker_image_exists("ctf-sandbox-core") if docker_ok else False
    ready_packs: list[str] = []
    missing: list[str] = []
    for pack_id in packs:
        if _pack_cache_is_ready(pack_id):
            ready_packs.append(pack_id)
        else:
            missing.append(pack_id)

    cli, hint = _path_hint()
    ready = bool(docker_ok and core and not missing)
    if ready:
        parts = ["Sandbox ready (L0 + pack caches)."]
    elif not docker_ok:
        parts = ["Docker is not reachable. Start Docker Desktop / Colima, then Install."]
    elif not core:
        parts = ["Missing ctf-sandbox-core image. Install builds it."]
    else:
        parts = [f"Pack caches not baked yet: {', '.join(missing[:6])}"]
    if hint:
        parts.append(hint)
    return SetupStatus(
        ready=ready,
        docker_ok=docker_ok,
        core_image=core,
        packs_ready=ready_packs,
        packs_missing=missing,
        message=" ".join(parts),
        global_cli=cli,
        path_hint=hint,
    )


async def run_gate_install(
    *,
    skip_warm_runtime: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Build L0 + materialize default packs (first-run TUI gate).

    Warm runtime commits are optional here — they can take a long time; operators
    can run ``artemis setup`` later for max speed. Gate defaults to skip warm so
    Install finishes sooner; full warm remains ``artemis setup``.
    """
    from backend.sandbox.setup_bake import run_setup

    return await run_setup(
        skip_core=False,
        skip_warm_runtime=skip_warm_runtime,
        on_progress=on_progress,
    )
