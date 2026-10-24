"""The launcher's web server on :8642.

``/jarvis/`` is the setup screen and its API; everything else goes to Hermes once JARVIS has one (the dashboard is
Hermes's ``/vesper``, served by the connector). Before that, ``/`` and ``/vesper`` open the setup screen.

Every request passes the login first (auth.py): a session cookie, the password as Basic auth, or Hermes's API key. A
few paths stay open because something without the password needs them: health checks, the login page itself, and the
connect script, connector download and pairing call that someone's own Hermes uses ("I already have Hermes", guarded by
its one-time pairing code). The browser desktop (noVNC) is served behind the same login at /desktop/.

Changes also need the ``X-JARVIS: 1`` header (a cross-site page can't send it without a CORS preflight, which is never
approved) and, once a Hermes is connected, its API key as a Bearer token, which the setup page gets the same way the
dashboard does.
"""

import asyncio
import hmac
import html
import json
import logging
import os
import re
import socket
import time
from typing import Optional
from urllib.parse import quote

import aiohttp
from aiohttp import web

from . import JARVIS_VERSION, auth, checks, config, connect, detect, hermes_setup, state
from .managed import Gateway, InstallJob
from .proxy import Proxy
from .updates import Updater, hermes_version

logger = logging.getLogger(__name__)

PAIR_WAIT_SECONDS = 180
# noVNC, told to open its WebSocket under /desktop/ too (it defaults to /websockify at the root).
DESKTOP_PAGE = "/desktop/vnc.html?autoconnect=true&resize=remote&path=desktop/websockify"
# The desktop shows every site the agent is logged into: no other site may frame it.
DESKTOP_HEADERS = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'",
                   "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


class Launcher:
    def __init__(self):
        self.gateway = Gateway()
        self.installer = InstallJob(self.gateway)
        self.updater = Updater(self.gateway, self.installer)
        self.proxy = Proxy()
        self.pair = {"status": "idle", "detail": ""}    # idle | waiting | connected | failed
        self._health = (0.0, None)                       # (checked at, healthy?)
        self._version = (0.0, "")                        # (checked at, managed Hermes version)

    async def managed_version(self) -> str:
        """``hermes --version`` takes a second or two, so the setup screen's polling reuses it for a few minutes."""
        checked, value = self._version
        if time.monotonic() - checked > 300 or not value or self.updater.busy:
            value = await asyncio.to_thread(hermes_version)
            self._version = (time.monotonic(), value)
        return value

    # --- where Hermes is --------------------------------------------------------------------------------------------

    def target(self) -> Optional[str]:
        data = state.load()
        if data.get("mode") == "managed":
            return config.MANAGED_HERMES_URL
        if data.get("mode") == "external" and data.get("hermes_url"):
            return data["hermes_url"]
        return None

    def api_key(self) -> str:
        data = state.load()
        if data.get("mode") == "managed":
            return hermes_setup.read_env_value("API_SERVER_KEY")
        return data.get("api_key") or ""

    async def healthy(self) -> Optional[bool]:
        target = self.target()
        if not target:
            return None
        checked, value = self._health
        if time.monotonic() - checked < 5:
            return value
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                async with session.get(f"{target}/health") as resp:
                    value = resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            value = False
        self._health = (time.monotonic(), value)
        return value

    # --- startup ----------------------------------------------------------------------------------------------------

    async def on_startup(self, app) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = state.load()
        if data.get("mode") == "managed":
            if hermes_setup.installed():
                # prestart.py normally brought the connector up to date before the gateway started; if it couldn't,
                # the gateway runs the old one and needs a restart once apply() has copied it.
                stale = not hermes_setup.connector_current()
                try:
                    # A new JARVIS image may bring a new connector, and an old data volume old settings.
                    await asyncio.to_thread(hermes_setup.apply, self.updater.log.write, managed=True, persona=False)
                except Exception:  # noqa: BLE001 — start Hermes anyway; the setup screen shows its health
                    logger.exception("Could not refresh the Hermes setup")
                if stale:
                    await self.gateway.restart()
                else:
                    await self.gateway.start()
            else:
                logger.warning("Managed mode, but Hermes is not installed in %s: the setup screen offers to reinstall",
                               config.HERMES_INSTALL_DIR)
        app["schedule"] = asyncio.create_task(self.updater.run_schedule())

    async def on_cleanup(self, app) -> None:
        # The gateway is left running: it is Hermes's own s6 service, stopped with the container, and stopping it
        # here would save "stopped" as its desired state so it would not come back after a restart.
        app["schedule"].cancel()
        await self.proxy.close()


