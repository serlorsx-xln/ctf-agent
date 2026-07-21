#!/usr/bin/env bash
# Spawn OOO hardest-per-category services (where Docker images exist), then
# run ctf-solve against each challenge sequentially.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
unset DOCKER_HOST

MODEL="${MODEL:-cursor/grok-4.5}"
CHALS=(
  chainedrsa
  barb-metal
  pinboooll
  nooode
  shooow-your-shell
  rorschach
  keml
  VeryAndroidoso
  bytecoooding
  casinooo-life
)

spawn() {
  local name="$1" image="$2" port="${3:-}"
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker rm -f "$name" >/dev/null || true
  fi
  echo "[spawn] $name <- $image${port:+ :$port}"
  if [[ -n "$port" ]]; then
    docker run -d --platform linux/amd64 --name "$name" -p "${port}:${port}" "$image" >/dev/null
  else
    docker run -d --platform linux/amd64 --name "$name" "$image" >/dev/null
  fi
}

echo "== pull + spawn services =="
docker pull --platform linux/amd64 archiveooo/pub:chainedrsa
docker pull --platform linux/amd64 archiveooo/pub:pinboooll
docker pull --platform linux/amd64 archiveooo/pub:shooow-your-shell
docker pull --platform linux/amd64 archiveooo/pub:keml
docker pull --platform linux/amd64 archiveooo/pub:bytecoooding
docker pull --platform linux/amd64 archiveooo/pub:rorschach

# barb-metal: host qemu (Docker+Rosetta cannot run nested qemu-system-i386)
./scripts/spawn-barb-metal.sh
spawn pinboooll archiveooo/pub:pinboooll 2003
spawn shooow-your-shell archiveooo/pub:shooow-your-shell 9090
spawn keml archiveooo/pub:keml 5000

# services that listen on 5000 inside the image — remap host ports
if docker ps -a --format '{{.Names}}' | grep -qx bytecoooding; then docker rm -f bytecoooding >/dev/null || true; fi
docker run -d --platform linux/amd64 --name bytecoooding -p 5001:5000 archiveooo/pub:bytecoooding >/dev/null
if docker ps -a --format '{{.Names}}' | grep -qx rorschach; then docker rm -f rorschach >/dev/null || true; fi
docker run -d --platform linux/amd64 --name rorschach -p 5002:5000 archiveooo/pub:rorschach >/dev/null
if docker ps -a --format '{{.Names}}' | grep -qx chainedrsa; then docker rm -f chainedrsa >/dev/null || true; fi
docker run -d --platform linux/amd64 --name chainedrsa -p 5003:5000 archiveooo/pub:chainedrsa >/dev/null
# stock image: nobody cannot write /flags or traverse /keys
docker exec chainedrsa chmod 777 /flags
docker exec chainedrsa chmod 755 /keys

# casinooo-life: build from local blackjack (player handout under distfiles/bjgame)
if ! docker image inspect casinooo-life-local >/dev/null 2>&1; then
  echo "[build] casinooo-life-local"
  docker build --platform linux/amd64 -t casinooo-life-local \
    challenges/casinooo-life/distfiles/bjgame
fi
if docker ps -a --format '{{.Names}}' | grep -qx casinooo-life; then docker rm -f casinooo-life >/dev/null || true; fi
docker run -d --platform linux/amd64 --name casinooo-life -p 4080:80 casinooo-life-local >/dev/null

# nooode: official initial_flag injected outside distfiles
./scripts/spawn-nooode.sh

echo
echo "== solve each challenge with MODEL=$MODEL =="
echo "Tip: set MODEL=cursor/composer-2.5 or pass args after --"
echo

set +e
for c in "${CHALS[@]}"; do
  echo
  echo "######## $c ########"
  uv run ctf-solve --challenge "./challenges/$c" --models "$MODEL" -v "$@"
done
set -e

echo
echo "Done. Stop services with:"
echo "  docker rm -f chainedrsa barb-metal pinboooll nooode shooow-your-shell keml bytecoooding rorschach casinooo-life"
