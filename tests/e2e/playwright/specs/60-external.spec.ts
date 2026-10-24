// "I already have Hermes", end to end, against a stock Hermes in another container: the pasted prompt's command runs
// there (tests/e2e/hermes-stock/entry.sh plays the agent), pairs back, and JARVIS connects by itself. Then the
// dashboard runs through the proxy on that Hermes, chat streams through it, and the paths around it work.
import * as fs from 'fs';
import * as path from 'path';
import { test, expect, SHARED_DIR, setupHeaders, guide, skipGuide, openDashboard, openGuideAt, say, dashboardKey, ensurePassword, ANONYMOUS } from './helpers';
import { request as apiRequest } from '@playwright/test';

/** Hand a command to the test Hermes's "agent" and wait for its output. */
async function runOnHermes(command: string, timeoutMs = 240_000): Promise<string> {
  for (const f of ['run.out', 'done.sh']) fs.rmSync(path.join(SHARED_DIR, f), { force: true });
  fs.writeFileSync(path.join(SHARED_DIR, 'run.sh'), command + '\n');
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(path.join(SHARED_DIR, 'done.sh'))) return fs.readFileSync(path.join(SHARED_DIR, 'run.out'), 'utf8');
    await new Promise(r => setTimeout(r, 1000));
  }
  throw new Error(`the test Hermes did not finish: ${command}`);
}

test.describe.serial('connect an existing Hermes', () => {
  test.beforeAll(async ({ request }) => { await ensurePassword(request); });

  test('the pasted command installs the connector there and pairs JARVIS by itself', async ({ page, problems }) => {
    test.setTimeout(10 * 60_000);
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    await expect(page.locator('#command')).toContainText('--pair');
    const command = (await page.locator('#command').textContent())!.trim();
    const out = await runOnHermes(command);
    expect(out, out).toContain('Installing the JARVIS connector');
    expect(out).toContain('Sent the address and key to JARVIS');
    expect(out).toContain('exit=0');
    // The screen shows "waiting", then reloads into the connected view once the paired Hermes answers.
    await expect(page.getByRole('heading', { name: 'JARVIS is connected' })).toBeVisible({ timeout: 4 * 60_000 });
    await expect(page.locator('#s-mode')).toHaveText('Your own Hermes');
  });

  test("that Hermes, opened directly on its own port, doesn't hand out the dashboard or its key", async ({ request }) => {
    const direct = await apiRequest.newContext({ httpCredentials: ANONYMOUS });
    for (const path of ['/vesper', '/vesper/files?path=/etc/hostname', '/vesper/status']) {
      const resp = await direct.get('http://hermes-ext:8642' + path);
      expect(resp.status(), path).toBe(403);
      expect(await resp.text()).not.toContain('data-vesper-key');
    }
    const key = await dashboardKey(request);   // through JARVIS, after its login
    expect((await direct.get('http://hermes-ext:8642/vesper', { headers: { Authorization: `Bearer ${key}` } })).status()).toBe(200);
    await direct.dispose();
  });

  test('Test connection is all green, and Find my Hermes now finds it with its connector', async ({ page, problems }) => {
    test.setTimeout(120_000);
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.locator('#retest-results')).toContainText('Connection works.', { timeout: 30_000 });
    await expect(page.locator('#retest-results li.no')).toHaveCount(0);
    await expect(page.locator('#updates-card')).toBeHidden();
  });

  test('the dashboard is that Hermes\'s, through JARVIS; the guide says so', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await openGuideAt(page, 'hermes');
    const tour = guide(page);
    await expect(tour.getByText('your own Hermes Agent')).toBeVisible({ timeout: 30_000 });
    await expect(tour.getByText('The connection works.')).toBeVisible({ timeout: 30_000 });
  });

  test('a model connected through the guide answers in chat, streamed through the proxy', async ({ page, request, problems }) => {
    test.setTimeout(240_000);
    await skipGuide(page);
    await openDashboard(page);
    await openGuideAt(page, 'connect');
    const tour = guide(page);
    await tour.getByRole('tab', { name: /Local/ }).click();
    await expect(tour.getByText('Found a model server')).toBeVisible({ timeout: 60_000 });
    await tour.getByRole('button', { name: 'Connect', exact: true }).click();
    await expect(tour.getByRole('tabpanel').getByText('Connected: mock-jarvis')).toBeVisible({ timeout: 60_000 });
    await tour.getByRole('button', { name: 'Close tour' }).click();
    await say(page, 'Hello through the proxy');
    await expect(page.getByRole('log', { name: 'Dialogue' })).toContainText('Mock reply: Hello through the proxy', { timeout: 90_000 });
  });

  test('the personality is installed only when asked (--persona-only)', async ({ request }) => {
    const out = await runOnHermes('bash /tmp/jarvis-connect.sh --persona-only');
    expect(out).toContain('JARVIS personality installed');
    expect(out).toContain('exit=0');
  });

  test('disconnecting returns to the welcome screen; nothing on that Hermes is touched', async ({ page, request, problems }) => {
    const key = await dashboardKey(request);
    const resp = await request.post('/jarvis/api/disconnect', { headers: setupHeaders(key) });
    expect(resp.ok()).toBeTruthy();
    await page.goto('/jarvis/');
    await expect(page.getByRole('heading', { name: 'Connect JARVIS to Hermes' })).toBeVisible();
  });

  test('connecting again by address and key, from the form', async ({ page, request, problems }) => {
    test.setTimeout(120_000);
    const out = await runOnHermes('grep -h ^API_SERVER_KEY= ~/.hermes/.env | tail -1 | cut -d= -f2; hostname -I');
    const [key, ips] = out.split('\n');
    const url = `http://${ips.trim().split(/\s+/)[0]}:8642`;
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: /I already have Hermes/ }).click();
    await page.getByLabel('Hermes API server address').fill(url);
    await page.getByLabel(/API key/).fill(key.trim());
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.locator('#test-results')).toContainText('Connection works.', { timeout: 30_000 });
    await page.getByRole('button', { name: 'Connect', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'JARVIS is connected' })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('#s-url')).toHaveText(url);
  });
});
