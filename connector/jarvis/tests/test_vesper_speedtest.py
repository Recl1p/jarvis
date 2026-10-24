"""Browser regression coverage for bounded Cloudflare failures and cancellation."""
import asyncio
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs,urlparse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_large_sample_retains_partial_result_and_bounds_retries():
    pw=pytest.importorskip('playwright.async_api')
    adapter=APIServerAdapter(PlatformConfig(enabled=True,extra={'key':'speedtest-fixture-key'}))
    app=web.Application()
    for method,path,handler in adapter._http_route_table():app.router.add_route(method,path,handler)
    cached=sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
    async with TestServer(app) as server,pw.async_playwright() as p:
        browser=await p.chromium.launch(headless=True,**({'executable_path':str(cached[-1])} if cached else {}))
        try:
            page=await browser.new_page(viewport={'width':1440,'height':1000})
            # A first visit opens the setup guide, which covers the page; this test is about the speed panel.
            await page.add_init_script("try{localStorage.setItem('vesperTourDone','1')}catch(e){}")
            failures=[];sizes=[]
            async def transport(route):
                url=urlparse(route.request.url)
                size=int(parse_qs(url.query).get('bytes',['0'])[0]);sizes.append(size)
                if url.path=='/__down' and size>=25000000:
                    failures.append(route.request.url)
                    await route.abort('failed')
                else:await route.continue_()
            await page.route('https://speed.cloudflare.com/**',transport)
            await page.goto(str(server.make_url('/vesper')))
            await pw.expect(page.locator('.jarvis')).to_be_visible(timeout=60000)
            await page.keyboard.press('Control+k')
            await page.get_by_role('textbox',name='Search commands').fill('internet speed')
            await page.get_by_role('textbox',name='Search commands').press('Enter')
            sidebar=page.locator('.floating-panels > div').nth(2)
            await pw.expect(sidebar.get_by_role('button',name='RUN SPEED TEST',exact=True)).to_be_enabled(timeout=60000)
            assert failures and max(Counter(failures).values())<=3
            await pw.expect(sidebar).to_contain_text('PARTIAL')
            assert max(sizes)<=25000000
            assert '—' not in await sidebar.inner_text()
            await pw.expect(sidebar).to_contain_text('Some Cloudflare transfers failed')
            # A fresh run must clear the old error; cancelling during retry backoff
            # must stop subsequent requests and preserve a usable Run button.
            await page.unroute('https://speed.cloudflare.com/**',transport)
            attempts=[];failed=asyncio.Event()
            async def offline(route):
                attempts.append(route.request.url)
                await route.abort('failed');failed.set()
            await page.route('https://speed.cloudflare.com/**',offline)
            await sidebar.get_by_role('button',name='RUN SPEED TEST',exact=True).click()
            await asyncio.wait_for(failed.wait(),timeout=10)
            await sidebar.get_by_role('button',name='CANCEL TEST',exact=True).click()
            await pw.expect(sidebar).to_contain_text('CANCELLED')
            count=len(attempts)
            await page.wait_for_timeout(2000)
            assert len(attempts)==count
            await pw.expect(sidebar.get_by_role('button',name='RUN SPEED TEST',exact=True)).to_be_enabled()
            # No successful transfer must remain unavailable, never a false 0 Mbps.
            await sidebar.get_by_role('button',name='RUN SPEED TEST',exact=True).click()
            await pw.expect(sidebar.get_by_role('button',name='RUN SPEED TEST',exact=True)).to_be_enabled(timeout=30000)
            await pw.expect(sidebar).to_contain_text('INCOMPLETE')
            assert (await sidebar.inner_text()).count('—') >= 2
        finally:await browser.close()
