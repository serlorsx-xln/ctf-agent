#!/usr/bin/env bash
# Reclaim local disk without invalidating pack cache or tagged donors.
#
# Safe (no re-bake / no rematerialize):
#   - Docker build cache
#   - Dangling (<none>) images
#   - Legacy ubuntu:20.04 base (Artemis L0/pwn use 24.04)
#   - Repo gitignored caches (.pytest_cache, .ruff_cache, old logs/)
#
# Never touches:
#   - ctf-sandbox-* tagged images
#   - ~/.cache/ctf-agent/packs (host pack trees + .ready)
#   - Running containers
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
else
  section "Docker cleanup"
  echo "skipped — daemon unavailable"
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
