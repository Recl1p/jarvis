// OmniVoice, installed for real from the setup screen (the CPU build, about 9 GB: opt in with E2E_OMNIVOICE=1), then
// speaking through the dashboard's voice route, then removed again. The GPU build only differs in its torch.
import { test, expect, dashboardKey, auth } from './helpers';

test.describe.serial('OmniVoice, the optional voice engine', () => {
  test.skip(process.env.E2E_OMNIVOICE !== '1', 'E2E_OMNIVOICE=1 to download and install OmniVoice (about 9 GB)');

  test('installs from the setup screen, with its log', async ({ page, problems }) => {
    test.setTimeout(90 * 60_000);
    page.on('dialog', dialog => dialog.accept());
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: 'Install for the processor (CPU)' }).click();
    await expect(page.locator('#ov-log')).toContainText('== Getting Python 3.12', { timeout: 120_000 });
    await expect(page.locator('#ov-state')).toContainText('Installed (CPU build)', { timeout: 85 * 60_000 });
    await expect(page.locator('#ov-log')).toContainText('== OmniVoice is installed');
    await expect(page.getByRole('button', { name: 'Remove OmniVoice' })).toBeVisible();
  });

  test('the dashboard speaks with it', async ({ request }) => {
    test.setTimeout(15 * 60_000);
    const key = await dashboardKey(request);
    const setup = await (await request.get('/vesper/setup/voice', { headers: auth(key) })).json();
    expect(setup.engines.omnivoice).toBe(true);
    const resp = await request.post('/vesper/tts', { headers: auth(key), timeout: 14 * 60_000,
      data: { text: 'OmniVoice is working.', persona: 'jarvis', engine: 'omnivoice' } });
    expect(resp.status(), await resp.text()).toBe(200);
    expect(resp.headers()['x-vesper-voice']).toBe('omnivoice');
    expect((await resp.body()).length).toBeGreaterThan(10_000);
    const status = await (await request.get('/vesper/voice/status?engine=omnivoice', { headers: auth(key) })).json();
    expect(status.device).toBe('cpu');
  });

  test('removing it frees the space and the dashboard stops offering it', async ({ page, request, problems }) => {
    test.setTimeout(10 * 60_000);
    page.on('dialog', dialog => dialog.accept());
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: 'Remove OmniVoice' }).click();
    await expect(page.locator('#ov-state')).toContainText('Not installed', { timeout: 5 * 60_000 });
    const key = await dashboardKey(request);
    const setup = await (await request.get('/vesper/setup/voice', { headers: auth(key) })).json();
    expect(setup.engines.omnivoice).toBe(false);
  });
});
