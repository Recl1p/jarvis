#!/bin/sh
# Move the live NAS stack from JARVIS 1 (the forked-Hermes image, container jarvis-hermes) to JARVIS 2.
#
#   sudo sh cutover.sh /path/to/jarvis-2-source        (a checkout of this repo, e.g. extracted from `git archive`)
#
# 1. snapshots the SSDCACHE256/jarvis dataset (data + files) and copies the stack folder to
#    /mnt/hdd_2tb/configs/jarvis1-backup-<stamp> (outside Dockge's stacks and outside that dataset), for rollback.sh
# 2. stops jarvis-hermes (its image stays, tagged localhost/jarvis-unified:local)
# 3. puts the new source, compose.yaml, update.sh and README.md in the stack folder
# 4. builds and starts JARVIS 2 (update.sh)
# Then open http://10.0.0.103:8642/jarvis/ and choose "Install Hermes for me": JARVIS finds the existing data, installs
# the official Hermes into it, and keeps settings, logins, memories, skills, chats, notes and cron jobs.
set -eu
NEW=${1:?usage: sudo sh cutover.sh /path/to/jarvis-2-source}
D=${DOCKER:-docker}
STACK=${STACK_DIR:-/mnt/hdd_2tb/configs/dockge/stacks/jarvis}
DATASET=${JARVIS_DATASET:-SSDCACHE256/jarvis}
stamp=$(date +%Y%m%d-%H%M)

[ -f "$NEW/Dockerfile" ] && [ -d "$NEW/connector/jarvis" ] && [ -f "$NEW/deployment/nas/compose.yaml" ] \
    || { echo "$NEW is not a JARVIS 2 source tree" >&2; exit 1; }
[ -f "$STACK/compose.yaml" ] || { echo "no stack at $STACK" >&2; exit 1; }

echo "== 1. Snapshot $DATASET and back up the stack folder"
zfs snapshot "$DATASET@pre-jarvis2-$stamp"
backup="${BACKUP_ROOT:-/mnt/hdd_2tb/configs}/jarvis1-backup-$stamp"
cp -a "$STACK" "$backup"
printf '%s\n' "$stamp" > "$backup/.cutover-stamp"
echo "   snapshot $DATASET@pre-jarvis2-$stamp, stack copy $backup"

echo "== 2. Stop JARVIS 1"
$D compose -f "$STACK/compose.yaml" down

echo "== 3. Install the JARVIS 2 stack files"
rm -rf "$STACK/src"
cp -R "$NEW" "$STACK/src"
cp "$NEW/deployment/nas/compose.yaml" "$STACK/compose.yaml"
cp "$NEW/deployment/nas/update.sh" "$STACK/update.sh"
cp "$NEW/deployment/nas/rollback.sh" "$STACK/rollback.sh"
cp "$NEW/deployment/nas/README.md" "$STACK/README.md"
chown -R apps:apps "$STACK" 2>/dev/null || true

echo "== 4. Build and start JARVIS 2"
DOCKER="$D" sh "$STACK/update.sh"

port=8642
# shellcheck disable=SC1091
[ -f "$STACK/.env" ] && port=$(. "$STACK/.env"; echo "${JARVIS_PORT:-8642}")
echo
echo "JARVIS 2 is running. Open http://10.0.0.103:$port/jarvis/ and choose \"Install Hermes for me\"."
echo "To go back to JARVIS 1:  sudo sh $STACK/rollback.sh $backup"
