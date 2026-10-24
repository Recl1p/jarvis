"""A small stand-in for a Hermes API server with the JARVIS connector, configurable per test."""

import asyncio

from aiohttp import web

KEY = "k" * 64


def make_app(*, platform="hermes-agent", version="0.22.5", connector="2.1.0", isolation="in_process",
             model="mock-model", key=KEY):
    def authed(request):
        return request.headers.get("Authorization") == f"Bearer {key}"

    async def health(request):
        return web.json_response({"status": "ok", "platform": platform, "version": version})

    async def models(request):
        return web.json_response({"data": []}) if authed(request) else web.json_response({"error": "no"}, status=401)

    async def ping(request):
        if connector is None:
            raise web.HTTPNotFound()
        return web.json_response({"connector": "jarvis", "version": connector})

    async def check(request):
        if connector is None:
            raise web.HTTPNotFound()
        if not authed(request):
            return web.json_response({"error": "no"}, status=401)
        return web.json_response({"ok": True, "isolation": isolation, "version": connector})

    async def vesper(request):
        if connector is None:
            raise web.HTTPNotFound()
        return web.Response(text=f'<body data-vesper-key="{key}">dashboard</body>', content_type="text/html")

    async def setup_status(request):
        return web.json_response({"provider": "custom", "model": model}) if authed(request) else web.Response(status=401)

    async def echo(request):
        body = await request.read()
        return web.json_response({"host": request.host, "method": request.method, "path": request.path_qs,
                                  "length": len(body), "forwarded_for": request.headers.get("X-Forwarded-For", ""),
                                  "authorization": request.headers.get("Authorization", ""),
                                  "cookie": request.headers.get("Cookie", ""),
                                  "launcher": request.headers.get("X-JARVIS-Launcher", "")})

    async def stream(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for i in range(3):
            await resp.write(f"data: {i}\n\n".encode())
            await asyncio.sleep(0.05)
        return resp

    app = web.Application(client_max_size=300 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/vesper/connector", ping)
    app.router.add_get("/vesper/connector/check", check)
    app.router.add_get("/vesper", vesper)
    app.router.add_get("/vesper/setup/status", setup_status)
    app.router.add_route("*", "/echo/{tail:.*}", echo)
    app.router.add_get("/stream", stream)
    return app
