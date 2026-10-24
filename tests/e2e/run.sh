#!/usr/bin/env bash
# JARVIS end-to-end UI and regression tests (Playwright), on any Docker host.
#
#   tests/e2e/run.sh                       build, then run every phase on throwaway containers
#   tests/e2e/run.sh --phase fresh         setup screen -> install -> dashboard, API, settings
#   tests/e2e/run.sh --phase external      "I already have Hermes" against a stock Hermes
#   tests/e2e/run.sh --live http://host:8642 [--chat]   smoke test an instance in use (read-only; --chat sends one message)
#   --no-build   reuse the images from a previous run      --keep   leave the containers up afterwards
#   DOCKER="sudo -n docker" tests/e2e/run.sh               where docker needs sudo
#   JARVIS_VOICE_KEY=... tests/e2e/run.sh   also unlock the voice pack in the fresh JARVIS and check its voices
#   E2E_PASSWORD=...     the instance's password for --live (fresh instances get a test password)
#   E2E_OMNIVOICE=1      also install OmniVoice (CPU build, about 9 GB) from the setup screen, speak with it, remove it
#
# Everything runs on its own Docker network (jarvis-e2e): a model server that answers deterministically (mock_llm.py,
# named "ollama" so the guide's Local search finds it), a fresh JARVIS, a stock Hermes (hermes-stock/) and a second
# JARVIS to connect to it. Phases run one after the other to stay within a small machine's memory. The HTML report
# lands in tests/e2e/playwright/report; failures keep a trace, screenshot and video in playwright/results.
set -euo pipefail
D=${DOCKER:-docker}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
E2E="$ROOT/tests/e2e"
NET=jarvis-e2e
WORK=${E2E_WORK:-/tmp/jarvis-e2e}
IMAGE=${JARVIS_E2E_IMAGE:-localhost/jarvis:e2e}
HERMES_IMAGE=localhost/hermes-stock:e2e
PW_IMAGE=mcr.microsoft.com/playwright:v1.64.1-noble
# A ceiling per JARVIS under test: if one runs out (OmniVoice on a CPU is big), only it is stopped, not the host's apps.
MEMORY=${E2E_MEMORY:-6g}

phase=all build=1 keep=0 live="" chat=0
while [ $# -gt 0 ]; do
    case "$1" in
        --phase) phase=$2; shift ;;
        --no-build) build=0 ;;
        --keep) keep=1 ;;
        --live) live=$2; phase=smoke; shift ;;
        --chat) chat=1 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n=== %s\n' "$*"; }
containers="jarvis-fresh jarvis-ext hermes-ext ollama jarvis-e2e-pw"
wipe_work() { [ -d "$WORK" ] && $D run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1 || true; }
collect() {   # container: keep its logs (and Hermes's) with the test results
    local dir="$E2E/playwright/results/containers"
    mkdir -p "$dir"
    $D logs "$1" >"$dir/$1.log" 2>&1 || return 0
    $D exec "$1" sh -c 'for f in /opt/data/jarvis/install.log /opt/data/jarvis/updates.log /opt/data/logs/gateways/default/current /opt/data/logs/errors.log /tmp/gateway.log; do [ -f "$f" ] && { echo "===== $f"; tail -200 "$f"; }; done'         >"$dir/$1.files.log" 2>&1 || true
}
cleanup() {
    for c in jarvis-fresh jarvis-ext hermes-ext ollama; do collect "$c"; done
    [ "$keep" = 1 ] && { echo "(--keep: containers left running on network $NET)"; return; }
    $D rm -f $containers >/dev/null 2>&1 || true
    $D network rm "$NET" >/dev/null 2>&1 || true
    wipe_work
}
trap cleanup EXIT

wait_for() {   # container url seconds
    local i=0
    until $D exec "$1" curl -fsS "$2" >/dev/null 2>&1; do
        i=$((i + 1)); [ "$i" -lt "$3" ] || { echo "$1 did not answer $2" >&2; $D logs --tail 40 "$1" >&2; return 1; }
        sleep 1
    done
}