# --- request guards --------------------------------------------------------------------------------------------------

def _guard(request: web.Request, launcher: Launcher, *, need_key: bool = True) -> Optional[web.Response]:
    if request.headers.get("X-JARVIS") != "1":
        return _error(403, "missing_header", "Requests to the setup API need the X-JARVIS: 1 header.")
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] != request.host:
        return _error(403, "cross_origin", "Cross-origin requests are not allowed.")
    if need_key and state.load().get("mode"):
        key = launcher.api_key()
        if key and request.headers.get("Authorization") != f"Bearer {key}":
            return _error(401, "unauthorized", "This JARVIS is set up: changes need its API key.")
    return None


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


# --- the login ---------------------------------------------------------------------------------------------------------

# Open without the password (compared with the raw path, so an encoded or dotted path can't pass as one of these).
PUBLIC_PATHS = frozenset({
    "/jarvis/login", "/jarvis/health", "/health", "/jarvis/api/auth", "/jarvis/api/login", "/jarvis/api/password",
    "/jarvis/connect.sh", "/jarvis/connector.tar.gz", "/jarvis/persona/SOUL.md", "/jarvis/api/pair",
    # Browsers fetch a web-app manifest without cookies; the service worker script and icons hold nothing private.
    "/vesper/manifest.webmanifest", "/vesper/sw.js",
})
PUBLIC_PREFIXES = ("/jarvis/static/", "/vesper/icons/")
LOGIN_PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                  "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                  "form-action 'self'; base-uri 'none'")


def _public(raw_path: str) -> bool:
    if raw_path in PUBLIC_PATHS:
        return True
    return (raw_path.startswith(PUBLIC_PREFIXES) and "%" not in raw_path and ".." not in raw_path
            and "//" not in raw_path and "\\" not in raw_path)


async def _authorized(request: web.Request, launcher: "Launcher") -> bool:
    if not auth.enabled():
        return True
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        key = launcher.api_key()
        if key and hmac.compare_digest(header[7:].strip().encode("utf-8"), key.encode("utf-8")):
            return True
    elif header.startswith("Basic ") and auth.has_password():   # no password yet: nothing to guess, nothing to count
        if auth.basic_cached(header):
            return True
        remote = request.remote or ""
        if not auth.locked_for(remote):
            password = auth.basic_password(header)
            if password is not None and await asyncio.to_thread(auth.check_password, password):
                auth.record_success(remote)
                auth.remember_basic(header)
                return True
            auth.record_failure(remote)
    return auth.valid_session(request.cookies.get(auth.COOKIE))


def _login_needed(request: web.Request) -> web.Response:
    page = (request.method in ("GET", "HEAD") and request.headers.get("Upgrade", "").lower() != "websocket"
            and "text/html" in request.headers.get("Accept", ""))
    if page:
        raise web.HTTPFound("/jarvis/login?next=" + quote(request.path_qs, safe=""))
    if not auth.has_password():
        return _error(401, "password_not_set", "Open JARVIS in a browser and create its password first.")
    return _error(401, "login_required", "Log in to JARVIS first (/jarvis/login), or send its password as Basic auth.")


def _safe_next(value: str) -> str:
    """Where to go after logging in: a path on this server only. Browsers drop tabs and newlines from a URL, so
    "/<tab>/evil.example" would become "//evil.example": any whitespace or control character is refused, and
    backslashes, which browsers read as "/"."""
    if (not value or not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in value)):
        return "/jarvis/"
    return value


def _https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _set_session(request: web.Request, resp: web.Response) -> None:
    resp.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_SECONDS, path="/", httponly=True,
                    samesite="Lax", secure=_https(request))


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_ORIGIN = re.compile(r"^https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?$")


