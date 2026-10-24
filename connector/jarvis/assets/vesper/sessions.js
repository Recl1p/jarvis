/* Sessions drawer for the JARVIS dashboard: switch between chats, start a fresh one, rename or delete old ones,
   and see how full each chat's context window is.
   Opens on window event `vesper:sessions` ({current: id | null}). Asks the dashboard to change chat with
   `vesper:session-switch` ({id, fresh?: true}). Also defines `window.vesperSessions`:
   messages(id) -> [{who: 'YOU' | 'JARVIS', text}], usage(id) -> {used, limit, pct, compressAt} | null,
   create() -> new session id. */
(() => {
  if (customElements.get('vesper-sessions')) return;   // loaded twice: keep the first
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
  const enc = encodeURIComponent;
  // The API server stores the dashboard's sessions (created with source "vesper-dashboard") as api_server.
  const SOURCES = ['api_server', 'vesper-dashboard'];
  const WARN = 70, CRITICAL = 90;
  const LONG_HINT = 'This chat is getting long — start a new one to keep JARVIS fast and sharp.';

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, headers: {
        Authorization: 'Bearer ' + (document.body.dataset.vesperKey || ''),
        ...(options.body ? { 'Content-Type': 'application/json' } : {})
      } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.error?.message || (typeof data.error === 'string' ? data.error : '') || `Request failed (${response.status})`);
        error.status = response.status; error.code = data.error?.code;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out.');
      throw error;
    } finally { clearTimeout(timeout); }
  }

  /* Transcript text: what the user saw in the chat, without the lines the dashboard adds for the agent. */
  const CONTEXT_START = /^\s*\[(?:JARVIS dashboard|VESPER|Dashboard context)\b/i;
  const ATTACHED = /^\s*\[Attached (?:file|PDF): (.+?)(?: — |\])/;
  const stripContext = text => {
    const kept = []; let inside = false;
    for (const line of String(text).split('\n')) {
      if (!inside && CONTEXT_START.test(line)) inside = true;
      if (inside) { if (/\]\s*$/.test(line)) inside = false; continue; }
      kept.push(line);
    }
    return kept.join('\n').trim();
  };
  // A message with attachments is a list of parts: the typed text, then per attachment a header part
  // ("[Attached file: name]" + the extracted text, split over several parts) or an image. The chat showed
  // the typed text plus one chip per attachment, so that is what comes back.
  const userText = content => {
    if (!Array.isArray(content)) return stripContext(typeof content === 'string' ? content : '');
    let typed = '', skipping = false;
    const chips = [];
    for (const part of content) {
      if (part?.type === 'image_url' || part?.type === 'image') { chips.push('image'); skipping = false; continue; }
      const text = typeof part?.text === 'string' ? part.text : '';
      const attached = ATTACHED.exec(text);
      if (attached) { chips.push(attached[1]); skipping = true; continue; }
      if (!skipping && text) typed += (typed ? '\n' : '') + text;
    }
    return [stripContext(typed), chips.map(name => '📎 ' + name).join('  ')].filter(Boolean).join('\n');
  };
  const assistantText = content => (Array.isArray(content)
    ? content.map(part => (typeof part?.text === 'string' ? part.text : '')).join('\n')
    : typeof content === 'string' ? content : '').trim();

  async function messages(id) {
    // Stored messages include every tool call and result, so a long chat needs more than one page of 500.
    let rows = [], offset = 0;
    for (let page = 0; page < 4; page++) {
      const data = await api(`/api/sessions/${enc(id)}/messages?limit=500&offset=${offset}&order=latest`);
      const got = data.data || [];
      rows = got.concat(rows);
      if (got.length < 500) break;
      offset += got.length;
    }
    const transcript = [];
    for (const row of rows) {
      if (row.display_kind === 'hidden') continue;
      if (row.role === 'user') {
        const text = userText(row.content);
        if (text) transcript.push({ who: 'YOU', text });
      } else if (row.role === 'assistant' && !(row.tool_calls && row.tool_calls.length)) {
        const text = assistantText(row.content);
        if (text) transcript.push({ who: 'JARVIS', text });
      }
    }
    return transcript;
  }

  async function usage(id) {
    try {
      const data = await api(`/vesper/sessions/${enc(id)}/context`);
      return data.used_tokens == null || !data.context_length ? null
        : { used: data.used_tokens, limit: data.context_length, pct: data.pct, compressAt: data.compress_at ?? null };
    } catch { return null; }
  }

  // The same call ensureSession() in the dashboard makes.
  async function create() {
    const data = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ source: 'vesper-dashboard' }) });
    return data.session.id;
  }

  window.vesperSessions = { messages, usage, create };

  /* Presentation helpers. */
  const compact = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'K' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(Math.round(n));
  const ago = ms => {
    const seconds = Math.max(0, (Date.now() - ms) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' min ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' h ago';
    if (seconds < 172800) return 'yesterday';
    if (seconds < 7 * 86400) return Math.floor(seconds / 86400) + ' days ago';
    return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  };
  const previewTitle = raw => {
    let text = String(raw || '');
    if (text.startsWith('\u0000json:')) {   // a first message with attachments is stored as JSON
      const m = /"text":\s*"((?:[^"\\]|\\.)*)/.exec(text);
      text = m ? m[1].replace(/\\n/g, ' ').replace(/\\(.)/g, '$1') : '';
    }
    const attached = ATTACHED.exec(text);
    if (attached) return '📎 ' + attached[1];
    return text.replace(/\s*\[(?:JARVIS dashboard|VESPER|Dashboard context)\b[^\]]*\]?/i, '').replace(/\s+/g, ' ').trim();
  };
  const level = pct => pct >= CRITICAL ? 'crit' : pct >= WARN ? 'warn' : 'ok';
  // Long enough to suggest a fresh chat: past 70% of the window, or close to where Hermes compacts it
  // (models with a huge window compact long before 70%).
  const isLong = u => !!u && (u.pct >= WARN || (u.compressAt > 0 && u.used >= 0.85 * u.compressAt));

  class VesperSessions extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.current = null; this.rows = []; this.defaultModel = '';
      this.loading = false; this.error = ''; this.metaState = 'idle';
      this.renaming = null; this.confirming = null; this.busy = false; this.generation = 0;
      this.openPanel = event => this.open(event.detail || {});
      this.shadowRoot.innerHTML = `
        <style>
          :host{--ac:var(--v-ac, #5cc8ff);--ink:var(--v-text, #dbe9ff);--mute:var(--v-mute2, #8fb0dc);--line:rgba(var(--v-ac2-rgb, 90,160,255),.3);--warn:#ffb454;--crit:#ff6b6b;
            color:var(--ink);font:14px 'Space Grotesk',system-ui,sans-serif}
          *{box-sizing:border-box} [hidden]{display:none!important}
          dialog{position:fixed;inset:0 0 0 auto;margin:0;padding:0;border:0;border-left:1px solid var(--line);
            width:min(440px,100vw);max-width:100vw;height:100dvh;max-height:100dvh;color:inherit;
            background:linear-gradient(180deg,rgba(var(--v-panel9-rgb, 9,24,52),.98),rgba(var(--v-deep10-rgb, 4,12,28),.99));box-shadow:-24px 0 80px #000b;overflow:hidden}
          dialog[open]{display:flex;flex-direction:column;animation:slide .2s ease-out}
          dialog::backdrop{background:rgba(var(--v-deep11-rgb, 1,6,16),.62);backdrop-filter:blur(4px)}
          @keyframes slide{from{transform:translateX(28px);opacity:0}}
          @media(prefers-reduced-motion:reduce){dialog[open]{animation:none}}
          .panel{display:flex;flex-direction:column;min-height:0;height:100%}
          header{display:flex;align-items:flex-start;gap:12px;padding:16px 16px 12px 18px;border-bottom:1px solid var(--line)}
          h1{margin:0;font:600 15px 'JetBrains Mono',monospace;letter-spacing:.18em;color:var(--v-text-hi2, #eaf6ff)}
          header small{display:block;margin-top:5px;color:var(--mute);font-size:12px;line-height:1.4}
          .close{margin-left:auto;flex:none}
          button,input{font:inherit;color:inherit}
          button{cursor:pointer;min-height:40px;min-width:40px;padding:0 14px;border:1px solid var(--line);border-radius:8px;
            background:rgba(var(--v-line4-rgb, 20,50,92),.45);font-size:13px}
          button:hover:not(:disabled){border-color:var(--ac);background:rgba(var(--v-line5-rgb, 36,86,140),.55)}
          button:disabled{opacity:.5;cursor:default}
          button:focus-visible,input:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
          .primary{color:var(--v-deep8, #02101c);background:var(--ac);border-color:var(--ac);font-weight:600}
          .primary:hover:not(:disabled){background:var(--v-hi5, #9bdeff);border-color:var(--v-hi5, #9bdeff)}
          .danger{color:#ffb1ae;border-color:rgba(255,107,107,.45)}
          .danger.solid{color:#2a0406;background:var(--crit);border-color:var(--crit);font-weight:600}
          .danger.solid:hover:not(:disabled){background:#ff9a9a;border-color:#ff9a9a}
          .tools{padding:14px 16px 10px}
          .tools .primary{width:100%;font:600 12px 'JetBrains Mono',monospace;letter-spacing:.14em}
          #notice{margin:10px 16px 0;padding:10px 12px;border-radius:8px;background:rgba(var(--v-line6-rgb, 40,100,150),.3);font-size:13px;line-height:1.45}
          #notice.error{background:rgba(120,36,44,.45);color:#ffc8bf}
          .body{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:2px 16px 8px}
          ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
          .chat{border:1px solid var(--line);border-radius:10px;background:rgba(var(--v-panel10-rgb, 8,24,52),.6);overflow:hidden}
          .chat.current{border-color:var(--ac);background:rgba(var(--v-line7-rgb, 18,52,96),.5);box-shadow:0 0 0 1px rgba(var(--v-ac-rgb, 92,200,255),.25) inset}
          .open{display:block;width:100%;min-height:64px;padding:12px 14px;text-align:left;border:0;border-radius:0;background:none}
          .open:hover:not(:disabled){background:rgba(var(--v-ac-rgb, 92,200,255),.08)}
          .open>span{display:block}
          .open>.top{display:flex;align-items:flex-start;gap:8px;min-width:0}
          .name{flex:1;min-width:0;font-weight:600;font-size:15px;line-height:1.3;color:var(--v-text-hi2, #eaf6ff);overflow:hidden;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2}
          .badge{flex:none;margin-top:1px;padding:2px 7px;border-radius:99px;font:600 10px 'JetBrains Mono',monospace;letter-spacing:.12em;color:var(--v-deep8, #02101c);background:var(--ac)}
          .open>.sub{display:flex;gap:5px;margin-top:5px;color:var(--mute);font-size:12px;line-height:1.35;min-width:0}
          .sub span{white-space:nowrap}.sub .model{min-width:0;overflow:hidden;text-overflow:ellipsis}
          .meter{position:relative;height:6px;margin-top:10px;border-radius:99px;background:rgba(var(--v-ac2-rgb, 90,160,255),.16);overflow:visible}
          .meter i{display:block;height:100%;min-width:2px;border-radius:99px;background:var(--v-meter-b, var(--ac))}
          .meter.warn i{background:var(--warn)} .meter.crit i{background:var(--crit)}
          .meter.pending i{width:35%;opacity:.25;animation:pulse 1.2s ease-in-out infinite}
          @keyframes pulse{50%{opacity:.08}} @media(prefers-reduced-motion:reduce){.meter.pending i{animation:none}}
          .meter b{position:absolute;top:-3px;width:2px;height:12px;margin-left:-1px;background:var(--v-text, #dbe9ff);opacity:.55;border-radius:1px}
          .open>.legend{display:flex;justify-content:space-between;gap:8px;margin-top:6px;color:var(--mute);font:11px 'JetBrains Mono',monospace}
          .legend.warn{color:var(--warn)} .legend.crit{color:var(--crit)}
          .compact-note{margin-top:3px;color:var(--mute);font-size:11px}
          .hint{margin:0 12px 10px;padding:11px 12px;border:1px solid rgba(255,180,84,.55);border-radius:8px;background:rgba(255,180,84,.1);
            color:#ffe2bb;font-size:13px;line-height:1.45}
          .hint.crit{border-color:rgba(255,107,107,.7);background:rgba(255,107,107,.12);color:#ffd5d0}
          .hint button{margin-top:9px;width:100%}
          .actions{display:flex;gap:8px;padding:0 12px 12px}
          .actions button{flex:1}
          .confirm{margin:0 12px 12px;padding:11px 12px;border:1px solid rgba(255,107,107,.5);border-radius:8px;background:rgba(255,107,107,.08);font-size:13px;line-height:1.45}
          .confirm .actions{padding:10px 0 0}
          form.rename{display:flex;flex-wrap:wrap;gap:8px;padding:12px}
          form.rename input{flex:1 1 100%;min-height:44px;padding:0 12px;font-size:16px;border:1px solid var(--ac);border-radius:8px;background:var(--v-deep9, #030d1c)}
          form.rename button{flex:1}
          .empty{padding:28px 8px;text-align:center;color:var(--mute);line-height:1.6}
          footer{padding:10px 18px calc(12px + env(safe-area-inset-bottom,0px));border-top:1px solid var(--line);color:var(--mute);font-size:12px;line-height:1.5}
          @media(max-width:700px){
            dialog{width:100vw;border-left:0}
            header{padding:max(14px,env(safe-area-inset-top,0px)) 12px 12px 14px}
            .tools{padding:12px 12px 10px}.body{padding:2px 12px 8px}
          }
        </style>
        <dialog aria-labelledby="title"><div class="panel">
          <header><div><h1 id="title">SESSIONS</h1><small id="count">Each chat keeps its own memory and context window.</small></div>
            <button class="close" aria-label="Close sessions">✕</button></header>
          <div class="tools"><button id="new" class="primary">+ NEW CHAT</button></div>
          <div id="notice" role="status" aria-live="polite" hidden></div>
          <div class="body"><ul id="list"></ul></div>
          <footer>A chat's skills are fixed when it starts, so start a new chat to pick up skills added since.</footer>
        </div></dialog>`;
      this.$('.close').onclick = () => this.close();
      this.$('#new').onclick = () => this.newChat();
      const dialog = this.$('dialog');
      dialog.addEventListener('click', event => { if (event.target === dialog) this.close(); });
      // Escape first backs out of an inline rename or delete confirmation, then closes the drawer.
      dialog.addEventListener('cancel', event => {
        if (this.renaming || this.confirming) {
          event.preventDefault();
          const id = this.renaming || this.confirming, act = this.renaming ? 'rename' : 'delete';
          this.renaming = this.confirming = null;
          this.render(`[data-id="${CSS.escape(id)}"] [data-act="${act}"]`);
        }
      });
      // The native modal makes the page inert, but Tab can still leave through the browser's own controls: wrap it.
      dialog.addEventListener('keydown', event => {
        if (event.key !== 'Tab') return;
        const items = [...this.shadowRoot.querySelectorAll('button, input')].filter(el => !el.disabled && el.getClientRects().length);
        if (!items.length) return;
        const active = this.shadowRoot.activeElement;
        if (event.shiftKey && (active === items[0] || !active)) { event.preventDefault(); items[items.length - 1].focus(); }
        else if (!event.shiftKey && (active === items[items.length - 1] || !active)) { event.preventDefault(); items[0].focus(); }
      });
      dialog.addEventListener('close', () => {
        this.generation++;
        const opener = this.opener; this.opener = null;
        if (opener && opener.isConnected) { try { opener.focus(); } catch { /* element gone */ } }
      });
      this.$('#list').addEventListener('click', event => this.onListClick(event));
      this.$('#list').addEventListener('submit', event => { event.preventDefault(); this.saveRename(event.target); });
    }
    $(selector) { return this.shadowRoot.querySelector(selector); }
    connectedCallback() { window.addEventListener('vesper:sessions', this.openPanel); }
    disconnectedCallback() { window.removeEventListener('vesper:sessions', this.openPanel); }

    isCurrent(row) { return !!this.current && row.ids.includes(this.current); }
    // Confirmations fade after a few seconds; errors stay until the next action.
    notice(message, error = false) {
      const el = this.$('#notice'); el.textContent = message; el.className = error ? 'error' : ''; el.hidden = !message;
      clearTimeout(this.noticeTimer);
      if (message && !error) this.noticeTimer = setTimeout(() => { el.hidden = true; }, 4000);
    }
    setBusy(value) {
      this.busy = value;
      this.shadowRoot.querySelectorAll('button:not(.close)').forEach(b => { b.disabled = value; });
    }

    async open({ current = null } = {}) {
      this.current = current || null;
      this.renaming = this.confirming = null;
      const dialog = this.$('dialog');
      if (!dialog.open) {
        let active = document.activeElement;
        while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
        this.opener = active && active !== document.body ? active : null;
        dialog.showModal();
        this.$('.close').focus();
      }
      this.notice('');
      await this.load();
    }
    close() {
      const dialog = this.$('dialog');
      if (dialog.open) dialog.close();
    }

    /* Data */
    async list(source) {
      const data = await api(`/api/sessions?source=${enc(source)}&limit=100`);
      return data.data || [];
    }
    toRow(s) {
      const lineage = [s.id, s._lineage_root_id].filter(Boolean);
      return { id: s.id, ids: lineage, deleteIds: lineage, title: s.title || '', preview: previewTitle(s.preview),
        active: (s.last_active || s.started_at || 0) * 1000, count: s.message_count || 0, model: s.model || '', usage: null };
    }
    async load() {
      const generation = ++this.generation;
      this.loading = true; this.error = '';
      this.render();   // straight away, so the current-chat marker is right while the list refreshes
      try {
        const [main, extra] = await Promise.all([this.list(SOURCES[0]), this.list(SOURCES[1]).catch(() => [])]);
        if (generation !== this.generation) return;
        const byId = new Map();
        for (const s of [...main, ...extra]) if (!byId.has(s.id)) byId.set(s.id, this.toRow(s));
        // The chat the dashboard is in may be a session from before a compaction (the list shows its live tip) or
        // be missing from the list altogether (older than the page): ask the server where it went.
        if (this.current && ![...byId.values()].some(r => r.ids.includes(this.current))) {
          const found = (await api(`/vesper/sessions/meta?ids=${enc(this.current)}`).catch(() => null))?.data?.[this.current];
          if (generation !== this.generation) return;
          if (found) {
            if (!byId.has(found.id)) {
              try { const one = (await api(`/api/sessions/${enc(found.id)}`)).session; byId.set(found.id, this.toRow({ ...one, last_active: one.started_at })); } catch { /* gone */ }
            }
            byId.get(found.id)?.ids.push(this.current, ...(found.lineage_ids || []));
          }
        }
        this.rows = [...byId.values()].filter(r => r.count > 0 || this.isCurrent(r)).sort((a, b) => b.active - a.active);
        this.loading = false; this.metaState = this.rows.length ? 'loading' : 'idle';
        this.render();
      } catch (error) {
        if (generation !== this.generation) return;
        this.loading = false; this.error = error.message; this.render();
        return;
      }
      this.loadMeta(generation);
    }
    // Context usage and compression lineage come from one server call; the list is usable without them.
    async loadMeta(generation) {
      const ids = this.rows.slice(0, 60).map(r => r.id);
      try {
        const data = (await api(`/vesper/sessions/meta?ids=${ids.map(enc).join(',')}`)).data || {};
        if (generation !== this.generation) return;
        for (const row of this.rows) {
          const meta = data[row.id];
          if (!meta) continue;
          if (meta.used_tokens != null && meta.context_length) {
            row.usage = { used: meta.used_tokens, limit: meta.context_length, pct: meta.pct ?? 0, compressAt: meta.compress_at ?? null };
          }
          row.model = row.model || meta.model || '';
          row.deleteIds = [...new Set([...(meta.lineage_ids || []).slice().reverse(), ...row.ids])];
          row.ids = [...new Set([...row.ids, ...(meta.lineage_ids || [])])];
        }
        this.metaState = 'done';
      } catch { if (generation !== this.generation) return; this.metaState = 'failed'; }
      if (!this.renaming) this.render();
    }

    /* View */
    render(focusSelector) {
      const root = this.shadowRoot;
      // Keep keyboard focus on the same control across a re-render.
      const active = root.activeElement;
      const keep = focusSelector || (active?.dataset?.act && active.closest('li[data-id]')
        ? `[data-id="${CSS.escape(active.closest('li').dataset.id)}"] [data-act="${active.dataset.act}"]` : null);
      const rows = this.rows;
      this.$('#count').textContent = this.loading ? 'Loading chats…'
        : this.error ? 'Each chat keeps its own memory and context window.'
        : rows.length ? `${rows.length} ${rows.length === 1 ? 'chat' : 'chats'} · newest activity first`
        : 'Each chat keeps its own memory and context window.';
      const list = this.$('#list');
      if (this.error) {
        list.innerHTML = `<li class="empty">Couldn't load your chats.<br>${escape(this.error)}<br><br><button data-act="retry">Try again</button></li>`;
      } else if (this.loading && !rows.length) {
        list.innerHTML = '<li class="empty">Loading chats…</li>';
      } else if (!rows.length) {
        list.innerHTML = '<li class="empty">No chats yet. Start one with “New chat”.</li>';
      } else list.innerHTML = rows.map(row => this.rowHtml(row)).join('');
      root.querySelectorAll('button').forEach(b => { if (!b.classList.contains('close')) b.disabled = this.busy; });
      const target = keep ? root.querySelector(keep) : null;
      if (target) target.focus();
      const renameInput = this.$('form.rename input');
      if (renameInput) { renameInput.focus(); renameInput.select(); }
    }
    rowHtml(row) {
      const current = this.isCurrent(row);
      const title = row.title || row.preview || 'Untitled chat';
      const u = row.usage, lv = u ? level(u.pct) : '';
      const model = (row.model || '').split('/').pop();
      const label = escape(title), id = escape(row.id);
      const sub = `<span class="sub"><span>${escape(ago(row.active))}</span><span>· ${row.count} ${row.count === 1 ? 'msg' : 'msgs'}</span>${model ? `<span class="model">· ${escape(model)}</span>` : ''}</span>`;
      let meter = '';
      if (u) {
        const tick = u.compressAt && u.compressAt < u.limit ? `<b style="left:${(100 * u.compressAt / u.limit).toFixed(1)}%"></b>` : '';
        const detail = `About ${Math.round(u.used).toLocaleString()} of ${Math.round(u.limit).toLocaleString()} tokens used (estimate).${u.compressAt ? ` Hermes compacts the chat at about ${compact(u.compressAt)}.` : ''}`;
        meter = `<span class="meter ${lv}" role="meter" aria-label="Context used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(u.pct)}" aria-valuetext="${escape(`${Math.round(u.pct)} percent of the context window`)}" title="${escape(detail)}"><i style="width:${Math.max(1, Math.min(100, u.pct))}%"></i>${tick}</span>`
          + `<span class="legend ${lv}"><span>~${compact(u.used)} of ${compact(u.limit)}</span><span>${Math.round(u.pct)}%</span></span>`
          + (current && u.compressAt ? `<span class="compact-note">Auto-compacts near ${compact(u.compressAt)}</span>` : '');
      } else if (this.metaState === 'loading') {
        meter = '<span class="meter pending" aria-hidden="true"><i></i></span><span class="legend"><span>Measuring context…</span></span>';
      }
      let head;
      if (this.renaming === row.id) {
        head = `<form class="rename"><input name="title" maxlength="100" value="${escape(row.title)}" placeholder="Chat name (blank = automatic)" aria-label="Chat name" autocomplete="off" enterkeyhint="done">`
          + '<button type="submit" class="primary">Save</button><button type="button" data-act="cancel">Cancel</button></form>';
      } else {
        head = `<button class="open" data-act="${current ? 'stay' : 'switch'}" aria-label="${current ? 'Current chat: ' : 'Open chat: '}${label}">`
          + `<span class="top"><span class="name">${label}</span>${current ? '<span class="badge">CURRENT</span>' : ''}</span>${sub}${meter}</button>`;
      }
      const long = current && isLong(u) && this.renaming !== row.id && this.confirming !== row.id;
      const hint = long ? `<div class="hint ${lv === 'crit' ? 'crit' : ''}" role="note">${escape(LONG_HINT)}<button class="primary" data-act="new">Start a new chat</button></div>` : '';
      let foot = '';
      if (this.confirming === row.id) {
        foot = `<div class="confirm" role="alertdialog" aria-label="Confirm delete">Delete “${label}” and its history? This can't be undone.${current ? ' You are in this chat, so a fresh one will start.' : ''}`
          + '<div class="actions"><button data-act="cancel">Keep it</button><button class="danger solid" data-act="confirm-delete">Delete chat</button></div></div>';
      } else if (this.renaming !== row.id) {
        foot = `<div class="actions"><button data-act="rename" aria-label="Rename ${label}">Rename</button><button class="danger" data-act="delete" aria-label="Delete ${label}">Delete</button></div>`;
      }
      return `<li class="chat${current ? ' current' : ''}" data-id="${id}">${head}${hint}${foot}</li>`;
    }

    /* Actions */
    onListClick(event) {
      const button = event.target.closest('button[data-act]');
      if (!button || this.busy) return;
      const li = button.closest('li');
      const row = li && this.rows.find(r => r.id === li.dataset.id);
      const act = button.dataset.act;
      if (act === 'retry') return this.load();
      if (act === 'new') return this.newChat();
      if (!row) return;
      if (act === 'stay') return this.close();
      if (act === 'switch') return this.switchTo(row.id);
      this.notice('');
      if (act === 'rename') { this.renaming = row.id; this.confirming = null; return this.render(); }
      if (act === 'delete') { this.confirming = row.id; this.renaming = null; return this.render(`[data-id="${CSS.escape(row.id)}"] [data-act="cancel"]`); }
      if (act === 'cancel') {
        const was = this.renaming ? 'rename' : 'delete';
        this.renaming = this.confirming = null;
        return this.render(`[data-id="${CSS.escape(row.id)}"] [data-act="${was}"]`);
      }
      if (act === 'confirm-delete') return this.remove(row);
    }
    switchTo(id, fresh = false) {
      this.current = id;
      window.dispatchEvent(new CustomEvent('vesper:session-switch', { detail: fresh ? { id, fresh: true } : { id } }));
      this.close();
    }
    async newChat() {
      if (this.busy) return;
      // A chat with nothing in it is already a clean slate: reuse it instead of piling up empty sessions.
      const here = this.rows.find(r => this.isCurrent(r));
      if (here && here.count === 0) { this.switchTo(this.current, true); return; }
      this.setBusy(true); this.notice('');
      try { this.switchTo(await create(), true); }
      catch (error) { this.notice(`Couldn't start a new chat: ${error.message}`, true); }
      finally { this.setBusy(false); }
    }
    async saveRename(form) {
      const li = form.closest('li'), row = this.rows.find(r => r.id === li?.dataset.id);
      if (!row || this.busy) return;
      const title = form.elements.title.value.trim();
      this.setBusy(true); this.notice('');
      try {
        const data = await api(`/api/sessions/${enc(row.id)}`, { method: 'PATCH', body: JSON.stringify({ title }) });
        row.title = data.session?.title || '';
        this.renaming = null;
        this.setBusy(false);
        this.render(`[data-id="${CSS.escape(row.id)}"] [data-act="rename"]`);
        this.notice(title ? 'Chat renamed.' : 'Chat name cleared.');
      } catch (error) {
        this.setBusy(false);
        this.notice(/already in use/i.test(error.message) ? 'Another chat already has that name. Pick a different one.' : error.message, true);
        form.elements.title.focus();
      }
    }
    async remove(row) {
      if (this.busy) return;
      const wasCurrent = this.isCurrent(row);
      this.setBusy(true); this.notice('');
      try {
        // A compacted chat is a chain of sessions; delete the newest first so none is left behind as a ghost.
        for (const id of row.deleteIds) {
          try { await api(`/api/sessions/${enc(id)}`, { method: 'DELETE' }); }
          catch (error) { if (error.status !== 404) throw error; }
        }
        this.rows = this.rows.filter(r => r !== row);
        this.confirming = null;
        if (wasCurrent) {
          this.setBusy(false);
          this.switchTo(await create(), true);
          return;
        }
        this.setBusy(false);
        this.render();
        this.notice('Chat deleted.');
      } catch (error) {
        this.setBusy(false);
        this.confirming = null;
        this.notice(`Couldn't delete that chat: ${error.message}`, true);
        this.load();
      }
    }
  }
  customElements.define('vesper-sessions', VesperSessions);
})();
