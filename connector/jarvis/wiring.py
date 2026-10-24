"""Attaches JARVIS to a stock Hermes API server.

``wire_api_server(app, adapter)`` is the ``api_server`` platform handler factory: Hermes calls it from
``APIServerAdapter.connect()`` with its aiohttp ``Application`` before ``AppRunner.setup()`` freezes the router, so the
dashboard routes and one middleware can still be added. The middleware covers the two things JARVIS used to patch into
Hermes's own middlewares: same-origin module imports (they always carry an Origin header, which the stock CORS check
rejects unless that origin is listed) and the bigger request bodies of music uploads, document extraction and chats
with images attached.
"""

import hmac
import logging
import re
import shutil
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


def _manifest_version() -> str:
    with suppress(Exception):
        for line in (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return "0"


CONNECTOR_VERSION = _manifest_version()

# Music uploads are raw audio bytes (up to 100 MB, vesper.py's _MUSIC_MAX_BYTES), document extraction takes up to
# 200 MB, and the dashboard inlines attached images as base64 in the chat JSON. Small margin for framing overhead.
MUSIC_UPLOAD_MAX_BYTES = 100 * 1024 * 1024 + 4096
ATTACHMENT_EXTRACT_MAX_BYTES = 200 * 1024 * 1024 + 4096
CHAT_MESSAGE_MAX_BYTES = 48 * 1024 * 1024 + 4096

# What the stock security_headers_middleware would have added to a response that skips the middleware chain.
_SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

# Routes a browser opens without a way to add the API key: the dashboard page (which carries the key), download links,
# <audio> sources, and the panels' status and weather. They answer only requests the JARVIS launcher forwarded (after
# its own login it adds the key as LAUNCHER_HEADER) or that carry the key themselves, so a Hermes listening on the
# network ("I already have Hermes") doesn't hand the dashboard and its key to whoever opens its port.
LAUNCHER_HEADER = "X-JARVIS-Launcher"
_LAUNCHER_ONLY = re.compile(r"^/vesper(/files|/status|/weather|/radio|/music/tracks/[^/]+)?/?$")


def _vouched(request) -> bool:
    adapter = request.app.get("jarvis_adapter")
    key = adapter._expected_api_key() if adapter is not None else ""
    if not key:
        return True   # a Hermes without a key is open to everyone anyway
    sent = request.headers.get(LAUNCHER_HEADER, "")
    if sent and hmac.compare_digest(sent.encode("utf-8"), key.encode("utf-8")):
        return True
    return hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"),
                               f"Bearer {key}".encode("utf-8"))


def _large_body_limit(request) -> int:
    if request.method != "POST":
        return 0
    path = request.path
    if path.endswith("/vesper/music/tracks"):
        return MUSIC_UPLOAD_MAX_BYTES
    if path.endswith("/vesper/chat/extract-attachment"):
        return ATTACHMENT_EXTRACT_MAX_BYTES
    if path.startswith("/api/sessions/") and (path.endswith("/chat") or path.endswith("/chat/stream")):
        return CHAT_MESSAGE_MAX_BYTES
    return 0


def _same_origin(origin: str, host: str) -> bool:
    with suppress(Exception):
        return bool(host) and urlsplit(origin).netloc == host
    return False


def _default_body_limit() -> int:
    from gateway.platforms import api_server
    return int(getattr(api_server, "MAX_REQUEST_BYTES", 10_000_000))


@web.middleware
async def jarvis_middleware(request, handler):
    """Outermost middleware (inserted at index 0, so it runs before Hermes's own)."""
    if request.method in ("GET", "HEAD") and _LAUNCHER_ONLY.match(request.path) and not _vouched(request):
        from gateway.platforms import api_server
        return api_server._error_response(
            "Open JARVIS at its own address (the JARVIS container, port 8642), not this Hermes directly.", 403,
            code="open_jarvis")
    origin = request.headers.get("Origin")
    if origin and _same_origin(origin, request.host):
        # A same-origin request needs no CORS approval; drop the header so the stock check doesn't 403 it.
        headers = request.headers.copy()
        del headers["Origin"]
        request = request.clone(headers=headers)
    limit = _large_body_limit(request)
    if limit:
        length = request.content_length
        if length is not None and length > limit:
            from gateway.platforms import api_server
            return api_server._error_response("Request body too large.", 413, code="body_too_large")
        if length is None or length > _default_body_limit():
            # Over the stock 10 MB ceiling: Hermes's body limit middleware would refuse it, so call the route
            # directly with a request that may read up to this route's own limit. These handlers authenticate
            # themselves, and only same-origin browsers or Bearer clients reach them.
            response = await request.match_info.handler(request.clone(client_max_size=limit))
            for key, value in _SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)
            return response
    return await handler(request)


