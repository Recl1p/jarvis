"""Extra session information for the dashboard's sessions drawer (context-window usage and the like).

``/api/sessions`` has titles, counts and cumulative token totals, but not how full a chat's context window is:
the totals add up every model call, and the prompt size of the latest call is never stored. So the context
figure here is an estimate of the next request (system prompt + tool schemas + stored messages, the same
chars/4 heuristic Hermes uses for its own compaction trigger), against the model's real window.
"""

import asyncio
import time
from functools import partial

from aiohttp import web

_MAX_IDS = 60
_CACHE_SECONDS = 600
_MAX_CACHED_USAGE = 512
_window_cache = {}   # model -> (expires, {model, context_length, compress_at})
_tools_cache = []    # [(expires, tokens)] for the api_server toolsets' schemas
_usage_cache = {}    # (tip id, message_count, last_activity_at) -> estimated tokens of the stored chat


def http_routes(adapter):
    return [
        ("GET", "/vesper/sessions/meta", partial(sessions_meta, adapter)),
        ("GET", "/vesper/sessions/{session_id}/context", partial(session_context, adapter)),
    ]


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def _compress_at(model, context_length):
    """Prompt size at which Hermes compacts a chat, from the same config and rules the agent uses
    (small windows are floored at 75%, a ``threshold_tokens`` cap wins when lower). None when compaction is off."""
    from agent.context_compressor import ContextCompressor, resolve_model_threshold
    from gateway.run import _load_gateway_config
    config = _load_gateway_config().get("compression") or {}
    if config.get("enabled") is False:
        return None
    percent = resolve_model_threshold(model, config.get("model_thresholds"), float(config.get("threshold", 0.50)))
    percent = ContextCompressor._effective_threshold_percent(context_length, percent)
    tokens = ContextCompressor._compute_threshold_tokens(context_length, percent)
    cap = config.get("threshold_tokens")
    if isinstance(cap, int) and cap > 0:
        tokens = min(tokens, cap, context_length)
    return int(tokens)


def _window_for(model):
    """``{model, context_length, compress_at}`` for a session's model (None: the gateway's configured model).
    Blocking (may probe a provider), so callers run it in a thread; cached because it rarely changes."""
    key = model or ""
    cached = _window_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    from gateway.run import _resolve_gateway_model_context
    resolved = _resolve_gateway_model_context(model or None)
    window = {"model": resolved.model, "context_length": int(resolved.context_length), "compress_at": None}
    try:
        window["compress_at"] = _compress_at(resolved.model, window["context_length"])
    except Exception:
        pass
    _window_cache[key] = (time.monotonic() + _CACHE_SECONDS, window)
    return window


def _tool_schema_tokens():
    """Estimated size of the tool schemas every request carries (dozens of tools run to tens of thousands of tokens)."""
    if _tools_cache and _tools_cache[0][0] > time.monotonic():
        return _tools_cache[0][1]
    tokens = 0
    try:
        from agent.model_metadata import estimate_request_tokens_rough
        from gateway.run import _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions
        toolsets = sorted(_get_platform_tools(_load_gateway_config(), "api_server"))
        tokens = estimate_request_tokens_rough([], tools=get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True))
    except Exception:
        pass
    _tools_cache[:] = [(time.monotonic() + _CACHE_SECONDS, tokens)]
    return tokens


def _stored_chat_tokens(db, tip, session):
    """Estimated tokens of the system prompt plus the messages Hermes would replay for this chat."""
    key = (tip, session.get("message_count"), session.get("last_activity_at"))
    if key not in _usage_cache:
        from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough
        history = db.get_messages_as_conversation(tip)
        _usage_cache[key] = (estimate_tokens_rough(session.get("system_prompt") or "")
                             + estimate_messages_tokens_rough(history)) if history else 0
        if len(_usage_cache) > _MAX_CACHED_USAGE:
            _usage_cache.pop(next(iter(_usage_cache)))
    return _usage_cache[key]


def _session_meta(db, session_id):
    """Context usage and compression lineage of one chat, or None for an unknown id. Compression moves a chat's
    messages to a continuation session, so an id from before a compaction is followed to the live tip."""
    tip = db.resolve_resume_session_id(session_id)
    session = db.get_session(tip)
    if not session:
        return None
    lineage = db.get_compression_lineage(tip)
    meta = {"id": tip, "lineage_ids": lineage if tip in lineage else [tip], "model": session.get("model") or None,
            "used_tokens": None, "context_length": None, "pct": None, "compress_at": None, "estimated": True}
    try:
        window = _window_for(session.get("model"))
        used = _stored_chat_tokens(db, tip, session)
        if used:
            used += _tool_schema_tokens()
        meta.update(model=meta["model"] or window["model"], used_tokens=used, context_length=window["context_length"],
                    compress_at=window["compress_at"],
                    pct=min(100.0, round(100.0 * used / window["context_length"], 1)) if window["context_length"] else None)
    except Exception:
        pass
    return meta


async def sessions_meta(adapter, request):
    """GET /vesper/sessions/meta?ids=a,b,c: context usage and compression lineage for up to 60 chats at once,
    keyed by the id asked for (null for an unknown id)."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    ids = list(dict.fromkeys(i.strip() for i in request.query.get("ids", "").split(",") if i.strip()))
    if not ids or len(ids) > _MAX_IDS or any(len(i) > 200 for i in ids):
        return _error(400, "invalid_ids", f"ids must list 1 to {_MAX_IDS} session ids, comma separated")
    db = await adapter._ensure_session_db_async()
    if db is None:
        return _error(503, "session_db_unavailable", "Session database unavailable")

    def collect():
        return {i: _session_meta(db, i) for i in ids}
    return web.json_response({"object": "list", "data": await asyncio.to_thread(collect)})


async def session_context(adapter, request):
    """GET /vesper/sessions/{id}/context: how full one chat's context window is (``pct`` is 0 to 100)."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    db = await adapter._ensure_session_db_async()
    if db is None:
        return _error(503, "session_db_unavailable", "Session database unavailable")
    session_id = request.match_info["session_id"]
    meta = await asyncio.to_thread(_session_meta, db, session_id)
    if meta is None:
        return _error(404, "session_not_found", f"Session not found: {session_id}")
    return web.json_response({"object": "hermes.session.context", "session_id": session_id, **meta})
