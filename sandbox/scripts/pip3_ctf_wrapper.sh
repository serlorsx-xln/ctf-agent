#!/bin/bash
# CTF L0: Ubuntu PEP 668 blocks bare `pip install`. Agents rarely pass
# --break-system-packages, so wrap pip3/pip to add it for install only.
set -euo pipefail
REAL="${CTF_REAL_PIP3:-/usr/bin/pip3}"
if [[ ! -x "$REAL" ]]; then
  REAL="$(command -v -a pip3 2>/dev/null | grep -v '/usr/local/bin/pip3' | head -1 || true)"
fi
if [[ -z "${REAL}" || ! -x "$REAL" ]]; then
  echo "pip3 wrapper: real pip3 not found" >&2
  exit 127
fi
if [[ "${1:-}" == "install" ]]; then
  has_break=0
  for a in "$@"; do
    if [[ "$a" == "--break-system-packages" ]]; then
      has_break=1
      break
    fi
  done
  if [[ "$has_break" -eq 0 ]]; then
    exec "$REAL" install --break-system-packages "${@:2}"
  fi
fi
exec "$REAL" "$@"
