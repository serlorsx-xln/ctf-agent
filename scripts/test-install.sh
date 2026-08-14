#!/usr/bin/env bash
# Exhaustive install smoke — run before shipping dist/ to friends.
# Usage: bash scripts/test-install.sh [--with-docker] [--skip-windows]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

WITH_DOCKER=0
SKIP_WINDOWS=0
WIN_TARGET="${ARTEMIS_WIN_SSH:-serlorsx@100.69.32.95}"
FRESH="${ARTEMIS_FRESH_INSTALL:-/tmp/artemis-install-fresh-$$}"

for arg in "$@"; do
  case "$arg" in
    --with-docker) WITH_DOCKER=1 ;;
    --skip-windows) SKIP_WINDOWS=1 ;;
    -h|--help)
      echo "Usage: bash scripts/test-install.sh [--with-docker] [--skip-windows]"
      exit 0
      ;;
    *) echo "Unknown: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n[test] %s\n' "$*"; }
fail() { printf '[test] FAIL: %s\n' "$*" >&2; exit 1; }

log "1/7 Current repo structure"
bash scripts/verify-install.sh --structure-only

log "2/7 pack-installer --test"
bash scripts/pack-installer.sh --test
test -f dist/Artemis-Install.sh
test -f dist/Artemis-Install.ps1
test -f dist/INSTALL.txt

log "3/7 Fresh one-file install → ${FRESH} (--skip-docker)"
export ARTEMIS_HOME="$FRESH"
rm -rf "$FRESH"
bash dist/Artemis-Install.sh --skip-docker
test -d "$FRESH/.venv"
test -f "$FRESH/chassis/bin/artemis"

log "4/7 Verify fresh install"
bash "$FRESH/scripts/verify-install.sh" --skip-docker

log "5/7 Fresh install CLI help"
( cd "$FRESH" && uv run artemis --help >/dev/null )

if [[ "$WITH_DOCKER" -eq 1 ]]; then
  log "5b Docker L0 on fresh tree (optional)"
  if docker info >/dev/null 2>&1; then
    ( cd "$FRESH" && docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core . )
    bash "$FRESH/scripts/verify-install.sh"
  else
    log "Docker not running — skipped L0 build"
  fi
fi

log "6/7 install.sh help"
bash scripts/install.sh --help >/dev/null
test -f scripts/install.ps1
test -f scripts/lib/windows-docker.ps1

log "7/7 Windows remote fresh one-file install"
if [[ "$SKIP_WINDOWS" -eq 0 ]] && command -v ssh >/dev/null 2>&1; then
  if ssh -o ConnectTimeout=5 "$WIN_TARGET" "echo ok" >/dev/null 2>&1; then
    scp -q dist/Artemis-Install.ps1 "$WIN_TARGET:Downloads/Artemis-Install-test.ps1"
    scp -q scripts/win-install-test.ps1 "$WIN_TARGET:Downloads/win-install-test.ps1"
    ssh "$WIN_TARGET" "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\serlorsx\\Downloads\\win-install-test.ps1" | tee /tmp/artemis-win-install-test.log
    grep -q WIN_OK /tmp/artemis-win-install-test.log || fail "Windows install test"
  else
    log "Windows SSH unreachable — skipped"
  fi
else
  log "Windows test skipped"
fi

log "Cleaning fresh Mac install ${FRESH}"
rm -rf "$FRESH"

log "ALL INSTALL TESTS PASSED"
