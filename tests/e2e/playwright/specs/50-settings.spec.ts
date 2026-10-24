// The setup screen once JARVIS has installed Hermes: status, Test connection, update controls, personality, and the
// key it now requires for changes.
import { test, expect, setupHeaders } from './helpers';

test.describe.serial('settings after install', () => {
  test('shows the managed Hermes, running, and an all-green connection test', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    await expect(page.getByRole('heading', { name: 'JARVIS is connected' })).toBeVisible();
    await expect(page.locator('#s-mode')).toHaveText('Installed by JARVIS');
    await expect(page.locator('#s-health')).toHaveText('Running');
    await expect(page.locator('#s-version')).toHaveText(/^\d+\.\d+\.\d+/);
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.locator('#retest-results')).toContainText('Connection works.', { timeout: 30_000 });
    await expect(page.locator('#retest-results li.no')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Open JARVIS' })).toHaveAttribute('href', '/vesper');
  });

  test('update check reports the channel\'s answer', async ({ page, problems }) => {
    test.setTimeout(240_000);
    await page.goto('/jarvis/');
    await page.getByRole('button', { name: 'Check now' }).click();
    await expect(page.locator('#u-status')).toHaveText(/is up to date|An update is available/, { timeout: 200_000 });
  });

  test('auto-update setting and hour are saved', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    const auto = page.getByLabel(/Install new stable Hermes releases automatically/);
    await expect(auto).toBeChecked();
    await page.locator('#u-hour').selectOption('5');
    await auto.uncheck();
    await page.waitForTimeout(800);
    await page.reload();
    await expect(page.getByLabel(/Install new stable Hermes releases automatically/)).not.toBeChecked();
    await expect(page.locator('#u-hour')).toHaveValue('5');
    await page.getByLabel(/Install new stable Hermes releases automatically/).check();
    await page.locator('#u-hour').selectOption('4');
    await page.waitForTimeout(800);
    await page.reload();
    await expect(page.getByLabel(/Install new stable Hermes releases automatically/)).toBeChecked();
  });

  test('the personality card says it is applied, and applying again works', async ({ page, problems }) => {
    await page.goto('/jarvis/');
    await expect(page.locator('#persona-status')).toHaveText(/Applied/);
    await page.getByRole('button', { name: 'Apply the JARVIS personality' }).click();
    await expect(page.locator('#persona-status')).toHaveText(/Done/);
  });

  test('changes now need the key', async ({ request }) => {
    for (const path of ['/jarvis/api/disconnect', '/jarvis/api/update/run', '/jarvis/api/persona', '/jarvis/api/install']) {
      const resp = await request.post(path, { headers: setupHeaders(), data: { accept: true } });
      expect(resp.status(), path).toBe(401);
    }
  });

  test('phone width: the settings view fits', async ({ page, problems }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/jarvis/');
    await expect(page.getByRole('heading', { name: 'JARVIS is connected' })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
