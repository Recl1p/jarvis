"""The dashboard's own HTTP surface that used to live inside Hermes's api_server.py (JARVIS fork).

Same handlers, now plain functions taking the stock ``APIServerAdapter``: the page itself (/vesper, unauthenticated so it
opens in a browser, with the adapter's API key stamped on <body> for the dashboard's same-origin calls), the readiness
summary (/vesper/status), voice in and out (/vesper/tts, /vesper/stt), weather, file downloads and the radio proxy.
"""

import asyncio
import html as _html
import json
import logging
import os
import re
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "vesper"
# The dashboard's dc-runtime needs 'unsafe-eval' (it compiles its component via new Function())
# plus the CDN/font hosts it loads from; the global default is default-src 'none', which the
# security_headers_middleware only setdefault()s, so setting this on the response overrides it.
CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self' https://speed.cloudflare.com; "
    "img-src 'self' data:; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "media-src blob: 'self'; "
    "frame-ancestors 'none'")

# Cron job state / execution status -> dashboard tile color (unknown states fall back to blue).
AGENT_COLORS = {
    "completed": "#7fb2dd", "success": "#7fb2dd", "error": "#ffb347", "failed": "#ffb347",
    "running": "#4fc3ff", "scheduled": "#4fc3ff", "paused": "#5f8fb8"}


# Curated allowlist for /vesper/radio — keys only, so the client never carries a raw upstream
# URL and this can't become an open proxy for arbitrary hosts. Each station lists its streams best
# first: SomaFM publishes 256k MP3 for only some channels (the rest 404 there), and 128k MP3 on
# every channel and relay, so the proxy tries them in order.
def _somafm(channel: str, hq: bool = False) -> tuple:
    return ((f"https://ice1.somafm.com/{channel}-256-mp3",) if hq else ()) + (
        f"https://ice1.somafm.com/{channel}-128-mp3", f"https://ice2.somafm.com/{channel}-128-mp3")


STATIONS = {
    "groovesalad": _somafm("groovesalad", hq=True),
    "dronezone": _somafm("dronezone", hq=True),
    "spacestation": _somafm("spacestation"),
    "beatblender": _somafm("beatblender"),
    "secretagent": _somafm("secretagent"),
    "lush": _somafm("lush"),
    "deepspaceone": _somafm("deepspaceone")}

# WMO weather code -> short text (https://open-meteo.com/en/docs, "WMO Weather interpretation codes").
WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm (hail)", 99: "Thunderstorm (hail)"}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _error(message, status, code):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def http_routes(adapter):
    return [
        ("GET", "/", root_redirect),
        ("GET", "/vesper", partial(dashboard, adapter)),
        ("GET", "/vesper/support.js", support_js),
        ("GET", "/vesper/status", partial(status, adapter)),
        ("POST", "/vesper/tts", partial(tts, adapter)),
        ("POST", "/vesper/stt", partial(stt, adapter)),
        ("GET", "/vesper/weather", weather),
        ("GET", "/vesper/files", serve_file),
        ("GET", "/vesper/radio", radio),
    ]


def page_prefs(page: str) -> str:
    """The saved dashboard preferences on <body data-vesper-prefs>, and the home-screen app links (manifest,
    apple-touch-icon, favicon, app title) set to the saved persona, so a fresh device or a home-screen launch
    starts as the user left it (vesper_prefs.py, vesper_app.py)."""
    from .vesper_app import APPS
    from .vesper_prefs import read_prefs
    prefs = read_prefs()
    persona = prefs.get("persona") if prefs.get("persona") in APPS else "jarvis"
    page = page.replace('<body data-vesper-key=', f'<body data-vesper-prefs="{_html.escape(json.dumps(prefs), quote=True)}" data-vesper-key=', 1)
    if persona != "jarvis":
        page = (page.replace("/vesper/icons/jarvis-", f"/vesper/icons/{persona}-")
                .replace("/vesper/manifest.webmanifest?persona=jarvis", f"/vesper/manifest.webmanifest?persona={persona}")
                .replace('<meta name="apple-mobile-web-app-title" content="JARVIS">',
                         f'<meta name="apple-mobile-web-app-title" content="{APPS[persona][0]}">'))
    return page


# The template's event placeholders (onClick="{{ send }}"). Parsed as HTML they are inline event handlers, which the
# CSP blocks, logging an error for each one every time the page or the runtime parses the markup (about 270 per load).
_EVENT_PLACEHOLDER = re.compile(r"(\s)on([A-Z][A-Za-z0-9]*)(\s*=)")


