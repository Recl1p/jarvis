#!/usr/bin/env bash
# Rehearse cutover.sh and rollback.sh on a CLONE of the live data, beside the live JARVIS (which is not touched).
#
#   sudo bash rehearse.sh /path/to/jarvis-2-source [--keep]
#
# 1. snapshots SSDCACHE256/jarvis and clones it to SSDCACHE256/jarvis-rehearsal, with Telegram and cron jobs switched
#    off in the clone (a second gateway on the same bot would take the live JARVIS's messages)
# 2. runs JARVIS 1 (localhost/jarvis-unified:local) on the clone as a stand-in "old stack" on port 8655
# 3. runs the real cutover.sh against that stack (stack, ports, image and dataset overridden by its .env)
# 4. installs Hermes into the cloned data through the setup API, as the setup screen's button does, and waits
# 5. leaves it up for the Playwright smoke test (tests/e2e/run.sh --live http://10.0.0.103:8654), unless --no-wait
# Then `sudo bash rehearse.sh --rollback` runs the real rollback.sh and checks JARVIS 1 answers again,
# and `sudo bash rehearse.sh --clean` removes everything the rehearsal made.
set -euo pipefail
D=${DOCKER:-docker}
LIVE=SSDCACHE256/jarvis
CLONE=SSDCACHE256/jarvis-rehearsal
ROOT=/mnt/$CLONE
STACK=/mnt/SSDCACHE256/jarvis-rehearsal-stack
BACKUPS=/mnt/SSDCACHE256/jarvis-rehearsal-backups
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "${1:-}" = --clean ]; then
    $D rm -f jarvis-rehearsal jarvis-rehearsal-old >/dev/null 2>&1 || true
    $D network rm jarvis-rehearsal_default >/dev/null 2>&1 || true
    zfs list "$CLONE" >/dev/null 2>&1 && zfs destroy -r "$CLONE"
    for s in $(zfs list -H -t snapshot -o name "$LIVE" | grep "@rehearsal-" || true); do zfs destroy "$s"; done
    rm -rf "$STACK" "$BACKUPS"
    $D image ls --format '{{.Repository}}:{{.Tag}}' | grep '^localhost/jarvis:\(rehearsal\|pre-update\|failed\)' | xargs -r $D rmi >/dev/null 2>&1 || true
    echo "rehearsal removed"; exit 0
fi
if [ "${1:-}" = --rollback ]; then
    backup=$(ls -d "$BACKUPS"/jarvis1-backup-* | tail -1)
    echo yes | STACK_DIR="$STACK" JARVIS_DATASET="$CLONE" DOCKER="$D" sh "$HERE/rollback.sh" "$backup"
    for i in $(seq 1 60); do
        v=$(curl -fsS http://127.0.0.1:8655/health 2>/dev/null || true)
        case "$v" in *hermes-agent*) echo "JARVIS 1 answers again: $v"; exit 0 ;; esac
        sleep 3
    done
    echo "JARVIS 1 did not come back after the rollback" >&2; exit 1
fi

NEW=${1:?usage: sudo bash rehearse.sh /path/to/jarvis-2-source | --rollback | --clean}
stamp=$(date +%Y%m%d-%H%M)

echo "== 1. Clone the live data"
zfs snapshot "$LIVE@rehearsal-$stamp"
zfs clone "$LIVE@rehearsal-$stamp" "$CLONE"
sed -i -E 's/^(TELEGRAM_[A-Z_]+=)/# off for the rehearsal: \1/' "$ROOT/data/.env"
python3 - "$ROOT/data/config.yaml" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(re.sub(r"(\nplatforms:\n  telegram:\n    enabled: )true", r"\1false", s))
PY
mkdir -p "$ROOT/parked-cron" && mv "$ROOT/data/cron/"* "$ROOT/parked-cron/" 2>/dev/null || true

echo "== 2. JARVIS 1 on the clone, as the old stack"
mkdir -p "$STACK" "$BACKUPS"
cat > "$STACK/compose.yaml" <<EOF
name: jarvis-rehearsal
services:
  hermes:
    image: localhost/jarvis-unified:local
    pull_policy: never
    container_name: jarvis-rehearsal-old
    ports: ["8655:8642"]
    environment: {HERMES_UID: "10001", HERMES_GID: "10001", API_SERVER_HOST: 0.0.0.0}
    volumes: ["$ROOT/data:/opt/data"]
    command: [gateway, run]
    shm_size: 1gb
networks: {}
EOF
cat > "$STACK/.env" <<EOF
JARVIS_STACK_NAME=jarvis-rehearsal
JARVIS_CONTAINER=jarvis-rehearsal
JARVIS_IMAGE=localhost/jarvis:rehearsal
JARVIS_PORT=8654
JARVIS_DESKTOP_BIND=10.0.0.103
JARVIS_DESKTOP_PORT=6084
JARVIS_DATA_ROOT=$ROOT
EOF
$D compose -f "$STACK/compose.yaml" up -d
for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8655/health >/dev/null 2>&1 && break; sleep 3; done
echo "   JARVIS 1: $(curl -fsS http://127.0.0.1:8655/health)"

echo "== 3. The real cutover"
STACK_DIR="$STACK" JARVIS_DATASET="$CLONE" BACKUP_ROOT="$BACKUPS" DOCKER="$D" sh "$HERE/cutover.sh" "$NEW"

echo "== 4. Install Hermes into the cloned data (the setup screen's button, with consent)"
curl -fsS -X POST http://127.0.0.1:8654/jarvis/api/install -H 'X-JARVIS: 1' -H 'Content-Type: application/json' \
    -d '{"accept": true, "persona": false, "auto_update": true}'
echo
for i in $(seq 1 120); do
    status=$(curl -fsS http://127.0.0.1:8654/jarvis/api/log | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
    [ "$status" = running ] || break
    sleep 10
done
curl -fsS "http://127.0.0.1:8654/jarvis/api/log" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['status'], d['error']);print('\n'.join(l for l in d['lines'] if l.startswith(('==','!!','  '))))" | tail -40
[ "$status" = done ] || { echo "install did not finish: $status" >&2; exit 1; }
echo "Rehearsal JARVIS 2 is up on http://10.0.0.103:8654 (smoke test it, then --rollback, then --clean)"
