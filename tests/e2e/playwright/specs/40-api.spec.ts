// API regression for everything the dashboard calls, on the managed JARVIS from 20-install: each route answers, the
// ones that change or reveal anything need the key, the page's security headers are right, big uploads get past
// Hermes's 10 MB limit, files are guarded, and the voices speak.
import { test, expect, dashboardKey, auth, samplePdf, sampleWav } from './helpers';
import { request as apiRequest } from '@playwright/test';

const PUBLIC_GET = [
  '/vesper', '/vesper/support.js', '/vesper/core.js', '/vesper/tour.js', '/vesper/personas.js', '/vesper/sessions.js',
  '/vesper/agents.js', '/vesper/storage.js', '/vesper/cloudflare-speedtest.js', '/vesper/status', '/vesper/connector',
  '/vesper/manifest.webmanifest', '/vesper/sw.js', '/vesper/icons/jarvis-180.png', '/health',
];
// Routes that must refuse a request without the key (GET unless noted).
const NEED_KEY: [string, string][] = [
  ['GET', '/vesper/setup/status'], ['GET', '/vesper/setup/providers'], ['GET', '/vesper/setup/claude/status'],
  ['GET', '/vesper/setup/telegram'], ['GET', '/vesper/setup/voice'], ['GET', '/vesper/prefs'], ['GET', '/vesper/notes'],
  ['GET', '/vesper/music/tracks'], ['GET', '/vesper/location'], ['GET', '/vesper/skills'],
  ['GET', '/vesper/connector/check'], ['GET', '/api/jobs'], ['GET', '/api/sessions'], ['GET', '/v1/models'],
  ['POST', '/vesper/tts'], ['POST', '/vesper/stt'], ['POST', '/vesper/doctor'], ['POST', '/vesper/notes'],
  ['POST', '/vesper/agents'], ['POST', '/vesper/chat/extract-attachment'], ['POST', '/vesper/music/tracks'],
];

