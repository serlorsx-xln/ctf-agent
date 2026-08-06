#!/usr/bin/env bash
# Artemis one-shot installer (macOS / Linux / WSL).
# Usage: bash scripts/install.sh [--full] [--skip-docker] [--skip-bake]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

FULL=0
SKIP_DOCKER=0
SKIP_BAKE=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --skip-docker) SKIP_DOCKER=1 ;;
    --skip-bake) SKIP_BAKE=1 ;;
    -h|--help)
      cat <<EOF
Artemis install — Python 3.14 + uv + Bun + Docker L0 (+ optional pack warm).

  bash scripts/install.sh              # deps + L0 image
  bash scripts/install.sh --full       # also warm common pack caches (slow)
  bash scripts/install.sh --skip-docker  # no docker build (CI / no daemon)
EOF
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

install_uv() {
  if have uv; then
    log "uv $(uv --version)"
    return 0
  fi
  log "Installing uv…"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH:-}"
  have uv || { echo "uv install failed" >&2; exit 1; }
}

install_bun() {
  if have bun; then
    log "bun $(bun --version)"
    return 0
  fi
  log "Installing Bun…"
  curl -fsSL https://bun.sh/install | bash
  export PATH="${HOME}/.bun/bin:${PATH:-}"
  have bun || { echo "Bun install failed" >&2; exit 1; }
}

docker_ready() {
  docker info >/dev/null 2>&1
}

pick_docker_host() {
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    return 0
  fi
  for candidate in \
    "${HOME}/.colima/default/docker.sock" \
    "${HOME}/.docker/run/docker.sock" \
    /var/run/docker.sock
  do
    if [[ -S "$candidate" ]]; then
      export DOCKER_HOST="unix://${candidate}"
      log "DOCKER_HOST=${DOCKER_HOST}"
      return 0
    fi
  done
  return 1
}

build_l0() {
  log "Building ctf-sandbox-core (L0)…"
  docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core .
}

build_donors() {
  log "Building common donor images (pwn, mobile, crypto, ghidra, steg, linux)…"
  docker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .
  docker build -f sandbox/Dockerfile.mobile -t ctf-sandbox-mobile .
  docker build -f sandbox/Dockerfile.crypto -t ctf-sandbox-crypto .
  docker build -f sandbox/Dockerfile.ghidra -t ctf-sandbox-ghidra .
  docker build -f sandbox/Dockerfile.steg -t ctf-sandbox-steg .
  docker build -f sandbox/Dockerfile.linux -t ctf-sandbox-linux .
}

log "Artemis install @ ${REPO_ROOT}"
install_uv
install_bun

log "Python deps (uv sync, Python 3.14)…"
uv sync --python 3.14

log "TUI deps (bun install in chassis/)…"
( cd chassis && bun install )

if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  pick_docker_host || true
  if docker_ready; then
    build_l0
    if [[ "$FULL" -eq 1 ]]; then
      build_donors
    fi
    if [[ "$SKIP_BAKE" -eq 0 && "$FULL" -eq 1 ]]; then
      log "Warming pack caches (artemis setup)…"
      uv run artemis setup -v
    fi
  else
    log "Docker not running — skipped image build."
    log "  Start Docker Desktop / Colima, then: docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core ."
  fi
else
  log "Skipping Docker (--skip-docker)."
fi

cat <<EOF

Artemis install complete.

  Launch TUI:     ./chassis/bin/artemis
  Headless:       uv run artemis swarm --challenge PATH --models 'cursor/composer-2.5' -v
  QA smoke:       bash scripts/qa.sh
  Connect keys:   open TUI → /connect

EOF
