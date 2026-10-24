"""Connecting a Hermes the user already has: the prompt they paste into it, the script that prompt runs, and pairing.

The prompt asks their Hermes to download ``/jarvis/connect.sh`` from this JARVIS, summarise it, and run it. The script
installs the connector plugin (``/jarvis/connector.tar.gz``), turns on the API server, makes a key if there is none, and
reports the address and key back to this JARVIS with a one-time pairing code, so the key never goes through the chat.
It prints them too, for the manual "enter address and key" form.
"""

import io
import secrets
import tarfile
import time
from typing import Optional

from . import config, hermes_setup

PAIR_TTL_SECONDS = 60 * 60

_pairing: dict = {}    # code -> expiry (monotonic)


def new_pairing_code() -> str:
    now = time.monotonic()
    for code, expiry in list(_pairing.items()):
        if expiry < now:
            del _pairing[code]
    code = secrets.token_urlsafe(18)
    _pairing[code] = now + PAIR_TTL_SECONDS
    return code


def pairing_valid(code: str) -> bool:
    expiry = _pairing.get(code or "")
    return expiry is not None and expiry >= time.monotonic()


def consume_pairing(code: str) -> None:
    _pairing.pop(code, None)


def prompt(jarvis_url: str, code: str) -> str:
    return f"""Please connect yourself to my JARVIS dashboard.

JARVIS is a dashboard for Hermes Agent, running at {jarvis_url}. It needs its connector plugin installed in this Hermes and this Hermes's API server reachable from it. Please do this, telling me briefly what you are doing:

1. Download the JARVIS connect script, read it, and give me a two-line summary of what it will change before running it:
   curl -fsSL {jarvis_url}/jarvis/connect.sh -o /tmp/jarvis-connect.sh
2. Run it with my pairing code:
   bash /tmp/jarvis-connect.sh --pair {code}
   It installs the "jarvis" plugin into your plugins folder, enables it, turns on your API server (listening on all interfaces, on port 8642 unless one is already set), creates an API_SERVER_KEY if there is none, sends the address and key to JARVIS, and restarts your gateway a few seconds later.
3. Tell me the address it printed. Don't repeat the API key in this chat: JARVIS already has it.
4. Then ask me whether you should take on the JARVIS personality. Only if I say yes, run:
   bash /tmp/jarvis-connect.sh --persona-only
   (it keeps my current SOUL.md as SOUL.md.before-jarvis).

If a step fails, show me the error and stop."""


def script(jarvis_url: str) -> str:
    return CONNECT_SCRIPT.replace("@JARVIS_URL@", jarvis_url)


def connector_tarball() -> bytes:
    """The connector plugin as a .tar.gz with one top-level folder, ``jarvis/`` (tests and caches left out)."""
    buffer = io.BytesIO()
    source = config.CONNECTOR_DIR / "jarvis"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if "__pycache__" in rel.parts or (rel.parts and rel.parts[0] == "tests"):
                continue
            tar.add(path, arcname=f"jarvis/{rel.as_posix()}", recursive=False)
    return buffer.getvalue()


def connector_version() -> str:
    return hermes_setup.bundled_connector_version()


def soul() -> Optional[str]:
    try:
        return (config.PERSONA_DIR / "SOUL.md").read_text(encoding="utf-8")
    except OSError:
        return None


