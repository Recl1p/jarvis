#!/bin/sh
# Update JARVIS to the newest published image and swap it in safely: what Dockge's Update button does (pull, recreate),
# plus a health check and an AUTOMATIC ROLLBACK to the image that ran before if the new one isn't healthy within
# 4 minutes. Hermes is not in the image: it lives in the data volume and updates itself (setup screen, Updates).
#   sudo sh /mnt/hdd_2tb/configs/dockge/stacks/jarvis/update.sh
#   DOCKER="sudo -n /usr/bin/docker" sh update.sh                  (a user with passwordless sudo docker)
# Names and paths come from compose.yaml's defaults, or the stack's .env.
set -eu
D=${DOCKER:-docker}
STACK=${STACK_DIR:-/mnt/hdd_2tb/configs/dockge/stacks/jarvis}
C="$STACK/compose.yaml"
# shellcheck disable=SC1091
[ -f "$STACK/.env" ] && . "$STACK/.env"
IMG=${JARVIS_IMAGE:-ghcr.io/recl1p/jarvis:2}
NAME=${JARVIS_CONTAINER:-jarvis}
stamp=$(date +%Y%m%d-%H%M)
cd /tmp

# The image the running container uses (by ID, so a pull that moves the tag can't lose it).
prev=$($D inspect -f '{{.Image}}' "$NAME" 2>/dev/null || true)
[ -n "$prev" ] && $D tag "$prev" "jarvis-rollback:$stamp" && echo "running image kept as jarvis-rollback:$stamp"

$D compose -f "$C" pull jarvis
if [ -n "$prev" ] && [ "$($D image inspect -f '{{.Id}}' "$IMG")" = "$prev" ]; then
    echo "JARVIS is already on the newest image."
    $D rmi "jarvis-rollback:$stamp" >/dev/null 2>&1 || true
    exit 0
fi
$D compose -f "$C" up -d jarvis

i=0
while [ $i -lt 48 ]; do
    if $D exec "$NAME" sh -c 'curl -fsS http://127.0.0.1:8642/jarvis/health >/dev/null' 2>/dev/null; then
        mode=$($D exec "$NAME" python3 -c "import json;print(json.load(open('/opt/data/jarvis/state.json')).get('mode') or '')" 2>/dev/null || true)
        # Not set up yet: the launcher answering is enough. Set up: the connector must answer too (with the API key,
        # which also passes JARVIS's login; read inside the container so it never shows in the host's process list).
        if [ -z "$mode" ] || $D exec "$NAME" sh -c 'k=$(grep "^API_SERVER_KEY=" /opt/data/.env | tail -1 | cut -d= -f2-);
                curl -fsS -H "Authorization: Bearer $k" http://127.0.0.1:8642/vesper/connector/check' >/dev/null 2>&1; then
            echo "JARVIS is up on the new image ($($D exec "$NAME" sh -c 'curl -s http://127.0.0.1:8642/jarvis/health')). Fully reload /vesper in your browser."
            # Keep the three newest rollback images.
            $D image ls jarvis-rollback --format '{{.Tag}}' | sort -r | tail -n +4 \
                | while read -r tag; do $D rmi "jarvis-rollback:$tag" >/dev/null 2>&1 || true; done
            exit 0
        fi
    fi
    i=$((i + 1)); sleep 5
done
$D logs --tail 40 "$NAME" >&2 || true
if [ -n "$prev" ]; then
    echo "NOT healthy after 4 minutes -- rolling back to the image that ran before (jarvis-rollback:$stamp)" >&2
    # An env file rather than an environment variable: `sudo` (in DOCKER) would drop the variable.
    envf="/tmp/jarvis-rollback-$NAME.env"
    { [ -f "$STACK/.env" ] && cat "$STACK/.env"; printf '\nJARVIS_IMAGE=jarvis-rollback:%s\n' "$stamp"; } > "$envf"
    $D compose --env-file "$envf" -f "$C" up -d jarvis
    rm -f "$envf"
    echo "Rolled back. To stay there through Dockge, put JARVIS_IMAGE=jarvis-rollback:$stamp in the stack's .env." >&2
else
    echo "NOT healthy after 4 minutes, and no earlier image to go back to (see the log above)" >&2
fi
exit 1