def encode_event_placeholders(page: str) -> str:
    """Serve onClick="..." as sc-camel-on-click="...": the dc runtime's own encoding for camelCase attributes
    (support.js encodeCase / CAMEL_ATTR), which it decodes back to onClick when it compiles the template. The browser
    then never parses an on* attribute, and the dashboard gets the same event props. Only the template markup inside
    <x-dc>...</x-dc> is touched: the logic script after it uses these names as JavaScript."""
    start, end = page.find("<x-dc>"), page.rfind("</x-dc>")
    if start < 0 or end < start:
        return page
    template = _EVENT_PLACEHOLDER.sub(
        lambda m: m.group(1) + "sc-camel-" + re.sub(r"[A-Z]", lambda c: "-" + c.group(0).lower(), "on" + m.group(2))
        + m.group(3),
        page[start:end])
    return page[:start] + template + page[end:]


async def dashboard(adapter, request):
    """GET /vesper — the dashboard shell (static asset: unauthenticated like /health so it opens directly in a
    browser). Stamps the API_SERVER_KEY onto <body> (as a data attribute, not inline JS, so the relaxed CSP doesn't
    need 'unsafe-inline') so the dashboard's same-origin fetches to /api/sessions/* can authenticate themselves.
    That is only reasonable on a trusted, single-user network: whoever can open this page controls the agent."""
    page = encode_event_placeholders((ASSETS_DIR / "dashboard.dc.html").read_text(encoding="utf-8"))
    key = _html.escape(adapter._expected_api_key() or "", quote=True)
    page = page.replace("<body>", f'<body data-vesper-key="{key}">', 1)
    page = await asyncio.to_thread(page_prefs, page)
    resp = web.Response(text=page, content_type="text/html")
    resp.headers["Content-Security-Policy"] = CSP
    # The global default is microphone=() (blocked); the dashboard's mic button needs it.
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(self)"
    return resp


async def root_redirect(request):
    """GET / -- sends a bare-port visit (Dockge's port links, a typed host:port) to the dashboard.
    Relative, so the /p/<profile>/ mirror lands on that profile's dashboard too."""
    raise web.HTTPFound("vesper")


async def support_js(request):
    """GET /vesper/support.js — dc-runtime bundle that boots the dashboard template."""
    resp = web.Response(text=(ASSETS_DIR / "support.js").read_text(encoding="utf-8"),
                        content_type="application/javascript")
    resp.headers["Content-Security-Policy"] = CSP
    return resp


def _cron():
    """(available, list_jobs) from the api_server module, which imports the cron store when it exists."""
    from gateway.platforms import api_server
    return bool(getattr(api_server, "_CRON_AVAILABLE", False)), getattr(api_server, "_cron_list", None)


