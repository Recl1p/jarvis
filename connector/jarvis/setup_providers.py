"""Dashboard setup for every model provider, without a terminal (the VESPER setup guide).

Three ways in, each saving exactly what ``hermes model`` saves:
- an API key pasted into the guide (``POST /vesper/setup/key``),
- a sign-in with the provider's own device-code page (``/vesper/setup/login/...``; the flows and pollers are the
  ones the Hermes web dashboard uses), and
- a local OpenAI-compatible server such as Ollama or LM Studio (``/vesper/setup/local...``).

Every write happens inside the gateway process, so the next chat turn uses the new model without a restart.
Claude Pro/Max plans sign in through Anthropic's own Claude Code CLI instead (``api_server_setup_claude``): Hermes'
built-in Claude login presents itself as Claude Code, so it stays a terminal-only choice.
"""

import asyncio
import logging
import os
import re
import urllib.parse
from functools import partial

from aiohttp import web

logger = logging.getLogger(__name__)

# Shown first, in this order; every other API-key provider follows under "More".
FEATURED_KEY_PROVIDERS = ("anthropic", "openai-api", "gemini", "deepseek", "xai")
# API-key providers the guide can't finish on its own: they need an endpoint URL, a cloud SDK login, a bespoke
# model-id mapping, or are covered by another tab (OpenRouter is the Free tab, LM Studio the Local tab).
EXCLUDED_KEY_PROVIDERS = frozenset({
    "openrouter", "lmstudio", "custom", "moa", "azure-foundry", "copilot", "opencode-zen", "opencode-go",
    "opencode-free", "bedrock", "vertex"})
SIGNUP_URLS = {
    "anthropic": "https://platform.claude.com/settings/keys", "openai-api": "https://platform.openai.com/api-keys",
    "gemini": "https://aistudio.google.com/app/apikey", "deepseek": "https://platform.deepseek.com/api_keys",
    "xai": "https://console.x.ai/"}
KEY_HINTS = {"gemini": "Billing must be turned on for the key's Google Cloud project; free-tier keys run out "
                       "within a few messages."}

# Sign-in providers: id -> (name, what it uses). The device-code starters live in hermes_cli.web_routers.oauth.
LOGIN_PROVIDERS = {
    "openai-codex": ("ChatGPT", "Your ChatGPT Plus or Pro plan"),
    "nous": ("Nous Portal", "A Nous Research account"),
    "xai-oauth": ("Grok", "SuperGrok or X Premium+"),
    "minimax-oauth": ("MiniMax", "A MiniMax account"),
}

# Where a local model server usually answers from inside the JARVIS container: the compose `ollama` service,
# then the host machine (Docker's host alias and default bridge gateway).
LOCAL_CANDIDATES = (
    ("Ollama", "http://ollama:11434/v1"), ("Ollama", "http://host.docker.internal:11434/v1"),
    ("Ollama", "http://172.17.0.1:11434/v1"), ("LM Studio", "http://host.docker.internal:1234/v1"),
    ("LM Studio", "http://172.17.0.1:1234/v1"), ("Ollama", "http://127.0.0.1:11434/v1"),
)
LOCAL_PROBE_TIMEOUT = 2.5
KEY_CHECK_TIMEOUT = 10
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,400}")   # one printable token, no spaces


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def http_routes(adapter):
    return [
        ("GET", "/vesper/setup/providers", partial(list_providers, adapter)),
        ("POST", "/vesper/setup/key", partial(connect_key, adapter)),
        ("POST", "/vesper/setup/login/{provider}/start", partial(login_start, adapter)),
        ("GET", "/vesper/setup/login/{provider}/poll/{session_id}", partial(login_poll, adapter)),
        ("DELETE", "/vesper/setup/login/{provider}/{session_id}", partial(login_cancel, adapter)),
        ("POST", "/vesper/setup/login/{provider}/use", partial(login_use, adapter)),
        ("GET", "/vesper/setup/local/scan", partial(local_scan, adapter)),
        ("POST", "/vesper/setup/local/probe", partial(local_probe, adapter)),
        ("POST", "/vesper/setup/local", partial(connect_local, adapter)),
    ]


async def _json_body(request):
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return None, _error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return None, _error(400, "invalid_request", "Expected a JSON object")
    return body, None


