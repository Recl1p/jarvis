"""A tiny OpenAI-compatible model server for JARVIS's end-to-end tests: deterministic replies, no API key, no cost.

  GET  /v1/models              one model, "mock-jarvis", with a 128K context (Hermes refuses models under 64K)
  POST /v1/chat/completions    streams (SSE) or returns "Mock reply: <the user's last message>"
  POST /api/show, GET /api/tags  the Ollama details Hermes probes for a server on port 11434
  GET  /mock/requests          the last requests' system prompts and last user messages, for assertions
  POST /mock/reset             forget them

Run: python3 mock_llm.py [port]   (aiohttp is all it needs; the JARVIS image has it)
"""

import asyncio
import json
import sys
import time
import uuid
from collections import deque

from aiohttp import web

MODEL = "mock-jarvis"
SEEN = deque(maxlen=50)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _reply_for(messages: list) -> str:
    user = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    system = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    SEEN.append({"at": time.time(), "system": system[-6000:], "user": user[-2000:], "count": len(messages)})
    # Title and other auxiliary requests ask for a short title; give them one.
    if "title" in system.lower()[:400] and len(messages) <= 3:
        return "Mock conversation"
    last = " ".join(user.split())[-160:]
    return f"Mock reply: {last}"


async def models(request):
    return web.json_response({"object": "list", "data": [
        {"id": MODEL, "object": "model", "owned_by": "jarvis-tests", "context_length": 131072, "max_model_len": 131072}]})


async def chat(request):
    body = await request.json()
    reply = _reply_for(body.get("messages") or [])
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())
    usage = {"prompt_tokens": 42, "completion_tokens": len(reply.split()), "total_tokens": 42 + len(reply.split())}
    if not body.get("stream"):
        return web.json_response({"id": cid, "object": "chat.completion", "created": created, "model": MODEL,
                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                               "finish_reason": "stop"}], "usage": usage})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)

    async def send(delta, finish=None, extra=None):
        chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if extra:
            chunk.update(extra)
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

    await send({"role": "assistant", "content": ""})
    for word in reply.split(" "):
        await send({"content": word + " "})
        await asyncio.sleep(0.02)
    await send({}, "stop")
    if (body.get("stream_options") or {}).get("include_usage"):
        await resp.write(f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': MODEL, 'choices': [], 'usage': usage})}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp


async def ollama_show(request):
    """Ollama's model details, which Hermes reads for the context window of a server on :11434."""
    return web.json_response({"model_info": {"general.architecture": "llama", "llama.context_length": 131072},
                              "parameters": "num_ctx 131072", "details": {"family": "llama", "format": "gguf"}})


async def ollama_tags(request):
    return web.json_response({"models": [{"name": MODEL, "model": MODEL, "size": 1}]})


UNKNOWN = deque(maxlen=50)


async def unknown(request):
    """Anything else Hermes asks for is recorded (GET /mock/requests shows it) and answered 404."""
    UNKNOWN.append(f"{request.method} {request.path_qs}")
    print(f"unhandled: {request.method} {request.path_qs}", flush=True)
    return web.json_response({"error": {"message": f"mock: no route {request.path}"}}, status=404)


async def seen(request):
    return web.json_response({"requests": list(SEEN), "unhandled": list(UNKNOWN)})


async def reset(request):
    SEEN.clear()
    UNKNOWN.clear()
    return web.json_response({"ok": True})


async def health(request):
    return web.json_response({"ok": True})


def main():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/api/show", ollama_show)
    app.router.add_get("/api/tags", ollama_tags)
    app.router.add_get("/mock/requests", seen)
    app.router.add_post("/mock/reset", reset)
    app.router.add_get("/health", health)
    app.router.add_route("*", "/{tail:.*}", unknown)
    web.run_app(app, host="0.0.0.0", port=int(sys.argv[1]) if len(sys.argv) > 1 else 11434, access_log=None)


if __name__ == "__main__":
    main()
