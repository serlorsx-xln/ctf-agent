#!/usr/bin/env bash
# Full Artemis QA — exit non-zero on any failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SKIP_DOCKER=0
SKIP_TUI=0
for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=1 ;;
    --skip-tui) SKIP_TUI=1 ;;
  esac
done

log() { printf '[qa] %s\n' "$*"; }
fail() { printf '[qa] FAIL: %s\n' "$*" >&2; exit 1; }

PY="${REPO_ROOT}/.venv/bin/python"
UV="${UV:-uv}"
if [[ ! -x "$PY" ]]; then
  have_uv=$(command -v uv || true)
  [[ -n "$have_uv" ]] || fail "run scripts/install.sh first"
  PY="$("$have_uv" run python -c 'import sys; print(sys.executable)')"
fi

log "ruff…"
"$PY" -m ruff check backend tests scripts

log "pytest (361+)…"
"$PY" -m pytest tests/ -q --tb=no

if [[ "$SKIP_TUI" -eq 0 ]]; then
  BUN="$(command -v bun || true)"
  [[ -n "$BUN" ]] || fail "bun missing — run scripts/install.sh"
  log "TUI typecheck…"
  ( cd chassis/packages/tui && "$BUN" run typecheck )
  log "TUI tests…"
  ( cd chassis/packages/tui && "$BUN" test --timeout 30000 )
  log "artemis-auth…"
  ( cd chassis/packages/opencode && "$BUN" test test/provider/artemis-auth.test.ts --timeout 30000 )
fi

if [[ "$SKIP_DOCKER" -eq 0 ]] && docker info >/dev/null 2>&1; then
  log "Docker pack smoke (11 packs)…"
  "$PY" scripts/smoke_packs.py
else
  log "Docker smoke skipped (no daemon or --skip-docker)"
fi

log "ALL QA PASSED"
