// Smoke test for a JARVIS that is already set up and in use (the NAS test instance, or the live one after a
// cutover). Read-only, apart from one short chat when LIVE_ALLOW_CHAT=1 (it uses that instance's real model).
import { test, expect, dashboardKey, auth, skipGuide, openDashboard, openGuideAt, say, guide } from './helpers';

test.describe('smoke: an instance in use', () => {
  test('launcher, setup screen and connection test are healthy', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    await expect(page.getByRole('heading', { name: 'JARVIS is connected' })).toBeVisible();
    await expect(page.locator('#s-health')).toHaveText('Running');
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.locator('#retest-results')).toContainText('Connection works.', { timeout: 30_000 });
    await expect(page.locator('#retest-results li.no')).toHaveCount(0);
  });

  test('the dashboard loads clean and its guide\'s Hermes step is green', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await openGuideAt(page, 'hermes');
    const tour = guide(page);
    await expect(tour.getByRole('heading', { name: 'Hermes connection' })).toBeVisible({ timeout: 30_000 });
    await expect(tour.getByText('The connection works.')).toBeVisible({ timeout: 30_000 });
  });

  test('dashboard data routes answer', async ({ request }) => {
    const key = await dashboardKey(request);
    for (const path of ['/vesper/status', '/vesper/setup/status', '/vesper/voice/status', '/vesper/notes', '/vesper/skills',
      '/vesper/prefs', '/api/jobs', '/api/sessions']) {
      expect((await request.get(path, { headers: auth(key) })).status(), path).toBe(200);
    }
  });

  test('a real chat round trip', async ({ page, problems }) => {
    test.skip(process.env.LIVE_ALLOW_CHAT !== '1', 'LIVE_ALLOW_CHAT=1 to send one message to this instance\'s model');
    test.setTimeout(180_000);
    await skipGuide(page);
    await openDashboard(page);
    await say(page, 'This is an automated JARVIS smoke test. Reply with the single word PONG.');
    // The question itself says PONG once; the model's answer is the second one.
    const dialogue = page.getByRole('log', { name: 'Dialogue' });
    await expect.poll(async () => ((await dialogue.textContent()) || '').match(/PONG/gi)?.length || 0,
      { timeout: 150_000 }).toBeGreaterThan(1);
  });
});
