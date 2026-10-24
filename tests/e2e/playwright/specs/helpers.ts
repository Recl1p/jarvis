import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { test as base } from '@playwright/test';

export const MOCK_LLM = process.env.MOCK_LLM || 'http://ollama:11434';
export const E2E_PASSWORD = process.env.E2E_PASSWORD || 'jarvis-e2e-password';
export const SHARED_DIR = process.env.SHARED_DIR || '/shared';

/** Browser console problems collected for a page: uncaught errors, console.error, and failed requests. */
export type Problems = { errors: string[]; failed: string[] };

// Resource failures that are expected in these tests, not defects: no weather location saved yet (404 by design),
// Google Fonts/unpkg when the test network has no internet, and requests a test cancels on purpose.
const EXPECTED_FAILURES = [/\/vesper\/weather.*404/, /fonts\.(googleapis|gstatic)\.com/, /speed\.cloudflare\.com/];
const EXPECTED_CONSOLE = [/Failed to load resource: the server responded with a status of 404/];

export function watch(page: Page): Problems {
  const problems: Problems = { errors: [], failed: [] };
  page.on('pageerror', err => problems.errors.push(`pageerror: ${err.message}`));
  page.on('console', msg => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (!EXPECTED_CONSOLE.some(re => re.test(text))) problems.errors.push(`console.error: ${text}`);
  });
  page.on('response', resp => {
    const line = `${resp.request().method()} ${resp.url()} ${resp.status()}`;
    if (resp.status() >= 500 && !EXPECTED_FAILURES.some(re => re.test(line))) problems.failed.push(line);
  });
  page.on('requestfailed', req => {
    const line = `${req.method()} ${req.url()} ${req.failure()?.errorText}`;
    if (!/ERR_ABORTED/.test(line) && !EXPECTED_FAILURES.some(re => re.test(line))) problems.failed.push(line);
  });
  return problems;
}

/** Credentials that are never sent (Basic auth only answers a challenge, which JARVIS never sends). */
export const ANONYMOUS = { username: 'nobody', password: 'nothing', send: 'unauthorized' as const };

/** Make sure a fresh JARVIS has its password (05-login creates it through the page on the first instance). */
export async function ensurePassword(request: APIRequestContext) {
  const status = await (await request.get('/jarvis/api/auth')).json();
  if (status.has_password) return;
  const resp = await request.post('/jarvis/api/password', { headers: { 'X-JARVIS': '1' }, data: { password: E2E_PASSWORD } });
  expect(resp.ok(), await resp.text()).toBeTruthy();
}

/** Log *api* in to JARVIS (it keeps the session cookie, like a browser). A fresh test instance gets the test password
 *  first; an instance in use (--live) never does, and a JARVIS from before the login needs nothing. */
export async function logIn(api: APIRequestContext) {
  const auth = await api.get('/jarvis/api/auth');
  const status = auth.ok() ? await auth.json() : { enabled: false };
  const live = !!process.env.JARVIS_LIVE;
  if (!status.enabled || (!status.has_password && live)) return;
  if (!status.has_password) await ensurePassword(api);
  const resp = await api.post('/jarvis/api/login', { headers: { 'X-JARVIS': '1' }, data: { password: E2E_PASSWORD } });
  expect(resp.ok(), `logging in: ${await resp.text()}`).toBeTruthy();
}

/** A test whose page must end with no console errors and no failed (5xx / network) requests. Every browser context and
 *  request context is logged in first (context.request shares the browser context's cookies). */
export const test = base.extend<{ problems: Problems }>({
  context: async ({ context }, use) => {
    await logIn(context.request);
    await use(context);
  },
  request: async ({ request }, use) => {
    await logIn(request);
    await use(request);
  },
  problems: async ({ page }, use) => {
    const problems = watch(page);
    await use(problems);
    expect(problems.errors, 'browser console errors').toEqual([]);
    expect(problems.failed, 'failed requests').toEqual([]);
  },
});
export { expect };

/** The API key the dashboard page carries (the same one the launcher stamps on /jarvis/). */
export async function dashboardKey(request: APIRequestContext): Promise<string> {
  const html = await (await request.get('/vesper')).text();
  const key = /data-vesper-key="([^"]*)"/.exec(html)?.[1] || '';
  expect(key.length, 'dashboard API key').toBeGreaterThanOrEqual(32);
  return key;
}

export function auth(key: string) {
  return { Authorization: `Bearer ${key}` };
}

/** Headers the launcher's setup API wants for a change. */
export function setupHeaders(key = '') {
  return { 'X-JARVIS': '1', 'Content-Type': 'application/json', ...(key ? auth(key) : {}) };
}

/** Skip the setup guide's automatic first-visit opening (tests that use the dashboard itself). */
export async function skipGuide(page: Page) {
  await page.addInitScript(() => { try { localStorage.setItem('vesperTourDone', '1'); } catch { /* ignore */ } });
}

/** Open the dashboard and wait until it has rendered its composer. */
export async function openDashboard(page: Page) {
  await page.goto('/vesper');
  await expect(page.getByRole('textbox', { name: 'Message JARVIS' })).toBeVisible({ timeout: 30_000 });
}

/** Run a command from the dashboard's command palette (Ctrl+K), by its label. */
export async function palette(page: Page, label: string) {
  await page.keyboard.press('Control+k');
  const search = page.getByRole('textbox', { name: 'Search commands' });
  await expect(search).toBeVisible();
  await search.fill(label);
  await page.getByRole('dialog', { name: 'Commands' }).getByText(label, { exact: false }).first().click();
}

/** Send a chat message from the composer. */
export async function say(page: Page, text: string) {
  const box = page.getByRole('textbox', { name: 'Message JARVIS' });
  await box.fill(text);
  await box.press('Enter');
}

/** The guide (a custom element with an open shadow root; Playwright's selectors reach into it). */
export function guide(page: Page) {
  return page.locator('vesper-tour');
}

/** Requests the mock model server saw (system prompt and last user message of each). */
export async function mockRequests(request: APIRequestContext): Promise<{ system: string; user: string }[]> {
  const resp = await request.get(`${MOCK_LLM}/mock/requests`);
  expect(resp.ok()).toBeTruthy();
  return (await resp.json()).requests;
}

/** A tiny one-page PDF with a text layer that poppler (pdftotext) and anydoc can read. */
export function samplePdf(text: string): Buffer {
  const stream = `BT /F1 18 Tf 72 720 Td (${text}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, i) => { offsets.push(pdf.length); pdf += `${i + 1} 0 obj\n${body}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const off of offsets) pdf += `${String(off).padStart(10, '0')} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, 'latin1');
}

/** A short, valid WAV file (silence). */
export function sampleWav(seconds = 0.2): Buffer {
  const rate = 8000, samples = Math.floor(rate * seconds), data = samples * 2;
  const buf = Buffer.alloc(44 + data);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + data, 4); buf.write('WAVE', 8); buf.write('fmt ', 12);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36);
  buf.writeUInt32LE(data, 40);
  return buf;
}

/** No horizontal page scroll at the current viewport. */
export async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, 'horizontal overflow in px').toBeLessThanOrEqual(1);
}

/** Open the setup guide at one of its steps ('hermes', 'connect', 'voice', ...) the way the dashboard does
 *  (window event `vesper:tour`, tour.js). Call after skipGuide() + openDashboard(). */
export async function openGuideAt(page: Page, step: string) {
  await page.evaluate(s => window.dispatchEvent(new CustomEvent('vesper:tour', { detail: { step: s, reason: 'manual' } })), step);
  await expect(page.locator('vesper-tour').getByRole('heading').first()).toBeVisible();
}
