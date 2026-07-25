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

# Build outputs that make a Dart version cheap to reuse. They do not exist in
# the read-only install tree, so a plain mirror would delete them every run.
CACHED_DIRS=(dartsdk build bin packages .build.lock)

if [[ ! -w "$ENGINE" ]] || ! touch "$ENGINE/.blutter_write_test" 2>/dev/null; then
  mkdir -p "$BLUTTER_HOME"
  # Swarm agents share this cache across containers; two Dart VM builds writing
  # the same tree would corrupt it, so serialize whole runs.
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$BLUTTER_HOME/.build.lock" || true
    if ! flock -n 9; then
      echo "blutter: another run holds the shared cache — waiting" >&2
      flock 9 || true
    fi
  fi
  if command -v rsync >/dev/null 2>&1; then
    excludes=(--exclude .blutter_write_test --exclude .build.lock)
    for d in "${CACHED_DIRS[@]}"; do
      excludes+=(--exclude "$d")
    done
    rsync -a --delete "${excludes[@]}" "$ENGINE"/ "$BLUTTER_HOME"/
  else
    # Portable fallback. BLUTTER_HOME may be a bind mount, so clear its
    # contents instead of removing the directory itself.
    for entry in "$BLUTTER_HOME"/* "$BLUTTER_HOME"/.[!.]*; do
      [[ -e "$entry" ]] || continue
      keep=0
      for d in "${CACHED_DIRS[@]}"; do
        [[ "$(basename "$entry")" == "$d" ]] && keep=1
      done
      [[ "$keep" == 1 ]] || rm -rf "$entry"
    done
    for entry in "$ENGINE"/* "$ENGINE"/.[!.]*; do
      [[ -e "$entry" ]] || continue
      name="$(basename "$entry")"
      [[ "$name" == ".blutter_write_test" ]] && continue
      skip=0
      for d in "${CACHED_DIRS[@]}"; do
        [[ "$name" == "$d" ]] && skip=1
      done
      [[ "$skip" == 1 ]] || cp -a "$entry" "$BLUTTER_HOME"/
    done
  fi
  ENGINE="$BLUTTER_HOME"
  if [[ -d "$ENGINE/bin" ]] && compgen -G "$ENGINE/bin/blutter_dartvm*" >/dev/null; then
    echo "blutter: reusing cached Dart VM build(s) in $ENGINE/bin" >&2
  else
    echo "blutter: no cached Dart VM yet — first run for this Dart version compiles it" >&2
  fi
  echo "blutter: /opt/blutter is read-only; using writable engine at $ENGINE" >&2
else
  rm -f "$ENGINE/.blutter_write_test"
fi

BLUTTER_PY="$ENGINE/blutter.py"
mkdir -p "$OUT"
echo "blutter: in=$DIR out=$OUT"
exec python3 "$BLUTTER_PY" "$DIR" "$OUT"