def _string(body, name):
    value = body.get(name)
    return value.strip() if isinstance(value, str) else ""


def _main_model():
    from .setup_guide import _main_model as main_model
    return main_model()


# ---- Provider list ---------------------------------------------------------------------------

def _key_provider_configs():
    """``{id: (catalog entry, ProviderConfig)}`` for every API-key provider the guide can finish, featured first."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.provider_catalog import provider_catalog
    rows = {}
    for entry in provider_catalog():
        pconfig = PROVIDER_REGISTRY.get(entry.slug)
        if (entry.slug in EXCLUDED_KEY_PROVIDERS or pconfig is None or pconfig.auth_type != "api_key"
                or not pconfig.api_key_env_vars or not pconfig.inference_base_url):
            continue
        rows[entry.slug] = (entry, pconfig)
    featured = [pid for pid in FEATURED_KEY_PROVIDERS if pid in rows]
    return {pid: rows[pid] for pid in featured + sorted(set(rows) - set(featured), key=lambda p: rows[p][0].label.lower())}


def _connected(pid):
    from hermes_cli.models import _provider_has_credentials
    return _provider_has_credentials(pid)


def _providers_payload():
    provider, model = _main_model()
    keys = [{"id": pid, "name": entry.label, "featured": pid in FEATURED_KEY_PROVIDERS,
             "signup_url": SIGNUP_URLS.get(pid) or entry.signup_url or "", "hint": KEY_HINTS.get(pid, ""),
             "connected": _connected(pid)}
            for pid, (entry, _pconfig) in _key_provider_configs().items()]
    logins = [{"id": pid, "name": name, "uses": uses, "connected": _connected(pid)}
              for pid, (name, uses) in LOGIN_PROVIDERS.items()]
    from .setup_claude import claude_code_binary, plan_connected
    claude = {"installed": bool(claude_code_binary()), "connected": plan_connected()}
    return {"current": {"provider": provider, "model": model}, "keys": keys, "logins": logins, "claude": claude}


async def list_providers(adapter, request):
    """GET /vesper/setup/providers -- what the guide can connect, and which are connected already."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        return web.json_response(await asyncio.to_thread(_providers_payload))
    except Exception as exc:
        logger.warning("setup provider list failed: %s", type(exc).__name__)
        return _error(500, "providers_failed", "Could not read the provider list")


# ---- Choosing and saving the model -----------------------------------------------------------

def _default_model(pid):
    """The model the guide picks when the user didn't: the provider's labelled default, else its first model."""
    from hermes_cli.models import pick_silent_default_model, provider_model_ids
    return pick_silent_default_model(provider_model_ids(pid), pid)


def _confirm_saved(pid, model):
    provider, saved = _main_model()
    if (provider, saved) != (pid, model):
        return "Hermes could not save the model choice (the configuration is locked)"
    return None


def _persist_key_model(pid, pconfig, model):
    from hermes_cli.model_setup_flows_common import _persist_model
    if pid == "anthropic":   # resolve_runtime_provider() pins Anthropic's URL; a stale base_url leaks elsewhere
        _persist_model(model, pid, drop_base_url=True, drop_api_mode=True)
    else:
        base_url = (os.getenv(pconfig.base_url_env_var, "") if pconfig.base_url_env_var else "") or pconfig.inference_base_url
        _persist_model(model, pid, base_url=base_url.strip(), drop_api_mode=True)
    return _confirm_saved(pid, model)


def _activate_codex():
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL
    return "openai-codex", DEFAULT_CODEX_BASE_URL


def _activate_xai():
    from hermes_cli.auth import DEFAULT_XAI_OAUTH_BASE_URL, resolve_xai_oauth_runtime_credentials
    try:
        base_url = (resolve_xai_oauth_runtime_credentials().get("base_url") or "").strip().rstrip("/")
    except Exception:   # credentials may live only in the pool; the CLI flow falls back the same way
        base_url = ""
    return "xai-oauth", base_url or DEFAULT_XAI_OAUTH_BASE_URL


def _activate_minimax():
    from hermes_cli.auth import resolve_minimax_oauth_runtime_credentials
    return "minimax-oauth", resolve_minimax_oauth_runtime_credentials()["base_url"]


