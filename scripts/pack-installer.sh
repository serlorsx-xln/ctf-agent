#!/usr/bin/env bash
# Build single-file Artemis installers for friends (no git / no prior deps).
# Output: dist/Artemis-Install.sh  dist/Artemis-Install.ps1  dist/INSTALL.txt
#
# Usage:
#   bash scripts/pack-installer.sh          # build installers
#   bash scripts/pack-installer.sh --test     # build + self-test extract/structure
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$REPO_ROOT/dist"
STAGE="$(mktemp -d)"
TAR="$STAGE/artemis.tar.gz"
RUN_TEST=0

for arg in "$@"; do
  case "$arg" in
    --test) RUN_TEST=1 ;;
    -h|--help)
      echo "Usage: bash scripts/pack-installer.sh [--test]"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required tool: $1" >&2
    exit 1
  }
}
need tar
need gzip
need zip
need python3

log "Staging source (excludes .git, node_modules, .venv, challenges, logs)…"
mkdir -p "$STAGE/artemis"
tar cf - -C "$REPO_ROOT" \
  --exclude='.git' \
  --exclude='node_modules' --exclude='**/node_modules' \
  --exclude='.venv' \
  --exclude='challenges' --exclude='challenges*' \
  --exclude='logs' --exclude='dist' \
  --exclude='._*' --exclude='.DS_Store' \
  --exclude='.pytest_cache' --exclude='.ruff_cache' \
  . | tar xf - -C "$STAGE/artemis"

log "Structure check (staged tree)…"
bash "$STAGE/artemis/scripts/verify-install.sh" --structure-only

log "Compressing…"
tar czf "$TAR" -C "$STAGE" artemis
TAR_SIZE=$(du -h "$TAR" | awk '{print $1}')
log "Payload: $TAR ($TAR_SIZE)"

mkdir -p "$DIST"

# --- macOS / Linux / WSL: self-extracting shell archive ---
SH_OUT="$DIST/Artemis-Install.sh"
cat > "$SH_OUT" <<'HEADER'
#!/usr/bin/env bash
# Artemis one-file installer - macOS / Linux / WSL (NOT Windows native - use Artemis-Install.ps1)
# Usage: bash Artemis-Install.sh [--full] [--skip-docker] [--skip-bake]
set -euo pipefail

if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "win32" ]]; then
  echo "On Windows use: powershell -ExecutionPolicy Bypass -File Artemis-Install.ps1" >&2
  exit 1
fi
if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "Re-run with bash: bash $0 $*" >&2
  exit 1
fi

INSTALL_DIR="${ARTEMIS_HOME:-$HOME/artemis}"
MARKER='__ARTEMIS_TAR__'
SELF="${BASH_SOURCE[0]:-$0}"

log() { printf '==> %s\n' "$*"; }

line=$(awk "/^${MARKER}\$/ { print NR + 1; exit }" "$SELF")
if [[ -z "$line" ]]; then
  echo "Corrupt installer (missing archive marker)." >&2
  exit 1
fi

parent="$(cd "$(dirname "$INSTALL_DIR")" && pwd)"
if [[ -e "$INSTALL_DIR" ]]; then
  log "Removing existing $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi

log "Extracting Artemis to $INSTALL_DIR …"
mkdir -p "$parent"
tail -n+"$line" "$SELF" | tar xzf - -C "$parent"
extracted="$parent/artemis"
if [[ "$INSTALL_DIR" != "$extracted" && -d "$extracted" ]]; then
  mv "$extracted" "$INSTALL_DIR"
fi
if [[ ! -d "$INSTALL_DIR" ]]; then
  echo "Extract failed (expected $INSTALL_DIR)." >&2
  exit 1
fi

log "Running setup (uv, bun, Docker L0)…"
exec bash "$INSTALL_DIR/scripts/install.sh" "$@"
exit 0
HEADER
echo '__ARTEMIS_TAR__' >> "$SH_OUT"
cat "$TAR" >> "$SH_OUT"
chmod +x "$SH_OUT"

# --- Windows: self-extracting PowerShell (base64 zip) ---
PS_OUT="$DIST/Artemis-Install.ps1"
ZIP="$STAGE/artemis.zip"
(
  cd "$STAGE"
  zip -rq "$ZIP" artemis
)

export ZIP PS_OUT="$PS_OUT"
python3 <<'PY'
import base64
import os
from pathlib import Path

