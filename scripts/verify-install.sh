#!/usr/bin/env bash
# Post-install checks - macOS / Linux / WSL. Exit 0 when ready (Docker optional with --skip-docker).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SKIP_DOCKER=0
STRUCTURE_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=1 ;;
    --structure-only) STRUCTURE_ONLY=1; SKIP_DOCKER=1 ;;
    -h|--help)
      echo "Usage: bash scripts/verify-install.sh [--skip-docker] [--structure-only]"
      exit 0
      ;;
  esac
done

ok=0
fail=0
check() {
  local name=$1
  shift
  if "$@"; then
    printf '  OK   %s\n' "$name"
    ok=$((ok + 1))
  else
    printf '  FAIL %s\n' "$name"
    fail=$((fail + 1))
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

echo "Artemis verify @ ${REPO_ROOT}"

check pyproject.toml test -f pyproject.toml
check Dockerfile.core test -f sandbox/Dockerfile.core
check chassis/bin/artemis test -f chassis/bin/artemis
check scripts/install.sh test -f scripts/install.sh
check platform_paths.py test -f backend/platform_paths.py

if [[ "$STRUCTURE_ONLY" -eq 1 ]]; then
  echo "Structure: ${ok} ok, ${fail} fail"
  exit "$fail"
fi

export PATH="${HOME}/.local/bin:${HOME}/.bun/bin:${PATH:-}"
check uv have uv
check bun have bun
check .venv test -d .venv
if test -x .venv/bin/python 2>/dev/null || test -x .venv/Scripts/python.exe 2>/dev/null; then
  printf '  OK   venv python\n'
  ok=$((ok + 1))
else
  printf '  FAIL venv python\n'
  fail=$((fail + 1))
fi

if have uv && test -d .venv; then
  check import-backend uv run python -c "import backend.platform_paths"
  check pydantic-ai uv run python -c "from pydantic_ai.usage import RunUsage"
fi

if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  if [[ -z "${DOCKER_HOST:-}" ]]; then
    for candidate in \
      "${HOME}/.colima/default/docker.sock" \
      "${HOME}/.docker/run/docker.sock" \
      /var/run/docker.sock
    do
      if [[ -S "$candidate" ]]; then
        export DOCKER_HOST="unix://${candidate}"
        break
      fi
    done
  fi
  if docker info >/dev/null 2>&1; then
    check ctf-sandbox-core docker image inspect ctf-sandbox-core >/dev/null 2>&1
  else
    printf '  WARN Docker not running (start Desktop/Colima, then build L0)\n'
    fail=$((fail + 1))
  fi
fi

echo "Result: ${ok} ok, ${fail} fail"
exit "$fail"
