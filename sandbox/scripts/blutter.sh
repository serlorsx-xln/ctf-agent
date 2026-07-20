#!/usr/bin/env bash
# Stable blutter entrypoint: clearer errors, require libapp + libflutter together.
set -euo pipefail

BLUTTER_PY="${BLUTTER_PY:-/opt/blutter/blutter.py}"

usage() {
  cat <<'EOF'
blutter — dump Flutter/Dart AOT (libapp.so)

Usage:
  blutter <dir-with-libapp.so-and-libflutter.so> [outdir]

Typical:
  unzip -o app.apk -d apk
  blutter apk/lib/arm64-v8a ./blutter_out
  # then read blutter_out/*/pp.txt , asm/, objs/

Needs matching libapp.so + libflutter.so in the input directory.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

IN="$1"
OUT="${2:-./blutter_out}"

if [[ ! -f "$BLUTTER_PY" ]]; then
  echo "blutter: engine missing at $BLUTTER_PY" >&2
  exit 127
fi

resolve_dir() {
  local p="$1"
  if [[ -d "$p" ]]; then
    echo "$p"
    return
  fi
  if [[ -f "$p" ]]; then
    dirname "$p"
    return
  fi
  echo ""
}

DIR="$(resolve_dir "$IN")"
if [[ -z "$DIR" ]]; then
  echo "blutter: input not found: $IN" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "$DIR/libapp.so" ]]; then
  echo "blutter: $DIR/libapp.so missing — extract APK lib/<abi>/ first" >&2
  exit 2
fi
if [[ ! -f "$DIR/libflutter.so" ]]; then
  echo "blutter: $DIR/libflutter.so missing — place next to libapp.so (same ABI)" >&2
  exit 2
fi

mkdir -p "$OUT"
echo "blutter: in=$DIR out=$OUT"
exec python3 "$BLUTTER_PY" "$DIR" "$OUT"
