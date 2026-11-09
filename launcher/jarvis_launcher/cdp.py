"""A DevTools (CDP) proxy for the browser desktop's Chromium, so a Hermes in another container can drive it.

Chromium serves DevTools on 127.0.0.1:9222 only (a visible, non-headless Chromium binds loopback whatever
--remote-debugging-address says), and refuses an HTTP request whose Host header is neither an IP address nor
"localhost". This proxy listens on config.CDP_PROXY_LISTEN, sends each request on with Chromium's own address as the
Host, and rewrites the addresses in Chromium's JSON answers (webSocketDebuggerUrl, devtoolsFrontendUrl) to the one the
client used, so the client opens its WebSockets through the proxy too.

DevTools has no authentication: whoever reaches it controls the browser and every site logged in there. Listen only
on a private network that just JARVIS and its Hermes share (compose.yaml), never on a published port.
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from aiohttp import web
from yarl import URL

from . import config

logger = logging.getLogger(__name__)

_WS_MAX_MESSAGE = 256 * 1024 * 1024    # screenshots and DOM snapshots can be large
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)


def _listen(value: str) -> tuple:
    host, _, port = value.rpartition(":")
    return (host.strip("[]") or "0.0.0.0"), int(port)


class CDPProxy:
    def __init__(self, upstream: str = ""):
        self.upstream = (upstream or config.CDP_UPSTREAM).rstrip("/")
        self.upstream_host = URL(self.upstream).raw_authority
        self._session: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT, auto_decompress=False)
        return self._session

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def start(self, listen: str) -> None:
        host, port = _listen(listen)
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        logger.info("DevTools proxy for the browser desktop on %s:%s -> %s", host, port, self.upstream)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request)
        session = await self.session()
        body = await request.read() if request.body_exists else None
        try:
            async with session.request(request.method, URL(self.upstream + str(request.rel_url), encoded=True),
                                       data=body, allow_redirects=False) as upstream:
                data = await upstream.read()
                content_type = upstream.headers.get("Content-Type", "application/json")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return web.json_response({"error": f"the browser desktop's Chromium is not answering: {exc}"}, status=502)
        if "json" in content_type and request.host:
            data = data.replace(self.upstream_host.encode(), request.host.encode())
        return web.Response(body=data, status=upstream.status, headers={"Content-Type": content_type})

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        url = self.upstream.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + str(request.rel_url)
        session = await self.session()
        try:
            # No Origin and Chromium's own Host: what a local DevTools client would send.
            upstream = await session.ws_connect(URL(url, encoded=True), max_msg_size=_WS_MAX_MESSAGE, autoping=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return web.json_response({"error": f"the browser desktop's Chromium is not answering: {exc}"}, status=502)
        client = web.WebSocketResponse(max_msg_size=_WS_MAX_MESSAGE)
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
            logger.info("DevTools WebSocket ended: %s", exc)
        await client.close()
        return client
