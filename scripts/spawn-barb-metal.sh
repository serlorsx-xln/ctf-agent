#!/usr/bin/env bash
# Spawn barb-metal on localhost:7828 for Apple Silicon / Docker Desktop.
#
# The official archiveooo/pub:barb-metal image runs qemu-system-i386 inside an
# amd64 container. On Docker Desktop (aarch64 + Rosetta) that fails with:
#   -sandbox …: There is no option group 'sandbox'
#   then: rosetta error: Unimplemented syscall number 282
#
# This helper runs the handout kernel with the host's qemu-system-i386 and
# exposes it via socat on :7828 (same port the checklist / agent expect).
#
# The listener is double-fork + setsid daemonized so it survives the shell that
# started it (Cursor agent shells otherwise reap background jobs).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/challenges/barb-metal/.local-service"
DIST="$ROOT/challenges/barb-metal/distfiles"
PIDFILE="$RUNTIME/supervise.pid"
PORT="${BARB_METAL_PORT:-7828}"
LSOF="$(command -v lsof || true)"
[[ -x /usr/sbin/lsof ]] && LSOF=/usr/sbin/lsof

need() {
  command -v "$1" >/dev/null || {
    echo "missing: $1 (brew install $1)" >&2
    exit 1
  }
}
need qemu-system-i386
need socat
need python3

mkdir -p "$RUNTIME"
cp -f "$DIST/service" "$RUNTIME/service"
cp -f "$DIST/payload.bin" "$RUNTIME/payload.bin"

# Prefer live flag from the official image when Docker can pull it; otherwise keep
# an existing local flag (do not invent a fake OOO{} that agents might submit).
if [[ ! -f "$RUNTIME/flag" ]] || [[ "${BARB_METAL_REFRESH_FLAG:-}" == "1" ]]; then
  if command -v docker >/dev/null; then
    unset DOCKER_HOST
    docker pull --platform linux/amd64 archiveooo/pub:barb-metal >/dev/null
    cid="$(docker create --platform linux/amd64 archiveooo/pub:barb-metal)"
    docker cp "$cid:/flag" "$RUNTIME/flag"
    docker rm -f "$cid" >/dev/null
  else
    echo "No $RUNTIME/flag and docker unavailable — place the challenge flag there." >&2
    exit 1
  fi
fi

# Stop prior supervisor / listeners / conflicting docker publish.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  # Kill process group (supervisor + socat children).
  kill -- "-$(cat "$PIDFILE")" 2>/dev/null || kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
  sleep 0.3
fi
if command -v docker >/dev/null; then
  unset DOCKER_HOST
  docker rm -f barb-metal 2>/dev/null || true
fi
if [[ -n "$LSOF" ]]; then
  "$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | xargs kill 2>/dev/null || true
  sleep 0.2
fi
# Legacy pidfile from earlier script versions.
rm -f "$RUNTIME/socat.pid"

# Relay must keep qemu stdin open after flag+payload (plain `cat | qemu` EOFs
# the UART and barbOS stops answering commands).
cp -f "$ROOT/scripts/barb-metal-run-one.py" "$RUNTIME/run-one.py"
chmod +x "$RUNTIME/run-one.py"
cat >"$RUNTIME/run-one.sh" <<EOF
#!/bin/bash
exec python3 "$RUNTIME/run-one.py"
EOF
chmod +x "$RUNTIME/run-one.sh"

# Supervisor: restart socat if it exits. Bind 0.0.0.0 for Docker Desktop.
cat >"$RUNTIME/supervise.sh" <<EOF
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\${PATH:-}"
PORT=${PORT}
RUNTIME=${RUNTIME}
LOG="\$RUNTIME/socat.log"
echo "\$\$" > "\$RUNTIME/supervise.pid"
while true; do
  echo "\$(date -u +%Y-%m-%dT%H:%M:%SZ) starting socat on :\$PORT" >>"\$LOG"
  socat \\
    "TCP-LISTEN:\${PORT},bind=0.0.0.0,reuseaddr,fork,keepalive" \\
    "EXEC:\${RUNTIME}/run-one.sh" \\
    >>"\$LOG" 2>&1 || true
  echo "\$(date -u +%Y-%m-%dT%H:%M:%SZ) socat exited; restarting in 0.5s" >>"\$LOG"
  sleep 0.5
done
EOF
chmod +x "$RUNTIME/supervise.sh"

# Double-fork + setsid so Cursor/agent shell teardown cannot reap the listener.
BARB_METAL_RUNTIME="$RUNTIME" python3 - <<'PY'
import os, sys, time
from pathlib import Path

runtime = Path(os.environ["BARB_METAL_RUNTIME"])
supervise = runtime / "supervise.sh"
pidfile = runtime / "supervise.pid"
log = runtime / "daemon.log"

if os.fork() > 0:
    for _ in range(50):
        if pidfile.exists():
            break
        time.sleep(0.05)
    sys.exit(0)

os.setsid()
if os.fork() > 0:
    os._exit(0)

os.chdir(str(runtime))
os.umask(0)
devnull = os.open("/dev/null", os.O_RDWR)
os.dup2(devnull, 0)
out = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(out, 1)
os.dup2(out, 2)
os.execv("/bin/bash", ["bash", str(supervise)])
PY

sleep 0.5
if [[ ! -f "$PIDFILE" ]] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "barb-metal daemon failed to start; see $RUNTIME/daemon.log $RUNTIME/socat.log" >&2
  exit 1
fi

# Smoke test: boot banner should appear quickly.
if ! python3 - <<PY
import socket, sys
s = socket.create_connection(("127.0.0.1", ${PORT}), timeout=3)
s.settimeout(6)
data = b""
while b"Waiting" not in data and b"barbOS" not in data and len(data) < 4096:
    chunk = s.recv(256)
    if not chunk:
        break
    data += chunk
s.close()
sys.stdout.buffer.write(data[:200])
sys.exit(0 if (b"Waiting" in data or b"OOO Bootloader" in data or b"barbOS" in data) else 1)
PY
then
  echo "barb-metal local spawn failed; see $RUNTIME/socat.log" >&2
  exit 1
fi
echo
echo "barb-metal ready on localhost:${PORT} (daemonized host qemu + socat)"
echo "  pid: $(cat "$PIDFILE")"
echo "  stop: kill \$(cat $PIDFILE)"
