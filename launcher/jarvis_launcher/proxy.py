"""Forward a request to the connected Hermes and stream the answer back (chat streams, radio, music, WebSockets).

The browser's Host header is kept, so the connector's same-origin check (wiring.py) sees the dashboard's own origin
whether Hermes runs in this container or on another computer.
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from aiohttp import web
from yarl import URL

from .auth import COOKIE

# Tells the connector this request passed JARVIS's login (connector/jarvis/wiring.py, LAUNCHER_HEADER): its value is the
# Hermes API key, which that Hermes already has. A browser's own copy of the header is never passed on.
LAUNCHER_HEADER = "X-JARVIS-Launcher"

logger = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}
# No total timeout: chat streams and the radio run as long as the user listens.
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
# The desktop's framebuffer updates can be large; Hermes's own WebSocket messages are small.
_WS_MAX_MESSAGE = 64 * 1024 * 1024


def upstream_headers(request: web.Request, skip=()) -> dict:
    """The request's headers minus hop-by-hop ones and JARVIS's own login (its session cookie and a Basic password),
    which the upstream has no use for. Hermes's Bearer key passes: Hermes checks it."""
    headers = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower.startswith(skip) or lower == LAUNCHER_HEADER.lower():
            continue
        if lower == "authorization" and not value.startswith("Bearer "):
            continue
        if lower == "cookie":
            value = "; ".join(p.strip() for p in value.split(";") if p.strip() and p.strip().split("=", 1)[0] != COOKIE)
            if not value:
                continue
        headers[key] = value
    return headers


class Proxy:
    def __init__(self):
        self._session = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT, auto_decompress=False,
                                                  connector=aiohttp.TCPConnector(limit=0))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def forward(self, request: web.Request, target: str, path: Optional[str] = None,
                      name: str = "Hermes", vouch: str = "", extra_headers: Optional[dict] = None) -> web.StreamResponse:
        """Send *request* to *target*, at *path* (the raw path and query; default: the request's own). *vouch* (the
        Hermes API key) goes along as LAUNCHER_HEADER."""
        rel = path if path is not None else str(request.rel_url)   # the raw (still percent-encoded) path and query
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request, target, rel, name, vouch)
        url = target.rstrip("/") + rel
        headers = upstream_headers(request)
        if vouch:
            headers[LAUNCHER_HEADER] = vouch
        headers["X-Forwarded-For"] = request.remote or ""
        headers["X-Forwarded-Proto"] = request.scheme
        data = request.content if request.body_exists else None
        session = await self.session()
        try:
            upstream = await session.request(request.method, URL(url, encoded=True), headers=headers, data=data, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("%s unreachable for %s %s: %s", name, request.method, request.path, exc)
            if name == "Hermes":
                message = "JARVIS can't reach Hermes right now. Open /jarvis/ to check the connection."
            else:
                message = f"JARVIS can't reach {name} right now."
            return web.json_response({"error": {"code": "upstream_unreachable" if name != "Hermes" else
                                                "hermes_unreachable", "message": message}}, status=502)
        try:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    response.headers.add(key, value)
            for key, value in (extra_headers or {}).items():
                response.headers[key] = value
            if upstream.headers.get("Content-Length") and "Content-Encoding" not in upstream.headers:
                response.content_length = int(upstream.headers["Content-Length"])
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
        except (ConnectionResetError, asyncio.CancelledError, aiohttp.ClientError):
            # The browser went away (a stopped stream, a closed tab): nothing more to send.
            return response
        finally:
            upstream.release()

    async def _websocket(self, request: web.Request, target: str, rel: str, name: str,
                         vouch: str = "") -> web.StreamResponse:
        url = target.rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1) + rel
        headers = upstream_headers(request, skip=("sec-websocket",))
        if vouch:
            headers[LAUNCHER_HEADER] = vouch
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        session = await self.session()
        try:
            # Upstream first, so the browser gets the subprotocol the upstream chose (noVNC asks for "binary").
            upstream = await session.ws_connect(URL(url, encoded=True), headers=headers, protocols=protocols,
                                                max_msg_size=_WS_MAX_MESSAGE)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("WebSocket to %s failed: %s", name, exc)
            return web.json_response({"error": {"code": "upstream_unreachable",
                                                "message": f"JARVIS can't reach {name} right now."}}, status=502)
        client = web.WebSocketResponse(protocols=(upstream.protocol,) if upstream.protocol else (),
                                       max_msg_size=_WS_MAX_MESSAGE)
        await client.prepare(request)
        try:
            async with upstream:
                async def pump(source, sink):
                    async for message in source:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await sink.send_str(message.data)
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            await sink.send_bytes(message.data)
                        else:
                            break
                tasks = [asyncio.create_task(pump(client, upstream)), asyncio.create_task(pump(upstream, client))]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("WebSocket to %s failed: %s", name, exc)
        await client.close()
        return client
