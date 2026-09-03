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
  bash scripts/install.sh --full       # all donor images + pack warm (slow, Sage ~1GB)
  bash scripts/install.sh --skip-docker  # no docker build (CI / no daemon)

Platforms: macOS (Intel/ARM), Linux, WSL2 (use this script, not .ps1).
EOF
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

export PYTHONIOENCODING=utf-8

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

detect_platform() {
  local uname_s uname_m
  uname_s="$(uname -s 2>/dev/null || echo unknown)"
  uname_m="$(uname -m 2>/dev/null || echo unknown)"
  if grep -qi microsoft /proc/version 2>/dev/null; then
    log "Platform: WSL2 ($uname_m)"
  elif [[ "$uname_s" == "Darwin" ]]; then
    log "Platform: macOS ($uname_m)"
  elif [[ "$uname_s" == "Linux" ]]; then
    log "Platform: Linux ($uname_m)"
  else
    log "Platform: $uname_s ($uname_m)"
  fi
}

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
    /run/docker.sock \
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

docker_permission_denied() {
  docker info 2>&1 | grep -qi 'permission denied'
}

wait_docker() {
  local i
  for i in $(seq 1 72); do
    pick_docker_host || true
    if docker_ready; then
      return 0
    fi
    if docker_permission_denied; then
      warn "Docker is running but this user cannot use it (socket permission denied)."
      warn "  sudo usermod -aG docker \"$USER\""
      warn "  then log out and back in (or reboot), and re-run install."
      return 1
    fi
    [[ "$i" -eq 1 ]] && log "Waiting for Docker (start Desktop / Colima / system daemon)…"
    sleep 5
  done
  return 1
}

docker_build() {
  local dockerfile="$1" tag="$2"
  local name attempt ctx
  name="$(basename "$dockerfile")"
  # ExFAT AppleDouble breaks BuildKit — scrub + build from APFS cache copy.
  export COPYFILE_DISABLE=1
  find "$REPO_ROOT" -name '._*' ! -path '*/.git/*' ! -path '*/.venv/*' ! -path '*/node_modules/*' -delete 2>/dev/null || true
  # Unique context per build so concurrent install/test-install cannot rmtree
  # a context another docker build is still reading.
  ctx="${ARTEMIS_CACHE:-$HOME/.cache/artemis}/docker-ctx/sandbox-$$-$(date +%s)-$RANDOM"
  mkdir -p "$(dirname "$ctx")"
  # ditto/rsync avoid copying AppleDouble when possible; fallback cp -R
  if command -v rsync >/dev/null 2>&1; then
    mkdir -p "$ctx"
    rsync -a --no-xattrs --exclude '._*' --exclude '.DS_Store' "$REPO_ROOT/sandbox/" "$ctx/"
  else
    cp -R "$REPO_ROOT/sandbox" "$ctx"
    find "$ctx" -name '._*' -delete 2>/dev/null || true
  fi
  for attempt in 1 2 3; do
    log "docker build ${tag} (attempt ${attempt}/3)…"
    if docker build -f "${ctx}/${name}" -t "$tag" "$ctx"; then
      rm -rf "$ctx" 2>/dev/null || true
      return 0
    fi
    warn "${tag} failed (attempt ${attempt}) — retrying in 20s"
    sleep 20
  done
  rm -rf "$ctx" 2>/dev/null || true
  echo "docker build ${tag} failed after 3 attempts" >&2
  return 1
}

build_l0() {
  docker_build sandbox/Dockerfile.core ctf-sandbox-core
}

build_donors() {
  log "Building all donor images (pwn, mobile, crypto, crypto-tools, ghidra, steg, linux)…"
  docker_build sandbox/Dockerfile.pwn ctf-sandbox-pwn
  docker_build sandbox/Dockerfile.mobile ctf-sandbox-mobile
  docker_build sandbox/Dockerfile.crypto ctf-sandbox-crypto
  docker_build sandbox/Dockerfile.crypto-tools ctf-sandbox-crypto-tools
  docker_build sandbox/Dockerfile.ghidra ctf-sandbox-ghidra
  docker_build sandbox/Dockerfile.steg ctf-sandbox-steg
  docker_build sandbox/Dockerfile.linux ctf-sandbox-linux
}