test.describe('dashboard API', () => {
  let key = '';
  test.beforeAll(async ({ request }) => { key = await dashboardKey(request); });

  test('public assets and status answer', async ({ request }) => {
    for (const path of PUBLIC_GET) {
      const resp = await request.get(path);
      expect(resp.status(), path).toBe(200);
    }
  });

  test('every route that changes or reveals something refuses a request without the key', async ({ request }) => {
    for (const [method, path] of NEED_KEY) {
      const resp = await request.fetch(path, { method, data: method === 'GET' ? undefined : '{}',
        headers: method === 'GET' ? {} : { 'Content-Type': 'application/json' } });
      expect(resp.status(), `${method} ${path}`).toBe(401);
    }
  });

  test('the dashboard page carries its CSP, permissions and the key', async ({ request }) => {
    const resp = await request.get('/vesper');
    const csp = resp.headers()['content-security-policy'];
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(resp.headers()['permissions-policy']).toContain('microphone=(self)');
    expect(resp.headers()['x-content-type-options']).toBe('nosniff');
    const sw = await request.get('/vesper/sw.js');
    expect(sw.headers()['content-security-policy']).toContain("connect-src 'self'");
  });

  test('a same-origin module import passes the CORS check; another origin does not', async ({ request, baseURL }) => {
    const same = await request.get('/vesper/cloudflare-speedtest.js', { headers: { Origin: baseURL! } });
    expect(same.status()).toBe(200);
    const other = await request.get('/v1/models', { headers: { Origin: 'http://evil.example', ...auth(key) } });
    expect(other.status()).toBe(403);
  });

  test('keyed reads answer', async ({ request }) => {
    for (const path of ['/vesper/setup/status', '/vesper/setup/providers', '/vesper/setup/voice', '/vesper/voice/status',
      '/vesper/prefs', '/vesper/notes', '/vesper/music/tracks', '/vesper/location', '/api/jobs', '/v1/toolsets',
      '/api/model/options', '/vesper/connector/check']) {
      const resp = await request.get(path, { headers: auth(key) });
      expect(resp.status(), path).toBe(200);
    }
  });

  test('skills are listed (the connector works around Hermes\'s broken /v1/skills)', async ({ request }) => {
    const resp = await request.get('/vesper/skills', { headers: auth(key) });
    expect(resp.status()).toBe(200);
    const names = (await resp.json()).data.map((s: { name: string }) => s.name);
    expect(names).toContain('jarvis-notes');
    expect(names.length).toBeGreaterThan(10);
  });

  test('connector check reports in-process plugins and the image\'s features', async ({ request }) => {
    const check = await (await request.get('/vesper/connector/check', { headers: auth(key) })).json();
    expect(check.ok).toBe(true);
    expect(check.isolation).toBe('in_process');
    expect(check.features.browser_desktop).toBe(true);
    expect(check.hermes_version).toMatch(/^\d+\.\d+\.\d+/);
  });

  test('notes: create, read, edit, delete', async ({ request }) => {
    const created = await request.post('/vesper/notes', { headers: auth(key), data: { title: 'API note', body: '- one' } });
    expect(created.status()).toBe(201);
    const note = (await created.json()).note;
    const patched = await request.patch(`/vesper/notes/${note.id}`, { headers: auth(key), data: { body: '- one\n- two' } });
    expect(patched.ok()).toBeTruthy();
    const got = (await (await request.get(`/vesper/notes/${note.id}`, { headers: auth(key) })).json()).note;
    expect(got.body).toBe('- one\n- two');
    expect((await request.delete(`/vesper/notes/${note.id}`, { headers: auth(key) })).ok()).toBeTruthy();
    expect((await request.get(`/vesper/notes/${note.id}`, { headers: auth(key) })).status()).toBe(404);
  });

  test('music: upload, list, stream, rename, delete', async ({ request }) => {
    const up = await request.post('/vesper/music/tracks', {
      headers: { ...auth(key), 'X-Music-Filename': encodeURIComponent('Regression Tone.wav'), 'Content-Type': 'audio/wav' },
      data: sampleWav(0.5) });
    expect(up.status()).toBe(201);
    const track = (await up.json()).track;
    const list = (await (await request.get('/vesper/music/tracks', { headers: auth(key) })).json()).tracks;
    expect(list.map((t: { id: string }) => t.id)).toContain(track.id);
    const stream = await request.get(`/vesper/music/tracks/${track.id}`);
    expect(stream.status()).toBe(200);
    expect((await stream.body()).subarray(0, 4).toString()).toBe('RIFF');
    expect((await request.patch(`/vesper/music/tracks/${track.id}`, { headers: auth(key), data: { title: 'Renamed tone' } })).ok()).toBeTruthy();
    expect((await request.delete(`/vesper/music/tracks/${track.id}`, { headers: auth(key) })).ok()).toBeTruthy();
  });

  test('uploads bigger than Hermes\'s 10 MB limit get through (music up to 100 MB)', async ({ request }) => {
    test.setTimeout(180_000);
    const big = Buffer.concat([sampleWav(0.1), Buffer.alloc(15 * 1024 * 1024)]);
    const up = await request.post('/vesper/music/tracks', {
      headers: { ...auth(key), 'X-Music-Filename': encodeURIComponent('Big tone.wav'), 'Content-Type': 'audio/wav' }, data: big });
    expect(up.status(), await up.text()).toBe(201);
    const track = (await up.json()).track;
    expect((await request.delete(`/vesper/music/tracks/${track.id}`, { headers: auth(key) })).ok()).toBeTruthy();
  });

  test('a PDF attachment comes back as Markdown and is saved for the agent', async ({ request }) => {
    const resp = await request.post('/vesper/chat/extract-attachment', {
      headers: { ...auth(key), 'X-Attachment-Filename': 'Regression.pdf' }, data: samplePdf('Regression PDF text layer') });
    expect(resp.status(), await resp.text()).toBe(200);
    const body = await resp.json();
    expect(body.format).toBe('markdown');
    expect(body.text).toContain('# Regression.pdf');
    expect(body.text).toContain('Regression PDF text layer');
    expect(body.markdown_path).toMatch(/\/pdf-markdown\/[0-9a-f]{32}\/Regression\.md$/);
  });

  test('location and weather', async ({ request }) => {
    const put = await request.put('/vesper/location', { headers: auth(key), data: { name: 'Boston', lat: 42.36, lon: -71.06 } });
    expect(put.ok()).toBeTruthy();
    const weather = await request.get('/vesper/weather');
    // Open-Meteo is on the internet: a 502 only when the test network has none.
    expect([200, 502]).toContain(weather.status());
    if (weather.status() === 200) expect((await weather.json()).location).toBe('Boston');
    expect((await request.delete('/vesper/location', { headers: auth(key) })).ok()).toBeTruthy();
    expect((await request.get('/vesper/weather')).status()).toBe(404);
  });

  test('files: JARVIS\'s downloads work, Hermes\'s credential files are refused', async ({ request }) => {
    expect((await request.get('/vesper/files?path=/opt/data/.env')).status()).toBe(403);
    expect((await request.get('/vesper/files?path=relative/path')).status()).toBe(400);
    expect((await request.get('/vesper/files?path=/opt/data/SOUL.md')).status()).toBe(200);
  });

  test('radio only plays its own stations', async ({ request }) => {
    expect((await request.get('/vesper/radio?station=http://evil.example/stream')).status()).toBe(400);
  });

  test('voice: Piper speaks (installed through Hermes\'s package manager on first use)', async ({ request }) => {
    test.setTimeout(10 * 60_000);
    const resp = await request.post('/vesper/tts', { headers: auth(key), timeout: 9 * 60_000,
      data: { text: 'Regression test.', persona: 'jarvis', engine: 'piper' } });
    expect(resp.status(), await resp.text()).toBe(200);
    expect(resp.headers()['content-type']).toBe('audio/wav');
    expect(resp.headers()['x-vesper-voice']).toBe('local');
    expect((await resp.body()).length).toBeGreaterThan(10_000);
  });

  test('voice requests are validated', async ({ request }) => {
    expect((await request.post('/vesper/tts', { headers: auth(key), data: { text: '' } })).status()).toBe(400);
    expect((await request.post('/vesper/tts', { headers: auth(key), data: { text: 'x', persona: 'nobody' } })).status()).toBe(400);
    expect((await request.post('/vesper/tts', { headers: auth(key), data: { text: 'x', voice_id: '../../etc' } })).status()).toBe(400);
  });

  test('diagnostics run hermes doctor and summarise it', async ({ request }) => {
    test.setTimeout(240_000);
    const resp = await request.post('/vesper/doctor', { headers: auth(key), data: {}, timeout: 200_000 });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.output).toContain('Hermes Doctor');
    expect(body.summary.errors).toBeGreaterThanOrEqual(0);
    expect(body.output).not.toMatch(/sk-[A-Za-z0-9]{20,}/);
  });

  test('the browser desktop is served behind the login at /desktop/', async ({ request, baseURL }) => {
    const resp = await request.get('/desktop/vnc.html');
    expect(resp.status()).toBe(200);
    expect(await resp.text()).toContain('noVNC');
    expect(resp.headers()['x-frame-options']).toBe('SAMEORIGIN');
    const home = await request.get('/desktop/', { maxRedirects: 0 });
    expect(home.headers()['location']).toBe('/desktop/vnc.html?autoconnect=true&resize=remote&path=desktop/websockify');
    const anon = await apiRequest.newContext({ baseURL });
    expect((await anon.get('/desktop/vnc.html')).status()).toBe(401);
    await anon.dispose();
  });

  test('the voice pack: JARVIS, Ultron and Cortana speak their own Pocket TTS voices', async ({ request }) => {
    test.skip(process.env.E2E_VOICE_PACK !== '1', 'E2E_VOICE_PACK=1 when the instance was started with JARVIS_VOICE_KEY');
    test.setTimeout(5 * 60_000);
    const setup = await (await request.get('/vesper/setup/voice', { headers: auth(key) })).json();
    expect(setup.pocket.installed).toBe(true);
    expect(setup.pocket.voice_pack).toBe(true);
    for (const persona of ['jarvis', 'ultron', 'cortana']) {
      const resp = await request.post('/vesper/tts', { headers: auth(key), timeout: 4 * 60_000,
        data: { text: 'Voice pack check.', persona, engine: 'pocket' } });
      expect(resp.status(), await resp.text()).toBe(200);
      expect(resp.headers()['x-vesper-voice'], persona).toBe('pocket');
      expect((await resp.body()).length).toBeGreaterThan(10_000);
    }
    const status = await (await request.get('/vesper/voice/status?engine=pocket', { headers: auth(key) })).json();
    expect(status.voices).toEqual({ jarvis: 'pack', ultron: 'pack', cortana: 'pack' });
  });
});
