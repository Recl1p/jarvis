"""Dashboard setup and diagnostics routes (VESPER): the free OpenRouter setup and `hermes doctor`."""

import asyncio
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

OPENROUTER_API = "https://openrouter.ai/api/v1"
FREE_MODELS_TTL_SECONDS = 600
MIN_CONTEXT_LENGTH = 32000
OPENROUTER_FREE_LIMITS = (
    "OpenRouter's free models allow 20 requests per minute and 50 requests per day, or 1000 requests per day "
    "once you have bought at least 10 credits in total.")

# Strong agentic/coding free families, best first. Matched by id prefix because OpenRouter renames often;
# whatever of these is listed ranks ahead of the rest, which are sorted by context length.
PREFERRED_FREE_MODELS = (
    "qwen/qwen3-coder", "moonshotai/kimi-k2", "z-ai/glm-", "openai/gpt-oss-120b", "deepseek/deepseek-",
    "nvidia/nemotron-3-ultra", "thinkingmachines/inkling", "minimax/minimax-m", "qwen/qwen3-next",
    "qwen/qwen3-235b", "mistralai/devstral", "nvidia/nemotron-3-super", "poolside/laguna",
    "meta-llama/llama-3.3-70b", "google/gemma-4-31b", "cohere/north")
_SMALL_VARIANT = re.compile(r"(?:^|[-/_.])(?:small|mini|nano|lite|flash|xs|tiny|lightning|distill)(?:$|[-/_.:])")

DOCTOR_TIMEOUT_SECONDS = 120
DOCTOR_OUTPUT_LIMIT = 200_000
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SK_TOKEN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Za-z0-9_.-]{0,60}(?:key|token|secret|password|passwd|credential|authorization)[A-Za-z0-9_.-]{0,60}"
    r"\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
_REDACTED = "[redacted]"

_KEY_SHAPE = re.compile(r"sk-or-[A-Za-z0-9_-]{16,200}")
_OK_MARKS, _WARN_MARKS, _ERROR_MARKS = "✓✔", "⚠", "✗✘✖"


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def _now():
    return time.monotonic()


class _State:
    """Per-adapter caches and locks (built in http_routes, so each app gets its own)."""

    def __init__(self):
        self.models = None  # (expires_at, payload)
        self.models_lock = asyncio.Lock()
        self.doctor_lock = asyncio.Lock()


def http_routes(adapter):
    state = _State()
    return [
        ("GET", "/vesper/setup/status", partial(setup_status, adapter)),
        ("GET", "/vesper/setup/openrouter-free/models", partial(free_models, adapter, state)),
        ("POST", "/vesper/setup/openrouter-free", partial(setup_openrouter_free, adapter, state)),
        ("POST", "/vesper/doctor", partial(run_doctor, adapter, state)),
    ]


# ---- Setup status ----------------------------------------------------------------------------

def _main_model():
    """(provider, model) of the main model, from config.yaml the way the model picker reads it."""
    from hermes_cli.config import coerce_provider_id, load_config_readonly
    section = (load_config_readonly() or {}).get("model")
    if isinstance(section, dict):
        return (str(coerce_provider_id(section.get("provider", "")) or ""),
                str(section.get("default") or section.get("model") or section.get("name") or ""))
    return "", str(section or "")


def _configured_openrouter_key():
    """The OpenRouter key Hermes would use ('' when none): .env/environment, then the credential pool."""
    try:
        from hermes_cli.auth import ProviderConfig, _resolve_api_key_provider_secret
        pconfig = ProviderConfig(id="openrouter", name="OpenRouter", auth_type="api_key",
                                 api_key_env_vars=("OPENROUTER_API_KEY",))
        return _resolve_api_key_provider_secret("openrouter", pconfig)[0] or ""
    except Exception:
        return ""


def _status_payload():
    provider, model = _main_model()
    return {"provider": provider, "model": model, "openrouter_key_set": bool(_configured_openrouter_key())}


