#!/usr/bin/env bash
# Spawn chainedrsa with writable /flags and traversable /keys (image defaults break as nobody).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
unset DOCKER_HOST

docker pull --platform linux/amd64 archiveooo/pub:chainedrsa
docker rm -f chainedrsa 2>/dev/null || true
docker run -d --platform linux/amd64 --name chainedrsa -p 5003:5000 archiveooo/pub:chainedrsa
# xinetd runs /wrapper as nobody; stock image perms deny writing flags/ and listing keys/
docker exec chainedrsa chmod 777 /flags
docker exec chainedrsa chmod 755 /keys
echo "chainedrsa ready on localhost:5003"