def _session_provider_is_current(session) -> bool:
    """A session's persisted model only means something with the provider that served it. Once the main provider
    changes (``hermes model``, the dashboard's free setup), an unlocked session follows the new default instead of
    sending its old model id to the new provider. Unknown either side: keep the pin."""
    recorded = str((session or {}).get("billing_provider") or "").strip() if isinstance(session, dict) else ""
    if not recorded:
        return True
    try:
        from hermes_cli.config import coerce_provider_id, load_config_readonly
        section = (load_config_readonly() or {}).get("model")
        current = str(coerce_provider_id(section.get("provider", "")) or "") if isinstance(section, dict) else ""
    except Exception:
        return True
    return not current or current == "auto" or recorded.lower() == current.lower()


def _follow_main_provider(adapter) -> None:
    """Wrap this adapter's ``_stored_session_model`` (an instance attribute, so only this server changes)."""
    original = getattr(adapter, "_stored_session_model", None)
    if original is None or getattr(original, "_jarvis_wrapped", False):
        return

    def stored_session_model(session):
        model = original(session)
        return model if model and _session_provider_is_current(session) else None

    stored_session_model._jarvis_wrapped = True
    adapter._stored_session_model = stored_session_model


def _accept_named_custom_providers(adapter) -> None:
    """A chat locked to a model on a named custom endpoint (the guide's Local route saves Ollama / LM Studio as
    "ollama-11434" and the like) runs with agent.provider "custom". Hermes 0.22.5's confirmed-lock check compares the
    locked name with "custom", calls it a mismatch and throws the finished reply away. Accept exactly that case: the
    agent ran on "custom" with the locked model. Any other mismatch still fails."""
    original = getattr(adapter, "_turn_runtime_metadata", None)
    if original is None or getattr(original, "_jarvis_wrapped", False):
        return

    def turn_runtime_metadata(agent, **kwargs):
        try:
            return original(agent, **kwargs)
        except RuntimeError as exc:
            if not kwargs.get("confirmed_runtime_lock") or "model lock runtime mismatch" not in str(exc):
                raise
            route, requested = kwargs.get("route") or {}, kwargs.get("requested_runtime") or {}
            locked_model = str(route.get("model") or requested.get("model") or "").strip()
            if str(getattr(agent, "provider", "")) != "custom" or str(getattr(agent, "model", "")) != locked_model:
                raise
            return original(agent, **{**kwargs, "confirmed_runtime_lock": False})

    turn_runtime_metadata._jarvis_wrapped = True
    adapter._turn_runtime_metadata = turn_runtime_metadata


