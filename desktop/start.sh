#!/bin/sh
# The browser desktop: a visible Chromium on a virtual screen, served by noVNC on 127.0.0.1:6080 (the launcher shows
# it at /desktop/, behind the login) and driven by Hermes's own browser tools over CDP on 127.0.0.1:9222
# (browser.cdp_url, set by the launcher). Runs as the hermes user under s6; the profile (site logins) persists in the
# data volume. JARVIS_DESKTOP_LISTEN=0.0.0.0 also opens noVNC to the container's network (no login there).
set -eu
PROFILE="${JARVIS_BROWSER_PROFILE:-}"
if [ -z "$PROFILE" ]; then
    # Volumes from the JARVIS image before 2.0 keep their logins in openjarvis/browser.
    if [ -d "$HERMES_HOME/openjarvis/browser" ]; then PROFILE="$HERMES_HOME/openjarvis/browser"
    else PROFILE="$HERMES_HOME/jarvis/browser"; fi
fi
mkdir -p "$PROFILE" /tmp/jarvis-desktop
chmod 700 /tmp/jarvis-desktop
export XDG_RUNTIME_DIR=/tmp/jarvis-desktop DISPLAY=:99
# One browser owns this profile, including across container replacements.
exec 9>"$PROFILE/.jarvis-desktop.lock"
flock -n 9 || { echo 'The browser profile is already in use.' >&2; exit 1; }
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket"

chrome_pid="" others=""
stop() {
    trap - INT TERM EXIT
    # Chromium flushes cookies and the session on SIGTERM; give it a moment before the display goes away.
    [ -n "$chrome_pid" ] && kill -TERM "$chrome_pid" 2>/dev/null && wait "$chrome_pid" 2>/dev/null || true
    for pid in $others; do kill -TERM "$pid" 2>/dev/null || true; done
}
trap stop INT TERM EXIT

Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp &
others="$!"
tries=0
until xdpyinfo -display :99 >/dev/null 2>&1; do
    tries=$((tries + 1)); [ "$tries" -lt 100 ] || exit 1
    sleep 0.1
done
openbox >/tmp/jarvis-desktop/openbox.log 2>&1 &
others="$others $!"
chromium --no-sandbox --disable-dev-shm-usage --disable-gpu --no-first-run --no-default-browser-check \
    --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="$PROFILE" \
    --window-size=1440,1000 about:blank >/tmp/jarvis-desktop/chromium.log 2>&1 &
chrome_pid=$!
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 >/tmp/jarvis-desktop/x11vnc.log 2>&1 &
others="$others $!"
websockify --web=/usr/share/novnc "${JARVIS_DESKTOP_LISTEN:-127.0.0.1}:6080" localhost:5900 >/tmp/jarvis-desktop/novnc.log 2>&1 &
others="$others $!"
# The service lives as long as the browser: s6 restarts the whole desktop if Chromium dies.
wait "$chrome_pid"
