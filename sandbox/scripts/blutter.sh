#!/usr/bin/env bash
# Stable blutter entrypoint: clearer errors, require libapp + libflutter together.
# Pack binds mount /opt/blutter RO; blutter must write dartsdk/ + build/ — use a
# writable work tree when the install dir is not writable.
set -euo pipefail

BLUTTER_SRC="${BLUTTER_SRC:-/opt/blutter}"
BLUTTER_HOME="${BLUTTER_HOME:-/var/cache/ctf-blutter}"

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
First run may fetch/build a matching Dart SDK (cached under /var/cache/ctf-blutter).
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

IN="$1"
OUT="${2:-./blutter_out}"

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

# Prefer the install tree when writable; otherwise materialize a work copy.
ENGINE="$BLUTTER_SRC"
if [[ ! -f "$ENGINE/blutter.py" ]]; then
  echo "blutter: engine missing at $ENGINE/blutter.py" >&2
  exit 127
fi

if [[ ! -w "$ENGINE" ]] || ! touch "$ENGINE/.blutter_write_test" 2>/dev/null; then
  mkdir -p "$BLUTTER_HOME"
  # Refresh sources; keep dartsdk/build so version caches survive re-runs.
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude dartsdk --exclude build --exclude .blutter_write_test \
      "$ENGINE"/ "$BLUTTER_HOME"/
  else
    # Portable fallback: copy tree then restore cached build dirs if any.
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/blutter-src.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN
    cp -a "$ENGINE"/. "$tmp"/
    rm -rf "$tmp/dartsdk" "$tmp/build" "$tmp/.blutter_write_test"
    if [[ -d "$BLUTTER_HOME/dartsdk" ]]; then
      mv "$BLUTTER_HOME/dartsdk" "$tmp/dartsdk"
    fi
    if [[ -d "$BLUTTER_HOME/build" ]]; then
      mv "$BLUTTER_HOME/build" "$tmp/build"
    fi
    rm -rf "$BLUTTER_HOME"
    mkdir -p "$BLUTTER_HOME"
    cp -a "$tmp"/. "$BLUTTER_HOME"/
  fi
  ENGINE="$BLUTTER_HOME"
  echo "blutter: /opt/blutter is read-only; using writable engine at $ENGINE" >&2
else
  rm -f "$ENGINE/.blutter_write_test"
fi

BLUTTER_PY="$ENGINE/blutter.py"
mkdir -p "$OUT"
echo "blutter: in=$DIR out=$OUT"
exec python3 "$BLUTTER_PY" "$DIR" "$OUT"
