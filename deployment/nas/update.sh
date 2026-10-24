#!/bin/sh
# Rebuild JARVIS from ./src and swap it in safely. Hermes is not in the image: it lives in the data volume and JARVIS
# updates it itself (setup screen, Updates), so this only matters when ./src (the dashboard, connector, launcher) changes.
#   sudo sh /mnt/hdd_2tb/configs/dockge/stacks/jarvis/update.sh
#   DOCKER="sudo -n /usr/bin/docker" sh update.sh                  (a user with passwordless sudo docker)
# Steps: stage the source on tmpfs (the stacks dataset's ZFS ACLs make every file 770) -> tag the current image as
# rollback -> build -> recreate -> wait until the launcher, and Hermes with the connector, answer -> AUTOMATIC
# ROLLBACK to the previous image if not within 4 minutes. Names and paths come from compose.yaml's defaults, or the
# stack's .env.
set -eu
D=${DOCKER:-docker}
STACK=${STACK_DIR:-/mnt/hdd_2tb/configs/dockge/stacks/jarvis}
C="$STACK/compose.yaml"
# shellcheck disable=SC1091
[ -f "$STACK/.env" ] && . "$STACK/.env"
IMG=${JARVIS_IMAGE:-localhost/jarvis:local}
NAME=${JARVIS_CONTAINER:-jarvis}
stamp=$(date +%Y%m%d-%H%M)
prev="${IMG%:*}:pre-update-$stamp"
cd /tmp

STAGE=/tmp/jarvis-build-src-$NAME
rm -rf "$STAGE" && mkdir -p "$STAGE" && cp -R "$STACK/src/." "$STAGE/"
find "$STAGE" -type d -exec chmod 755 {} + && find "$STAGE" -type f -exec chmod 644 {} +

$D image inspect "$IMG" >/dev/null 2>&1 && $D tag "$IMG" "$prev" && echo "previous image kept as $prev"
# One env file for the build: the stack's .env plus the staged context (`sudo` drops the caller's environment).
{ [ -f "$STACK/.env" ] && cat "$STACK/.env"; printf '\nJARVIS_BUILD_CONTEXT=%s\n' "$STAGE"; } > "/tmp/jarvis-build-$NAME.env"
$D compose --env-file "/tmp/jarvis-build-$NAME.env" -f "$C" build jarvis
rm -f "/tmp/jarvis-build-$NAME.env"
rm -rf "$STAGE"

$D compose -f "$C" up -d jarvis
i=0
while [ $i -lt 48 ]; do
    if $D exec "$NAME" sh -c 'curl -fsS http://127.0.0.1:8642/jarvis/health >/dev/null' 2>/dev/null; then
        mode=$($D exec "$NAME" python3 -c "import json;print(json.load(open('/opt/data/jarvis/state.json')).get('mode') or '')" 2>/dev/null || true)
        # Not set up yet (first run of JARVIS 2): the launcher answering is enough.
        if [ -z "$mode" ] || $D exec "$NAME" sh -c 'curl -fsS http://127.0.0.1:8642/vesper/connector >/dev/null' 2>/dev/null; then
            echo "JARVIS is up on the new image. Fully reload /vesper in your browser."
            exit 0
        fi
    fi
    i=$((i + 1)); sleep 5
done
$D logs --tail 40 "$NAME" >&2 || true
if $D image inspect "$prev" >/dev/null 2>&1; then
    echo "NOT healthy after 4 minutes -- rolling back to $prev" >&2
    $D tag "$IMG" "${IMG%:*}:failed-$stamp"
    $D tag "$prev" "$IMG"
    $D compose -f "$C" up -d jarvis
else
    echo "NOT healthy after 4 minutes, and there is no earlier JARVIS 2 image to go back to (see the log above)" >&2
fi
exit 1