playwright() {   # phase project-args...
    local phase_name=$1; shift
    $D rm -f jarvis-e2e-pw >/dev/null 2>&1 || true
    $D run --rm --name jarvis-e2e-pw --network "$NET" --ipc=host --add-host host.docker.internal:host-gateway \
        -v "$E2E/playwright:/work" -v "$WORK/shared:/shared" -w /work \
        -e JARVIS_FRESH=http://jarvis-fresh:8642 -e JARVIS_EXT=http://jarvis-ext:8642 -e MOCK_LLM=http://ollama:11434 \
        -e SHARED_DIR=/shared -e JARVIS_LIVE="$live" -e LIVE_ALLOW_CHAT="$chat" -e CI=1 -e PHASE="$phase_name" \
        -e E2E_PASSWORD -e E2E_OMNIVOICE -e E2E_VOICE_PACK="$([ -n "${JARVIS_VOICE_KEY:-}" ] && echo 1)" \
        "$PW_IMAGE" bash -c "npm install --no-audit --no-fund --loglevel=error >/dev/null && npx playwright test $*"
}

if [ "$build" = 1 ] && [ "$phase" != smoke ]; then
    say "Building $IMAGE (voices: ${E2E_VOICES:-pocket}, as published) and $HERMES_IMAGE"
    $D build --build-arg JARVIS_LOCAL_VOICES="${E2E_VOICES:-pocket}" -t "$IMAGE" "$ROOT" >/tmp/jarvis-e2e-build.log 2>&1 \
        || { tail -30 /tmp/jarvis-e2e-build.log; exit 1; }
    $D build -t "$HERMES_IMAGE" "$E2E/hermes-stock" >/tmp/jarvis-e2e-hermes.log 2>&1 \
        || { tail -30 /tmp/jarvis-e2e-hermes.log; exit 1; }
fi

$D rm -f $containers >/dev/null 2>&1 || true
$D network rm "$NET" >/dev/null 2>&1 || true
$D network create "$NET" >/dev/null
wipe_work
mkdir -p "$WORK/fresh" "$WORK/ext" "$WORK/shared"
chmod 777 "$WORK/shared"
rm -rf "$E2E/playwright/report" "$E2E/playwright/results"
status=0

if [ "$phase" = smoke ]; then
    say "Smoke test of $live"
    playwright smoke --project=smoke || status=$?
    exit $status
fi

$D run -d --name ollama --network "$NET" --no-healthcheck --entrypoint python3 -v "$E2E/mock_llm.py:/mock_llm.py:ro" \
    "$IMAGE" /mock_llm.py 11434 >/dev/null
wait_for ollama http://127.0.0.1:11434/health 30

if [ "$phase" = all ] || [ "$phase" = fresh ]; then
    say "Phase 1: a fresh JARVIS (setup screen, install, dashboard, API, settings)"
    $D run -d --name jarvis-fresh --network "$NET" -v "$WORK/fresh:/opt/data" --shm-size 1g --memory "$MEMORY" \
        -e TZ=UTC -e JARVIS_VOICE_KEY "$IMAGE" >/dev/null
    wait_for jarvis-fresh http://127.0.0.1:8642/jarvis/health 90
    playwright fresh --project=setup --project=install --project=dashboard || status=$?
    collect jarvis-fresh
    [ "$keep" = 1 ] || $D rm -f jarvis-fresh >/dev/null   # frees its memory for the next phase
fi

if [ "$phase" = all ] || [ "$phase" = external ]; then
    say "Phase 2: a JARVIS connecting to someone's own Hermes"
    $D run -d --name hermes-ext --network "$NET" -v "$WORK/shared:/shared" "$HERMES_IMAGE" >/dev/null
    $D run -d --name jarvis-ext --network "$NET" -v "$WORK/ext:/opt/data" --memory "$MEMORY" -e JARVIS_DESKTOP=0 -e TZ=UTC "$IMAGE" >/dev/null
    wait_for jarvis-ext http://127.0.0.1:8642/jarvis/health 90
    sleep 10   # the test Hermes's gateway finishes starting
    playwright external --project=external || status=$?
fi

say "Reports: $E2E/playwright/report/<phase>/index.html"
exit $status