_LOGIN_BASE_URLS = {"openai-codex": _activate_codex, "xai-oauth": _activate_xai, "minimax-oauth": _activate_minimax}


def _nous_default_model():
    """A Nous model this account can use: the CLI's curated catalog with the tier's unavailable models removed."""
    from hermes_cli.model_setup_flows import _nous_model_catalog
    from hermes_cli.models import check_nous_free_tier, get_curated_nous_model_ids, pick_silent_default_model
    from hermes_cli.models_pricing import get_pricing_for_provider
    from hermes_cli.auth import get_provider_auth_state
    model_ids = get_curated_nous_model_ids()
    portal = (get_provider_auth_state("nous") or {}).get("portal_base_url", "")
    catalog = _nous_model_catalog(check_nous_free_tier(force_fresh=True), portal, model_ids,
                                  get_pricing_for_provider("nous"))
    if catalog is None:
        return ""
    usable = [m for m in catalog[0] if m not in set(catalog[2] or ())]
    return pick_silent_default_model(usable, "nous")


def _activate_login(pid):
    """After a sign-in: make the provider's default model the main one, the way its ``hermes model`` flow does."""
    if pid == "nous":
        from hermes_cli.auth import resolve_nous_runtime_credentials
        from hermes_cli.model_setup_flows import _nous_persist_selection
        model = _nous_default_model()
        if not model:
            return None, "Nous Portal has no model this account can use yet"
        _nous_persist_selection(model, resolve_nous_runtime_credentials())
    else:
        from hermes_cli.model_setup_flows_common import _activate_provider_model
        model = _default_model(pid)
        if not model:
            return None, "The provider did not list any models"
        provider, base_url = _LOGIN_BASE_URLS[pid]()
        _activate_provider_model(model, provider, base_url, "", no_change=None)
    return model, _confirm_saved(pid, model)


# ---- API keys --------------------------------------------------------------------------------

async def _key_rejected(pid, pconfig, key):
    """True if the provider refuses *key*, False if it accepts it, None when that can't be told
    (no /models route, an outage). Never logs or returns the key or the exception text."""
    import aiohttp as _aiohttp_client
    base = pconfig.inference_base_url.rstrip("/")
    if pid == "anthropic":
        url, headers, refused = f"{base}/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}, (401, 403)
    elif pid == "gemini":
        url, headers, refused = f"{base}/models", {"x-goog-api-key": key}, (400, 401, 403)
    else:
        url, headers, refused = f"{base}/models", {"Authorization": f"Bearer {key}"}, (401, 403)
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=_aiohttp_client.ClientTimeout(total=KEY_CHECK_TIMEOUT)) as resp:
                if resp.status in refused:
                    return True
                return False if resp.status == 200 else None
    except Exception as exc:
        logger.warning("%s key check failed: %s", pid, type(exc).__name__)
        return None


def _save_key(pid, pconfig, key):
    from hermes_cli.config import get_env_value_prefer_dotenv, save_anthropic_api_key, save_env_value
    if pid == "anthropic":
        save_anthropic_api_key(key, save_fn=save_env_value)
        env_var = "ANTHROPIC_API_KEY"
    else:
        env_var = pconfig.api_key_env_vars[0]
        save_env_value(env_var, key)
    if get_env_value_prefer_dotenv(env_var) != key:
        return "Hermes could not save the key (the configuration is locked)"
    return None


