/* JARVIS login: the first visit creates the password, later ones log in (jarvis_launcher/server.py, auth.py). */
(() => {
  const $ = id => document.getElementById(id);
  const next = new URLSearchParams(location.search).get('next') || '/jarvis/';

  async function post(path, body) {
    try {
      const res = await fetch('/jarvis/api/' + path, {
        method: 'POST', cache: 'no-store', headers: { 'X-JARVIS': '1', 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      let data = {};
      try { data = await res.json(); } catch { /* not JSON */ }
      return { ok: res.ok, status: res.status, data };
    } catch {
      return { ok: false, status: 0, data: { error: { message: 'JARVIS is not answering. Is the container running?' } } };
    }
  }
  const message = r => r.data?.error?.message || `Request failed (HTTP ${r.status})`;
  // The server checks it too. A path on this server only: no second slash or backslash, and no whitespace or control
  // characters, which browsers drop ("/\t/x" would become "//x", another site).
  const go = target => { location.replace(/^\/(?![\/\\])[^\x00-\x20\x7f\\]*$/.test(target) ? target : '/jarvis/'); };

  async function start() {
    let status = {};
    try { status = await (await fetch('/jarvis/api/auth', { cache: 'no-store' })).json(); } catch { /* shown below */ }
    if (status.logged_in || status.enabled === false) { go(next); return; }
    $('env-note').hidden = !status.from_env;
    if (status.has_password) {
      $('title').textContent = 'Log in';
      $('form-login').hidden = false;
      $('password').focus();
    } else {
      $('title').textContent = 'Welcome to JARVIS';
      $('form-create').hidden = false;
      $('new-password').minLength = status.min_length || 8;
      $('new-password').focus();
    }
  }

  $('form-login').addEventListener('submit', async e => {
    e.preventDefault();
    const button = e.submitter || $('form-login').querySelector('button');
    button.disabled = true;
    $('login-error').textContent = '';
    const r = await post('login', { password: $('password').value, next });
    button.disabled = false;
    if (r.ok) { go(r.data.next || next); return; }
    $('login-error').textContent = message(r);
    $('password').select();
  });

  $('form-create').addEventListener('submit', async e => {
    e.preventDefault();
    const a = $('new-password').value, b = $('new-password2').value;
    $('create-error').textContent = '';
    if (a !== b) { $('create-error').textContent = 'The two passwords are different.'; return; }
    const button = e.submitter || $('form-create').querySelector('button');
    button.disabled = true;
    const r = await post('password', { password: a });
    button.disabled = false;
    if (r.ok) { go(next); return; }
    $('create-error').textContent = message(r);
  });

  start();
})();
