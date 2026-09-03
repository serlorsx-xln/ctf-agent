"""Pre-TUI launch setup gate — terminal prompt + live logs, never opens the TUI.

Called by ``chassis/bin/artemis`` before Bun/TUI starts. If the sandbox is not
fully ready, asks interactively whether to run ``artemis setup``-equivalent work
(L0 + pack bake + warm runtimes). Declining continues with whatever is installed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import TextIO


@dataclass
class LaunchSetupReport:
    """What is missing before a full warm setup."""

    gate_ready: bool
    docker_ok: bool
    core_image: bool
    packs_missing: list[str] = field(default_factory=list)
    warm_missing: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def needs_prompt(self) -> bool:
        return (not self.gate_ready) or bool(self.warm_missing)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"  Docker: {'ok' if self.docker_ok else 'not reachable'}")
        lines.append(f"  L0 core (ctf-sandbox-core): {'ok' if self.core_image else 'missing'}")
        if self.packs_missing:
            lines.append(f"  Pack caches missing: {', '.join(self.packs_missing)}")
        else:
            lines.append("  Pack caches: ok")
        if self.warm_missing:
            lines.append(
                f"  Warm runtimes missing: {', '.join(self.warm_missing)} "
                "(first solve of those packs may be slow)"
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
    """Probe gate readiness + warm runtime markers for default warm packs."""
    from backend.sandbox.setup_ready import probe_setup_status
    from backend.sandbox.warm_runtime import WARM_BAKE_PACKS, read_warm_runtime

    status = probe_setup_status()
    warm_missing: list[str] = []
    if status.docker_ok and status.core_image:
        for pack_id in WARM_BAKE_PACKS:
            if read_warm_runtime(pack_id, require_image=True) is None:
                warm_missing.append(pack_id)
    elif status.docker_ok:
        # Core missing → warm cannot exist usefully; list as pending after core.
        warm_missing = list(WARM_BAKE_PACKS)

    return LaunchSetupReport(
        gate_ready=bool(status.ready),
        docker_ok=bool(status.docker_ok),
        core_image=bool(status.core_image),
        packs_missing=list(status.packs_missing),
        warm_missing=warm_missing,
        message=str(status.message or ""),
    )


def _prompt_yes_no(question: str, *, default_yes: bool, stdin: TextIO, stderr: TextIO) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    while True:
        try:
            stderr.write(f"{question} [{hint}] ")
            stderr.flush()
            raw = stdin.readline()
        except (OSError, EOFError):
            return default_yes
        if raw == "":
            return default_yes
        ans = raw.strip().lower()
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        stderr.write("Please answer y or n.\n")
        stderr.flush()


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

    _print(stderr, "artemis: starting full setup (L0 + packs + warm runtimes)…")
    return await run_setup(
        skip_core=False,
        skip_warm_runtime=False,
        on_progress=on_progress,
    )


def run_launch_setup_gate(
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    interactive: bool | None = None,
) -> int:
    """Entry for launchers. Always returns 0 so TUI can still start after decline/fail.

    Exit code is informational only (0). Fatal Python errors still raise.
    """
    # Tests / CI / non-interactive automation.
    if _env_truthy("ARTEMIS_SKIP_LAUNCH_SETUP") is True:
        return 0

    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    if interactive is None:
        interactive = bool(getattr(stdin, "isatty", lambda: False)()) and bool(
            getattr(stderr, "isatty", lambda: False)()
        )

    report = assess_launch_setup()
    if not report.needs_prompt:
        return 0

    _print(stderr, "artemis: sandbox is not fully set up yet:")
    for line in report.summary_lines():
        _print(stderr, line)

    auto = _env_truthy("ARTEMIS_SETUP_AUTO")
    if auto is True:
        do_setup = True
        _print(stderr, "artemis: ARTEMIS_SETUP_AUTO=1 — running setup")
    elif auto is False:
        do_setup = False
        _print(stderr, "artemis: ARTEMIS_SETUP_AUTO=0 — skipping setup")
    elif not interactive:
        do_setup = False
        _print(
            stderr,
            "artemis: non-interactive launch — skipping setup prompt "
            "(set ARTEMIS_SETUP_AUTO=1 to force, or run: artemis setup)",
        )
    else:
        do_setup = _prompt_yes_no(
            "Run full setup now? (logs below; TUI opens after — can take a long time)",
            default_yes=True,
            stdin=stdin,
            stderr=stderr,
        )

    if not do_setup:
        _print(stderr, "artemis: continuing with what is installed")
        return 0

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
