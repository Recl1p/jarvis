#!/bin/sh
# JARVIS setup: check prerequisites, get the image, start JARVIS, wait until its setup screen answers.
#
# Safe to re-run: it never overwrites an existing .env, and `docker compose up -d` leaves a running, unchanged JARVIS
# alone. Works with Docker Engine, Docker Desktop and rootless Podman's docker CLI.
#
# Usage:  ./setup.sh [--build]
#   --build   build the image from this folder (compose.build.yaml) instead of pulling the published one
set -eu
cd "$(dirname "$0")"

BUILD=0
for arg in "$@"; do
    case "$arg" in
        --build) BUILD=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "Docker is not installed (https://docs.docker.com/get-docker/)." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (the 'docker compose' command)." >&2; exit 1; }
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example (edit it to add the voice key or change the port)."; }

# shellcheck disable=SC1091
set -a; . ./.env; set +a
BIND="${JARVIS_BIND:-0.0.0.0}" PORT="${JARVIS_PORT:-8642}"

if [ "$BUILD" = 1 ]; then
    echo "Building the JARVIS image (the first build downloads a few GB; later builds reuse the cache)..."
    docker compose -f compose.yaml -f compose.build.yaml build
    docker compose -f compose.yaml -f compose.build.yaml up -d --no-build
else
    echo "Downloading the JARVIS image..."
    docker compose pull jarvis
    docker compose up -d
fi

url="http://$( [ "$BIND" = 0.0.0.0 ] && echo localhost || echo "$BIND" ):$PORT"
printf 'Waiting for JARVIS'
i=0
until curl -fsS "$url/jarvis/health" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || { echo; echo "JARVIS did not answer in 2 minutes: docker compose logs --tail=50 jarvis" >&2; exit 1; }
    printf '.'; sleep 2
done
echo
echo "JARVIS is up: open $url (or this computer's address from another device) and create its password."
case "$BIND" in
    127.*|localhost|::1) ;;
    *) echo "Do it now: until a password exists, anyone on your network who opens JARVIS first could set one." ;;
esac