async def setup_status(adapter, request):
    """GET /vesper/setup/status -- the current main provider/model and whether an OpenRouter key exists."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        return web.json_response(await asyncio.to_thread(_status_payload))
    except Exception as exc:
        logger.warning("setup status failed: %s", type(exc).__name__)
        return _error(500, "status_failed", "Could not read the Hermes configuration")


# ---- Free OpenRouter models ------------------------------------------------------------------

def _is_zero_price(value):
    if isinstance(value, bool):
        return False
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def _expired(model):
    try:
        return date.fromisoformat(str(model.get("expiration_date"))[:10]) < datetime.now(timezone.utc).date()
    except ValueError:  # no expiration_date (None -> "None"), or an unreadable one
        return False


def _free_candidate(model):
    """``{id, name, context_length}`` when *model* is free, tool-capable and roomy enough, else None."""
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
        return None
    model_id = model["id"].strip()
    pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
    if not (model_id.endswith(":free") or (_is_zero_price(pricing.get("prompt"))
                                           and _is_zero_price(pricing.get("completion")))):
        return None
    if "tools" not in (model.get("supported_parameters") or []):
        return None
    try:
        context = int(model.get("context_length") or 0)
    except (TypeError, ValueError):
        return None
    if context < MIN_CONTEXT_LENGTH or _expired(model):
        return None
    outputs = (model.get("architecture") or {}).get("output_modalities") if isinstance(
        model.get("architecture"), dict) else None
    if isinstance(outputs, list) and "text" not in outputs:
        return None
    return {"id": model_id, "name": str(model.get("name") or model_id).strip(), "context_length": context}


def _rank_key(candidate):
    model_id = candidate["id"].lower()
    rank = next((i for i, prefix in enumerate(PREFERRED_FREE_MODELS) if model_id.startswith(prefix)),
                len(PREFERRED_FREE_MODELS))
    small = bool(_SMALL_VARIANT.search(model_id)) if rank < len(PREFERRED_FREE_MODELS) else False
    return rank, small, -candidate["context_length"], model_id


def _free_models_payload(upstream):
    models = sorted(filter(None, (_free_candidate(m) for m in upstream)), key=_rank_key)
    return {"models": models, "recommended": models[0]["id"] if models else ""}


async def _fetch_free_models(state):
    """The ranked free-model payload (cached for 10 minutes), or an error response."""
    cached = state.models
    if cached and cached[0] > _now():
        return cached[1], None
    async with state.models_lock:
        cached = state.models
        if cached and cached[0] > _now():
            return cached[1], None
        import aiohttp as _aiohttp_client
        try:
            async with _aiohttp_client.ClientSession() as session:
                async with session.get(f"{OPENROUTER_API}/models",
                                       timeout=_aiohttp_client.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None, _error(502, "upstream_unavailable",
                                            f"OpenRouter's model list is unavailable (HTTP {resp.status})")
                    data = await resp.json()
        except Exception as exc:
            return None, _error(502, "upstream_unavailable",
                                f"Could not reach OpenRouter for the model list ({type(exc).__name__})")
        upstream = data.get("data") if isinstance(data, dict) else None
        if not isinstance(upstream, list):
            return None, _error(502, "upstream_unavailable", "OpenRouter returned an unreadable model list")
        payload = _free_models_payload(upstream)
        if not payload["models"]:
            return None, _error(502, "upstream_unavailable",
                                "OpenRouter lists no free tool-capable models right now")
        state.models = (_now() + FREE_MODELS_TTL_SECONDS, payload)
        return payload, None


async def free_models(adapter, state, request):
    """GET /vesper/setup/openrouter-free/models -- free, tool-capable OpenRouter models, best first."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    payload, error = await _fetch_free_models(state)
    return error if error is not None else web.json_response(payload)


# ---- Free OpenRouter setup -------------------------------------------------------------------

async def _key_rejected(key):
    """Ask OpenRouter about *key*: True if it rejects it, False if it accepts it, None if unknown."""
    import aiohttp as _aiohttp_client
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get(f"{OPENROUTER_API}/key", headers={"Authorization": f"Bearer {key}"},
                                   timeout=_aiohttp_client.ClientTimeout(total=10)) as resp:
                if resp.status in (401, 403):
                    return True
                return False if resp.status == 200 else None
    except Exception as exc:  # never log or return exc text: it can carry request details
        logger.warning("OpenRouter key check failed: %s", type(exc).__name__)
        return None


def _persist_setup(api_key, model_id):
    """Save the key to .env and the model to config.yaml the way ``hermes model`` does. Returns an
    error message, or None when both stuck."""
    from hermes_cli.config import get_env_value_prefer_dotenv, save_env_value
    from hermes_cli.model_setup_flows_common import _persist_model
    from hermes_constants import OPENROUTER_BASE_URL
    if api_key:
        save_env_value("OPENROUTER_API_KEY", api_key)
        if get_env_value_prefer_dotenv("OPENROUTER_API_KEY") != api_key:
            return "Hermes could not save the OpenRouter key (the configuration is locked)"
    _persist_model(model_id, "openrouter", base_url=OPENROUTER_BASE_URL, api_mode="chat_completions")
    provider, model = _main_model()
    if (provider, model) != ("openrouter", model_id):
        return "Hermes could not save the model choice (the configuration is locked)"
    return None


