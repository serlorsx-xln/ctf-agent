# Remote Windows deploy + install + QA (run from dev machine via SSH).
# Usage: bash scripts/deploy-windows.sh [user@host]
set -euo pipefail

TARGET="${1:-serlorsx@100.69.32.95}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP=$(date +%Y%m%d%H%M%S)
TAR="/tmp/artemis-deploy-${STAMP}.tar.gz"
REMOTE_DIR='C:/Users/serlorsx/artemis'

log() { printf '==> %s\n' "$*"; }

log "Creating deploy tarball (excludes node_modules, .venv, .git)…"
tar czf "$TAR" -C "$REPO_ROOT" \
  --exclude='._*' --exclude='.DS_Store' \
  --exclude='node_modules' --exclude='**/node_modules' \
  --exclude='.venv' --exclude='.git' \
  --exclude='challenges' --exclude='challenges*' \
  --exclude='logs' --exclude='workspace' \
  --exclude='.pytest_cache' --exclude='.ruff_cache' \
  .

log "Uploading to ${TARGET}…"
scp "$TAR" "${TARGET}:Downloads/artemis-deploy.tar.gz"

log "Extract + install + QA on Windows…"
ssh "$TARGET" "powershell -NoProfile -ExecutionPolicy Bypass -Command \"
  \$tar = Join-Path \$env:USERPROFILE 'Downloads\\artemis-deploy.tar.gz'
  \$root = Join-Path \$env:USERPROFILE 'artemis'
  if (Test-Path \$root) { Remove-Item -Recurse -Force \$root }
  New-Item -ItemType Directory -Path \$root | Out-Null
  tar -xzf \$tar -C \$root
  Set-Location \$root
  & powershell -ExecutionPolicy Bypass -File scripts\\install.ps1
  # Try starting Docker Desktop if installed
  \$dd = 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'
  if (Test-Path \$dd) {
    Start-Process \$dd -ErrorAction SilentlyContinue
    for (\$i=0; \$i -lt 60; \$i++) {
      docker info 2>\$null | Out-Null
      if (\$LASTEXITCODE -eq 0) { Write-Host 'Docker ready'; break }
      Start-Sleep -Seconds 5
    }
  }
  & powershell -ExecutionPolicy Bypass -File scripts\\qa.ps1
\""

log "Windows deploy + QA finished."
