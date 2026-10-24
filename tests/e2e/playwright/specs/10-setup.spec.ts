// The setup screen of a JARVIS that has no Hermes yet: the two paths, the consent gate, the "I already have Hermes"
// tools, the request guards, and a phone-sized layout. Nothing here changes the instance (install runs in 20-install).
import { test, expect, setupHeaders, expectNoHorizontalScroll } from './helpers';

test.describe('setup screen, before any Hermes', () => {
  test('a bare visit lands on the setup screen with both paths', async ({ page, problems }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/jarvis\/$/);
    await expect(page.getByRole('heading', { name: 'Connect JARVIS to Hermes' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Install Hermes for me/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /I already have Hermes/ })).toBeVisible();
    await expect(page.getByText(/JARVIS \d+\.\d+\.\d+/)).toBeVisible();
  });

  test('/vesper also opens the setup screen until a Hermes is connected', async ({ page, problems }) => {
    await page.goto('/vesper');
    await expect(page).toHaveURL(/\/jarvis\/$/);
  });

  test('installing needs the consent box, and says exactly what will be downloaded', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /Install Hermes for me/ }).click();
    await expect(page.getByRole('heading', { name: 'Install Hermes Agent' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'https://hermes-agent.nousresearch.com/install.sh' })).toBeVisible();
    await expect(page.locator('#c-channel')).toHaveText('stable');
    await expect(page.locator('#c-dir')).toHaveText('/opt/data/hermes-agent');
    const install = page.getByRole('button', { name: 'Download and install' });
    await expect(install).toBeDisabled();
    const agree = page.getByLabel(/I agree to download and install Hermes Agent/);
    await agree.check();
    await expect(install).toBeEnabled();
    await agree.uncheck();
    await expect(install).toBeDisabled();
    await expect(page.getByLabel(/Give Hermes the JARVIS personality/)).toBeChecked();
    await expect(page.getByLabel(/Keep Hermes updated automatically/)).toBeChecked();
    await page.getByRole('button', { name: 'Back' }).click();
    await expect(page.getByRole('heading', { name: 'Connect JARVIS to Hermes' })).toBeVisible();
  });

  test('a wrong address fails the connection test with what to do about it', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    await expect(page.getByRole('heading', { name: 'Connect your Hermes' })).toBeVisible();
    await page.getByLabel('Hermes API server address').fill('http://127.0.0.1:9');
    await page.getByRole('button', { name: 'Test connection' }).click();
    const failed = page.locator('#test-results li.no').first();
    await expect(failed).toContainText('Hermes answers at this address');
    await expect(failed.locator('.hint')).toContainText('listen on 0.0.0.0');
    await page.getByRole('button', { name: 'Connect', exact: true }).click();
    await expect(page.locator('#test-results li.no').first()).toContainText('Hermes answers at this address');
    await expect(page.getByRole('heading', { name: 'Connect your Hermes' })).toBeVisible();
  });

  test('the connect prompt carries this JARVIS address and a fresh one-time code', async ({ page, baseURL, problems }) => {
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    const prompt = page.locator('#prompt');
    await expect(prompt).toHaveValue(/connect\.sh --pair \S{20,}/);
    const text = await prompt.inputValue();
    expect(text).toContain(`${baseURL}/jarvis/connect.sh`);
    expect(text).toContain("Don't repeat the API key in this chat");
    const code = /--pair (\S+)/.exec(text)![1];
    await expect(page.locator('#command')).toContainText(`--pair ${code}`);
    await page.getByRole('button', { name: 'New code' }).click();
    await expect(prompt).not.toHaveValue(new RegExp(code));
  });

  test('Find my Hermes searches and reports its result', async ({ page, problems }) => {
    test.setTimeout(90_000);
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    await page.getByRole('button', { name: 'Find my Hermes' }).click();
    await expect(page.locator('#detect-status')).toHaveText(/Found \d+\.|No Hermes found/, { timeout: 60_000 });
    await expect(page.getByRole('button', { name: 'Find my Hermes' })).toBeEnabled();
  });

  test('phone width: every view fits without sideways scrolling', async ({ page, problems }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/jarvis/');
    await expectNoHorizontalScroll(page);
    await page.getByRole('button', { name: /Install Hermes for me/ }).click();
    await expectNoHorizontalScroll(page);
    await page.getByRole('button', { name: 'Back' }).click();
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    await expect(page.locator('#prompt')).toHaveValue(/connect\.sh/);
    await expectNoHorizontalScroll(page);
  });
});

test.describe('setup API guards', () => {
  test('changes need the X-JARVIS header, the same origin, and explicit consent', async ({ request }) => {
    const noHeader = await request.post('/jarvis/api/install', { data: { accept: true } });
    expect(noHeader.status()).toBe(403);
    expect((await noHeader.json()).error.code).toBe('missing_header');
    const crossOrigin = await request.post('/jarvis/api/install', {
      headers: { ...setupHeaders(), Origin: 'http://evil.example' }, data: { accept: true } });
    expect(crossOrigin.status()).toBe(403);
    const noConsent = await request.post('/jarvis/api/install', { headers: setupHeaders(), data: { accept: false } });
    expect(noConsent.status()).toBe(400);
    expect((await noConsent.json()).error.code).toBe('consent_required');
    const state = await (await request.get('/jarvis/api/state')).json();
    expect(state.configured).toBe(false);
    expect(state.install.status).toBe('idle');
  });

  test('pairing needs a valid code', async ({ request }) => {
    const resp = await request.post('/jarvis/api/pair', {
      headers: setupHeaders(), data: { code: 'not-a-real-code', urls: ['http://127.0.0.1:9'], api_key: 'x'.repeat(40) } });
    expect(resp.status()).toBe(403);
  });

  test('before a Hermes is connected, Hermes routes answer 503 and the launcher stays healthy', async ({ request }) => {
    const health = await request.get('/jarvis/health');
    expect(health.ok()).toBeTruthy();
    expect((await health.json()).platform).toBe('jarvis-launcher');
    const models = await request.get('/v1/models');
    expect(models.status()).toBe(503);
    expect((await models.json()).error.code).toBe('not_connected');
  });

  test('the connector and connect script are served for other Hermes installs', async ({ request, baseURL }) => {
    const tarball = await request.get('/jarvis/connector.tar.gz');
    expect(tarball.ok()).toBeTruthy();
    const bytes = await tarball.body();
    expect(bytes[0]).toBe(0x1f);
    expect(bytes[1]).toBe(0x8b);
    const script = await (await request.get('/jarvis/connect.sh')).text();
    expect(script.startsWith('#!/usr/bin/env bash')).toBeTruthy();
    expect(script).toContain(`JARVIS_URL="${baseURL}"`);
    expect(script).not.toContain('@JARVIS_URL@');
    const soul = await request.get('/jarvis/persona/SOUL.md');
    expect(await soul.text()).toContain('You are JARVIS');
  });
});