zip_path = Path(os.environ["ZIP"])
ps_out = Path(os.environ["PS_OUT"])
marker = "__ARTEMIS_ZIP_B64__"
b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")
header = r'''# Artemis one-file installer - Windows (PowerShell 5+)
# Double-click Install Artemis.bat in the same folder (NOT this .ps1 directly).
# Usage: powershell -ExecutionPolicy Bypass -File Artemis-Install.ps1 [-Full] [-SkipDocker] [-SkipBake] [-NoPause]
param(
  [switch]$Full,
  [switch]$SkipDocker,
  [switch]$SkipBake,
  [switch]$NoPause
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$InstallDir = if ($env:ARTEMIS_HOME) { $env:ARTEMIS_HOME } else { Join-Path $env:USERPROFILE "artemis" }
$Marker = "__ARTEMIS_ZIP_B64__"

function Log($m) { Write-Host "==> $m" -ForegroundColor Cyan }

function Wait-CloseWindow {
  param([int]$ExitCode = 0)
  if ($NoPause) { exit $ExitCode }
  if ($env:ARTEMIS_INSTALL_NO_PAUSE -eq "1") { exit $ExitCode }
  Write-Host ""
  if ($ExitCode -ne 0) {
    Write-Host "Install FAILED (exit $ExitCode)." -ForegroundColor Red
  } else {
    Write-Host "Install finished OK." -ForegroundColor Green
  }
  Write-Host "Press Enter to close this window..." -ForegroundColor Yellow
  try { [void][Console]::ReadLine() } catch { Start-Sleep -Seconds 86400 }
  exit $ExitCode
}

$exitCode = 0
try {
  $raw = [System.IO.File]::ReadAllText($MyInvocation.MyCommand.Path)
  $idx = $raw.LastIndexOf($Marker)
  if ($idx -lt 0) { throw "Corrupt installer (missing archive marker)." }
  $tail = $raw.Substring($idx + $Marker.Length)
  $end = $tail.IndexOf("#>")
  if ($end -ge 0) { $tail = $tail.Substring(0, $end) }
  $b64 = ($tail -replace '\s', '')
  $bytes = [Convert]::FromBase64String($b64)
  $zip = Join-Path $env:TEMP ("artemis-install-" + [guid]::NewGuid().ToString("N") + ".zip")
  [IO.File]::WriteAllBytes($zip, $bytes)

  function Remove-InstallDir($Path) {
    if (-not (Test-Path $Path)) { return }
    Log "Removing existing $Path"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "._*" -or $_.Name -eq ".DS_Store" } |
        Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
      Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
      if (Test-Path $Path) {
        cmd /c "rmdir /s /q `"$Path`"" 2>$null | Out-Null
      }
    } finally {
      $ErrorActionPreference = $prev
    }
    if (Test-Path $Path) { throw "Could not remove existing $Path (close apps using it and retry)." }
  }

  if (Test-Path $InstallDir) { Remove-InstallDir $InstallDir }

  $destParent = Split-Path $InstallDir -Parent
  Log "Extracting Artemis to $InstallDir ..."
  Expand-Archive -Path $zip -DestinationPath $destParent -Force
  Remove-Item $zip -Force
  $extracted = Join-Path $destParent "artemis"
  if ($InstallDir -ne $extracted -and (Test-Path $extracted)) {
    Move-Item $extracted $InstallDir
  }
  if (-not (Test-Path $InstallDir)) { throw "Extract failed (expected $InstallDir)." }

  Log "Running setup (uv, bun, Docker L0) ..."
  $installArgs = @()
  if ($Full) { $installArgs += "-Full" }
  if ($SkipDocker) { $installArgs += "-SkipDocker" }
  if ($SkipBake) { $installArgs += "-SkipBake" }
  $env:ARTEMIS_INSTALL_GUI = "1"
  & powershell -ExecutionPolicy Bypass -File (Join-Path $InstallDir "scripts\install.ps1") @installArgs
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "install.ps1 exited with code $LASTEXITCODE" }
} catch {
  Write-Host ""
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
  $exitCode = 1
}
Wait-CloseWindow -ExitCode $exitCode

<#
'''
ps_out.write_text(header + marker + "\n" + b64 + "\n#>\n", encoding="utf-8")
print("wrote", ps_out, "size", ps_out.stat().st_size)
PY

cat > "$DIST/INSTALL.txt" <<'EOF'
Artemis — one-file install (friend with empty machine)
======================================================

