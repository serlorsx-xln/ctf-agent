"""Artemis chassis launcher helpers — Bun discovery + TUI exec."""

from __future__ import annotations

import os
from pathlib import Path


def find_bun() -> Path | None:
    """Locate the Bun binary even when ~/.bun/bin is not on PATH."""
    candidates: list[Path] = []
    which = None
    try:
        import shutil

        which = shutil.which("bun")
    except Exception:
        which = None
    if which:
        candidates.append(Path(which))

    home = Path.home()
    bun_install = os.environ.get("BUN_INSTALL", "").strip()
    if bun_install:
        candidates.append(Path(bun_install) / "bin" / "bun")
    candidates.extend(
        [
            home / ".bun" / "bin" / "bun",
            Path("/usr/local/bin/bun"),
            Path("/opt/homebrew/bin/bun"),
        ]
    )
    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file() and os.access(p, os.X_OK):
                return p.resolve()
        except OSError:
            continue
    return None


def chassis_paths() -> tuple[Path, Path, Path]:
    """Return (repo_root, chassis_root, launcher_script)."""
    repo = Path(__file__).resolve().parents[2]
    chassis = repo / "chassis"
    launcher = chassis / "bin" / "artemis"
    return repo, chassis, launcher


def bun_missing_message() -> str:
    return (
        "Artemis requires Bun to run the interactive TUI.\n"
        "  Install:  curl -fsSL https://bun.sh/install | bash\n"
        "  Then:     export PATH=\"$HOME/.bun/bin:$PATH\"\n"
        "  Or reopen your terminal and retry: uv run artemis\n"
        "Swarm without TUI: uv run artemis swarm --challenge PATH"
    )


def try_launch_chassis(argv: list[str] | None = None) -> bool:
    """Exec the Artemis TUI chassis. Returns False only if Bun/launcher missing.

    On success this process is replaced (does not return).
    """
    argv = list(argv or [])
    repo, chassis, launcher = chassis_paths()
    if not launcher.is_file():
        return False
    bun = find_bun()
    if bun is None:
        return False

    env = os.environ.copy()
    env["ARTEMIS"] = "1"
    env["ARTEMIS_REPO_ROOT"] = str(repo)
    env["ARTEMIS_CHASSIS_ROOT"] = str(chassis)
    env.pop("OPENCODE_PURE", None)
    env["OPENCODE_CONFIG"] = env.get("OPENCODE_CONFIG") or str(chassis / "opencode.json")
    from backend.shell.credentials import credentials_into

    env = credentials_into(env, overwrite=True)
    # Ensure child sees bun on PATH
    bun_dir = str(bun.parent)
    path = env.get("PATH", "")
    if bun_dir not in path.split(":"):
        env["PATH"] = f"{bun_dir}:{path}" if path else bun_dir

    # Prefer bash launcher (sets .env + TUI auth + cursor stub); it will find bun via updated PATH
    os.execve(str(launcher), [str(launcher), *argv], env)
    return True  # unreachable