async def connect_key(adapter, request):
    """POST /vesper/setup/key {provider, api_key, model?} -- check and save a provider's API key, make its model main."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    pid, key, model = _string(body, "provider"), _string(body, "api_key"), _string(body, "model")
    providers = await asyncio.to_thread(_key_provider_configs)
    if pid not in providers:
        return _error(400, "invalid_provider", "That provider can't be connected with a key here")
    _entry, pconfig = providers[pid]
    if not key:
        return _error(400, "missing_key", "Paste the API key to continue")
    if not _KEY_SHAPE.fullmatch(key):
        return _error(400, "invalid_key", "That doesn't look like an API key (it should be one word, no spaces)")
    if await _key_rejected(pid, pconfig, key):
        return _error(400, "invalid_key", f"{_entry.label} rejected that key")
    if pid == "gemini":
        from agent.gemini_native_adapter import probe_gemini_tier
        if await asyncio.to_thread(probe_gemini_tier, key, pconfig.inference_base_url) == "free":
            return _error(400, "free_tier_key", "That is a free-tier Gemini key. Its daily limit runs out within a few "
                                                "messages, so turn on billing for its Google Cloud project first.")
    try:
        problem = await asyncio.to_thread(_save_key, pid, pconfig, key)
        if not problem:
            model = model or await asyncio.to_thread(_default_model, pid)
            problem = (await asyncio.to_thread(_persist_key_model, pid, pconfig, model) if model
                       else "The key was saved, but the provider listed no models to use")
    except Exception as exc:
        logger.warning("%s key setup could not be saved: %s", pid, type(exc).__name__)
        problem = "Hermes could not save the setup"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": pid, "model": model, "name": _entry.label})


# ---- Sign-ins (device code) ------------------------------------------------------------------

async def login_start(adapter, request):
    """POST /vesper/setup/login/{provider}/start -- begin a sign-in; returns the page to open and the code to enter."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    pid = request.match_info["provider"]
    if pid not in LOGIN_PROVIDERS:
        return _error(400, "invalid_provider", "That provider can't be signed in to here")
    from hermes_cli.web_routers.oauth import _gc_oauth_sessions, _start_device_code_flow
    _gc_oauth_sessions()
    try:
        started = await _start_device_code_flow(pid)
    except Exception as exc:   # an HTTPException carries a user-facing detail; anything else is an outage
        detail = getattr(exc, "detail", None)
        logger.warning("%s sign-in could not start: %s", pid, type(exc).__name__)
        return _error(502, "login_unavailable", detail if isinstance(detail, str) and detail
                      else f"{LOGIN_PROVIDERS[pid][0]} sign-in isn't reachable right now")
    return web.json_response({k: started[k] for k in ("session_id", "user_code", "verification_url", "expires_in",
                                                      "poll_interval")})


def _session(pid, session_id):
    from hermes_cli.web_server_oauth import _oauth_sessions, _oauth_sessions_lock
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    return sess if sess and sess.get("provider") == pid else None


async def login_poll(adapter, request):
    """GET /vesper/setup/login/{provider}/poll/{session_id} -- pending | approved | denied | expired | error.
    On the first ``approved`` poll the provider's default model becomes the main model."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    pid, session_id = request.match_info["provider"], request.match_info["session_id"]
    sess = _session(pid, session_id) if pid in LOGIN_PROVIDERS else None
    if sess is None:
        return _error(404, "login_not_found", "That sign-in expired. Start it again.")
    status = sess["status"]
    payload = {"status": status, "error_message": sess.get("error_message"), "model": None}
    if status != "approved":
        return web.json_response(payload)
    lock = sess.setdefault("vesper_lock", asyncio.Lock())
    async with lock:
        if "vesper_result" not in sess:
            try:
                sess["vesper_result"] = await asyncio.to_thread(_activate_login, pid)
            except Exception as exc:
                logger.warning("%s model activation failed: %s", pid, type(exc).__name__)
                sess["vesper_result"] = (None, "Signed in, but Hermes could not pick a model")
    model, problem = sess["vesper_result"]
    if problem:
        return web.json_response({**payload, "status": "error", "error_message": problem})
    return web.json_response({**payload, "model": model, "provider": pid})


async def login_use(adapter, request):
    """POST /vesper/setup/login/{provider}/use -- already signed in: make that provider's model main, no new sign-in."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    pid = request.match_info["provider"]
    if pid not in LOGIN_PROVIDERS:
        return _error(400, "invalid_provider", "That provider can't be signed in to here")
    if not await asyncio.to_thread(_connected, pid):
        return _error(409, "not_connected", f"{LOGIN_PROVIDERS[pid][0]} isn't signed in yet")
    try:
        model, problem = await asyncio.to_thread(_activate_login, pid)
    except Exception as exc:
        logger.warning("%s model activation failed: %s", pid, type(exc).__name__)
        model, problem = None, f"Hermes could not switch to {LOGIN_PROVIDERS[pid][0]}"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": pid, "model": model})


