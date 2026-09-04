"""Pre-TUI launch setup gate — auto full setup when inventory is incomplete.

Called by ``chassis/bin/artemis`` before Bun/TUI starts. Probes Docker, L0,
full pack caches, and qemu guest libs (libstdc++ / libgcc_s). Missing L0,
pack caches, or guest libs runs full setup (no Y/n). Already-complete
inventory is a no-op. Skip with ``ARTEMIS_SKIP_LAUNCH_SETUP=1`` or
``ARTEMIS_SETUP_AUTO=0``. In-TUI Install stays L0-only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import TextIO


@dataclass
class LaunchSetupReport:
    """Detailed inventory before the TUI starts (Docker, L0, packs, guest libs)."""

    gate_ready: bool
    docker_ok: bool
    core_image: bool
    packs_missing: list[str] = field(default_factory=list)
    warm_missing: list[str] = field(default_factory=list)
    guest_libs_missing: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def needs_prompt(self) -> bool:
        return (not self.gate_ready) or bool(self.packs_missing) or bool(self.guest_libs_missing)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"  Docker: {'ok' if self.docker_ok else 'not reachable'}")
        lines.append(f"  L0 core (ctf-sandbox-core): {'ok' if self.core_image else 'missing'}")
        if self.packs_missing:
            lines.append(
                f"  Pack caches: missing {', '.join(self.packs_missing)} "
                "— run full setup before competing"
            )
        else:
            lines.append("  Pack caches: ok")
        if self.guest_libs_missing:
            lines.append(
                "  Guest libs (qemu C++): missing "
                + ", ".join(self.guest_libs_missing[:8])
            )
        else:
            lines.append("  Guest libs (libstdc++ / libgcc_s in pwn+mobile): ok")
        if self.warm_missing:
            lines.append(
                f"  Warm runtimes (optional): {', '.join(self.warm_missing)} "
                "— first solve of those packs may be slow"
            )
        else:
            lines.append("  Warm runtimes: ok")
        if self.message:
            lines.append(f"  Note: {self.message}")
        return lines


def _env_truthy(name: str) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return None


def assess_launch_setup() -> LaunchSetupReport:
    """Probe Docker/L0, pack caches, warm markers, and qemu guest libs."""
    from backend.sandbox.guest_libs import flatten_guest_lib_gaps, probe_donor_guest_libs
    from backend.sandbox.setup_ready import probe_setup_status
    from backend.sandbox.warm_runtime import WARM_BAKE_PACKS, read_warm_runtime

    status = probe_setup_status()
    warm_missing: list[str] = []
    guest_libs_missing: list[str] = []
    if status.docker_ok and status.core_image:
        for pack_id in WARM_BAKE_PACKS:
            if read_warm_runtime(pack_id, require_image=True) is None:
                warm_missing.append(pack_id)
        guest_libs_missing = flatten_guest_lib_gaps(probe_donor_guest_libs())
    elif status.docker_ok:
        # Core missing → warm cannot exist usefully; list as pending after core.
        warm_missing = list(WARM_BAKE_PACKS)

    return LaunchSetupReport(
        gate_ready=bool(status.ready),
        docker_ok=bool(status.docker_ok),
        core_image=bool(status.core_image),
        packs_missing=list(status.packs_missing),
        warm_missing=warm_missing,
        guest_libs_missing=guest_libs_missing,
        message=str(status.message or ""),
    )


def _print(stderr: TextIO, text: str) -> None:
    try:
        stderr.write(text if text.endswith("\n") else text + "\n")
        stderr.flush()
    except OSError:
        pass


async def _run_full_setup(*, stderr: TextIO) -> list[str]:
    from backend.sandbox.setup_bake import run_setup

    def on_progress(line: str) -> None:
        _print(stderr, f"  {line}")

    _print(stderr, "artemis: starting full sandbox setup (L0 + Jeopardy packs)…")
    return await run_setup(
        packs=None,
        skip_core=False,
        skip_warm_runtime=False,
        skip_blutter_vm=True,
        on_progress=on_progress,
    )


def run_launch_setup_gate(
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    interactive: bool | None = None,
) -> int:
    """Entry for launchers. Always returns 0 so TUI can still start after skip/fail.

    Exit code is informational only (0). Fatal Python errors still raise.
    ``stdin`` / ``interactive`` stay for callers; setup is automatic (no Y/n).
    """
    if _env_truthy("ARTEMIS_SKIP_LAUNCH_SETUP") is True:
        return 0

    _ = stdin
    _ = interactive
    stderr = stderr or sys.stderr

    report = assess_launch_setup()
    if not report.needs_prompt:
        return 0

    _print(stderr, "artemis: sandbox is not fully set up yet:")
    for line in report.summary_lines():
        _print(stderr, line)

    auto = _env_truthy("ARTEMIS_SETUP_AUTO")
    if auto is False:
        _print(stderr, "artemis: ARTEMIS_SETUP_AUTO=0 — skipping setup")
        _print(stderr, "artemis: continuing with what is installed")
        return 0
    if auto is True:
        _print(stderr, "artemis: ARTEMIS_SETUP_AUTO=1 — running setup")
    else:
        _print(stderr, "artemis: running full setup (inventory incomplete)")

    try:
        lines = asyncio.run(_run_full_setup(stderr=stderr))
    except Exception as e:
        _print(stderr, f"artemis: setup failed: {type(e).__name__}: {e}")
        _print(stderr, "artemis: opening TUI anyway — Install gate may still apply")
        return 0

    failed = [ln for ln in lines if ln.startswith("FAIL")]
    if failed:
        _print(stderr, "artemis: setup finished with failures:")
        for ln in failed:
            _print(stderr, f"  {ln}")
        _print(stderr, "artemis: opening TUI anyway")
    else:
        after = assess_launch_setup()
        if after.needs_prompt:
            _print(stderr, "artemis: setup finished but some items are still missing:")
            for line in after.summary_lines():
                _print(stderr, line)
        else:
            _print(stderr, "artemis: setup complete — starting TUI")
    return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv
    return run_launch_setup_gate()


if __name__ == "__main__":
    raise SystemExit(main())
