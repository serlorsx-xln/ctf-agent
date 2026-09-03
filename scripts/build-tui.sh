#!/usr/bin/env bash
# Build a native Artemis TUI binary for this machine (fast cold start).
# Usage: bash scripts/build-tui.sh
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OC="$REPO_ROOT/chassis/packages/opencode"
export PATH="${HOME}/.bun/bin:${PATH:-}"

if ! command -v bun >/dev/null 2>&1; then
  echo "bun required — run scripts/install.sh first" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/chassis/.github"
if [[ ! -f "$REPO_ROOT/chassis/.github/TEAM_MEMBERS" ]]; then
  printf '%s\n' '# Artemis chassis fork — stub for @opencode-ai/script build' \
    > "$REPO_ROOT/chassis/.github/TEAM_MEMBERS"
fi

echo "Building Artemis TUI binary (one-time; subsequent opens use dist/)…"
cd "$OC"
# Skip web UI embed — Artemis is TUI-first. skip-install assumes chassis bun install already ran.
bun run script/build.ts --single --skip-embed-web-ui --skip-install

os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
  Darwin) plat=darwin ;;
  Linux) plat=linux ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT) plat=windows ;;
  *) plat="$(echo "$os" | tr '[:upper:]' '[:lower:]')" ;;
esac
case "$arch" in
  arm64|aarch64) cpu=arm64 ;;
  x86_64|amd64) cpu=x64 ;;
  *) cpu="$arch" ;;
esac
bin="$OC/dist/opencode-${plat}-${cpu}/bin/opencode"
if [[ "$plat" == "windows" ]]; then
  bin="${bin}.exe"
fi
if [[ ! -x "$bin" && ! -f "$bin" ]]; then
  echo "Build finished but binary missing: $bin" >&2
  exit 1
fi
echo "OK: $bin"
"$bin" --version 2>/dev/null || true

# Mirror onto local cache so the next `artemis` does not re-read a USB/ExFAT tree.
# cp across volumes can invalidate the adhoc Mach-O signature → macOS SIGKILL
# ("Code Signature Invalid"). Re-sign after copy on Darwin.
cache_root="${ARTEMIS_CACHE:-$HOME/.cache/artemis}/tui-bin"
mkdir -p "$cache_root"
cp -f "$bin" "$cache_root/opencode"
chmod +x "$cache_root/opencode"
if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$cache_root/opencode" >/dev/null 2>&1 || true
fi
src_stamp="$(wc -c <"$bin" | tr -d ' '):$(stat -f '%m' "$bin" 2>/dev/null || stat -c '%Y' "$bin" 2>/dev/null || echo 0)"
printf '%s\n' "$src_stamp" >"$cache_root/stamp"
echo "Cached: $cache_root/opencode"