Pick files for their OS:

  macOS / Linux / WSL2     Artemis-Install.sh only (~10 MB)
  Windows                  folder with TWO files (send both together):
                             Install Artemis.bat   <- double-click this
                             Artemis-Install.ps1   <- required, do not delete

Do NOT use .ps1 on macOS/Linux. Do NOT use .sh on Windows (use WSL + .sh only inside WSL).
Do NOT double-click Artemis-Install.ps1 alone.

--- macOS / Linux / WSL2 ---

  bash Artemis-Install.sh
  bash Artemis-Install.sh --full          # + donor images + pack warm (slow)
  bash Artemis-Install.sh --skip-docker   # no Docker build

--- Windows (PowerShell as user, not Admin required) ---

  1. Put Install Artemis.bat and Artemis-Install.ps1 in the same folder
  2. Double-click Install Artemis.bat
  3. Window stays open until you press a key
  4. Open a NEW terminal, type: artemis

  Do NOT double-click Artemis-Install.ps1 (window closes on errors).

Custom install path:
  ARTEMIS_HOME=C:\path\to\artemis bash Artemis-Install.sh
  $env:ARTEMIS_HOME = "D:\artemis"; .\Artemis-Install.ps1

After install
-------------
1. Open a NEW terminal (cmd or PowerShell) so PATH updates apply
2. Run from anywhere:
     artemis                    # TUI
     artemis swarm --challenge PATH --models cursor/composer-2.5 -v
3. Start Docker Desktop if not running (installer tries winget if missing)
4. /connect → Cursor API key
5. Paste challenge path or text

No Visual Studio / C++ build tools required (bun uses --ignore-scripts on Windows).

Verify / QA
-----------
  bash scripts/verify-install.sh
  powershell -File scripts\verify-install.ps1
  bash scripts/qa.sh
  powershell -File scripts\qa.ps1

Prerequisites (auto-installed by setup)
---------------------------------------
  uv, Bun, Python 3.14 (via uv sync)
  Docker Desktop (manual start if not running)
EOF

log "Done."
log "  $SH_OUT  ($(du -h "$SH_OUT" | awk '{print $1}'))"
log "  $PS_OUT  ($(du -h "$PS_OUT" | awk '{print $1}'))"
log "  $DIST/INSTALL.txt"
log "  $DIST/Install Artemis.bat  (double-click on Windows + Artemis-Install.ps1)"

cat > "$DIST/Install Artemis.bat" <<'CMD'
@echo off
setlocal EnableExtensions
title Artemis Install
cd /d "%~dp0"
if not exist "%~dp0Artemis-Install.ps1" (
  echo.
  echo ERROR: Artemis-Install.ps1 not found in this folder.
  echo Send BOTH Install Artemis.bat and Artemis-Install.ps1 together.
  echo.
  set "EC=1"
  goto :hold
)
echo.
echo ============================================
echo   Artemis Installer
echo   This window stays open until you close it.
echo ============================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Artemis-Install.ps1" -NoPause %*
set "EC=%ERRORLEVEL%"
echo.
if errorlevel 1 (
  echo [FAILED] Exit code %EC%
) else (
  echo [OK] Install finished. Open a NEW terminal and type: artemis
)
:hold
if not defined EC set "EC=1"
echo.
echo Press any key to close this window...
pause >nul
exit /b %EC%
CMD

rm -f "$DIST/Run-Artemis-Install.cmd"

# ExFAT/USB volumes recreate AppleDouble sidecars next to outputs
find "$DIST" -name '._*' -delete 2>/dev/null || true

if [[ "$RUN_TEST" -eq 1 ]]; then
  log "Self-test: extract .sh payload…"
  TEST_DIR="$(mktemp -d)"
  TEST_INSTALL="$TEST_DIR/artemis-test"
  MARKER='__ARTEMIS_TAR__'
  line=$(awk "/^${MARKER}\$/ { print NR + 1; exit }" "$SH_OUT")
  parent="$(dirname "$TEST_INSTALL")"
  mkdir -p "$parent"
  tail -n+"$line" "$SH_OUT" | tar xzf - -C "$parent"
  mv "$parent/artemis" "$TEST_INSTALL"
  bash "$TEST_INSTALL/scripts/verify-install.sh" --structure-only
  rm -rf "$TEST_DIR"
  log "Self-test OK"
fi

cat <<EOF

Send friend:

  macOS / Linux / WSL:  Artemis-Install.sh
  Windows:              Install Artemis.bat + Artemis-Install.ps1 (same folder, zip together)

EOF