async def status(adapter, request):
    """GET /vesper/status — bounded, non-sensitive gateway readiness + cron job queue for the dashboard
    (unauthenticated like /vesper: counts/status only, no config, credentials, paths, or payloads)."""
    from gateway.readiness import collect_runtime_readiness
    from gateway.run import _resolve_gateway_model
    from gateway.status import read_runtime_status
    runtime = read_runtime_status() or {}
    active_api_runs, process_depth, active_delegations = adapter._readiness_work_counts()
    readiness = collect_runtime_readiness(
        configured_model=_resolve_gateway_model(), runtime_status=runtime,
        active_api_runs=active_api_runs, process_completion_queue_depth=process_depth,
        active_delegations=active_delegations)
    checks = readiness.get("checks", {})

    def _pct(check: dict) -> float:
        return 100.0 if check.get("status") == "ok" else 35.0

    disk = checks.get("disk", {})
    gw = checks.get("gateway", {})
    max_runs = max(0, int(getattr(adapter, "_max_concurrent_runs", 0) or 0))
    platforms_total = int(gw.get("platforms", 0) or 0)
    platforms_up = int(gw.get("connected_platforms", 0) or 0)

    vitals = [
        {"label": "GATEWAY", "value": str(gw.get("state", "unknown")).upper(), "pct": _pct(gw)},
        {"label": "STATE DB", "value": str(checks.get("state_db", {}).get("status", "?")).upper(),
         "pct": _pct(checks.get("state_db", {}))},
        {"label": "SESSION STORE", "value": str(checks.get("session_store", {}).get("status", "?")).upper(),
         "pct": _pct(checks.get("session_store", {}))},
        {"label": "DISK USED", "value": f"{disk.get('used_percent', 0):.1f}%",
         "pct": disk.get("used_percent", 0)},
        {"label": "API RUNS", "value": f"{active_api_runs}/{max_runs}" if max_runs else str(active_api_runs),
         "pct": (active_api_runs / max_runs * 100) if max_runs else 0},
        {"label": "PLATFORMS", "value": f"{platforms_up}/{platforms_total}",
         "pct": (platforms_up / platforms_total * 100) if platforms_total else 0}]

    agents, agenda, jobs = [], [], []
    cron_available, cron_list = _cron()
    if cron_available and cron_list is not None:
        try:
            jobs = cron_list(include_disabled=False)
        except Exception:
            jobs = []
        for job in jobs[:6]:
            latest = job.get("latest_execution") or {}
            display_state = str(latest.get("status") or job.get("state") or "scheduled").lower()
            pct = {"completed": "100%", "success": "100%", "error": "100%", "failed": "100%",
                   "running": "60%", "scheduled": "10%", "paused": "0%"}.get(display_state, "40%")
            agents.append({
                "name": str(job.get("name") or job.get("id") or "job")[:40],
                "state": display_state.upper(), "pct": pct,
                "color": AGENT_COLORS.get(display_state, "#4fc3ff")})

        # AGENDA panel: real upcoming scheduled jobs, not a separate data store — asking JARVIS to
        # "add/clear something on my agenda" is just it using cronjob_manage.
        upcoming = []
        for job in jobs:
            next_run = job.get("next_run_at")
            if not next_run:
                continue
            try:
                dt = datetime.fromisoformat(next_run)
            except ValueError:
                continue
            upcoming.append((dt, job))
        upcoming.sort(key=lambda pair: pair[0])
        for dt, job in upcoming[:5]:
            agenda.append({
                "time": dt.strftime("%H:%M"),
                # Absolute instant for the browser to show in ITS local time with a day.
                "at": dt.isoformat() if dt.tzinfo else None,
                "label": str(job.get("name") or job.get("id") or "task")[:60]})

    # NOTIFICATIONS: derived from real job completions/failures, plus a file JARVIS can append
    # {tag, color, text} objects to at $HERMES_HOME/vesper_notifications.json.
    notifications = []
    for job in jobs:
        latest = job.get("latest_execution") or {}
        state = str(latest.get("status") or "").lower()
        if state in ("completed", "failed"):
            name = str(job.get("name") or job.get("id") or "task")[:50]
            err = latest.get("error")
            notifications.append({
                "ts": str(latest.get("finished_at") or latest.get("claimed_at") or ""),
                "tag": "TASK DONE" if state == "completed" else "TASK FAILED",
                "color": "#7fb2dd" if state == "completed" else "#ffb347",
                "text": name + (f" — {err}" if state == "failed" and err else "")})
    try:
        from hermes_constants import get_hermes_home
        custom_path = get_hermes_home() / "vesper_notifications.json"
        if custom_path.exists():
            custom = json.loads(custom_path.read_text(encoding="utf-8"))
            if isinstance(custom, list):
                for n in custom[-20:]:
                    if isinstance(n, dict) and n.get("text"):
                        notifications.append({
                            "ts": str(n.get("ts") or ""),
                            "tag": str(n.get("tag") or "NOTE")[:24],
                            "color": str(n.get("color") or "#4fc3ff"),
                            "text": str(n.get("text"))[:200]})
    except Exception:
        logger.debug("vesper notifications: custom file read failed", exc_info=True)
    notifications.sort(key=lambda n: n["ts"], reverse=True)
    notifications = [{k: v for k, v in n.items() if k != "ts"} for n in notifications[:8]]

    return web.json_response({"vitals": vitals, "agents": agents, "agenda": agenda, "notifications": notifications})