install_cli() {
  local bindir="${HOME}/.local/bin"
  local share="${HOME}/.local/share/artemis"
  mkdir -p "$bindir" "$share"
  printf '%s\n' "${REPO_ROOT}" > "${share}/install-path.txt"
  # Wrapper (not a symlink) so a later launch from a moved USB can refresh the path.
  cat > "${bindir}/artemis" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
FILE="${HOME}/.local/share/artemis/install-path.txt"
if [[ -n "${ARTEMIS_REPO_ROOT:-}" && -x "${ARTEMIS_REPO_ROOT}/chassis/bin/artemis" ]]; then
  exec "${ARTEMIS_REPO_ROOT}/chassis/bin/artemis" "$@"
fi
if [[ -f "$FILE" ]]; then
  REPO="$(tr -d '\r\n' < "$FILE")"
  if [[ -x "${REPO}/chassis/bin/artemis" ]]; then
    exec "${REPO}/chassis/bin/artemis" "$@"
  fi
fi
echo "Artemis repo not found. cd to the checkout and run: bash scripts/install.sh" >&2
exit 1
EOF
  chmod +x "${bindir}/artemis"
  log "Global command: artemis (open a new terminal)"
}

sync_python_deps() {
  # External/USB volumes often cannot hardlink; copy avoids partial pydantic_ai installs.
  export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
  uv sync --python 3.14
  if ! uv run python -c "from pydantic_ai.usage import RunUsage" 2>/dev/null; then
    warn "Repairing incomplete pydantic-ai install…"
    uv sync --reinstall-package pydantic-ai-slim --reinstall-package pydantic-ai --python 3.14
    uv run python -c "from pydantic_ai.usage import RunUsage" \
      || { echo "pydantic-ai install failed" >&2; exit 1; }
  fi
}

detect_platform
log "Artemis install @ ${REPO_ROOT}"
install_uv
install_bun
export PATH="${HOME}/.local/bin:${HOME}/.bun/bin:${PATH:-}"

log "Python deps (uv sync, Python 3.14)…"
sync_python_deps

log "TUI deps (bun install in chassis/)…"
if ! ( cd chassis && bun install ); then
  warn "bun install failed (often missing native build tools) — retrying with --ignore-scripts"
  ( cd chassis && bun install --ignore-scripts )
fi

log "TUI binary (fast cold start)…"
if ! bash scripts/build-tui.sh; then
  warn "TUI binary build failed — artemis will fall back to slow bun src/index.ts"
fi

install_cli

if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  if wait_docker; then
    build_l0
    if [[ "$FULL" -eq 1 ]]; then
      build_donors
    fi
    if [[ "$SKIP_BAKE" -eq 0 && "$FULL" -eq 1 ]]; then
      log "Warming pack caches (artemis setup)…"
      uv run artemis setup -v
    fi
  else
    warn "Docker not usable — skipped image build."
    warn "  macOS: open Docker Desktop or run 'colima start'"
    warn "  Linux: sudo systemctl enable --now docker"
    warn "         sudo usermod -aG docker \"\$USER\"  (then log out / reboot)"
    warn "  WSL2: enable Docker Desktop WSL integration"
    warn "  Then: docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core sandbox"
  fi
else
  log "Skipping Docker (--skip-docker)."
fi

log "Running verify-install…"
bash scripts/verify-install.sh $( [[ "$SKIP_DOCKER" -eq 1 ]] && echo --skip-docker )

cat <<EOF

Artemis install complete.

  Open a NEW terminal, then run:  artemis
  Headless:       artemis swarm --challenge PATH --models 'cursor/composer-2.5' -v
  QA smoke:       bash scripts/qa.sh
  Connect keys:   open TUI → /connect

EOF
