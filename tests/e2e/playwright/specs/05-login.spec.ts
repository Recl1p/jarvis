// The login in front of a fresh JARVIS: the first visit creates the password; after that every page, the setup API and
// the browser desktop need it (a session cookie, the password as Basic auth, or Hermes's API key). The other specs'
// contexts log in first (helpers.ts, logIn); this one runs first and creates the password through the page.
import { test, expect, E2E_PASSWORD, ANONYMOUS, watch, expectNoHorizontalScroll } from './helpers';
import { request as apiRequest } from '@playwright/test';

/** A browser context that isn't logged in and never sends the test password (the other tests' contexts are). */
async function anonymous(browser, baseURL) {
  return browser.newContext({ baseURL, httpCredentials: ANONYMOUS });
}

test.describe.serial('login', () => {
  test('a fresh JARVIS asks for a password before anything else', async ({ browser, baseURL }) => {
    const context = await anonymous(browser, baseURL);
    const page = await context.newPage();
    const problems = watch(page);
    await page.goto('/');
    await expect(page).toHaveURL(/\/jarvis\/login\?next=/);
    await expect(page.getByRole('heading', { name: 'Welcome to JARVIS' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Create a password for JARVIS' })).toBeVisible();
    await page.getByLabel('New password').fill(E2E_PASSWORD);
    await page.getByLabel('Type it again').fill(E2E_PASSWORD + 'x');
    await page.getByRole('button', { name: 'Create password' }).click();
    await expect(page.getByText('The two passwords are different.')).toBeVisible();
    await page.getByLabel('Type it again').fill(E2E_PASSWORD);
    await page.getByRole('button', { name: 'Create password' }).click();
    await expect(page).toHaveURL(/\/jarvis\/$/);
    await expect(page.getByRole('heading', { name: 'Connect JARVIS to Hermes' })).toBeVisible();
    expect(problems.errors).toEqual([]);
    expect(problems.failed).toEqual([]);
    await context.close();
  });

  test('without the password nothing but the login answers', async ({ baseURL }) => {
    const anon = await apiRequest.newContext({ baseURL, httpCredentials: ANONYMOUS });
    for (const path of ['/jarvis/api/state', '/v1/models', '/desktop/vnc.html', '/vesper/status']) {
      const resp = await anon.get(path);
      expect(resp.status(), path).toBe(401);
    }
    const page = await anon.get('/vesper', { headers: { Accept: 'text/html' }, maxRedirects: 0 });
    expect(page.status()).toBe(302);
    expect(page.headers()['location']).toMatch(/^\/jarvis\/login\?next=/);
    const takeover = await anon.post('/jarvis/api/password', { headers: { 'X-JARVIS': '1' }, data: { password: 'my own password' } });
    expect(takeover.status()).toBe(401);
    for (const path of ['/jarvis/health', '/jarvis/login', '/jarvis/connect.sh']) {
      expect((await anon.get(path)).status(), path).toBe(200);
    }
    await anon.dispose();
  });

  test('logging in with the password, after a wrong one, and out again', async ({ browser, baseURL }) => {
    const context = await anonymous(browser, baseURL);
    const page = await context.newPage();
    await page.goto('/jarvis/');
    await expect(page.getByRole('heading', { name: 'Log in' })).toBeVisible();
    await page.getByLabel('Password', { exact: true }).fill('not the password');
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page.getByText("That password isn't right.")).toBeVisible();
    await page.getByLabel('Password', { exact: true }).fill(E2E_PASSWORD);
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page.getByRole('heading', { name: 'Connect JARVIS to Hermes' })).toBeVisible();
    await page.getByRole('link', { name: 'Log out' }).click();
    await expect(page.getByRole('heading', { name: 'Log in' })).toBeVisible();
    await page.goto('/jarvis/');
    await expect(page).toHaveURL(/\/jarvis\/login/);
    await context.close();
  });

  test('phone width: the login fits', async ({ browser, baseURL }) => {
    const context = await anonymous(browser, baseURL);
    const page = await context.newPage();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/jarvis/login');
    await expect(page.getByRole('button', { name: 'Log in' })).toBeVisible();
    await expectNoHorizontalScroll(page);
    await context.close();
  });
});
