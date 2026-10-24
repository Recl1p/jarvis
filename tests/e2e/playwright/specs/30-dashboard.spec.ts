// The dashboard on the JARVIS that 20-install set up: the setup guide (Hermes step, connecting a model through its
// Local route to the test model server), chat, notes, Agent Control, persona and preferences, attachments, diagnostics.
import { test, expect, dashboardKey, auth, skipGuide, openDashboard, openGuideAt, palette, say, guide, mockRequests, MOCK_LLM } from './helpers';

test.describe.serial('dashboard', () => {
  test('guide step 1 tests the Hermes connection: all green', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await openGuideAt(page, 'hermes');
    const tour = guide(page);
    await expect(tour.getByRole('heading', { name: 'Hermes connection' })).toBeVisible();
    await expect(tour.getByText('installed in this container')).toBeVisible({ timeout: 30_000 });
    await expect(tour.getByText('The connection works.')).toBeVisible({ timeout: 30_000 });
    await expect(tour.locator('.checks li.no')).toHaveCount(0);
    expect(await tour.locator('.checks li.ok').count()).toBeGreaterThanOrEqual(7);
    // Testing again works too.
    await tour.getByRole('button', { name: 'Test connection' }).click();
    await expect(tour.getByText('The connection works.')).toBeVisible({ timeout: 30_000 });
    await expect(tour.getByRole('link', { name: /Connection settings/ })).toHaveAttribute('href', '/jarvis/');
  });

  test('guide step 2, Local: finds the model server and connects it', async ({ page, request, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await openGuideAt(page, 'connect');
    const tour = guide(page);
    await expect(tour.getByRole('heading', { name: 'Connect a model' })).toBeVisible();
    await tour.getByRole('tab', { name: /Local/ }).click();
    // The test model server answers as "ollama", one of the places the scan looks.
    await expect(tour.getByText('Found a model server')).toBeVisible({ timeout: 60_000 });
    await expect(tour.locator('#local-model')).toHaveValue('mock-jarvis');
    await tour.getByRole('button', { name: 'Connect', exact: true }).click();
    await expect(tour.getByRole('tabpanel').getByText('Connected: mock-jarvis')).toBeVisible({ timeout: 60_000 });
    const key = await dashboardKey(request);
    const status = await (await request.get('/vesper/setup/status', { headers: auth(key) })).json();
    expect(status.model).toBe('mock-jarvis');
  });

  test('chat: a message gets the model\'s streamed reply, with the persona briefed', async ({ page, request, problems }) => {
    await request.post(`${MOCK_LLM}/mock/reset`);
    await skipGuide(page);
    await openDashboard(page);
    await say(page, 'Hello from the regression suite');
    const dialogue = page.getByRole('log', { name: 'Dialogue' });
    await expect(dialogue).toContainText('Hello from the regression suite');
    await expect(dialogue).toContainText('Mock reply: Hello from the regression suite', { timeout: 90_000 });
    // The dashboard briefs the model with the persona's instructions, and Hermes with JARVIS's SOUL.md.
    const seen = await mockRequests(request);
    const turn = seen.find(r => r.user.includes('Hello from the regression suite'));
    expect(turn, 'the chat turn reached the model').toBeTruthy();
    expect(turn!.system).toMatch(/JARVIS/);
  });

  test('chats: the conversation is in the chat list after a reload', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await page.getByRole('button', { name: 'Chats: start a new one or switch' }).click();
    await expect(page.getByText(/Mock conversation|Hello from the regression suite/).first()).toBeVisible({ timeout: 30_000 });
  });

  test('notes: "take a note" is saved by the dashboard itself, shown, and can be deleted', async ({ page, request, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await say(page, 'take a note called Regression groceries: eggs and milk');
    const key = await dashboardKey(request);
    await expect.poll(async () => {
      const notes = (await (await request.get('/vesper/notes', { headers: auth(key) })).json()).notes;
      return notes.find((n: { title: string }) => n.title === 'Regression groceries')?.body || '';
    }, { timeout: 30_000 }).toMatch(/eggs and milk/i);
    await palette(page, 'Open notes');
    await expect(page.getByText('Regression groceries').first()).toBeVisible();
    // Clean up through the API so the next run starts the same.
    const notes = (await (await request.get('/vesper/notes', { headers: auth(key) })).json()).notes;
    for (const n of notes.filter((n: { title: string }) => n.title === 'Regression groceries')) {
      expect((await request.delete(`/vesper/notes/${n.id}`, { headers: auth(key) })).ok()).toBeTruthy();
    }
  });

  test('Agent Control opens without errors, lists skills, and creates and deletes an agent', async ({ page, request, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await palette(page, 'Open agent control');
    const control = page.locator('vesper-agent-control');
    const dialog = control.getByRole('dialog', { name: 'JARVIS / AGENT CONTROL' });
    await expect(dialog).toBeVisible();
    await expect(control.locator('#new')).toBeEnabled({ timeout: 30_000 });
    await expect(control.locator('#notice')).toBeHidden();
    await control.locator('#new').click();
    await control.locator('input[name=name]').fill('Regression agent');
    await control.locator('textarea[name=prompt]').fill('Say hello. This agent exists only for an automated test.');
    await control.locator('input[name=schedule]').fill('every 6h');
    await control.getByRole('button', { name: 'Create agent' }).click();
    await expect(control.getByText('Regression agent').first()).toBeVisible({ timeout: 30_000 });
    const key = await dashboardKey(request);
    const jobs = (await (await request.get('/api/jobs?include_disabled=true', { headers: auth(key) })).json()).jobs;
    const job = jobs.find((j: { name: string }) => j.name === 'Regression agent');
    expect(job, 'agent saved as a Hermes cron job').toBeTruthy();
    expect((await request.delete(`/api/jobs/${job.id}`, { headers: auth(key) })).ok()).toBeTruthy();
    await control.getByRole('button', { name: 'Close agent control' }).click();
  });

  test('persona: switching re-themes the page and survives a reload', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    const personas = page.getByRole('radiogroup', { name: 'Persona' });
    await personas.getByRole('radio', { name: /cortana/i }).click();
    await expect(page.locator('html')).toHaveAttribute('data-persona', 'cortana');
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-persona', 'cortana', { timeout: 30_000 });
    await page.getByRole('radiogroup', { name: 'Persona' }).getByRole('radio', { name: /jarvis/i }).click();
    await expect(page.locator('html')).toHaveAttribute('data-persona', 'jarvis');
  });

  test('command palette opens with Ctrl+K and lists the dashboard\'s commands', async ({ page, problems }) => {
    await skipGuide(page);
    await openDashboard(page);
    await page.keyboard.press('Control+k');
    const dialog = page.getByRole('dialog', { name: 'Commands' });
    await expect(dialog).toBeVisible();
    for (const label of ['Open notes', 'Open agent control', 'Run diagnostics (hermes doctor)', 'Start a new chat']) {
      await expect(dialog.getByText(label)).toBeVisible();
    }
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  });
});
