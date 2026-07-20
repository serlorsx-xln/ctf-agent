#!/usr/bin/env bash
# Remove dangling Docker images left after sandbox rebuilds.
# Does not delete tagged ctf-sandbox-* donors.
set -euo pipefail
export DOCKER_HOST="${DOCKER_HOST:-unix://${HOME}/.colima/default/docker.sock}"
echo "Pruning dangling images…"
docker image prune -f
echo
docker system df
echo
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep ctf-sandbox | sort || true
