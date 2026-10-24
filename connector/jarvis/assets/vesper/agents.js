/* Agent Control: scheduled agents, implemented with Hermes's own scheduler. */
(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
  const templates = [
    { name: 'Custom agent', prompt: '', schedule: 'every 1h' },
    { name: 'Research monitor', schedule: 'every 6h', prompt: 'Research [topic] using available web sources. Report new developments, cite source links, and explain what changed since the previous report.' },
    { name: 'Morning briefing', schedule: '0 8 * * *', prompt: 'Prepare my morning briefing: upcoming tasks, relevant updates, and priorities for today. Use only connected sources and clearly identify anything unavailable.' },
    { name: 'Code reviewer', schedule: '0 17 * * 1-5', prompt: 'Review recent changes in [repository path]. Report actionable bugs and risks with file paths and line numbers. Do not modify files or publish comments.' },
    { name: 'Inbox triage', schedule: 'every 2h', prompt: 'Review my inbox using connected email tools. Group new messages by urgency and summarize what needs attention. Do not send, delete, or archive messages.' },
    { name: 'Meeting preparation', schedule: '0 8 * * 1-5', prompt: 'Review upcoming meetings using connected calendar and document tools. Prepare context, an agenda, and questions for each meeting. Do not contact attendees.' }
  ];
  const date = value => value ? new Date(value).toLocaleString() : '—';
  const status = job => job.state === 'paused' ? 'paused' :
    (job.latest_execution?.status === 'running' ? 'running' : job.state || (job.enabled ? 'scheduled' : 'disabled'));
  const scheduleValue = job => {
    const s = job.schedule;
    if (typeof s === 'string') return s;
    return s?.expr || (s?.kind === 'interval' ? `every ${s.minutes}m` : s?.run_at) || job.schedule_display || '';
  };

  class AgentControl extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.jobs = []; this.providers = []; this.skills = []; this.toolsets = [];
      this.selected = null; this.editing = null; this.busy = false;
      this.openPanel = event => this.open(event.detail || {});
      this.shadowRoot.innerHTML = `
        <style>
          :host{--ac:var(--v-ac, #5cc8ff);color:var(--v-text, #dbe9ff);font:13px 'JetBrains Mono',monospace}
          *{box-sizing:border-box} dialog{color:inherit;background:var(--v-bg2, #0a1c3c);border:1px solid var(--v-line2, #346389);
            width:min(1140px,96vw);height:min(830px,94dvh);padding:0;border-radius:12px;box-shadow:0 30px 100px #000a}
          dialog::backdrop{background:#010610dc;backdrop-filter:blur(7px)}
          header{display:flex;align-items:center;gap:12px;padding:20px 24px;border-bottom:1px solid var(--v-line, #23415e)}
          h1{font:20px 'Space Grotesk',sans-serif;letter-spacing:.13em;margin:0;color:var(--v-text-hi2, #eaf6ff)}
          h2{font-size:17px;margin:0 0 15px} h3{font-size:12px;color:var(--ac);margin-top:22px}
          p{line-height:1.6} .muted,small{color:var(--v-mute7, #8cadc8)}small{display:block;line-height:1.6;margin-top:7px}
          button,input,textarea,select{font:inherit;color:inherit}button{cursor:pointer;border:1px solid var(--v-line3, #315c7e);
            background:var(--v-panel5, #0d2540);border-radius:5px;padding:9px 12px}button:hover{border-color:var(--ac);background:var(--v-panel6, #173b59)}
          button:disabled{opacity:.5;cursor:wait}.primary{color:var(--v-deep8, #02101c);background:var(--ac);border-color:var(--ac)}
          .primary:hover{background:var(--v-hi5, #9bdeff)}.danger{color:#ffb1ae}button:focus-visible,input:focus-visible,
          select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
          header .close{margin-left:auto}.layout{display:grid;grid-template-columns:290px minmax(0,1fr);height:calc(100% - 82px)}
          aside{padding:18px;border-right:1px solid var(--v-line, #23415e);overflow:auto}main{padding:24px;overflow:auto;min-width:0}
          .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.between{justify-content:space-between}
          #jobs{display:flex;flex-direction:column;gap:9px;margin-top:16px}.job{text-align:left;padding:13px;width:100%;overflow-wrap:anywhere}
          .job[aria-pressed=true]{border-color:var(--ac);background:var(--v-panel7, #15334f)}.badge{font-size:10px;text-transform:uppercase;color:var(--v-hi6, #8edbff)}
          #notice{padding:12px 24px;background:var(--v-panel8, #13354a);white-space:pre-wrap}#notice.error{background:#41242a;color:#ffc8bf}
          [hidden]{display:none!important}label.field{display:block;margin:18px 0 0;color:var(--v-text9, #aacee9);font-size:12px}
          input:not([type=checkbox]),textarea,select{display:block;width:100%;margin-top:7px;border:1px solid #315473;
            border-radius:5px;background:var(--v-deep9, #030d1c);padding:11px}textarea{resize:vertical;min-height:145px;line-height:1.6}
          .checks{display:grid;gap:9px;max-height:170px;overflow:auto;margin:12px 0;padding:4px}
          .checks label{display:flex;align-items:flex-start;gap:8px;line-height:1.5}.checks input{accent-color:var(--ac)}
          details{margin-top:18px;border:1px solid var(--v-line, #23415e);padding:12px;border-radius:5px}summary{cursor:pointer}
          .actions{display:flex;gap:9px;margin-top:22px;flex-wrap:wrap}.metric{border:1px solid var(--v-line, #23415e);padding:12px;border-radius:5px;flex:1;min-width:125px}
          .metric strong{display:block;font-size:18px;margin-top:7px}.instructions,#output{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}
          #output{background:#020b17;border:1px solid var(--v-line, #23415e);padding:16px;max-height:390px;overflow:auto;font:12px/1.7 monospace}
          .instructions{color:#adc9df}.form-footer{position:sticky;bottom:-24px;background:#061326;padding:15px 0}
          @media(max-width:720px){dialog{width:98vw;height:96dvh;max-width:none;max-height:none}dialog[open]{display:flex;flex-direction:column;overflow:hidden}header{padding:15px}h1{font-size:16px}.layout{display:block;flex:1;min-height:0;height:auto;overflow:auto;overscroll-behavior:contain}aside{border-right:0;border-bottom:1px solid var(--v-line, #23415e);max-height:240px}main{overflow:visible;padding:18px}#jobs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.form-footer{bottom:0}input:not([type=checkbox]):not([type=radio]),textarea,select{font-size:16px}button{min-height:40px;min-width:40px}.checks{max-height:none}}
        </style>
        <dialog aria-labelledby="agent-title">
          <header><div><h1 id="agent-title">JARVIS / AGENT CONTROL</h1><small>Scheduled agents · powered by Hermes</small></div><button class="close" aria-label="Close agent control">✕</button></header>
          <div id="notice" role="status" hidden></div>
          <div class="layout"><aside><div class="row between"><button id="new" class="primary">+ New agent</button><button id="refresh" aria-label="Refresh agents">↻</button></div>
          <small id="counts">Loading agents…</small><div id="jobs"></div></aside><main id="main"><p class="muted">Select an agent to see its schedule and results, or create one.</p></main></div>
        </dialog>`;
      this.$('.close').onclick = () => this.$('dialog').close();
      this.$('#new').onclick = () => this.showForm();
      this.$('#refresh').onclick = () => this.refresh().catch(e => this.notice(e.message, true));
    }
    $(selector) { return this.shadowRoot.querySelector(selector); }
    connectedCallback() { window.addEventListener('vesper:agents', this.openPanel); }
    disconnectedCallback() { window.removeEventListener('vesper:agents', this.openPanel); clearInterval(this.timer); }
    async api(path, options = {}) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const response = await fetch(path, { ...options, signal: controller.signal, headers: {
          Authorization: 'Bearer ' + (document.body.dataset.vesperKey || ''),
          ...(options.body ? { 'Content-Type': 'application/json' } : {})
        } });
        const data = await response.json();
        if (!response.ok) {
          const error = new Error(data.error?.message || data.error || `Request failed (${response.status})`);
          error.savedJobId = data.job_saved ? data.job_id : null;
          throw error;
        }
        if (options.method && options.method !== 'GET') window.dispatchEvent(new Event('vesper:agents-changed'));
        return data;
      } catch (error) {
        if (error.name === 'AbortError') throw new Error('Request timed out. Refresh to check whether the change was saved.');
        throw error;
      } finally { clearTimeout(timeout); }
    }
    notice(message, error = false) {
      const el = this.$('#notice'); el.textContent = message; el.className = error ? 'error' : ''; el.hidden = !message;
    }
    async open({ id, create = false } = {}) {
      if (this.$('dialog').open) return;
      this.$('dialog').showModal(); this.notice('');
      this.$('#new').disabled = true;
      this.$('dialog').onclose = () => clearInterval(this.timer);
      const results = await Promise.allSettled([
        this.refresh(), this.api('/api/model/options'), this.api('/vesper/skills'), this.api('/v1/toolsets')
      ]);
      if (results[1].status === 'fulfilled') this.providers = (results[1].value.providers || []).filter(p => p.authenticated);
      if (results[2].status === 'fulfilled') this.skills = results[2].value.data || [];
      if (results[3].status === 'fulfilled') this.toolsets = results[3].value.data || [];
      const failed = results.map((r, i) => r.status === 'rejected' ? ['Agents', 'Models', 'Skills', 'Tools'][i] + ': ' + r.reason.message : '').filter(Boolean);
      if (failed.length) this.notice(failed.join('\n'), true);
      this.$('#new').disabled = false;
      if (this.$('dialog').open) {
        const job = id ? this.jobs.find(j => j.id === id) : null;
        if (job) { this.selected = job.id; this.renderJobs(); this.showDetail(job); }
        else if (create) this.showForm();
      }
      if (this.$('dialog').open) this.timer = setInterval(() => {
        if (!document.hidden && !this.busy) this.refresh().catch(e => this.notice(e.message, true));
      }, 5000);
    }
    async refresh() {
      if (this.refreshing) return;
      this.refreshing = true;
      try {
        const data = await this.api('/api/jobs?include_disabled=true');
        this.jobs = data.jobs || []; this.renderJobs();
        if (this.selected && !this.$('#agent-form')) {
          const job = this.jobs.find(j => j.id === this.selected);
          if (job) {
            const stamp = JSON.stringify(job);
            if (stamp !== this.detailStamp) this.showDetail(job);
          } else { this.selected = null; this.$('#main').textContent = 'This agent is no longer in the scheduler.'; }
        }
      } finally { this.refreshing = false; }
    }
    renderJobs() {
      this.$('#counts').textContent = `${this.jobs.length} agents · ${this.jobs.filter(j => status(j) === 'running').length} running · ${this.jobs.filter(j => j.state === 'paused').length} paused`;
      this.$('#jobs').innerHTML = this.jobs.length ? this.jobs.map(j => `<button class="job" data-id="${escape(j.id)}" aria-pressed="${j.id === this.selected}"><b>${escape(j.name || j.id)}</b><small><span class="badge">${escape(status(j))}</span> · ${escape(j.schedule_display || scheduleValue(j))}</small></button>`).join('') : '<p class="muted">No agents yet. Create a scheduled task to get started.</p>';
      this.$('#jobs').querySelectorAll('button').forEach(b => b.onclick = () => {
        if (this.busy) return;
        this.selected = b.dataset.id; this.renderJobs(); this.showDetail(this.jobs.find(j => j.id === this.selected));
        if (window.matchMedia('(max-width:720px)').matches) this.$('#main').scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
    }
    showForm(job = null) {
      if (this.busy) return;
      this.editing = job?.id || null; this.notice('');
      const models = this.providers.flatMap(p => (p.models || []).map(m => ({ provider: p.slug, model: m, label: `${p.name} / ${m}` })));
      if (job?.model && !models.some(m => m.provider === job.provider && m.model === job.model)) models.push({ provider: job.provider || '', model: job.model, label: `${job.provider || 'Saved'} / ${job.model}` });
      this.formModels = models;
      this.$('#main').innerHTML = `<h2>${job ? 'Edit agent' : 'Create an agent'}</h2><p class="muted">Give Hermes a task and a schedule. Results stay here and appear in your agenda.</p>
        <form id="agent-form">
          ${job ? '' : `<label class="field">Template<select name="template">${templates.map((t,i) => `<option value="${i}">${escape(t.name)}</option>`).join('')}</select></label>`}
          <label class="field">Agent name<input name="name" required maxlength="200" placeholder="e.g. AI research tracker" value="${escape(job?.name || '')}"></label>
          <label class="field">Instructions<textarea name="prompt" required maxlength="5000" placeholder="What should this agent do? Include topics, paths, or sources it needs.">${escape(job?.prompt || '')}</textarea></label>
          <label class="field">Model<select name="model"><option value="">Hermes default</option>${models.map((m,i) => `<option value="${i}" ${m.model === job?.model && m.provider === (job.provider || '') ? 'selected' : ''}>${escape(m.label)}</option>`).join('')}</select></label>
          <label class="field">Schedule<input name="schedule" required maxlength="200" value="${escape(job ? scheduleValue(job) : 'every 1h')}" list="schedules"><datalist id="schedules"><option value="every 1h"><option value="every 6h"><option value="0 8 * * *"><option value="0 8 * * 1-5"></datalist></label>
          <small>Use “every 1h”, a five-field cron expression, or an ISO date/time. Cron uses the gateway's configured timezone. Saving schedules the first run; Run now queues it immediately.</small>
          <details ${job?.enabled_toolsets ? 'open' : ''}><summary>Tools & skills</summary>
            <label class="field"><input type="checkbox" name="defaultTools" ${job?.enabled_toolsets ? '' : 'checked'}> Use Hermes default toolsets</label>
            <div id="tools" class="checks">${this.toolsets.map(t => `<label><input type="checkbox" name="tool" value="${escape(t.name)}" ${(job?.enabled_toolsets || []).includes(t.name) ? 'checked' : ''}><span>${escape(t.label || t.name)}<small>${escape(t.description)}${t.configured ? '' : ' · setup required'}</small></span></label>`).join('') || '<small>Tool inventory unavailable. Hermes defaults are available.</small>'}</div>
            <small>Only selected toolsets are requested for this agent. Credentials and runtime availability still apply.</small>
            <h3>Installed skills</h3><div class="checks">${this.skills.map(s => `<label><input type="checkbox" name="skill" value="${escape(s.name)}" ${(job?.skills || []).includes(s.name) ? 'checked' : ''}><span>${escape(s.name)}<small>${escape(s.description)}</small></span></label>`).join('') || '<small>No installed skills available.</small>'}</div>
          </details>
          <div class="actions form-footer"><button type="submit" class="primary">${job ? 'Save changes' : 'Create agent'}</button><button type="button" id="cancel-form">Cancel</button></div>
        </form>`;
      const form = this.$('#agent-form');
      const syncTools = () => { form.querySelectorAll('[name=tool]').forEach(el => el.disabled = form.elements.defaultTools.checked); };
      form.elements.defaultTools.onchange = syncTools; syncTools();
      if (form.elements.template) form.elements.template.onchange = () => {
        const t = templates[Number(form.elements.template.value)];
        form.elements.name.value = t.name === 'Custom agent' ? '' : t.name;
        form.elements.prompt.value = t.prompt; form.elements.schedule.value = t.schedule;
      };
      this.$('#cancel-form').onclick = () => {
        const selected = this.jobs.find(j => j.id === this.selected);
        if (selected) this.showDetail(selected); else this.$('#main').textContent = 'Select an agent or create a new one.';
      };
      form.onsubmit = event => { event.preventDefault(); this.saveForm(form); };
      form.elements.name.focus();
    }
    async saveForm(form) {
      if (this.busy) return;
      const values = new FormData(form);
      const selectedModel = values.get('model') === '' ? null : this.formModels[Number(values.get('model'))];
      const tools = values.has('defaultTools') ? null : values.getAll('tool');
      if (tools && !tools.length) { this.notice('Select at least one toolset or use Hermes defaults.', true); return; }
      const payload = { name: values.get('name'), prompt: values.get('prompt'), schedule: values.get('schedule'),
        model: selectedModel?.model || null, provider: selectedModel?.provider || null,
        enabled_toolsets: tools, skills: values.getAll('skill') };
      this.busy = true; this.setBusy(true); this.notice('Saving agent…');
      try {
        const data = await this.api(this.editing ? `/vesper/agents/${encodeURIComponent(this.editing)}` : '/vesper/agents', {
          method: this.editing ? 'PATCH' : 'POST', body: JSON.stringify(payload)
        });
        this.selected = data.job.id;
        await this.refresh(); this.showDetail(data.job); this.renderJobs();
        this.notice('Agent saved. Hermes will run it at its next scheduled time.');
      } catch (error) {
        if (error.savedJobId) {
          this.editing = error.savedJobId;
          this.selected = error.savedJobId;
          form.querySelector('[type=submit]').textContent = 'Save changes';
          await this.refresh().catch(() => {});
        }
        this.notice(error.message, true);
      }
      finally { this.busy = false; this.setBusy(false); }
    }
    setBusy(value) {
      this.shadowRoot.querySelectorAll('button').forEach(b => { if (!b.classList.contains('close')) b.disabled = value; });
    }
    showDetail(job) {
      this.detailStamp = JSON.stringify(job); this.editing = null;
      const latest = job.latest_execution || {};
      this.$('#main').innerHTML = `<div class="row between"><h2>${escape(job.name)}</h2><span class="badge">${escape(status(job))}</span></div>
        <div class="row"><div class="metric"><small>Schedule</small><strong>${escape(job.schedule_display || scheduleValue(job))}</strong></div><div class="metric"><small>Latest result</small><strong>${escape(latest.status || job.last_status || 'Not run yet')}</strong></div></div>
        <small>Next run: ${escape(date(job.next_run_at))}<br>Last run: ${escape(date(job.last_run_at))}</small>
        <div class="actions"><button data-action="run" class="primary">Run now</button><button data-action="${job.state === 'paused' ? 'resume' : 'pause'}">${job.state === 'paused' ? 'Resume' : 'Pause schedule'}</button><button id="edit">Edit</button><button data-action="delete" class="danger">Delete</button></div>
        <small>Pause affects future scheduled runs. A run already in progress continues. Run now also resumes a paused schedule.</small>
        <h3>Instructions</h3><div class="instructions">${escape(job.prompt)}</div>
        <small>Model: ${escape(job.model || job.model_snapshot || 'Hermes default')} · ${escape(job.provider || job.provider_snapshot || 'default provider')}<br>Tools: ${escape(job.enabled_toolsets?.join(', ') || 'Hermes defaults')}<br>Skills: ${escape(job.skills?.join(', ') || 'None selected')}</small>
        ${latest.error || job.last_error ? `<p class="danger">${escape(latest.error || job.last_error)}</p>` : ''}
        <h3>Run output</h3><div class="row"><select id="output-files" aria-label="Saved run output"></select><button id="refresh-output">Refresh output</button></div><pre id="output">Loading saved results…</pre>`;
      this.$('#edit').onclick = () => this.showForm(job);
      this.$('#main').querySelectorAll('[data-action]').forEach(b => b.onclick = () => this.action(job, b.dataset.action));
      this.$('#refresh-output').onclick = () => this.loadOutput(job.id, this.$('#output-files').value);
      this.$('#output-files').onchange = e => this.loadOutput(job.id, e.target.value);
      this.loadOutput(job.id);
    }
    async loadOutput(id, filename = '') {
      const generation = this.outputGeneration = (this.outputGeneration || 0) + 1;
      try {
        const data = await this.api(`/vesper/agents/${encodeURIComponent(id)}/output${filename ? '?file=' + encodeURIComponent(filename) : ''}`);
        if (this.selected !== id || !this.$('#output') || this.outputGeneration !== generation) return;
        this.$('#output-files').innerHTML = data.files.map(f => `<option ${f === data.selected ? 'selected' : ''}>${escape(f)}</option>`).join('') || '<option>No saved runs</option>';
        this.$('#output').textContent = data.content ? data.content + (data.truncated ? '\n\n[Preview truncated at 256 KB]' : '') : 'No output yet. Run the agent or wait for its scheduled time, then refresh.';
      } catch (error) { if (this.selected === id && this.$('#output') && this.outputGeneration === generation) this.$('#output').textContent = error.message; }
    }
    async action(job, action) {
      if (this.busy) return;
      if (action === 'delete' && !window.confirm(`Delete “${job.name}” and its saved run outputs?`)) return;
      this.busy = true; this.setBusy(true); this.notice('');
      try {
        await this.api(`/api/jobs/${encodeURIComponent(job.id)}${action === 'delete' ? '' : '/' + action}`, { method: action === 'delete' ? 'DELETE' : 'POST' });
        await this.refresh();
        this.notice({ run: 'Run queued. Status updates automatically; output appears when the run finishes.', pause: 'Schedule paused.', resume: 'Schedule resumed.', delete: 'Agent deleted.' }[action]);
      } catch (error) { this.notice(error.message, true); }
      finally { this.busy = false; this.setBusy(false); }
    }
  }
  customElements.define('vesper-agent-control', AgentControl);
})();
