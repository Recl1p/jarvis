#!/bin/sh
# Go back from JARVIS 2 to JARVIS 1 after cutover.sh.
#
#   sudo sh rollback.sh /mnt/hdd_2tb/configs/jarvis1-backup-<stamp>
#
# Restores the stack folder copy and rolls the SSDCACHE256/jarvis dataset back to the snapshot cutover.sh took: the
# official Hermes installer migrates the config to its current format, which the forked Hermes 0.21 in JARVIS 1 can't be
# relied on to read. Anything JARVIS learned or saved after the cutover (chats, notes, memories) is lost with it.
set -eu
BACKUP=${1:?usage: sudo sh rollback.sh <stack backup folder from cutover.sh>}
D=${DOCKER:-docker}
STACK=${STACK_DIR:-/mnt/hdd_2tb/configs/dockge/stacks/jarvis}
DATASET=${JARVIS_DATASET:-SSDCACHE256/jarvis}
stamp=$(cat "$BACKUP/.cutover-stamp")
snap="$DATASET@pre-jarvis2-$stamp"
zfs list -t snapshot "$snap" >/dev/null || { echo "snapshot $snap not found" >&2; exit 1; }

printf 'This rolls %s back to %s and loses everything saved since. Type yes to continue: ' "$DATASET" "$snap"
read -r answer
[ "$answer" = yes ] || { echo "cancelled"; exit 1; }

$D compose -f "$STACK/compose.yaml" down || true
rm -rf "$STACK"
cp -a "$BACKUP" "$STACK"
rm -f "$STACK/.cutover-stamp"
zfs rollback -r "$snap"
$D compose -f "$STACK/compose.yaml" up -d
echo "JARVIS 1 is back."
