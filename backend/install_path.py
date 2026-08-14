"""Remember where this Artemis checkout lives so the global ``artemis`` command
survives USB remounts / folder moves after a relaunch from the new path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Keep in sync with scripts/install.sh and scripts/lib/windows-path.ps1.
_UNIX_WRAPPER = """\
#!/usr/bin/env bash
set -euo pipefail
FILE="${HOME}/.local/share/artemis/install-path.txt"
if [[ -n "${ARTEMIS_REPO_ROOT:-}" && -x "${ARTEMIS_REPO_ROOT}/chassis/bin/artemis" ]]; then
  exec "${ARTEMIS_REPO_ROOT}/chassis/bin/artemis" "$@"
fi
if [[ -f "$FILE" ]]; then
  REPO="$(tr -d '\\r\\n' < "$FILE")"
  if [[ -x "${REPO}/chassis/bin/artemis" ]]; then
    exec "${REPO}/chassis/bin/artemis" "$@"
  fi
fi
echo "Artemis repo not found. cd to the checkout and run: bash scripts/install.sh" >&2
exit 1
"""

_WINDOWS_WRAPPER = """\
@echo off
setlocal EnableExtensions
set "PATH=%USERPROFILE%\\.local\\bin;%USERPROFILE%\\.bun\\bin;%PATH%"
set "PATHFILE=%USERPROFILE%\\.local\\share\\artemis\\install-path.txt"
if defined ARTEMIS_REPO_ROOT if exist "%ARTEMIS_REPO_ROOT%\\chassis\\bin\\artemis.cmd" (
  set "REPO=%ARTEMIS_REPO_ROOT%"
  goto run
)
if exist "%PATHFILE%" (
  set /p REPO=<"%PATHFILE%"
) else (
  set "REPO=%USERPROFILE%\\artemis"
)
:run
if not exist "%REPO%\\chassis\\bin\\artemis.cmd" (
  echo Artemis repo not found. Run scripts\\install.ps1 from the checkout.
  exit /b 1
)
if "%~1"=="" goto tui
if /i "%~1"=="swarm" goto py
if /i "%~1"=="setup" goto py
if /i "%~1"=="chassis" goto py
if /i "%~1"=="--help" goto py
if /i "%~1"=="-h" goto py
goto tui
:tui
call "%REPO%\\chassis\\bin\\artemis.cmd" %*
exit /b %ERRORLEVEL%
:py
cd /d "%REPO%"
if exist "%REPO%\\.venv\\Scripts\\python.exe" (
  "%REPO%\\.venv\\Scripts\\python.exe" -m backend.cli %*
) else (
  "%USERPROFILE%\\.local\\bin\\uv.exe" run --directory "%REPO%" artemis %*
)
exit /b %ERRORLEVEL%
"""


def install_path_file() -> Path:
    return Path.home() / ".local" / "share" / "artemis" / "install-path.txt"


def global_cli_path() -> Path:
    name = "artemis.cmd" if sys.platform == "win32" else "artemis"
    return Path.home() / ".local" / "bin" / name


def _wrapper_text() -> str:
    return _WINDOWS_WRAPPER if sys.platform == "win32" else _UNIX_WRAPPER


def _is_ours(text: str) -> bool:
    return "install-path.txt" in text or "ARTEMIS_REPO_ROOT" in text


def ensure_global_cli() -> Path | None:
    """Replace a broken/old ``artemis`` symlink with a relocatable wrapper.

    Leaves a foreign script (user-authored, no Artemis markers) untouched.
    """
    dest = global_cli_path()
    body = _wrapper_text()
    try:
        if dest.is_symlink():
            dest.unlink()
        elif dest.is_file():
            current = dest.read_text(encoding="utf-8", errors="replace")
            if not _is_ours(current):
                return None
            if current.replace("\r\n", "\n") == body.replace("\r\n", "\n"):
                return dest
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8", newline="\n")
        if sys.platform != "win32":
            dest.chmod(0o755)
    except OSError:
        return None
    return dest if dest.is_file() else None


def looks_like_repo(root: Path) -> bool:
    return (root / "pyproject.toml").is_file() and (
        (root / "chassis" / "bin" / "artemis").is_file()
        or (root / "chassis" / "bin" / "artemis.cmd").is_file()
    )


def read_install_path() -> Path | None:
    path = install_path_file()
    try:
        text = path.read_text(encoding="utf-8").strip().strip('"').rstrip("\\/")
    except OSError:
        return None
    if not text:
        return None
    root = Path(text)
    return root if looks_like_repo(root) else None


def remember_install_path(repo: str | Path | None = None) -> Path | None:
    """Write the registry if ``repo`` is a valid checkout. No-op if unchanged."""
    raw = repo or os.environ.get("ARTEMIS_REPO_ROOT") or ""
    if not raw:
        return read_install_path()
    try:
        root = Path(raw).expanduser().resolve()
    except OSError:
        return read_install_path()
    if not looks_like_repo(root):
        return read_install_path()
    dest = install_path_file()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        current = dest.read_text(encoding="utf-8").strip() if dest.is_file() else ""
        text = str(root)
        if current != text:
            dest.write_text(text + "\n", encoding="utf-8")
            ensure_global_cli()
        else:
            cli = global_cli_path()
            if cli.is_symlink() or not cli.exists():
                ensure_global_cli()
    except OSError:
        ensure_global_cli()
        return root
    return root