CONNECT_SCRIPT = r"""#!/usr/bin/env bash
# JARVIS connect script, served by the JARVIS at @JARVIS_URL@
#
# Connects this computer's Hermes Agent to that JARVIS:
#   - installs the "jarvis" connector plugin into $HERMES_HOME/plugins and enables it
#   - turns on Hermes's API server (all interfaces, port 8642 unless one is set) and creates an API_SERVER_KEY if needed
#   - with --pair CODE: sends the address and key to JARVIS (one-time code from JARVIS's connect screen)
#   - prints the address and key, then restarts the Hermes gateway (skip with --no-restart)
#   --persona-only   only install the JARVIS personality (SOUL.md; the old one is kept as SOUL.md.before-jarvis)
set -euo pipefail

JARVIS_URL="@JARVIS_URL@"
PAIR="" PERSONA_ONLY=0 RESTART=1
while [ $# -gt 0 ]; do
  case "$1" in
    --pair) PAIR="${2:-}"; shift ;;
    --persona-only) PERSONA_ONLY=1 ;;
    --no-restart) RESTART=0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME
command -v hermes >/dev/null 2>&1 || { echo "The hermes command is not on PATH. Install Hermes first: https://hermes-agent.nousresearch.com" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }

get() { hermes config get "$1" 2>/dev/null | tail -n 1 | tr -d '\r' || true; }
set_cfg() { hermes config set "$1" "$2" >/dev/null; echo "  set $1 = $2"; }

if [ "$PERSONA_ONLY" = 1 ]; then
  tmp="$(mktemp)"
  curl -fsSL "$JARVIS_URL/jarvis/persona/SOUL.md" -o "$tmp"
  if [ -s "$HERMES_HOME/SOUL.md" ] && ! cmp -s "$tmp" "$HERMES_HOME/SOUL.md" && [ ! -e "$HERMES_HOME/SOUL.md.before-jarvis" ]; then
    cp "$HERMES_HOME/SOUL.md" "$HERMES_HOME/SOUL.md.before-jarvis"
    echo "Kept your previous SOUL.md as SOUL.md.before-jarvis"
  fi
  mv "$tmp" "$HERMES_HOME/SOUL.md"
  echo "JARVIS personality installed. It applies from the next conversation."
  exit 0
fi

echo "Installing the JARVIS connector from $JARVIS_URL ..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$JARVIS_URL/jarvis/connector.tar.gz" -o "$tmp/connector.tar.gz"
mkdir -p "$HERMES_HOME/plugins"
tar -xzf "$tmp/connector.tar.gz" -C "$tmp"
rm -rf "$HERMES_HOME/plugins/jarvis"
mv "$tmp/jarvis" "$HERMES_HOME/plugins/jarvis"
hermes plugins enable jarvis
[ "$(get plugins.isolation)" = host ] && set_cfg plugins.isolation in_process

echo "Turning on the Hermes API server ..."
case "$(get platforms.api_server.enabled)" in True|true) ;; *) set_cfg platforms.api_server.enabled true ;; esac
case "$(get platforms.api_server.host)" in ""|None|null|127.0.0.1|localhost) set_cfg platforms.api_server.host 0.0.0.0 ;; esac
port="$(get platforms.api_server.port)"
case "$port" in ""|None|null) port=8642; set_cfg platforms.api_server.port 8642 ;; esac

env_file="$HERMES_HOME/.env"
key="$(grep -E '^API_SERVER_KEY=' "$env_file" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d "\"'" || true)"
if [ "${#key}" -lt 32 ]; then
  key="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  touch "$env_file"; chmod 600 "$env_file"
  grep -v '^API_SERVER_KEY=' "$env_file" > "$tmp/env" || true
  printf 'API_SERVER_KEY=%s\n' "$key" >> "$tmp/env"
  cat "$tmp/env" > "$env_file"
  echo "  created a new API_SERVER_KEY in $env_file"
fi

# Addresses JARVIS might reach this computer on (LAN first).
addrs="$( (hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null || true) | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^127\.' || true)"
urls=""
for a in $addrs; do urls="$urls http://$a:$port"; done
[ -n "$urls" ] || urls=" http://$(hostname):$port"

if [ -n "$PAIR" ]; then
  json_urls="$(printf '%s\n' $urls | sed 's/.*/"&"/' | paste -sd, -)"
  if curl -fsS -X POST "$JARVIS_URL/jarvis/api/pair" -H 'Content-Type: application/json' -H 'X-JARVIS: 1' \
       -d "{\"code\":\"$PAIR\",\"urls\":[$json_urls],\"api_key\":\"$key\"}" >/dev/null 2>&1; then
    echo "Sent the address and key to JARVIS. It connects as soon as this gateway is back up."
  else
    echo "Could not reach JARVIS at $JARVIS_URL to pair; enter the address and key on its connect screen instead."
  fi
fi

echo
echo "=== JARVIS connection ==="
for u in $urls; do echo "Address: $u"; done
echo "API key: $key"
echo "========================="

if [ "$RESTART" = 1 ]; then
  echo "Restarting the Hermes gateway in 5 seconds so it loads the connector ..."
  (nohup sh -c 'sleep 5; hermes gateway restart' >/dev/null 2>&1 &)
else
  echo "Restart the Hermes gateway to load the connector: hermes gateway restart"
fi
"""
