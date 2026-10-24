"""Optional browser contract test: real DC dashboard and isolated Hermes job API.

Run with scripts/run_tests.sh tests/gateway/test_vesper_browser.py -m integration
after installing Playwright and Chromium in the test environment.
"""

import asyncio
import io
import json
import wave
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from cron.jobs import get_job, list_jobs, save_job_output
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_agent_workflow_in_browser(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    from jarvis import vesper_voice
    # The page's /vesper/tts is stubbed below; the voice status it asks for must not wake a real model on the host.
    monkeypatch.setattr(api_server_vesper_voice, "available", lambda: {"elevenlabs": False, "pocket": False, "omnivoice": False, "edge": False, "piper": True})
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "browser-test-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    cached = sorted((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux64/chrome"))
    launch = {"headless": True}
    if cached:
        launch["executable_path"] = str(cached[-1])
    async with TestServer(application) as server, playwright.async_playwright() as p:
        browser = await p.chromium.launch(**launch)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            # The setup guide opens itself on a browser's first visit; this test drives the
            # dashboard behind it, so start as a returning visitor.
            await page.add_init_script("try { localStorage.setItem('vesperTourDone', '1') } catch (e) {}")
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(str(server.make_url("/vesper")))
            dashboard = page.locator('.jarvis')
            await playwright.expect(dashboard).to_have_attribute('data-layout', 'glass', timeout=60000)
            await page.get_by_title('Switch dashboard layout').click()
            await playwright.expect(dashboard).to_have_attribute('data-layout', 'cinematic')
            await page.reload()
            await playwright.expect(dashboard).to_have_attribute('data-layout', 'cinematic', timeout=60000)
            await page.keyboard.press('Control+k')
            await page.get_by_role('textbox', name='Search commands').fill('internet speed')
            await page.get_by_role('textbox', name='Search commands').press('Enter')
            await playwright.expect(page.locator('.sidebar-content')).to_contain_text('INTERNET SPEED')
            await playwright.expect(page.locator('.sidebar-content')).to_contain_text('COMPLETE', timeout=125000)
            assert '—' not in await page.locator('.sidebar-content').inner_text()
            await page.get_by_title('Vitals', exact=True).click()
            await page.get_by_role("button", name="▸ AGENT CONTROL · OPEN").click(timeout=60000)
            panel = page.locator("vesper-agent-control")
            await panel.get_by_role("button", name="+ New agent").click()
            await panel.locator('[name="template"]').select_option("1")
            await panel.locator('[name="name"]').fill("Browser research tracker")
            await panel.locator('[name="prompt"]').fill("Summarize supplied notes with citations.")
            await panel.locator('[name="schedule"]').fill("not a valid schedule")
            await panel.get_by_role("button", name="Create agent", exact=True).click()
            await playwright.expect(panel.locator("#notice")).to_have_class("error")
            assert not list_jobs(include_disabled=True)
            await panel.locator('[name="schedule"]').fill("every 6h")
            await panel.get_by_role("button", name="Create agent", exact=True).click()
            await playwright.expect(panel.get_by_role("heading", name="Browser research tracker")).to_be_visible()
            job = list_jobs(include_disabled=True)[0]
            assert job["schedule"]["minutes"] == 360
            await panel.get_by_role("button", name="Pause schedule", exact=True).click()
            await playwright.expect(panel.get_by_role("button", name="Resume", exact=True)).to_be_visible()
            assert get_job(job["id"])["state"] == "paused"
            await panel.get_by_role("button", name="Edit", exact=True).click()
            await panel.locator('[name="name"]').fill("Updated tracker")
            await panel.get_by_role("button", name="Save changes", exact=True).click()
            await playwright.expect(panel.get_by_role("heading", name="Updated tracker")).to_be_visible()
            save_job_output(job["id"], "# Real result\n\n<script>window.compromised=true</script>")
            await panel.get_by_role("button", name="Refresh output", exact=True).click()
            await playwright.expect(panel.locator("#output")).to_contain_text("# Real result")
            assert await page.evaluate("window.compromised") is None
            await page.screenshot(path=str(tmp_path / "vesper-agents-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(tmp_path / "vesper-agents-mobile.png"))
            assert await panel.locator('dialog').evaluate("el => el.scrollWidth <= el.clientWidth")
            await panel.get_by_role("button", name="Close agent control").click()
            await playwright.expect(panel.locator('dialog')).not_to_be_visible()
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.get_by_title('Operations board', exact=True).click()
            card = page.locator('.operations').get_by_role('button', name='Updated tracker', exact=False)
            await playwright.expect(card).to_be_visible()
            await playwright.expect(card).to_contain_text('PAUSED')
            await card.click()
            await playwright.expect(panel.get_by_role('heading', name='Updated tracker')).to_be_visible()
            await panel.get_by_role('button', name='Close agent control').click()
            await page.get_by_role('button', name='RETURN TO CORE', exact=True).click()
            await page.get_by_title('Switch model', exact=True).click()
            await playwright.expect(page.get_by_role('dialog', name='Select model')).to_be_visible()
            await page.keyboard.press('Escape')

            # Exercise the new composer against an explicit transport, without an LLM
            # call or TTS provider credentials. The scheduler above is the real store.
            requests = []
            async def chat_transport(route):
                req = route.request
                requests.append((req.url, req.headers.get('authorization'), req.post_data_json))
                if req.url.endswith('/api/sessions'):
                    await route.fulfill(json={'session': {'id': 'ui-test-session'}})
                else:
                    await route.fulfill(content_type='text/event-stream', body=(
                        'event: tool.started\ndata: {"tool_name":"oj_browser_extract"}\n\n'
                        'event: assistant.completed\ndata: ' + json.dumps({'content': 'Dashboard transport verified.'}) + '\n\n'
                    ))
            sound = io.BytesIO()
            with wave.open(sound, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
                wav.writeframes(b'\0' * 320000)
            async def voice_transport(route):
                requests.append((route.request.url, route.request.headers.get('authorization'), route.request.post_data_json))
                await route.fulfill(content_type='audio/wav', body=sound.getvalue())
            await page.route('**/api/sessions', chat_transport)
            await page.route('**/api/sessions/*/chat/stream', chat_transport)
            await page.route('**/vesper/tts', voice_transport)
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.get_by_title('Toggle sidebar', exact=True).click()
            await page.get_by_role('textbox', name='Message JARVIS').fill('Verify this dashboard')
            await page.get_by_role('button', name='SEND ▸', exact=True).click()
            await playwright.expect(page.get_by_role('log', name='Dialogue')).to_contain_text('Dashboard transport verified.', timeout=15000)
            skip = page.get_by_title('Skip voice (Escape)', exact=True)
            await playwright.expect(skip).to_be_visible()
            await page.keyboard.press('Escape')
            await playwright.expect(skip).not_to_be_visible()
            await playwright.expect(page.get_by_role('button', name='SEND ▸', exact=True)).to_be_enabled()
            await playwright.expect(page.get_by_role('log', name='Dialogue')).to_contain_text('Dashboard transport verified.')
            assert all(auth == 'Bearer browser-test-key'  for _, auth, _ in requests)
            assert any(url.endswith('/chat/stream') and data['message'] == 'Verify this dashboard' for url, _, data in requests)
            assert any(url.endswith('/vesper/tts') and data['text'] == 'Dashboard transport verified.' for url, _, data in requests)
            # Cancelling while synthesis is pending must also suppress a late response.
            hold_voice = asyncio.Event()
            async def pending_voice(route):
                await hold_voice.wait()
                try:
                    await route.fulfill(content_type='audio/wav', body=sound.getvalue())
                except playwright.Error:
                    pass  # Aborted request may already have been disposed by the browser.
            await page.unroute('**/vesper/tts', voice_transport)
            await page.route('**/vesper/tts', pending_voice)
            await page.get_by_role('textbox', name='Message JARVIS').fill('Verify pending narration skip')
            await page.get_by_role('button', name='SEND ▸', exact=True).click()
            await playwright.expect(skip).to_be_visible()
            await skip.click()
            hold_voice.set()
            await playwright.expect(skip).not_to_be_visible()
            await playwright.expect(page.get_by_role('button', name='SEND ▸', exact=True)).to_be_enabled()
            await playwright.expect(page.get_by_role('log', name='Dialogue')).to_contain_text('Dashboard transport verified.')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.get_by_title('Telemetry', exact=True).click()
            await playwright.expect(page.locator('.sidebar-content')).to_contain_text('oj_browser_extract started')
            assert not errors, errors
            print(f"Browser screenshots: {tmp_path}")
        finally:
            await browser.close()
