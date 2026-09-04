"""Sandbox setup readiness — Docker + L0 (packs attach on demand).

Used by the daemon/TUI first-run gate so operators cannot solve until
the core image exists. Pack caches are reported but do not block load.
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
    dns_ok: bool | None = None

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
            "dns_ok": self.dns_ok,
        }


def _image_refs(tag: str) -> tuple[str, ...]:
    """Docker 29+ often fails ``inspect name`` unless ``name:latest`` is explicit."""
    t = (tag or "").strip()
    if not t:
        return ()
    last = t.rsplit("/", 1)[-1]
    if ":" in last or "@" in t:
        return (t,)
    return (f"{t}:latest", t)


def _docker_run(args: list[str], *, timeout_s: float):
    """Run host ``docker``. Returns None on timeout / missing binary."""
    import subprocess

    try:
        from backend.subprocess_platform import resolve_docker_exe

        exe = resolve_docker_exe()
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _docker_image_exists(tag: str) -> bool:
    """True when the image is listed or inspectable.

    After a long ``docker commit`` (warm runtimes), Engine 29 can return
    ``No such image`` for an untagged inspect even though ``docker images``
    already shows ``ctf-sandbox-core:latest``. A 5s timeout also false-misses
    while Docker Desktop is busy — that showed up in the TUI as
    ``sandbox not installed``.
    """
    import time

    refs = _image_refs(tag)
    if not refs:
        return False
    for attempt in range(2):
        busy = False
        for ref in refs:
            listed = _docker_run(["images", "-q", ref], timeout_s=15)
            if listed is None or listed.returncode != 0:
                busy = True
            elif listed.stdout.strip():
                return True
            else:
                inspected = _docker_run(["image", "inspect", ref], timeout_s=15)
                if inspected is None:
                    busy = True
                elif inspected.returncode == 0:
                    return True
        if not busy:
            return False
        if attempt == 0:
            time.sleep(0.5)
    return False


def _docker_ok() -> bool:
    # Post-setup Docker Desktop can stall ``info`` well past 3s.
    for _ in range(2):
        proc = _docker_run(["info"], timeout_s=15)
        if proc is not None and proc.returncode == 0:
            return True
    return False


def _container_dns_ok() -> bool | None:
    """Probe name resolution inside L0. None when the probe itself could not run."""
    proc = _docker_run(
        [
            "run",
            "--rm",
            "--network",
            "bridge",
            "--entrypoint",
            "getent",
            "ctf-sandbox-core",
            "hosts",
            "pypi.org",
        ],
        timeout_s=10,
    )
    if proc is None:
        return None
    return proc.returncode == 0 and bool((proc.stdout or b"").strip())


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
            dns_ok=True,
        )

    from backend.sandbox.packs import _pack_cache_is_ready
    from backend.sandbox.setup_bake import GATE_REQUIRED_PACKS

    packs = list(required_packs) if required_packs is not None else list(GATE_REQUIRED_PACKS)
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
    ready = bool(docker_ok and core)
    if ready:
        if missing:
            parts = [
                "Sandbox ready (Docker + L0). "
                f"Packs attach on demand: {', '.join(missing[:6])}."
            ]
        else:
            parts = ["Sandbox ready (Docker + L0 + pack caches)."]
    elif not docker_ok:
        parts = ["Docker is not reachable. Start Docker Desktop / Colima, then Install."]
    else:
        parts = ["Missing ctf-sandbox-core image. Install builds it."]
    dns_ok = _container_dns_ok() if docker_ok and core else None
    if dns_ok is False:
        parts.append(
            "Container DNS failed (getent pypi.org). Pack bake / pip may stall — "
            "check Docker Desktop DNS, then retry."
        )
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
        dns_ok=dns_ok,
    )


async def run_gate_install(
    *,
    skip_warm_runtime: bool = False,
    skip_blutter_vm: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Build L0 + the full Jeopardy pack set (first-run TUI / launch gate).

    Pack bake and warm runtimes run here so the first solve has tools.
    The blutter Dart VM stays off the gate (10–30+ min) — ``artemis setup``
    prebuilds it when a sample Flutter APK is available.
    """
    from backend.sandbox.setup_bake import run_setup

    return await run_setup(
        packs=None,
        skip_core=False,
        skip_warm_runtime=skip_warm_runtime,
        skip_blutter_vm=skip_blutter_vm,
        on_progress=on_progress,
    )
