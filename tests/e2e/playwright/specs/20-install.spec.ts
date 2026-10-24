// "Install Hermes for me", the whole way, the way a person does it: consent, the official installer, JARVIS's setup,
// and the dashboard opening with its guide. Real download; takes a few minutes.
import { test, expect } from './helpers';

test('Install Hermes for me: consent, install, and the dashboard opens with its guide', async ({ page, problems }) => {
  test.setTimeout(15 * 60_000);
  await page.goto('/jarvis/');
  await page.getByRole('button', { name: /Install Hermes for me/ }).click();
  await page.getByLabel(/I agree to download and install Hermes Agent/).check();
  await page.getByRole('button', { name: 'Download and install' }).click();

  await expect(page.getByRole('heading', { name: 'Installing Hermes' })).toBeVisible();
  const log = page.locator('#progress-log');
  await expect(log).toContainText('== Downloading the official Hermes installer', { timeout: 60_000 });
  await expect(log).toContainText(/sha256 [0-9a-f]{64}/, { timeout: 60_000 });
  await expect(log).toContainText("== Installing Hermes Agent (Nous Research's installer)");
  await expect(log).toContainText('== Setting up Hermes for JARVIS', { timeout: 12 * 60_000 });
  await expect(log).toContainText('JARVIS persona written to SOUL.md', { timeout: 5 * 60_000 });
  await expect(log).toContainText('== Hermes is installed and connected', { timeout: 5 * 60_000 });
  await expect(page.locator('#progress-error')).toBeHidden();

  // It moves on to the dashboard by itself; the guide opens because no model is connected yet.
  await page.waitForURL(/\/vesper$/, { timeout: 30_000 });
  await expect(page.getByRole('textbox', { name: 'Message JARVIS' })).toBeVisible({ timeout: 30_000 });
  // No model yet, so the guide opens on its model step.
  const tour = page.locator('vesper-tour');
  await expect(tour.getByRole('heading', { name: 'Connect a model' })).toBeVisible({ timeout: 30_000 });
  await expect(tour.getByText('No model is connected yet.')).toBeVisible();
});

test('after the install the launcher reports a managed, healthy Hermes with the consent recorded', async ({ request }) => {
  const state = await (await request.get('/jarvis/api/state')).json();
  expect(state.mode).toBe('managed');
  expect(state.hermes.installed).toBe(true);
  expect(state.hermes.healthy).toBe(true);
  expect(state.hermes.version).toMatch(/^\d+\.\d+\.\d+/);
  expect(state.connector.installed).toBe(state.connector.bundled);
  expect(state.consent_info.accepted.installer_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(state.updates.auto).toBe(true);
});
