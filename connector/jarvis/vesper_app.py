"""The dashboard as a home-screen app: the web app manifest, the persona icons and a small service worker.

The manifest names the app after the persona and uses that persona's core as the icon (JARVIS's sphere, Cortana's
hologram, Ultron's taken-over core). The page links /vesper/manifest.webmanifest?persona=<id> and swaps the link,
the apple-touch-icon and the favicon when the persona changes, so "Add to Home Screen" picks up the one in use; with
no ?persona the saved preference decides. The icons are PNGs rendered from the cores
(~/.cache/jarvis-dev/gen-app-icons.py), kept in assets/vesper/icons/.

The service worker never caches the dashboard: every request goes to the network, so a deploy is live on the next
load. It exists so browsers treat the page as an installable app, and so a launch while the server is unreachable
shows a plain "can't reach the server" page with a retry instead of the browser's error screen. Browsers only run
service workers on https (or localhost); on plain http the page still installs as a home-screen shortcut.
"""

import json
import re
from pathlib import Path

from aiohttp import web

ICONS_DIR = Path(__file__).resolve().parent / "assets" / "vesper" / "icons"
# persona -> (app name, background colour, theme colour)
APPS = {
    "jarvis": ("JARVIS", "#071228", "#071428"),
    "cortana": ("Cortana", "#030a21", "#040b25"),
    "ultron": ("Ultron", "#0a0404", "#0c0505"),
}
_ICON_NAME = re.compile(r"^(jarvis|cortana|ultron)-(32|180|192|512|maskable-512)\.png$")


def http_routes(adapter):
    return [
        ("GET", "/vesper/manifest.webmanifest", manifest),
        ("GET", "/vesper/icons/{name}", icon),
        ("GET", "/vesper/sw.js", service_worker),
    ]


def persona_or_saved(requested: str) -> str:
    if requested in APPS:
        return requested
    from .vesper_prefs import read_prefs
    saved = read_prefs().get("persona")
    return saved if saved in APPS else "jarvis"


def manifest_for(persona: str) -> dict:
    name, background, theme = APPS[persona]
    icon = lambda size, purpose="any": {
        "src": f"/vesper/icons/{persona}-{'maskable-512' if purpose == 'maskable' else size}.png",
        "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
    return {
        # One id for every persona: switching persona restyles the same installed app, not a second one.
        "id": "/vesper", "name": name, "short_name": name,
        "description": f"{name}, your Hermes assistant: chat, voice, notes and agents.",
        "start_url": "/vesper", "scope": "/vesper", "display": "standalone", "orientation": "any",
        "background_color": background, "theme_color": theme,
        "icons": [icon(192), icon(512), icon(512, "maskable")],
    }


async def manifest(request):
    import asyncio
    persona = await asyncio.to_thread(persona_or_saved, request.query.get("persona", ""))
    return web.Response(text=json.dumps(manifest_for(persona)), content_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


async def icon(request):
    name = request.match_info["name"]
    path = ICONS_DIR / name
    if not _ICON_NAME.match(name) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400", "Content-Type": "image/png"})


_SW = """// JARVIS dashboard service worker: network only, never a cache (see api_server_vesper_app.py).
self.addEventListener('install', () => self.skipWaiting());
// On taking over, every page that isn't a running dashboard is reloaded through this worker. That frees a page left on
// an older worker's offline page (HANDOFF 33) the moment this one arrives, with no tap. A dashboard answers the ping
// (dashboard.dc.html) and is left alone. Not awaited: navigate() waits for the load, which waits for activation.
self.addEventListener('activate', event => event.waitUntil(self.clients.claim().then(() => { reloadStrays(); })));
async function reloadStrays() {
  const pages = await self.clients.matchAll({ type: 'window' });
  await Promise.all(pages.map(page => answers(page).then(ok => ok || page.navigate(page.url)).catch(() => {})));
}
function answers(page) {
  return new Promise(resolve => {
    const channel = new MessageChannel(), timer = setTimeout(() => resolve(false), 2000);
    channel.port1.onmessage = () => { clearTimeout(timer); resolve(true); };
    page.postMessage('vesper-sw-ping', [channel.port2]);
  });
}
// Shown when loading the dashboard fails. It checks the server by itself every few seconds and when the device comes
// back online. Once the server answers, it reloads with this worker unregistered, so a fault in the worker itself
// (like the CSP that blocked every load, HANDOFF 33) can never keep the app stuck here; the page registers it again.
const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Can't reach the server</title><meta name="theme-color" content="#071428">
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#071228;color:#dbe9ff;font:15px/1.5 system-ui,sans-serif;text-align:center;padding:24px">
<main><div style="width:64px;height:64px;margin:0 auto 18px;border-radius:50%;border:2px solid #5cc8ff;box-shadow:0 0 30px #5cc8ff55"></div>
<h1 style="font-size:19px;margin:0 0 8px">Can't reach the server</h1>
<p style="margin:0 0 20px;color:#82a6d4">Check that this device is on your home network, or on Tailscale away from home.</p>
<button id="retry" style="font:inherit;min-height:44px;padding:12px 26px;border-radius:10px;border:1px solid #5cc8ff;background:#5cc8ff;color:#03101f;font-weight:600">Try again</button>
<p id="note" role="status" style="margin:16px 0 0;min-height:1.5em;color:#82a6d4;font-size:13px">Trying again by itself every few seconds.</p></main>
<script>
const note = document.getElementById('note'), button = document.getElementById('retry');
let busy = false;
async function retry(byHand) {
  if (busy) return;
  busy = true; button.disabled = true;
  if (byHand) note.textContent = 'Checking\\u2026';
  try {
    const res = await fetch('/health', { cache: 'no-store' });   // not a page load, so the worker lets it through
    if (res.ok) {
      note.textContent = 'Connected. Loading\\u2026';
      const regs = navigator.serviceWorker ? await navigator.serviceWorker.getRegistrations() : [];
      await Promise.all(regs.map(reg => reg.unregister()));
      location.reload();
      return;
    }
  } catch (e) { /* still unreachable */ }
  note.textContent = byHand ? "Still can't reach it. Trying again by itself every few seconds." : note.textContent;
  busy = false; button.disabled = false;
}
button.addEventListener('click', () => retry(true));
addEventListener('online', () => retry(false));
document.addEventListener('visibilitychange', () => { if (!document.hidden) retry(false); });
setInterval(() => { if (!document.hidden) retry(false); }, 5000);
</script>`;
self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;   // everything else goes straight to the network, untouched
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE, {
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } })));
});
"""
# A worker's own fetch() obeys the CSP its script is served with. The global default (default-src 'none') blocked the
# worker's network request for every page load, so once installed it showed OFFLINE forever (HANDOFF 33).
SW_CSP = "default-src 'none'; connect-src 'self'; frame-ancestors 'none'"


async def service_worker(request):
    # Served from /vesper/sw.js but controls /vesper itself (no trailing slash), so it may widen its scope to /vesper.
    return web.Response(text=_SW, content_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/vesper",
                                 "Content-Security-Policy": SW_CSP})