def connector_routes(adapter):
    """What JARVIS's launcher and the setup guide's connection test ask this Hermes."""

    async def ping(request):
        # Unauthenticated, for discovery: says only that this is Hermes with the connector, and which versions.
        return web.json_response({"connector": "jarvis", "version": CONNECTOR_VERSION, "hermes_version": _hermes_version()})

    async def check(request):
        auth = adapter._check_auth(request)
        if auth is not None:
            return auth
        return web.json_response({
            "ok": True, "connector": "jarvis", "version": CONNECTOR_VERSION, "hermes_version": _hermes_version(),
            "isolation": _isolation(), "features": _features()})

    async def skills(request):
        """The installed skills, like Hermes's GET /v1/skills. Hermes 0.22.4-0.22.5 (and main as of 2026-10-23) call
        find_all_skills(include_editorial=True), which that function doesn't accept, so /v1/skills always answers 500;
        Agent Control asks here instead."""
        auth = adapter._check_auth(request)
        if auth is not None:
            return auth
        import asyncio
        from tools.skills_tool import find_all_skills, sort_skills

        def listing():
            try:
                return sort_skills(find_all_skills(skip_disabled=False, include_editorial=True))
            except TypeError:
                return sort_skills(find_all_skills(skip_disabled=False))
        try:
            data = await asyncio.to_thread(listing)
        except Exception:
            logger.exception("JARVIS: listing skills failed")
            from gateway.platforms import api_server
            return api_server._error_response("Failed to enumerate skills", 500, err_type="server_error")
        return web.json_response({"object": "list", "data": data})

    return [("GET", "/vesper/connector", ping), ("GET", "/vesper/connector/check", check),
            ("GET", "/vesper/skills", skills)]


def _hermes_version() -> str:
    """The version /health reports (a source checkout's package metadata says 0.0.0)."""
    with suppress(Exception):
        from gateway.platforms import api_server
        return str(api_server._hermes_version())
    return ""


def _isolation() -> str:
    with suppress(Exception):
        from hermes_cli.config import load_config_readonly
        return str(((load_config_readonly() or {}).get("plugins") or {}).get("isolation") or "in_process")
    return "unknown"


def _features() -> dict:
    """Optional parts that only exist where JARVIS's own image runs Hermes (the dashboard hides what's missing)."""
    from . import vesper_voice
    return {
        "local_voices": vesper_voice.pocket_installed() or vesper_voice.installed(),
        "browser_desktop": shutil.which("chromium") is not None,
    }


def wire_api_server(app, adapter) -> None:
    """``api_server`` platform handler: add the dashboard routes and middleware to the stock server."""
    if app is None:
        return
    if app.frozen:
        # A plugin enabled while the gateway runs is hot-loaded, but a running API server can't take new routes.
        logger.info("JARVIS connector %s loaded; restart the gateway to serve the dashboard (hermes gateway restart)",
                    CONNECTOR_VERSION)
        return
    from . import core_routes, vesper
    routes = core_routes.http_routes(adapter) + vesper.http_routes(adapter) + connector_routes(adapter)
    taken = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    for method, path, handler in routes:
        if (method, path) in taken:
            logger.warning("JARVIS: %s %s is already served by Hermes; keeping Hermes's handler", method, path)
            continue
        app.router.add_route(method, path, handler)
    app["jarvis_adapter"] = adapter
    if jarvis_middleware not in app.middlewares:
        app.middlewares.insert(0, jarvis_middleware)
    _follow_main_provider(adapter)
    _accept_named_custom_providers(adapter)
    prepare_voice_extras()
    logger.info("JARVIS connector %s: dashboard at /vesper (%d routes)", CONNECTOR_VERSION, len(routes))


def prepare_voice_extras() -> None:
    """Inside the JARVIS image (it ships the Piper voices), install Hermes's Edge and Piper extras in the background so
    the dashboard's fallback voices are ready, as they were when Hermes was built into the image. Elsewhere they are
    installed the first time they are used."""
    if not Path("/opt/piper-voices").is_dir():
        return
    import threading
    from . import vesper_voice

    def run():
        for engine in vesper_voice.VOICE_EXTRAS:
            vesper_voice.ensure_voice_extra(engine)

    threading.Thread(target=run, name="jarvis-voice-extras", daemon=True).start()


def sync_bundled_skills() -> None:
    """Keep the bundled skills in the user's own skills folder, where the dashboard's prompts and the skill's own
    instructions expect them (``$HERMES_HOME/skills/<category>/<name>``). Replaced when this plugin's copy differs,
    so an update reaches it; the user's other skills are never touched."""
    from hermes_constants import get_hermes_home
    target = Path(get_hermes_home()) / "skills" / "note-taking" / "jarvis-notes"
    source = PLUGIN_DIR / "skills" / "jarvis-notes"
    if _tree_digest(source) == _tree_digest(target):
        return
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))


def _tree_digest(root: Path) -> str:
    import hashlib
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