async def login_cancel(adapter, request):
    """DELETE /vesper/setup/login/{provider}/{session_id} -- stop a pending sign-in so it can't complete later."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    from hermes_cli.web_server_oauth import _oauth_sessions, _oauth_sessions_lock
    pid, session_id = request.match_info["provider"], request.match_info["session_id"]
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
        if sess is not None and sess.get("provider") == pid:
            sess["cancelled"] = True
            _oauth_sessions.pop(session_id, None)
        else:
            sess = None
    return web.json_response({"ok": sess is not None})


# ---- Local model servers ---------------------------------------------------------------------

def _normalize_url(raw):
    """``192.168.1.5`` / ``host:11434`` / ``http://host:11434/v1`` -> an http(s) URL, or "" when unusable."""
    url = raw.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.port is None and parsed.scheme == "http" and not parsed.path:
        url = f"{url}:11434"   # a bare address means Ollama's default port
    return url


def _probe(base_url, api_key=""):
    """``(working base URL, model ids)``; models is None when nothing answered."""
    from hermes_cli.models import probe_api_models
    probe = probe_api_models(api_key or None, base_url, timeout=LOCAL_PROBE_TIMEOUT)
    models = probe.get("models")
    resolved = probe.get("resolved_base_url") if probe.get("used_fallback") else None
    return (resolved or base_url).rstrip("/"), (sorted(models) if models is not None else None)


async def local_scan(adapter, request):
    """GET /vesper/setup/local/scan -- look for Ollama / LM Studio where they usually answer from this container."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    results = await asyncio.gather(*(asyncio.to_thread(_probe, url) for _name, url in LOCAL_CANDIDATES))
    found, seen = [], set()
    for (name, _url), (base_url, models) in zip(LOCAL_CANDIDATES, results):
        if models is not None and base_url not in seen:
            seen.add(base_url)
            found.append({"name": name, "base_url": base_url, "models": models})
    return web.json_response({"found": found})


async def local_probe(adapter, request):
    """POST /vesper/setup/local/probe {base_url, api_key?} -- does a model server answer there, with which models?"""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    base_url = _normalize_url(_string(body, "base_url"))
    if not base_url:
        return _error(400, "invalid_url", "Enter the server's address, like 192.168.1.20 or http://192.168.1.20:11434")
    resolved, models = await asyncio.to_thread(_probe, base_url, _string(body, "api_key"))
    if models is None:
        return _error(502, "not_reachable", "No model server answered at that address")
    return web.json_response({"base_url": resolved, "models": models})


def _save_local(base_url, model, api_key, models):
    from hermes_cli.config import load_config, save_config
    from hermes_cli.main_provider_setup import _auto_provider_name
    from hermes_cli.web_models import CustomEndpointUpdate
    from hermes_cli.web_routers.config_env import _write_custom_endpoint
    cfg = load_config()
    endpoint_id, _entry = _write_custom_endpoint(cfg, CustomEndpointUpdate(
        name=_auto_provider_name(base_url), base_url=base_url, model=model, api_key=api_key or None,
        make_default=True, models=models))
    save_config(cfg)
    return endpoint_id, _confirm_saved(endpoint_id, model)


async def connect_local(adapter, request):
    """POST /vesper/setup/local {base_url, model, api_key?} -- save a local server and make *model* the main model."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    base_url, model = _normalize_url(_string(body, "base_url")), _string(body, "model")
    api_key = _string(body, "api_key")
    if not base_url:
        return _error(400, "invalid_url", "Enter the server's address")
    resolved, models = await asyncio.to_thread(_probe, base_url, api_key)
    if models is None:
        return _error(502, "not_reachable", "No model server answered at that address")
    if not models:
        return _error(400, "no_models", "The server answered but has no models yet. Download one in Ollama or LM "
                                        "Studio, then try again.")
    model = model or models[0]
    if model not in models:
        return _error(400, "invalid_model", "That model isn't on the server")
    try:
        provider, problem = await asyncio.to_thread(_save_local, resolved, model, api_key, models)
    except Exception as exc:
        logger.warning("local model setup could not be saved: %s", type(exc).__name__)
        provider, problem = None, "Hermes could not save the local server"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": provider, "model": model, "base_url": resolved})