async def tts(adapter, request):
    """POST /vesper/tts {text, persona, engine, voice_id, edge_voice, piper_voice} -- audio bytes for one sentence in
    the persona's voice, from the engine picked in the dashboard, falling back to a quicker one when it fails
    (vesper_voice.py). Bearer-gated like the chat endpoints since ElevenLabs calls spend the user's characters."""
    auth = adapter._check_auth(request)
    if auth is not None:
        return auth
    body, err = await adapter._read_json_body(request)
    if err:
        return err
    text = str(body.get("text") or "").strip()[:2000]
    if not text:
        return _error("Missing 'text' field", 400, "missing_text")
    # Persona voices: an ElevenLabs voice id, edge-tts and Piper voices, checked by shape so a request can
    # only pick a voice, never point a provider elsewhere.
    voice_id = str(body.get("voice_id") or "").strip()
    edge_voice = str(body.get("edge_voice") or "").strip()
    piper_voice = str(body.get("piper_voice") or "").strip()
    if voice_id and not re.fullmatch(r"[A-Za-z0-9]{16,32}", voice_id):
        return _error("Invalid voice_id", 400, "invalid_voice")
    if edge_voice and not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural", edge_voice):
        return _error("Invalid edge_voice", 400, "invalid_voice")
    if piper_voice and not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}-[a-z0-9_]+-(?:x_low|low|medium|high)", piper_voice):
        return _error("Invalid piper_voice", 400, "invalid_voice")
    from . import vesper_voice
    persona = str(body.get("persona") or "jarvis").strip()
    if persona not in vesper_voice.PERSONAS:
        return _error("Invalid persona", 400, "invalid_voice")
    choice = body.get("engine") or None
    if choice is not None and choice not in vesper_voice.ENGINES:
        return _error("Invalid engine", 400, "invalid_voice")
    try:
        spoken = await vesper_voice.speak(request, text, persona, choice, voice_id=voice_id,
                                          edge_voice=edge_voice, piper_voice=piper_voice)
    except Exception as exc:
        return _error(f"TTS synthesis failed (every voice, the local one included): {exc}", 502, "tts_failed")
    if spoken is None:   # the browser stopped waiting (the user talked over it)
        return web.Response(status=204)
    audio_bytes, content_type, used = spoken
    # Which engine actually spoke (a fallback is otherwise silent): elevenlabs | pocket | omnivoice | edge | local
    return web.Response(body=audio_bytes, content_type=content_type, headers={"X-Vesper-Voice": used})


async def stt(adapter, request):
    """POST /vesper/stt — transcribes recorded mic audio (raw request body from the browser's MediaRecorder) via
    Hermes's own configured STT provider. The browser's SpeechRecognition can't reach Google's backend on
    non-Google Chromium builds like Brave, so the dashboard never relies on it."""
    auth = adapter._check_auth(request)
    if auth is not None:
        return auth
    raw = await request.read()
    if not raw:
        return _error("Empty audio body", 400, "missing_audio")
    import tempfile
    from tools.transcription_tools import transcribe_audio
    # Safari/iOS MediaRecorder records audio/mp4, and the STT pipeline validates by extension.
    suffix = {"audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/wav": ".wav",
              "audio/mpeg": ".mp3"}.get(request.content_type, ".webm")
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        result = await asyncio.to_thread(transcribe_audio, tmp_path, None, "vesper-dashboard")
    finally:
        with suppress(Exception):
            os.remove(tmp_path)
    if not result.get("success"):
        return _error(result.get("error") or "Transcription failed", 502, "stt_failed")
    return web.json_response({"text": result.get("transcript", "")})


