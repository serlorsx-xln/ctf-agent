#!/usr/bin/env bash
# Reclaim local disk without invalidating pack cache or L0 runtimes.
#
# Safe (no re-bake / no rematerialize):
#   - Docker build cache
#   - Dangling (<none>) images
#   - Legacy ubuntu:20.04 base (Artemis L0/pwn use 24.04)
#   - Repo gitignored caches (.pytest_cache, .ruff_cache, old logs/)
#
# Opt-in:
#   --drop-extract-only-donors  docker rmi crypto/ghidra/crypto-tools/linux/steg
#                               (keeps core / pwn / mobile / warm-*)
#   --drop-dev                  chassis/node_modules + duplicate ~/.cache/artemis/tui-bin
#                               when a repo dist binary exists
#
# Never touches by default:
#   - ctf-sandbox-* tagged images
#   - ~/.cache/ctf-agent/packs (host pack trees + .ready)
#   - Running containers
#   - chassis/node_modules (needed to rebuild the TUI)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DROP_EXTRACT=0
DROP_DEV=0
for arg in "$@"; do
  case "$arg" in
    --drop-extract-only-donors) DROP_EXTRACT=1 ;;
    --drop-dev) DROP_DEV=1 ;;
    -h|--help)
      cat <<EOF
Reclaim local disk without invalidating pack cache or L0 runtimes.

  bash scripts/prune_docker.sh
  bash scripts/prune_docker.sh --drop-extract-only-donors
  bash scripts/prune_docker.sh --drop-dev
EOF
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Prefer existing DOCKER_HOST; else Colima; else Docker Desktop / system sock.
if [[ -z "${DOCKER_HOST:-}" ]]; then
  for candidate in \
    "${HOME}/.colima/default/docker.sock" \
    "${HOME}/.docker/run/docker.sock" \
    "/run/docker.sock" \
    "/var/run/docker.sock"; do
    if [[ -S "${candidate}" ]]; then
      export DOCKER_HOST="unix://${candidate}"
      break
    fi
  done
fi

docker_ok() {
  [[ -n "${DOCKER_HOST:-}" ]] && docker info >/dev/null 2>&1
}

section() { echo; echo "=== $* ==="; }

section "Before"
if docker_ok; then
  docker system df 2>/dev/null || true
else
  echo "Docker not reachable (set DOCKER_HOST or start Colima/Desktop)"
fi
df -h / | tail -1

if docker_ok; then
  section "Docker build cache"
  docker builder prune -f

  section "Dangling images"
  docker image prune -f

  section "Legacy ubuntu:20.04 (unused by ctf-sandbox-*)"
  if docker image inspect ubuntu:20.04 >/dev/null 2>&1; then
    if ! docker ps -a --format '{{.Image}}' 2>/dev/null | grep -qE 'ubuntu:20.04|ubuntu:focal'; then
      docker rmi ubuntu:20.04 ubuntu:focal-20250404 2>/dev/null || true
      echo "removed ubuntu:20.04 tags"
    else
      echo "skip — container still references ubuntu:20.04"
    fi
  else
    echo "not present"
  fi
  if [[ "$DROP_EXTRACT" -eq 1 ]]; then
    section "Extract-only donors (cache already baked)"
    for image in \
      ctf-sandbox-crypto \
      ctf-sandbox-ghidra \
      ctf-sandbox-crypto-tools \
      ctf-sandbox-linux \
      ctf-sandbox-steg; do
      if docker image inspect "$image" >/dev/null 2>&1; then
        if docker rmi "$image" 2>/dev/null; then
          echo "removed $image"
        else
          echo "skip $image — in use or rmi failed"
        fi
      else
        echo "$image not present"
      fi
    done
  fi
else
  section "Docker cleanup"
  echo "skipped — daemon unavailable"
fi

if [[ "$DROP_DEV" -eq 1 ]]; then
  section "Dev trees (--drop-dev)"
  if [[ -d "${REPO_ROOT}/chassis/node_modules" ]]; then
    rm -rf "${REPO_ROOT}/chassis/node_modules"
    echo "removed chassis/node_modules (re-run: cd chassis && bun install)"
  else
    echo "chassis/node_modules not present"
  fi
  cache_tui="${ARTEMIS_CACHE:-${HOME}/.cache/artemis}/tui-bin"
  dist_bin="$(find "${REPO_ROOT}/chassis/packages/opencode/dist" -path '*/bin/opencode' -type f 2>/dev/null | head -1 || true)"
  if [[ -n "$dist_bin" && -d "$cache_tui" ]]; then
    rm -rf "$cache_tui"
    echo "removed duplicate TUI cache $cache_tui (repo dist: $dist_bin)"
  elif [[ -d "$cache_tui" ]]; then
    echo "keep $cache_tui — no chassis dist binary found"
  else
    echo "TUI cache not present"
  fi
fi

section "Repo caches (gitignored)"
rm -rf "${REPO_ROOT}/.pytest_cache" "${REPO_ROOT}/.ruff_cache" "${REPO_ROOT}/.mypy_cache"
if [[ -d "${REPO_ROOT}/logs" ]]; then
  find "${REPO_ROOT}/logs" -maxdepth 1 -name 'trace-*.jsonl' -mtime +7 -delete 2>/dev/null || true
fi
echo "cleared pytest/ruff/mypy cache; logs older than 7d"

section "After"
if docker_ok; then
  docker system df 2>/dev/null || true
else
  echo "Docker not reachable"
fi
df -h / | tail -1
echo
if docker_ok; then
  echo "Donor/runtime images kept:"
  docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep ctf-sandbox | sort || true
fi
echo
echo "Pack cache (unchanged): ${CTF_PACK_CACHE:-${HOME}/.cache/ctf-agent/packs}"
