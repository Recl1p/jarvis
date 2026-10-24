/* JARVIS setup screen. Talks to the launcher's /jarvis/api/* (jarvis_launcher/server.py): every change sends
   X-JARVIS: 1 and, once a Hermes is connected, the key the server stamped on <body data-jarvis-key>. */
(() => {
  const $ = id => document.getElementById(id);
  const key = () => document.body.dataset.jarvisKey || '';
  let current = null;           // last /jarvis/api/state
  let view = '';
  let logOffset = 0, logTimer = null, pairTimer = null, updateLogOffset = 0, updateTimer = null;

  async function api(path, body, method) {
    const headers = { 'X-JARVIS': '1' };
    if (key()) headers.Authorization = 'Bearer ' + key();
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    try {
      const res = await fetch('/jarvis/api/' + path, {
        method: method || (body === undefined ? 'GET' : 'POST'), headers, cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      let data = null;
      try { data = await res.json(); } catch { /* not JSON */ }
      if (res.status === 401 && /^(login_required|password_not_set)$/.test(data?.error?.code || '')) {
        // The login ran out (or the password changed elsewhere): log in again, then come back here.
        location.replace('/jarvis/login?next=' + encodeURIComponent(location.pathname));
      }
      return { ok: res.ok, status: res.status, data: data || {} };
    } catch {
      return { ok: false, status: 0, data: { error: { message: 'JARVIS is not answering. Is the container running?' } } };
    }
  }
  const errorText = r => r.data?.error?.message || `Request failed (HTTP ${r.status})`;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  function show(name) {
    view = name;
    for (const el of document.querySelectorAll('.view')) el.hidden = el.id !== 'view-' + name;
    $('title').textContent = {
      welcome: 'Connect JARVIS to Hermes', install: 'Install Hermes Agent', progress: 'Installing Hermes',
      existing: 'Connect your Hermes', connected: 'JARVIS is connected'
    }[name] || 'JARVIS';
    if (name === 'existing') loadPrompt();
    if (name === 'connected') renderConnected();
  }
  document.addEventListener('click', e => {
    const go = e.target.closest('[data-go]');
    if (go) { e.preventDefault(); show(go.dataset.go); }
  });

  // --- state ---------------------------------------------------------------------------------------------------
  async function refresh() {
    const r = await api('state');
    if (!r.ok) return;
    current = r.data;
    $('foot-version').textContent = `JARVIS ${current.jarvis_version}`;
    $('previous-data').hidden = !current.previous_data;
    const c = current.consent_info;
    $('c-installer').textContent = c.installer_url; $('c-installer').href = c.installer_url;
    $('c-repo').href = c.repo; $('c-license').href = c.license;
    $('c-channel').textContent = c.channel; $('c-dir').textContent = c.install_dir; $('c-home').textContent = c.hermes_home;
    if (current.install.busy || view === 'progress') {
      if (view !== 'progress') { show('progress'); followInstall(); }
      return;
    }
    if (current.configured) {
      // A key exists now: reload once so the server stamps it on the page.
      if (!key()) { location.reload(); return; }
      if (view !== 'connected') show('connected'); else renderConnected();
      return;
    }
    if (!view || view === 'connected') show('welcome');
  }

  // --- install ---------------------------------------------------------------------------------------------------
  $('opt-accept').addEventListener('change', e => { $('btn-install').disabled = !e.target.checked; });
  $('btn-install').addEventListener('click', async () => {
    $('btn-install').disabled = true;
    const r = await api('install', { accept: $('opt-accept').checked, persona: $('opt-persona').checked,
                                     auto_update: $('opt-auto').checked });
    if (!r.ok) { alert(errorText(r)); $('btn-install').disabled = false; return; }
    logOffset = 0; $('progress-log').textContent = '';
    show('progress'); followInstall();
  });
  $('btn-progress-retry').addEventListener('click', () => show('install'));

  function appendLog(target, lines) {
    const box = $(target);
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    for (const line of lines) {
      const span = document.createElement('span');
      if (line.startsWith('== ')) span.className = 'step';
      if (line.startsWith('!! ')) span.className = 'err';
      span.textContent = line + '\n';
      box.appendChild(span);
    }
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function followInstall() {
    clearInterval(logTimer);
    const tick = async () => {
      const r = await api('log?offset=' + logOffset);
      if (!r.ok) return;
      appendLog('progress-log', r.data.lines || []);
      logOffset = r.data.offset;
      $('progress-step').textContent = r.data.step || 'Starting…';
      const spin = $('progress-spin');
      spin.className = 'spinner' + (r.data.status === 'done' ? ' done' : r.data.status === 'failed' ? ' fail' : '');
      $('progress-error').hidden = r.data.status !== 'failed';
      $('progress-error').textContent = r.data.error || '';
      $('btn-progress-retry').hidden = $('btn-progress-back').hidden = r.data.status !== 'failed';
      $('btn-open').hidden = r.data.status !== 'done';
      if (r.data.status === 'done') {
        clearInterval(logTimer);
        $('title').textContent = 'Hermes is installed';
        setTimeout(() => { location.href = '/vesper'; }, 2500);
      }
      if (r.data.status === 'failed') clearInterval(logTimer);
    };
    tick();
    logTimer = setInterval(tick, 1200);
  }

  // --- existing Hermes -------------------------------------------------------------------------------------------
  $('btn-detect').addEventListener('click', async () => {
    $('btn-detect').disabled = true;
    $('detect-status').textContent = 'Looking on this computer and your network…';
    $('candidates').innerHTML = '';
    const r = await api('detect', {});
    $('btn-detect').disabled = false;
    if (!r.ok) { $('detect-status').textContent = errorText(r); return; }
    const list = r.data.candidates || [];
    $('detect-status').textContent = list.length ? `Found ${list.length}.` : 'No Hermes found. Enter its address below, or let it connect itself (3).';
    for (const c of list) {
      const li = document.createElement('li');
      const detail = c.problem ? `<span class="warn">${esc(c.problem)}</span>`
        : `Hermes ${esc(c.hermes_version || '?')}${c.connector_version ? ` · connector ${esc(c.connector_version)}` : ' · no connector yet'}`;
      li.innerHTML = `<div><div class="mono">${esc(c.url)}</div><div class="src">${esc(c.source)} · ${detail}</div></div>`;
      const use = document.createElement('button');
      use.className = 'btn small'; use.textContent = c.key_known ? 'Connect' : 'Use this';
      use.addEventListener('click', () => {
        $('f-url').value = c.url;
        if (c.key_known) connectTo(c.url, '');
        else { $('f-key').focus(); $('test-results').innerHTML = '<li class="info"><span class="mark">i</span><span>Now paste its API key and press Connect.</span></li>'; }
      });
      li.appendChild(use);
      $('candidates').appendChild(li);
    }
  });

  function renderChecks(target, result) {
    const ul = $(target);
    ul.innerHTML = '';
    for (const c of result.checks || []) {
      const li = document.createElement('li');
      li.className = c.ok === true ? 'ok' : c.ok === false ? 'no' : 'info';
      li.innerHTML = `<span class="mark">${c.ok === true ? '✓' : c.ok === false ? '✗' : '!'}</span>
        <span>${esc(c.label)} ${c.detail ? `<span class="detail">· ${esc(c.detail)}</span>` : ''}
        ${c.hint ? `<div class="hint">${esc(c.hint)}</div>` : ''}</span>`;
      ul.appendChild(li);
    }
    if (result.ok) {
      const li = document.createElement('li');
      li.className = 'ok';
      li.innerHTML = '<span class="mark">✓</span><span><b>Connection works.</b></span>';
      ul.appendChild(li);
    }
  }

  $('btn-test').addEventListener('click', async () => {
    const url = $('f-url').value.trim();
    if (!url) { $('f-url').focus(); return; }
    $('test-results').innerHTML = '<li class="info"><span class="mark">…</span><span>Testing…</span></li>';
    const r = await api('test', { url, api_key: $('f-key').value.trim() });
    if (!r.ok) { $('test-results').innerHTML = `<li class="no"><span class="mark">✗</span><span>${esc(errorText(r))}</span></li>`; return; }
    renderChecks('test-results', r.data);
  });
  $('btn-connect').addEventListener('click', () => connectTo($('f-url').value.trim(), $('f-key').value.trim()));

  async function connectTo(url, apiKey) {
    if (!url) { $('f-url').focus(); return; }
    $('test-results').innerHTML = '<li class="info"><span class="mark">…</span><span>Connecting…</span></li>';
    const r = await api('connect', { url, api_key: apiKey });
    renderChecks('test-results', r.data);
    if (r.ok && r.data.connected) location.reload();
    else if (!r.data.checks) $('test-results').innerHTML = `<li class="no"><span class="mark">✗</span><span>${esc(errorText(r))}</span></li>`;
  }

  async function loadPrompt() {
    const r = await api('connect-prompt', {});
    if (!r.ok) { $('prompt').value = errorText(r); return; }
    $('prompt').value = r.data.prompt;
    $('command').textContent = r.data.command;
    watchPairing();
  }
  $('btn-new-prompt').addEventListener('click', loadPrompt);
  const copy = async (text, label) => {
    try { await navigator.clipboard.writeText(text); $('copy-status').textContent = `${label} copied.`; }
    catch { $('prompt').select(); $('copy-status').textContent = 'Press Ctrl+C (or ⌘C) to copy.'; }
    setTimeout(() => { $('copy-status').textContent = ''; }, 3000);
  };
  $('btn-copy-prompt').addEventListener('click', () => copy($('prompt').value, 'Prompt'));
  $('btn-copy-cmd').addEventListener('click', () => copy($('command').textContent, 'Command'));

  function watchPairing() {
    clearInterval(pairTimer);
    pairTimer = setInterval(async () => {
      if (view !== 'existing') { clearInterval(pairTimer); return; }
      const r = await api('state');
      if (!r.ok) return;
      const p = r.data.pair || {};
      const box = $('pair-status');
      box.hidden = !p.status || p.status === 'idle';
      box.className = 'pair ' + (p.status || '');
      box.textContent = p.status === 'waiting' ? '⏳ ' + p.detail : p.status === 'connected' ? '✓ ' + p.detail + ' Opening JARVIS…'
        : p.status === 'failed' ? '✗ ' + p.detail : '';
      if (p.status === 'connected' || r.data.configured) {
        clearInterval(pairTimer);
        setTimeout(() => location.reload(), 1500);
      }
    }, 2500);
  }

  // --- connected -----------------------------------------------------------------------------------------------
  function renderConnected() {
    if (!current) return;
    const h = current.hermes, managed = current.mode === 'managed';
    $('s-mode').textContent = managed ? 'Installed by JARVIS' : 'Your own Hermes';
    $('s-url').textContent = managed ? 'inside this container' : h.url;
    $('s-version').textContent = h.version || '—';
    $('s-health').textContent = h.healthy ? 'Running' : managed && !h.installed ? 'Not installed' : 'Not answering';
    $('s-health').className = h.healthy ? 'good' : 'bad';
    const conn = current.connector;
    $('s-connector').textContent = managed ? `${conn.installed || '—'}${conn.installed && conn.installed !== conn.bundled ? ` (JARVIS ships ${conn.bundled})` : ''}` : `JARVIS ships ${conn.bundled}`;
    $('updates-card').hidden = !managed;
    $('persona-card').hidden = !managed;
    if (!$('persona-status').textContent && current.persona) $('persona-status').textContent = 'Applied: Hermes answers as JARVIS.';
    $('btn-use-managed').hidden = managed || !h.installed;
    const u = current.updates;
    if (!$('u-hour').options.length) {
      for (let i = 0; i < 24; i++) $('u-hour').add(new Option(`${String(i).padStart(2, '0')}:00`, i));
    }
    if (document.activeElement !== $('u-hour')) $('u-hour').value = u.hour;
    $('u-auto').checked = !!u.auto;
    const last = u.last_result;
    $('u-status').textContent = u.busy ? (u.step || 'Updating…')
      : last ? `Last update ${last.at?.slice(0, 16).replace('T', ' ')}: ${last.ok ? `${last.from} → ${last.to}` : `failed (${last.problem || 'see log'})${last.rollback ? `, rollback ${last.rollback}` : ''}`}`
      : u.last_check_result ? (u.last_check_result.available ? 'An update is available.' : 'Hermes is up to date.') : '';
    $('btn-update').disabled = $('btn-check').disabled = !!u.busy;
    if (u.busy) followUpdates();
  }

  $('btn-retest').addEventListener('click', async () => {
    $('retest-results').innerHTML = '<li class="info"><span class="mark">…</span><span>Testing…</span></li>';
    const r = await api('test', {});
    if (!r.ok) { $('retest-results').innerHTML = `<li class="no"><span class="mark">✗</span><span>${esc(errorText(r))}</span></li>`; return; }
    renderChecks('retest-results', r.data);
  });
  const saveUpdates = () => api('update/settings', { auto: $('u-auto').checked, hour: Number($('u-hour').value) }, 'PUT');
  $('u-auto').addEventListener('change', saveUpdates);
  $('u-hour').addEventListener('change', saveUpdates);
  $('btn-check').addEventListener('click', async () => {
    $('u-status').textContent = 'Checking…';
    const r = await api('update/check', {});
    $('u-status').textContent = !r.ok ? errorText(r) : r.data.available ? 'An update is available.' : `Hermes ${r.data.current} is up to date.`;
  });
  $('btn-update').addEventListener('click', async () => {
    const r = await api('update/run', {});
    if (!r.ok) { $('u-status').textContent = errorText(r); return; }
    followUpdates();
  });
  function followUpdates() {
    if (updateTimer) return;
    $('u-log').hidden = false;
    updateTimer = setInterval(async () => {
      const r = await api('log?which=updates&offset=' + updateLogOffset);
      if (r.ok) { appendLog('u-log', r.data.lines || []); updateLogOffset = r.data.offset; }
      if (current && !current.updates.busy) { clearInterval(updateTimer); updateTimer = null; }
    }, 1500);
  }
  $('btn-persona').addEventListener('click', async () => {
    const r = await api('persona', {});
    $('persona-status').textContent = r.ok ? 'Done: Hermes answers as JARVIS from the next conversation.' : errorText(r);
  });
  $('btn-disconnect').addEventListener('click', async () => {
    if (!confirm('Disconnect JARVIS from this Hermes? Nothing is deleted.')) return;
    const r = await api('disconnect', {});
    if (r.ok) location.reload(); else alert(errorText(r));
  });
  $('btn-use-managed').addEventListener('click', async () => {
    const r = await api('use-managed', {});
    if (r.ok) location.reload(); else alert(errorText(r));
  });

  // --- login -----------------------------------------------------------------------------------------------------
  (async () => {
    let status = {};
    try { status = await (await fetch('/jarvis/api/auth', { cache: 'no-store' })).json(); } catch { return; }
    if (!status.enabled) return;
    $('foot-logout').hidden = false;
    $('password-card').hidden = !!status.from_env;
    $('pw-new').minLength = status.min_length || 8;
  })();
  $('btn-logout').addEventListener('click', async e => {
    e.preventDefault();
    await api('logout', {});
    location.replace('/jarvis/login');
  });
  $('password-card').addEventListener('submit', async e => {
    e.preventDefault();
    $('pw-status').textContent = 'Saving…';
    const r = await api('password', { current: $('pw-current').value, password: $('pw-new').value });
    $('pw-status').textContent = r.ok ? 'Password changed.' : errorText(r);
    if (r.ok) { $('pw-current').value = ''; $('pw-new').value = ''; }
  });

  refresh();
  setInterval(refresh, 4000);
})();