async def weather(request):
    """GET /vesper/weather?lat=..&lon=.. — proxies Open-Meteo (free, no API key) for the ATMOS panel. Public like
    /vesper/status: no cost, no sensitive data, just a location the client chose to share."""
    place = None
    if "lat" not in request.query and "lon" not in request.query:
        # No coordinates from the browser (plain-http LAN origins can't geolocate): use the saved location.
        from .vesper import load_saved_location
        saved = await asyncio.to_thread(load_saved_location)
        if saved is None:
            return _error("No weather location saved yet", 404, "location_not_set")
        lat, lon, place = saved["lat"], saved["lon"], saved["name"]
    else:
        try:
            lat, lon = float(request.query.get("lat", "")), float(request.query.get("lon", ""))
        except ValueError:
            return _error("Missing/invalid lat/lon", 400, "invalid_location")
    import aiohttp as _aiohttp_client
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m"
           "&hourly=precipitation_probability&timezone=auto&forecast_days=1")
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get(url, timeout=_aiohttp_client.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return _error(f"Weather service error (HTTP {resp.status})", 502, "weather_failed")
                data = await resp.json()
    except Exception as exc:
        return _error(f"Weather fetch failed: {exc}", 502, "weather_failed")

    current = data.get("current") or {}
    temp, wind_speed, wind_dir = current.get("temperature_2m"), current.get("wind_speed_10m"), current.get("wind_direction_10m")
    condition = WEATHER_CODES.get(current.get("weather_code"), "Unknown")
    compass = COMPASS[round(wind_dir / 22.5) % 16] if wind_dir is not None else ""

    hourly = data.get("hourly") or {}
    rain_note = "no rain expected"
    now_iso = current.get("time", "")
    for t, p in zip(hourly.get("time", []), hourly.get("precipitation_probability", [])):
        if t > now_iso and p is not None and p >= 50:
            rain_note = f"rain {t.split('T')[-1]}"
            break

    return web.json_response({
        "temp_c": round(temp) if temp is not None else None,
        "condition": condition,
        "wind_kmh": round(wind_speed) if wind_speed is not None else None,
        "wind_dir": compass,
        "rain_note": rain_note,
        "location": place})


# JARVIS's own private files, which Hermes's credential block doesn't know about: the launcher's login and connection
# state, the browser desktop's profile (every site login the agent's browser holds), the voice pack and the gated
# Pocket TTS weights, and SSH keys.
_JARVIS_PRIVATE_DIRS = ("jarvis", "openjarvis/browser", "vesper-voices/pack", "pocket-tts", ".ssh")


def _jarvis_private(real_path: str) -> str:
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home()).resolve()
    target = Path(real_path)
    for sub in _JARVIS_PRIVATE_DIRS:
        root = (home / sub).resolve()
        if target == root or root in target.parents:
            return f"{real_path} is private to JARVIS and can't be downloaded."
    if ".ssh" in target.parts:
        return f"{real_path} is an SSH key folder and can't be downloaded."
    return ""


async def serve_file(request):
    """GET /vesper/files?path=<absolute path> — serves a file JARVIS created (a report, a generated PDF) so a link
    it gives in chat is directly downloadable. Only through the JARVIS launcher's login or with the API key
    (wiring.py, _LAUNCHER_ONLY), and never Hermes's credential stores (.env, auth.json, ...) or JARVIS's own login and
    voice files, whatever link leads there."""
    path = request.query.get("path", "")
    if not path or not os.path.isabs(path):
        return _error("Missing/invalid 'path' (must be an absolute path)", 400, "invalid_path")
    from agent.file_safety import get_read_block_error
    real_path = os.path.realpath(path)
    # Both the path as given and where it really leads: a link in the workspace must not reach .env.
    blocked = get_read_block_error(path) or get_read_block_error(real_path) or _jarvis_private(real_path)
    if blocked:
        return _error(blocked, 403, "file_blocked")
    if not os.path.isfile(real_path):
        return _error("File not found", 404, "file_not_found")
    if os.path.getsize(real_path) > 100 * 1024 * 1024:
        return _error("File too large to serve (>100MB)", 413, "file_too_large")
    return web.FileResponse(
        real_path, headers={"Content-Disposition": f'attachment; filename="{os.path.basename(real_path)}"'})


async def radio(request):
    """GET /vesper/radio?station=<key> — proxies a curated SomaFM stream server-side. Chromium's <audio> element
    sends a Range + Accept-Encoding:identity combination that SomaFM rejects with 403, while a plain server-side
    fetch works. `station` is a key into a fixed allowlist, never a raw URL, so this can't become an open proxy."""
    urls = STATIONS.get(request.query.get("station", ""))
    if not urls:
        return _error("Unknown station", 400, "invalid_station")
    import aiohttp as _aiohttp_client
    session = _aiohttp_client.ClientSession()
    upstream, problem = None, ""
    for url in urls:
        try:
            upstream = await session.get(
                url, timeout=_aiohttp_client.ClientTimeout(total=None, sock_connect=8, sock_read=30))
        except Exception as exc:
            upstream, problem = None, f"Station fetch failed: {exc}"
            continue
        if upstream.status == 200:
            break
        problem = f"Station returned HTTP {upstream.status}"
        upstream.release()
        upstream = None
    if upstream is None:
        await session.close()
        return _error(problem or "Station unavailable", 502, "radio_failed")
    resp = web.StreamResponse(status=200, headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-store"})
    await resp.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(65536):
            await resp.write(chunk)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError, asyncio.CancelledError):
        pass
    finally:
        upstream.release()
        await session.close()
    return resp
