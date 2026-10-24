"""The home-screen app's service worker in a real browser, served with the gateway's own security headers.

Once installed, the worker handles every dashboard load. It used to be served with the global CSP
(default-src 'none'), which blocked its own fetch, so every load after the first showed "Can't reach the server"
and Try again could not fix it (HANDOFF 33).

Run with scripts/run_tests.sh tests/gateway/test_vesper_service_worker_browser.py -m integration
after installing Playwright and Chromium in the test environment.
"""

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from jarvis import vesper_app as api_server_vesper_app
from gateway.platforms.api_server import APIServerAdapter

DASHBOARD = "JARVIS / VESPER"
OFFLINE = "Can't reach the server"
# What a phone had installed before the fix, in effect: a worker that answers every load with its offline page.
TRAPPING_WORKER = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.mode === 'navigate') event.respondWith(new Response(
    "<title>Can't reach the server</title><button onclick='location.reload()'>Try again</button>",
    { headers: { 'Content-Type': 'text/html' } }));
});
"""


def served_app(worker=None):
    """The routes behind the gateway's real middleware, the way connect() builds it. *worker* replaces /vesper/sw.js."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sw-test-key"}))
    application = web.Application(middlewares=[api_server.cors_middleware, api_server.body_limit_middleware,
                                               api_server.security_headers_middleware])
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, worker if worker and path == "/vesper/sw.js" else handler)
    application["api_server_adapter"] = adapter
    return application


def browser_launch():
    cached = sorted((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux64/chrome"))
    return {"headless": True, **({"executable_path": str(cached[-1])} if cached else {})}


async def phone(browser):
    context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    await context.add_init_script("try { localStorage.setItem('vesperTourDone', '1') } catch (e) {}")
    return context


@pytest.mark.integration
@pytest.mark.asyncio
async def test_installed_worker_loads_the_dashboard_and_recovers_after_going_offline(monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    from jarvis import vesper_voice
    monkeypatch.setattr(api_server_vesper_voice, "available", lambda: {"elevenlabs": False, "pocket": False, "omnivoice": False, "edge": False, "piper": True})
    # 127.0.0.1 is a secure context, so the browser runs the worker as it does on the https Tailscale address.
    async with TestServer(served_app(), host="127.0.0.1") as server, playwright.async_playwright() as p:
        browser = await p.chromium.launch(**browser_launch())
        try:
            context = await phone(browser)
            page = await context.new_page()
            await page.goto(str(server.make_url("/vesper")))
            assert await page.title() == DASHBOARD
            await page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=30000)

            for _ in range(2):   # every load through the worker: the dashboard, never the offline page
                await page.reload()
                assert await page.title() == DASHBOARD
                assert await page.evaluate("navigator.serviceWorker.controller !== null")

            # Offline: the worker's page. Back online: it reloads into the dashboard by itself.
            await context.set_offline(True)
            await page.reload()
            assert await page.title() == OFFLINE
            await context.set_offline(False)
            await page.wait_for_function(f"document.title === {DASHBOARD!r}", timeout=20000)
            # The worker is registered again by the page it came back to.
            await page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=30000)
            await page.reload()
            assert await page.title() == DASHBOARD
        finally:
            await browser.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_app_trapped_by_an_older_worker_recovers_with_no_tap_and_live_dashboards_are_never_reloaded(monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    from jarvis import vesper_voice
    monkeypatch.setattr(api_server_vesper_voice, "available", lambda: {"elevenlabs": False, "pocket": False, "omnivoice": False, "edge": False, "piper": True})
    serving = {"script": TRAPPING_WORKER}

    async def worker(request):
        response = await api_server_vesper_app.service_worker(request)
        response.text = serving["script"]
        return response

    async with TestServer(served_app(worker), host="127.0.0.1") as server, playwright.async_playwright() as p:
        browser = await p.chromium.launch(**browser_launch())
        try:
            context = await phone(browser)
            page = await context.new_page()
            url = str(server.make_url("/vesper"))
            await page.goto(url)
            await page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=30000)
            await page.reload()
            assert await page.title() == OFFLINE   # trapped

            serving["script"] = api_server_vesper_app._SW   # the fix is deployed; the user only opens the app
            await page.close()
            page = await context.new_page()
            await page.goto(url)
            assert await page.title() == OFFLINE
            await page.wait_for_function(f"document.title === {DASHBOARD!r}", timeout=20000)
            await page.reload()
            assert await page.title() == DASHBOARD

            # A later worker takes over a running dashboard without reloading it.
            await page.evaluate("window.stillHere = true")
            serving["script"] = api_server_vesper_app._SW + "\n// a later version\n"
            assert await page.evaluate("""() => new Promise(resolve => {
                navigator.serviceWorker.addEventListener('controllerchange', () => resolve(true));
                navigator.serviceWorker.getRegistration('/vesper').then(reg => reg.update());
                setTimeout(() => resolve(false), 15000);
            })""")
            await page.wait_for_timeout(4000)   # past the worker's 2 s wait for an answer
            assert await page.evaluate("window.stillHere === true")
        finally:
            await browser.close()