def _public_url(request: web.Request) -> str:
    """This JARVIS's address for someone's own Hermes to reach (the connect prompt and script). It goes into a shell
    script, so only a plain http(s)://host[:port] is used."""
    url = (os.environ.get("JARVIS_PUBLIC_URL") or f"{request.scheme}://{request.host}").rstrip("/")
    return url if _ORIGIN.match(url) else f"http://localhost:{config.LISTEN_PORT}"


def _own_addresses() -> tuple:
    try:
        return tuple(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return ()


def _clean_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if url.endswith("/vesper"):
        url = url[: -len("/vesper")]
    return url


# --- handlers ----------------------------------------------------------------------------------------------------------

def build_app() -> web.Application:
    launcher = Launcher()

    @web.middleware
    async def login_gate(request, handler):
        if _public(request.raw_path.split("?", 1)[0]) or await _authorized(request, launcher):
            return await handler(request)
        return _login_needed(request)

    app = web.Application(client_max_size=300 * 1024 * 1024, middlewares=[login_gate])
    app["launcher"] = launcher
    app.on_startup.append(launcher.on_startup)
    app.on_cleanup.append(launcher.on_cleanup)

    async def login_page(request):
        if auth.has_password() and await _authorized(request, launcher):
            raise web.HTTPFound(_safe_next(request.query.get("next", "")))
        resp = web.FileResponse(config.STATIC_DIR / "login.html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = LOGIN_PAGE_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    async def auth_status(request):
        return web.json_response({"enabled": auth.enabled(), "has_password": auth.has_password(),
                                  "from_env": bool(auth.env_password()), "min_length": auth.MIN_PASSWORD,
                                  "logged_in": await _authorized(request, launcher)})

    async def login(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        remote = request.remote or ""
        wait = auth.locked_for(remote)
        if wait:
            return web.json_response({"error": {"code": "locked", "message": f"Too many wrong passwords. Try again in "
                                                f"{wait} seconds.", "retry_after": wait}}, status=429,
                                     headers={"Retry-After": str(wait)})
        body = await _body(request)
        if not await asyncio.to_thread(auth.check_password, body.get("password")):
            auth.record_failure(remote)
            await asyncio.sleep(1)
            return _error(401, "wrong_password", "That password isn't right.")
        auth.record_success(remote)
        resp = web.json_response({"ok": True, "next": _safe_next(str(body.get("next") or ""))})
        _set_session(request, resp)
        return resp

    async def logout(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        auth.end_session(request.cookies.get(auth.COOKIE))
        resp = web.json_response({"ok": True})
        resp.del_cookie(auth.COOKIE, path="/")
        return resp

    async def set_password(request):
        """First run: anyone who reaches JARVIS before a password exists creates it. After that, changing it needs the
        login and the current password."""
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        if not auth.enabled():
            return _error(409, "auth_off", "The login is turned off (JARVIS_AUTH=off).")
        if auth.env_password():
            return _error(409, "from_env", "The password comes from JARVIS_PASSWORD; change it there.")
        body = await _body(request)
        if auth.has_password():
            if not await _authorized(request, launcher):
                return _error(401, "login_required", "Log in first.")
            remote = request.remote or ""
            if auth.locked_for(remote):
                return _error(429, "locked", "Too many wrong passwords. Try again later.")
            if not await asyncio.to_thread(auth.check_password, body.get("current")):
                auth.record_failure(remote)
                return _error(401, "wrong_password", "Your current password isn't right.")
        problem = auth.password_problem(body.get("password"))
        if problem:
            return _error(400, "weak_password", problem)
        await asyncio.to_thread(auth.set_password, body["password"])
        resp = web.json_response({"ok": True})
        _set_session(request, resp)
        return resp

    async def desktop(request):
        rel = str(request.rel_url)[len("/desktop"):] or "/"
        return await launcher.proxy.forward(request, config.DESKTOP_URL, path=rel, name="the browser desktop",
                                            extra_headers=DESKTOP_HEADERS)

    async def desktop_home(request):
        raise web.HTTPFound(DESKTOP_PAGE)

    async def setup_page(request):
        page = (config.STATIC_DIR / "setup.html").read_text(encoding="utf-8")
        key = launcher.api_key() if state.load().get("mode") else ""
        page = page.replace("<body>", f'<body data-jarvis-key="{html.escape(key, quote=True)}">', 1)
        resp = web.Response(text=page, content_type="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'self'; style-src 'self' "
                                                   "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        return resp

    async def static(request):
        name = request.match_info["name"]
        path = (config.STATIC_DIR / name).resolve()
        if path.parent != config.STATIC_DIR.resolve() or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def launcher_health(request):
        return web.json_response({"status": "ok", "platform": "jarvis-launcher", "version": JARVIS_VERSION})

    async def get_state(request):
        data = state.load()
        installed = hermes_setup.installed()
        healthy = await launcher.healthy()
        updates = {k: data["updates"].get(k) for k in ("auto", "hour", "last_check", "last_check_result", "last_result")}
        updates.update(busy=launcher.updater.busy, step=launcher.updater.step)
        return web.json_response({
            "jarvis_version": JARVIS_VERSION,
            "mode": data.get("mode"),
            "configured": bool(data.get("mode")),
            "hermes": {
                "url": launcher.target() or "", "installed": installed,
                "version": (await launcher.managed_version()) if installed and data.get("mode") == "managed" else "",
                "running": launcher.gateway.running, "healthy": healthy,
                "restarts": launcher.gateway.restarts,
            },
            # An old JARVIS data volume (Hermes config, memories, logins) without Hermes installed in it yet.
            "previous_data": (config.HERMES_HOME / "config.yaml").exists() and not installed,
            "install": {"status": launcher.installer.status, "step": launcher.installer.step,
                        "error": launcher.installer.error, "busy": launcher.installer.busy},
            "consent_info": {"installer_url": config.HERMES_INSTALLER_URL, "repo": config.HERMES_REPO_URL,
                             "license": config.HERMES_LICENSE_URL, "channel": config.HERMES_CHANNEL,
                             "install_dir": str(config.HERMES_INSTALL_DIR), "hermes_home": str(config.HERMES_HOME),
                             "accepted": data.get("consent")},
            "connector": {"bundled": hermes_setup.bundled_connector_version(),
                          "installed": hermes_setup.installed_connector_version() if data.get("mode") == "managed" else ""},
            "persona": data.get("persona", True),
            "pair": launcher.pair,
            "updates": updates,
        })

    async def install(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        body = await _body(request)
        if body.get("accept") is not True:
            return _error(400, "consent_required", "Installing Hermes needs your explicit consent (accept: true).")
        if launcher.installer.busy:
            return _error(409, "busy", "An install is already running.")
        if state.load().get("mode") == "external":
            return _error(409, "external", "JARVIS is connected to your own Hermes. Disconnect it first.")
        persona = bool(body.get("persona", True))
        data = state.load()
        data["updates"]["auto"] = bool(body.get("auto_update", data["updates"].get("auto", True)))
        state.save(data)
        state.update(persona=persona, consent={
            "accepted_at": state.now(), "installer_url": config.HERMES_INSTALLER_URL,
            "channel": config.HERMES_CHANNEL, "from": request.remote or ""})
        launcher.installer.start(persona=persona)
        return web.json_response({"started": True}, status=202)

    async def install_log(request):
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            offset = 0
        source = launcher.updater.log if request.query.get("which") == "updates" else launcher.installer.log
        lines, next_offset = source.since(offset)
        return web.json_response({"lines": lines, "offset": next_offset, "status": launcher.installer.status,
                                  "step": launcher.installer.step, "error": launcher.installer.error})

    async def find(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        host_ip = (request.host or "").rsplit(":", 1)[0].strip("[]")
        found = await detect.find(client_ip=request.remote or "", own_addresses=_own_addresses(), host_ip=host_ip)
        return web.json_response({"candidates": found})

    async def test(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        body = await _body(request)
        url = _clean_url(body.get("url") or "") or launcher.target() or ""
        if not url:
            return _error(400, "missing_url", "Which Hermes? Give its address.")
        key = str(body.get("api_key") or "") or detect.mounted_key(url) or (
            launcher.api_key() if url == launcher.target() else "")
        result = await checks.run(url, key)
        return web.json_response({"url": url, **result})

    async def do_connect(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        body = await _body(request)
        url = _clean_url(body.get("url") or "")
        key = str(body.get("api_key") or "") or detect.mounted_key(url)
        if not url:
            return _error(400, "missing_url", "Give the address of your Hermes API server.")
        result = await checks.run(url, key)
        if not result["ok"]:
            # Not connected is an answer, not an error: the screen shows the checks (and the console stays clean).
            return web.json_response({"connected": False, "url": url, **result})
        if state.load().get("mode") == "managed":
            await launcher.gateway.stop()
        state.update(mode="external", hermes_url=url, api_key=key)
        return web.json_response({"connected": True, "url": url, **result})

    async def connect_prompt(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        base = _public_url(request)
        code = connect.new_pairing_code()
        return web.json_response({"prompt": connect.prompt(base, code), "code": code, "jarvis_url": base,
                                  "script_url": f"{base}/jarvis/connect.sh",
                                  "command": f"curl -fsSL {base}/jarvis/connect.sh -o /tmp/jarvis-connect.sh && "
                                             f"bash /tmp/jarvis-connect.sh --pair {code}"})

    async def connect_script(request):
        return web.Response(text=connect.script(_public_url(request)), content_type="text/x-shellscript",
                            headers={"Cache-Control": "no-store"})

    async def connector_download(request):
        data = await asyncio.to_thread(connect.connector_tarball)
        return web.Response(body=data, content_type="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="jarvis-connector-{connect.connector_version()}.tar.gz"'})

    async def persona_file(request):
        text = connect.soul()
        if text is None:
            raise web.HTTPNotFound()
        return web.Response(text=text, content_type="text/markdown")

    async def pair(request):
        if (blocked := _guard(request, launcher, need_key=False)) is not None:
            return blocked
        body = await _body(request)
        code = str(body.get("code") or "")
        if not connect.pairing_valid(code):
            return _error(403, "bad_code", "This pairing code is unknown or expired; get a new prompt from JARVIS.")
        urls = [_clean_url(u) for u in (body.get("urls") or []) if isinstance(u, str)][:8]
        key = str(body.get("api_key") or "")
        if not urls or len(key) < 16:
            return _error(400, "invalid", "Expected {code, urls: [...], api_key}.")
        connect.consume_pairing(code)
        launcher.pair = {"status": "waiting", "detail": "Waiting for your Hermes to restart with the connector…",
                         "urls": urls}
        asyncio.create_task(_finish_pairing(launcher, urls, key))
        return web.json_response({"accepted": True}, status=202)

    async def update_check(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        if state.load().get("mode") != "managed":
            return _error(409, "not_managed", "JARVIS only updates a Hermes it installed. Update yours with `hermes update`.")
        return web.json_response(await launcher.updater.check())

    async def update_run(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        if state.load().get("mode") != "managed":
            return _error(409, "not_managed", "JARVIS only updates a Hermes it installed. Update yours with `hermes update`.")
        if not launcher.updater.start_update("requested"):
            return _error(409, "busy", "An update or install is already running.")
        return web.json_response({"started": True}, status=202)

    async def update_settings(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        body = await _body(request)
        data = state.load()
        if "auto" in body:
            data["updates"]["auto"] = bool(body["auto"])
        if "hour" in body:
            try:
                data["updates"]["hour"] = max(0, min(23, int(body["hour"])))
            except (TypeError, ValueError):
                return _error(400, "invalid_hour", "hour must be 0-23")
        state.save(data)
        return web.json_response({k: data["updates"][k] for k in ("auto", "hour")})

    async def apply_persona(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        if state.load().get("mode") != "managed":
            return _error(409, "not_managed", "For your own Hermes, run the connect script with --persona-only.")
        log = []
        await asyncio.to_thread(hermes_setup.apply_persona, log.append)
        state.update(persona=True)
        return web.json_response({"applied": True, "log": log})

    async def disconnect(request):
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        if state.load().get("mode") == "managed":
            await launcher.gateway.stop()
        state.update(mode=None, hermes_url="", api_key="")
        launcher.pair = {"status": "idle", "detail": ""}
        return web.json_response({"disconnected": True})

    async def use_managed(request):
        """Back to the Hermes JARVIS installed (after trying an external one, or after a disconnect)."""
        if (blocked := _guard(request, launcher)) is not None:
            return blocked
        if not hermes_setup.installed():
            return _error(409, "not_installed", "Hermes is not installed here yet.")
        await asyncio.to_thread(hermes_setup.apply, launcher.updater.log.write, managed=True, persona=False)
        state.update(mode="managed", hermes_url=config.MANAGED_HERMES_URL, api_key="")
        await launcher.gateway.start()
        return web.json_response({"mode": "managed"})

    async def everything_else(request):
        target = launcher.target()
        if target:
            # Past the login: vouch for it, so the connector serves the dashboard and downloads.
            return await launcher.proxy.forward(request, target, vouch=launcher.api_key())
        if request.method == "GET" and (request.path in ("/", "/vesper") or request.path.startswith("/vesper/")
                                        and request.path.endswith((".html", "/"))):
            raise web.HTTPFound("/jarvis/")
        if request.path == "/health":
            return await launcher_health(request)
        return _error(503, "not_connected", "JARVIS isn't connected to Hermes yet. Open /jarvis/ to set it up.")

    r = app.router
    async def to_setup(request):
        raise web.HTTPFound("/jarvis/")

    r.add_get("/jarvis", to_setup)
    r.add_get("/jarvis/", setup_page)
    r.add_get("/jarvis/static/{name}", static)
    r.add_get("/jarvis/health", launcher_health)
    r.add_get("/jarvis/api/state", get_state)
    r.add_post("/jarvis/api/install", install)
    r.add_get("/jarvis/api/log", install_log)
    r.add_post("/jarvis/api/detect", find)
    r.add_post("/jarvis/api/test", test)
    r.add_post("/jarvis/api/connect", do_connect)
    r.add_post("/jarvis/api/connect-prompt", connect_prompt)
    r.add_post("/jarvis/api/pair", pair)
    r.add_post("/jarvis/api/update/check", update_check)
    r.add_post("/jarvis/api/update/run", update_run)
    r.add_put("/jarvis/api/update/settings", update_settings)
    r.add_post("/jarvis/api/persona", apply_persona)
    r.add_post("/jarvis/api/disconnect", disconnect)
    r.add_post("/jarvis/api/use-managed", use_managed)
    r.add_get("/jarvis/connect.sh", connect_script)
    r.add_get("/jarvis/connector.tar.gz", connector_download)
    r.add_get("/jarvis/persona/SOUL.md", persona_file)
    r.add_get("/jarvis/login", login_page)
    r.add_get("/jarvis/api/auth", auth_status)
    r.add_post("/jarvis/api/login", login)
    r.add_post("/jarvis/api/logout", logout)
    r.add_post("/jarvis/api/password", set_password)
    r.add_get("/desktop", desktop_home)
    r.add_get("/desktop/", desktop_home)
    r.add_route("*", "/desktop/{tail:.+}", desktop)
    r.add_route("*", "/{tail:.*}", everything_else)
    return app


async def _finish_pairing(launcher: Launcher, urls: list, key: str) -> None:
    """The connect script paired before restarting its gateway: keep testing until the connector answers."""
    deadline = time.monotonic() + PAIR_WAIT_SECONDS
    last = None
    while time.monotonic() < deadline:
        for url in urls:
            result = await checks.run(url, key, timeout=4)
            if result["ok"]:
                if state.load().get("mode") == "managed":
                    await launcher.gateway.stop()
                state.update(mode="external", hermes_url=url, api_key=key)
                launcher.pair = {"status": "connected", "detail": f"Connected to Hermes at {url}.", "url": url}
                return
            last = result
        await asyncio.sleep(5)
    failed = next((c for c in (last or {}).get("checks", []) if c["ok"] is False), None)
    launcher.pair = {"status": "failed", "detail": (f"{failed['label']}: {failed['detail']}. {failed['hint']}" if failed
                                                    else "Your Hermes never answered at the addresses it sent."),
                     "urls": urls}


def main() -> None:
    logging.basicConfig(level=os.environ.get("JARVIS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(build_app(), host=config.LISTEN_HOST, port=config.LISTEN_PORT, access_log=None,
                shutdown_timeout=70)


if __name__ == "__main__":
    main()