async def setup_openrouter_free(adapter, state, request):
    """POST /vesper/setup/openrouter-free {api_key?, model?} -- store the key, make a free OpenRouter model main."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return _error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return _error(400, "invalid_request", "Expected a JSON object")
    api_key, model_id = body.get("api_key"), body.get("model")
    if api_key is not None and not isinstance(api_key, str):
        return _error(400, "invalid_key", "api_key must be a string")
    if model_id is not None and not isinstance(model_id, str):
        return _error(400, "invalid_model", "model must be a string")
    api_key = (api_key or "").strip()
    if api_key and not _KEY_SHAPE.fullmatch(api_key):
        return _error(400, "invalid_key", "That does not look like an OpenRouter key (it starts with sk-or-)")
    saved_key = "" if api_key else await asyncio.to_thread(_configured_openrouter_key)
    if not api_key and not saved_key:
        return _error(400, "missing_key", "Paste an OpenRouter API key to continue")

    payload, error = await _fetch_free_models(state)
    if error is not None:
        return error
    model_id = (model_id or "").strip() or payload["recommended"]
    if model_id not in {m["id"] for m in payload["models"]}:
        return _error(400, "invalid_model", "That model is not in OpenRouter's current free list")

    rejected = await _key_rejected(api_key or saved_key)
    if rejected:
        return _error(400, "invalid_key", "OpenRouter rejected that key" if api_key
                      else "OpenRouter rejected the saved key; paste a new one")
    if rejected is None and api_key:  # a saved key that can't be re-checked right now is still usable
        return _error(502, "upstream_unavailable", "Could not reach OpenRouter to check the key")

    try:
        problem = await asyncio.to_thread(_persist_setup, api_key, model_id)
    except Exception as exc:
        logger.warning("OpenRouter setup could not be saved: %s", type(exc).__name__)
        problem = "Hermes could not save the OpenRouter setup"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": "openrouter", "model": model_id,
                              "limits": OPENROUTER_FREE_LIMITS})


# ---- hermes doctor ---------------------------------------------------------------------------

def redact_secrets(text):
    """Mask anything secret-looking: bearer and sk- tokens, KEY=value / token: value values, long hex/base64 runs."""
    text = _BEARER.sub(f"Bearer {_REDACTED}", text)
    text = _SK_TOKEN.sub(_REDACTED, text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda m: m.group(0) if m.group(2).lower() == "bearer" else f"{m.group(1)}{_REDACTED}", text)

    def long_run(match):
        run = match.group(0)
        # Token-like only: digits, or mixed case. Plain lowercase paths and words stay readable.
        mixed = any(c.isdigit() for c in run) or (any(c.isupper() for c in run) and any(c.islower() for c in run))
        return _REDACTED if mixed else run
    return _LONG_RUN.sub(long_run, text)


def clean_doctor_output(raw):
    """ANSI stripped, carriage-return redraws collapsed to their final text, secrets redacted."""
    lines = []
    for line in _ANSI.sub("", raw[:DOCTOR_OUTPUT_LIMIT]).replace("\r\n", "\n").split("\n"):
        segments = [s for s in line.split("\r") if s.strip()]
        lines.append((segments[-1] if segments else "").rstrip())
    text = redact_secrets("\n".join(lines)).strip("\n")
    return text + "\n[output truncated]" if len(raw) > DOCTOR_OUTPUT_LIMIT else text


def doctor_summary(output):
    """Count doctor's check markers: ✓ ok, ⚠ warning, ✗ error."""
    counts = {"ok": 0, "warnings": 0, "errors": 0}
    for line in output.splitlines():
        mark = line.lstrip()[:1]
        if mark and mark in _OK_MARKS:
            counts["ok"] += 1
        elif mark and mark in _WARN_MARKS:
            counts["warnings"] += 1
        elif mark and mark in _ERROR_MARKS:
            counts["errors"] += 1
    return counts


def _hermes_source_root():
    """The Hermes checkout this gateway runs from (this plugin lives under HERMES_HOME, not in it)."""
    import hermes_cli
    return str(Path(hermes_cli.__file__).resolve().parents[1])


def _doctor_command(live):
    """argv for ``hermes doctor`` using the interpreter that runs this gateway. Never --fix."""
    argv = [sys.executable, "-m", "hermes_cli.main", "doctor"]
    return argv + ["--live"] if live else argv


async def run_doctor(adapter, state, request):
    """POST /vesper/doctor {live?} -- run ``hermes doctor`` (never --fix) and return its redacted report."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    live = False
    if request.can_read_body:
        try:
            body = await request.json()
        except (ValueError, LookupError):
            return _error(400, "invalid_json", "Invalid JSON body")
        if not isinstance(body, dict) or not isinstance(body.get("live", False), bool):
            return _error(400, "invalid_request", 'Expected {"live": true|false}')
        live = body.get("live", False)
    if state.doctor_lock.locked():
        return _error(409, "doctor_busy", "A diagnostics run is already in progress")
    async with state.doctor_lock:
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "PYTHONUNBUFFERED": "1"}
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *_doctor_command(live), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env, cwd=_hermes_source_root())
        except OSError as exc:
            return _error(500, "doctor_failed", f"Could not start hermes doctor ({type(exc).__name__})")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=DOCTOR_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _error(504, "doctor_timeout", f"hermes doctor did not finish within {DOCTOR_TIMEOUT_SECONDS} seconds")
        except asyncio.CancelledError:
            proc.kill()
            raise
    output = clean_doctor_output(stdout.decode("utf-8", errors="replace"))
    return web.json_response({
        "ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output,
        "duration_ms": int((time.monotonic() - started) * 1000), "summary": doctor_summary(output)})
