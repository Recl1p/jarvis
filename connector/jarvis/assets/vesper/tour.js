/* JARVIS setup guide: check the Hermes connection, connect a model, pick it, learn the dashboard. It speaks of the assistant by the current persona's
   name and takes on that persona's look (the colours come from the --v-* theme; see the persona block in the styles).
   Opens on window event `vesper:tour` ({step?, reason?: 'manual'|'no-model'}) and fires
   `vesper:tour-closed` when dismissed. Asks the dashboard for things through `vesper:ui`
   ({action: 'model-picker' | 'refresh-models' | 'setup-complete', model?, provider?}).
   Nothing here asks the user to run a command: the free setup talks to /vesper/setup/openrouter-free* and
   /vesper/setup/status; API keys, provider sign-ins and local servers go through /vesper/setup/{providers,key,
   login,local} (gateway/platforms/api_server_setup_providers.py); the voice step through /vesper/setup/voice*
   (api_server_setup_voice.py) and the Telegram step through /vesper/setup/telegram* (api_server_setup_telegram.py).
   The dashboard hears about voice changes through `vesper:ui` too: 'voice-refresh' and 'voice-engine' {engine}.
   The Hermes step asks JARVIS's launcher (/jarvis/api/test, launcher/jarvis_launcher/checks.py) or, without one,
   the connector's own /vesper/connector/check. */
(() => {
  const DONE_KEY = 'vesperTourDone';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
  // Tiny markup for authored copy: `code` and **bold**. Input is escaped first.
  const rich = text => escape(text)
    .replace(/`([^`]+)`/g, '<code class="k">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage can throw */ } }
  };
  const ui = (action, extra) => window.dispatchEvent(new CustomEvent('vesper:ui', { detail: { action, ...extra } }));
  // Bearer-authed call to the gateway. Never logs: a body may carry an API key.
  const api = async (path, body, ms = 20000, method) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
      const response = await fetch(path, {
        method: method || (body === undefined ? 'GET' : 'POST'), cache: 'no-store', signal: controller.signal,
        headers: { Authorization: 'Bearer ' + (document.body?.dataset?.vesperKey || ''), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      let data = null;
      try { data = await response.json(); } catch { /* error pages may not be JSON */ }
      return { ok: response.ok, status: response.status, data };
    } catch { return { ok: false, status: 0, data: null, network: true }; }
    finally { clearTimeout(timer); }
  };
  const ctxLabel = n => {
    n = Number(n);
    if (!n) return '';
    if (n >= 1048576) return `${+(n / 1048576).toFixed(1)}M`;
    return `${Math.round(n % 1024 === 0 ? n / 1024 : n / 1000)}K`;
  };

  // The ways to connect a model. None of them needs a terminal.
  const ROUTES = [
    { id: 'free', name: 'Free', hint: 'No card needed' },
    { id: 'login', name: 'Sign in', hint: 'Claude, ChatGPT…' },
    { id: 'key', name: 'API key', hint: 'Anthropic, OpenAI…' },
    { id: 'local', name: 'Local', hint: 'Your own computer' }
  ];
  const FREE_ERRORS = {
    invalid_key: 'That key was not accepted.', invalid_model: 'That model is not available.',
    upstream_unavailable: 'OpenRouter is not reachable right now.', network: 'Could not reach the gateway.'
  };
  // A failed call -> one plain sentence for the user.
  const problem = res => res.network ? 'Could not reach the server. Check your connection and try again.'
    : res.status === 401 ? 'The dashboard key was rejected. Reload the page and try again.'
    : (typeof res.data?.error?.message === 'string' && res.data.error.message) || `Something went wrong (HTTP ${res.status}).`;
  const LOGIN_STATUS = {
    denied: 'The sign-in was declined.', expired: 'The sign-in timed out.',
    error: 'The sign-in did not finish.'
  };
  // The assistant's name in the copy: the dashboard's current persona (JARVIS, Ultron, Cortana).
  const personaId = () => document.documentElement.dataset.persona || 'jarvis';
  const who = () => window.VESPER_PERSONAS?.[personaId()]?.short || 'JARVIS';
  const titleOf = step => typeof step.title === 'function' ? step.title() : step.title;
  const hostOf = url => { try { return new URL(url).host; } catch { return url; } };

  const STEPS = [
    { id: 'welcome', kicker: 'Welcome', title: () => `Meet ${who()}`, next: 'Start' },
    { id: 'hermes', kicker: 'Step 1 · the agent', title: 'Hermes connection' },
    { id: 'connect', kicker: 'Step 2 · the brain', title: 'Connect a model' },
    { id: 'voice', kicker: 'Step 3 · the voice', title: () => `Choose how ${who()} sounds` },
    { id: 'telegram', kicker: 'Step 4 · on your phone', title: () => `Message ${who()} on Telegram` },
    { id: 'model', kicker: 'Dashboard · model', title: 'Pick the model for this chat', targets: ['model'] },
    { id: 'around', kicker: 'Getting around', title: 'Sidebar, voice, commands', targets: ['sidebar'] },
    { id: 'done', kicker: 'All set', title: 'You are ready', targets: ['tab-telemetry', 'guide'], next: 'Finish' }
  ];
  const stepIndex = id => STEPS.findIndex(s => s.id === id);
  // Old and shorthand step names -> [step, connect route?].
  const ALIASES = {
    providers: ['connect'], connection: ['connect'], free: ['connect', 'free'], cloud: ['connect', 'key'],
    key: ['connect', 'key'], login: ['connect', 'login'], local: ['connect', 'local'], models: ['model'], notes: ['around'], voice: ['voice'], voices: ['voice'], elevenlabs: ['voice', 'eleven'],
    telegram: ['telegram'], phone: ['telegram'], palette: ['around']
  };
  const freshFree = () => ({ models: [], recommended: '', selected: '', keySet: false, loading: false, loaded: false, listError: false, busy: false, error: null, result: null });
  const freshProv = () => ({ data: null, loading: false, error: null });
  const freshKey = () => ({ pid: 'anthropic', busy: false, error: null, result: null });
  const freshLogin = () => ({ pid: '', start: null, status: '', busy: false, error: null, result: null, timer: 0 });
  const freshClaude = () => ({ busy: false, installing: null, sid: '', url: '', submitting: false, error: null, result: null, timer: 0 });
  const freshLocal = () => ({ scanning: false, scanned: false, found: [], url: '', models: null, model: '', busy: false, checking: false, error: null, result: null });
  // Voice step: Pocket TTS on this server (its cloning model from Hugging Face) or ElevenLabs.
  const VOICE_TABS = [{ id: 'server', name: 'On this server' }, { id: 'eleven', name: 'ElevenLabs' }];
  const freshVoice = () => ({ tab: 'server', data: null, loading: false, error: null, hfBusy: false, hfError: null, timer: 0,
    elBusy: false, elError: null, elNote: '', newKey: false, voices: null, voicesLoading: false, chosen: null, saving: false, saved: false, saveError: null });
  const freshTelegram = () => ({ status: null, loading: false, loadError: null, error: null, setup: null, busy: false, senders: [], finding: false,
    findTimer: 0, findUntil: 0, stale: false, result: null, replacing: false, waitTimer: 0 });
  const PERSONA_IDS = ['jarvis', 'cortana', 'ultron'];
  const mb = n => Math.round((n || 0) / 1048576);

  class VesperTour extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.i = 0; this.isOpen = false; this.peek = false; this.reason = 'manual';
      this.route = 'free'; this.free = freshFree();
      this.prov = freshProv(); this.key = freshKey(); this.login = freshLogin(); this.local = freshLocal(); this.claude = freshClaude();
      this.voice = freshVoice(); this.tg = freshTelegram(); this.hermes = { busy: false, result: null, state: null };
      this.mq = window.matchMedia('(max-width: 700px)');
      this.onOpen = event => this.open(event.detail || {});
      this.onKey = event => this.keydown(event);
      this.onFocusIn = event => { if (this.isOpen && !this.peek && !this.copying && event.target !== this) this.focusCard(); };
      this.onReflow = () => this.schedulePlace();
      this.onDialogClose = event => {
        if (!(event.target instanceof HTMLDialogElement)) return;
        setTimeout(() => { if (!document.querySelector('dialog[open]')) this.resume(); }, 150);
      };
      this.shadowRoot.innerHTML = `
        <style>
          :host{--ac:var(--v-ac, #5cc8ff);--text:var(--v-text, #dbe9ff);--muted:var(--v-mute, #82a6d4);--warn:#ffb454;--ok:#7fd6a8;--err:#ff9b9b;
            display:none;position:fixed;inset:0;z-index:2147483000;pointer-events:none;color:var(--text);color-scheme:dark;
            font:14px/1.55 'Space Grotesk',sans-serif}
          :host([open]){display:block}
          *{box-sizing:border-box}[hidden]{display:none!important}
          button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
          :focus-visible{outline:2px solid var(--ac);outline-offset:2px}
          code,pre{font-family:'JetBrains Mono',monospace}
          a{color:var(--ac)}a:hover{color:#fff}
          .shield{position:fixed;inset:0;pointer-events:auto;background:rgba(var(--v-deep5-rgb, 2,8,20),.7);
            -webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px)}
          .shield.spot{background:transparent;-webkit-backdrop-filter:none;backdrop-filter:none}
          .ring{position:fixed;pointer-events:none;border-radius:12px;
            box-shadow:0 0 0 2px var(--ac),0 0 26px 4px rgba(var(--v-ac-rgb, 92,200,255),.5),0 0 0 100vmax rgba(var(--v-deep5-rgb, 2,8,20),.72);
            transition:left .25s ease,top .25s ease,width .25s ease,height .25s ease,border-radius .25s ease;
            animation:glow 2.2s ease-in-out infinite}
          @keyframes glow{50%{box-shadow:0 0 0 2px var(--ac),0 0 36px 8px rgba(var(--v-ac-rgb, 92,200,255),.7),0 0 0 100vmax rgba(var(--v-deep5-rgb, 2,8,20),.72)}}
          .card{position:fixed;pointer-events:auto;display:flex;flex-direction:column;width:min(560px,calc(100vw - 32px));max-width:min(560px,calc(100vw - 32px));
            max-height:calc(100vh - 24px);max-height:calc(100dvh - 24px);overflow:hidden;outline:none;
            background:rgba(var(--v-panel3-rgb, 10,26,54),.94);-webkit-backdrop-filter:blur(18px) saturate(140%);backdrop-filter:blur(18px) saturate(140%);
            border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.4);border-radius:16px;
            box-shadow:0 30px 90px rgba(0,0,0,.65),0 0 60px rgba(var(--v-ac-rgb, 92,200,255),.1),inset 0 1px 0 rgba(255,255,255,.05)}
          .card.center{left:50%;top:50%;transform:translate(-50%,-50%)}
          .card.enter{animation:enter .22s ease-out}
          @keyframes enter{from{opacity:0;translate:0 8px}to{opacity:1;translate:0 0}}
          .head{display:flex;align-items:flex-start;gap:12px;padding:16px 12px 6px 20px}
          .kicker{font:600 10px/1.2 'JetBrains Mono',monospace;letter-spacing:.2em;text-transform:uppercase;color:var(--ac)}
          h2{margin:8px 0 0;font:600 20px/1.25 'Space Grotesk',sans-serif;letter-spacing:-.01em;color:var(--v-text8, #eef7ff)}
          .close{margin-left:auto;flex:none;width:40px;height:40px;border:1px solid transparent;border-radius:10px;
            background:transparent;color:var(--muted);font-size:15px}
          .close:hover{border-color:rgba(var(--v-ac-rgb, 92,200,255),.5);color:#fff;background:rgba(var(--v-ac-rgb, 92,200,255),.1)}
          .body{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:6px 20px 18px}
          .body p{margin:0 0 10px}.body p:last-child{margin-bottom:0}.body p.tight{margin-bottom:0}
          .muted{color:var(--muted)}b{color:var(--v-text8, #eef7ff);font-weight:600}
          code.k{padding:1px 6px;border-radius:5px;background:rgba(var(--v-deep-rgb, 5,14,30),.8);border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.2);
            color:var(--v-text7, #bfe6ff);font-size:12px;overflow-wrap:anywhere}
          .foot{display:flex;align-items:center;gap:10px;padding:12px 14px;border-top:1px solid rgba(var(--v-ac-rgb, 92,200,255),.18);
            background:rgba(var(--v-panel4-rgb, 6,16,36),.78)}
          .prog{flex:1;display:flex;flex-direction:column;align-items:center;gap:7px;min-width:0}
          .dots{display:flex;gap:5px}.dots i{width:6px;height:6px;border-radius:99px;background:rgba(var(--v-mute-rgb, 130,166,212),.35);transition:all .2s}
          .dots i.on{background:var(--ac);width:18px}.dots i.past{background:rgba(var(--v-ac-rgb, 92,200,255),.6)}
          .count{font:600 10px/1 'JetBrains Mono',monospace;letter-spacing:.2em;color:var(--muted)}
          .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:42px;padding:0 18px;border-radius:10px;
            border:1px solid rgba(var(--v-ac2-rgb, 90,160,255),.36);background:rgba(var(--v-ac2-rgb, 90,160,255),.1);color:var(--text);
            font:600 11px/1 'JetBrains Mono',monospace;letter-spacing:.16em;text-transform:uppercase;transition:all .15s}
          .btn:hover{border-color:var(--ac);background:rgba(var(--v-ac-rgb, 92,200,255),.18);color:#fff}
          .btn.primary{background:var(--ac);border-color:var(--ac);color:var(--v-bg5, #03101f)}.btn.primary:hover{background:var(--v-hi5, #9bdeff);border-color:var(--v-hi5, #9bdeff)}
          .btn[disabled]{opacity:.4;cursor:default;pointer-events:none}.btn.small{min-height:40px;padding:0 14px}
          a.btn{text-decoration:none}
          [role=tabpanel]+.note{margin-top:14px}
          .btn[aria-busy=true][disabled]{opacity:.8}
          .actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:14px}
          .actions+.note{margin-top:12px}
          .lead,.callout{margin:12px 0;padding:10px 12px;border-radius:10px;border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.3);
            background:rgba(var(--v-ac-rgb, 92,200,255),.07);font-size:13.5px;line-height:1.5}
          .lead,.callout.warn{border-color:rgba(255,180,84,.5);background:rgba(255,180,84,.08)}
          .callout.ok{border-color:rgba(127,214,168,.5);background:rgba(127,214,168,.08)}
          .checks{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:6px}
          .checks li{display:grid;grid-template-columns:18px 1fr;gap:6px;align-items:start}
          .checks .mk{font-weight:600}.checks .ok .mk{color:var(--ok)}.checks .no .mk{color:var(--err)}.checks .info .mk{color:var(--warn)}
          .checks small{display:block}.checks .hint{color:var(--warn)}
          .callout.err{border-color:rgba(255,155,155,.5);background:rgba(255,155,155,.08)}
          .lead b{color:var(--warn)}.callout.ok b{color:var(--ok)}.callout.err b{color:var(--err)}
          .routes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:8px 0 12px}
          .picks{display:grid;gap:8px;margin:8px 0 12px}
          .pick{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;min-height:52px;padding:10px 14px;text-align:left;
            border-radius:12px;border:1px solid rgba(var(--v-ac2-rgb, 90,160,255),.3);background:rgba(var(--v-deep-rgb, 5,14,30),.35);color:var(--text)}
          .pick b{font-size:14.5px}.pick small{font-size:12.5px;color:var(--muted)}
          .pick:hover{border-color:var(--ac);background:rgba(var(--v-ac-rgb, 92,200,255),.12)}.pick[disabled]{opacity:.5;pointer-events:none}
          .badge{margin-left:auto;font:600 10px/1.6 'JetBrains Mono',monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--ok)}
          .bar{height:8px;margin:12px 0 4px;border-radius:99px;background:rgba(var(--v-ac-rgb, 92,200,255),.12);overflow:hidden}
          .bar i{display:block;height:100%;background:var(--ac);transition:width .4s ease}
          pre.usercode{font-size:20px;letter-spacing:.18em;color:var(--v-text8, #eef7ff);text-align:center}
          .route{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:52px;padding:6px 8px;
            border-radius:12px;border:1px solid rgba(var(--v-ac2-rgb, 90,160,255),.3);background:rgba(var(--v-deep-rgb, 5,14,30),.35);color:var(--muted);text-align:center}
          .route b{font-size:14.5px;color:inherit}.route small{font-size:11.5px;line-height:1.2;color:var(--muted)}
          .route:hover{color:#fff;border-color:rgba(var(--v-ac-rgb, 92,200,255),.6)}
          .route[aria-selected=true]{background:rgba(var(--v-ac-rgb, 92,200,255),.18);border-color:var(--ac);color:#fff}
          .route[aria-selected=true] small{color:var(--v-text7, #bfe6ff)}
          .tabs{display:flex;gap:6px;overflow-x:auto;padding:4px 2px 8px;margin:0 -2px;scrollbar-width:thin}
          .tab{flex:none;min-height:40px;padding:0 12px;border-radius:99px;border:1px solid rgba(var(--v-ac2-rgb, 90,160,255),.3);
            background:transparent;color:var(--muted);font-size:13.5px;font-weight:600}
          .tab:hover{color:#fff;border-color:rgba(var(--v-ac-rgb, 92,200,255),.6)}
          .tab[aria-selected=true]{background:rgba(var(--v-ac-rgb, 92,200,255),.18);border-color:var(--ac);color:#fff}
          .chip{display:inline-block;padding:2px 8px;border-radius:6px;border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.3);
            background:rgba(var(--v-ac-rgb, 92,200,255),.08);font:11.5px 'JetBrains Mono',monospace;color:var(--v-text7, #bfe6ff);overflow-wrap:anywhere}
          .chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0}
          ol.steps{list-style:none;margin:6px 0 0;padding:0;counter-reset:s}
          ol.steps>li{position:relative;margin:0 0 10px;padding-left:30px;counter-increment:s;min-width:0}
          ol.steps>li::before{content:counter(s);position:absolute;left:0;top:1px;width:21px;height:21px;border-radius:99px;
            border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.5);color:var(--ac);font:600 10.5px/19px 'JetBrains Mono',monospace;text-align:center}
          ol.steps>li>.code{margin-left:-30px}
          .code{display:flex;align-items:stretch;margin:7px 0 0;border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.22);border-radius:10px;
            background:rgba(var(--v-deep-rgb, 5,14,30),.8);overflow:hidden;min-width:0}
          .code pre{flex:1;min-width:0;margin:0;padding:11px 12px;overflow-x:auto;white-space:pre;
            font-size:12px;line-height:1.5;color:var(--v-text7, #bfe6ff);scrollbar-width:thin}
          .copy{flex:none;min-width:72px;border:0;border-left:1px solid rgba(var(--v-ac-rgb, 92,200,255),.22);background:rgba(var(--v-ac-rgb, 92,200,255),.08);
            color:var(--ac);font:600 10px/1 'JetBrains Mono',monospace;letter-spacing:.16em;text-transform:uppercase}
          .copy:hover{background:rgba(var(--v-ac-rgb, 92,200,255),.2);color:#fff}.copy.done{color:var(--ok);background:rgba(127,214,168,.12)}
          .note{margin:8px 0 0;font-size:12.5px;line-height:1.5;color:var(--muted)}.note.warn{color:#ffd29a}
          .note.warn::before,.note.info::before{font:600 10px 'JetBrains Mono',monospace;letter-spacing:.18em;margin-right:8px}
          .note.warn::before{content:'HEADS UP';color:var(--warn)}.note.info::before{content:'NOTE';color:var(--ac)}
          .kv{display:grid;grid-template-columns:96px minmax(0,1fr);gap:10px 12px;align-items:start;margin:4px 0 12px}
          .kv dt{font:600 10px/1.9 'JetBrains Mono',monospace;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
          .kv dd{margin:0;min-width:0}
          .field{margin:0 0 12px}
          .lbl{display:block;margin:0 0 5px;font:600 10px/1.3 'JetBrains Mono',monospace;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
          input,select{display:block;width:100%;min-height:44px;padding:0 12px;border-radius:10px;border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.3);
            background:rgba(var(--v-deep-rgb, 5,14,30),.8);color:var(--text);font:14px 'Space Grotesk',sans-serif}
          input::placeholder{color:rgba(var(--v-mute-rgb, 130,166,212),.7)}
          input:focus-visible,select:focus-visible{outline:2px solid var(--ac);outline-offset:1px;border-color:var(--ac)}
          input[aria-invalid=true]{border-color:rgba(255,155,155,.7)}
          select{text-overflow:ellipsis}
          input[readonly],select[disabled]{opacity:.6}
          .wait{display:flex;align-items:center;min-height:44px;color:var(--muted)}
          .ext,.linkbtn{display:inline-flex;align-items:center;min-height:40px}
          .linkbtn{padding:0;border:0;background:none;color:var(--ac);text-decoration:underline}
          .spin{display:inline-block;width:11px;height:11px;margin-right:7px;border-radius:50%;border:2px solid rgba(var(--v-ac-rgb, 92,200,255),.3);
            border-top-color:var(--ac);vertical-align:-1px;animation:spin .8s linear infinite}
          @keyframes spin{to{transform:rotate(360deg)}}
          h3{margin:16px 0 4px;font:600 10px/1.3 'JetBrains Mono',monospace;letter-spacing:.2em;text-transform:uppercase;color:var(--ac)}
          kbd{padding:1px 6px;border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.4);border-bottom-width:2px;border-radius:5px;
            background:rgba(var(--v-deep-rgb, 5,14,30),.8);font:11px 'JetBrains Mono',monospace;color:var(--v-text7, #bfe6ff)}
          .pill{position:fixed;left:50%;bottom:calc(92px + env(safe-area-inset-bottom));transform:translateX(-50%);pointer-events:auto;
            display:flex;align-items:center;max-width:calc(100vw - 24px);border:1px solid rgba(var(--v-ac-rgb, 92,200,255),.5);border-radius:99px;
            background:rgba(var(--v-panel3-rgb, 10,26,54),.94);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
            box-shadow:0 14px 40px rgba(0,0,0,.55)}
          .pill button{min-height:42px;border:0;background:transparent;font:600 10.5px/1 'JetBrains Mono',monospace;
            letter-spacing:.14em;text-transform:uppercase;white-space:nowrap}
          .pill .resume{padding:0 8px 0 18px;color:var(--ac);border-radius:99px 0 0 99px}.pill .resume:hover{color:#fff}
          .pill .end{width:42px;color:var(--muted);border-radius:0 99px 99px 0}.pill .end:hover{color:#fff}
          .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
          @media(min-width:701px){.tabs{flex-wrap:wrap;overflow:visible}}
          @media(max-width:700px){
            .card{left:8px!important;right:8px;top:auto!important;bottom:calc(8px + env(safe-area-inset-bottom));width:auto;max-width:none;
              transform:none!important;max-height:80vh;max-height:80dvh}
            .card.dock-top{top:calc(8px + env(safe-area-inset-top))!important;bottom:auto}
            .head{padding:14px 8px 4px 16px}.body{padding:4px 16px 16px}h2{font-size:18px}
            .dots{display:none}.prog{flex-direction:row;justify-content:center}
            .foot{padding:10px 12px}.btn{padding:0 16px}.kv{grid-template-columns:1fr;gap:2px 0}.kv dd{margin-bottom:8px}
            input,select{font-size:16px}
          }
          @media(max-width:520px){.routes{grid-template-columns:repeat(2,minmax(0,1fr))}}
          @media(max-width:380px){.btn{padding:0 12px;letter-spacing:.1em}.route small{font-size:11px}}
          /* Persona looks, matching the dashboard's: Cortana's hologram glow and scanlines, Ultron's clipped chrome-edged panels */
          :host([persona=cortana]) .card{background:repeating-linear-gradient(0deg,rgba(var(--v-ac-rgb, 74,148,255),.045) 0 1px,transparent 1px 4px),rgba(var(--v-panel3-rgb, 10,26,54),.94);
            box-shadow:0 30px 90px rgba(0,0,0,.65),0 0 48px rgba(var(--v-glow, 64,140,255),.24),inset 0 1px 0 rgba(255,255,255,.08)}
          :host([persona=cortana]) h2{background:linear-gradient(90deg,var(--v-hi, #a9cdff),var(--v-data, #7fe3ff));-webkit-background-clip:text;background-clip:text;color:transparent}
          :host([persona=cortana]) .kicker{color:var(--v-data, #7fe3ff)}
          :host([persona=ultron]) .card{border-radius:3px;border-top-color:rgba(230,233,238,.5);
            clip-path:polygon(0 0,calc(100% - 18px) 0,100% 18px,100% 100%,18px 100%,0 calc(100% - 18px))}
          :host([persona=ultron]) h2{background:linear-gradient(180deg,#f6f8fa 0%,#a3aab4 52%,#e9ecef 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
          :host([persona=ultron]) :is(.ring,.btn,.route,.tab,.pick,input,select,.code,.lead,.callout,.close,.chip,kbd,code.k,.pill,.pill button){border-radius:3px}
          :host([persona=ultron]) .dots i{border-radius:1px}
          :host([persona=ultron]) ol.steps>li::before{border-radius:2px}
          @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
        </style>
        <div class="layer" hidden>
          <div class="shield" data-act="backdrop"></div>
          <div class="ring" hidden></div>
          <section class="card" role="dialog" aria-modal="true" aria-label="Guided setup tour" tabindex="-1">
            <header class="head"><div><div class="kicker"></div><h2></h2></div>
              <button type="button" class="close" data-act="close" aria-label="Close tour">✕</button></header>
            <div class="body"></div>
            <footer class="foot">
              <button type="button" class="btn" data-act="back" data-fk="back">Back</button>
              <div class="prog"><div class="dots" aria-hidden="true"></div><span class="count"></span></div>
              <button type="button" class="btn primary" data-act="next" data-fk="next">Next</button>
            </footer>
            <div class="sr" role="status" aria-live="polite"></div>
          </section>
        </div>
        <div class="pill" hidden><button type="button" class="resume" data-act="resume">▸ Resume tour</button>
          <button type="button" class="end" data-act="close" aria-label="End tour">✕</button></div>`;
      this.layer = this.$('.layer'); this.shield = this.$('.shield'); this.ring = this.$('.ring');
      this.card = this.$('.card'); this.bodyEl = this.$('.body'); this.pill = this.$('.pill');
      this.shadowRoot.addEventListener('click', event => this.click(event));
      this.shadowRoot.addEventListener('change', event => {
        const fk = event.target.dataset?.fk;
        if (fk === 'model') this.free.selected = event.target.value;
        else if (fk === 'key-more') { this.key = { ...freshKey(), pid: event.target.value }; this.renderBody(false); }
        else if (fk === 'local-model') this.local.model = event.target.value;
        else if (fk?.startsWith('el-voice-')) { this.voice.chosen = { ...this.voice.chosen, [fk.slice(9)]: event.target.value }; this.voice.saved = false; }
      });
      this.shadowRoot.addEventListener('input', event => {
        if (event.target.dataset?.fk !== 'key' || !this.free.error) return;
        this.free.error = null; this.paintFree();
      });
    }
    $(selector) { return this.shadowRoot.querySelector(selector); }
    connectedCallback() {
      window.addEventListener('vesper:tour', this.onOpen);
      this.syncPersona();
      this.personaWatch = new MutationObserver(() => this.syncPersona());
      this.personaWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['data-persona'] });
    }
    disconnectedCallback() { window.removeEventListener('vesper:tour', this.onOpen); this.personaWatch?.disconnect(); this.teardown(); }
    // A persona switch while the guide is open restyles it and renames the assistant in place.
    syncPersona() {
      const id = personaId();
      if (this.getAttribute('persona') === id) return;
      this.setAttribute('persona', id);
      if (this.isOpen && this.bodyEl) { this.$('h2').textContent = titleOf(STEPS[this.i]); this.renderBody(false); }
    }

    // ---- lifecycle -------------------------------------------------------------------------
    open(detail = {}) {
      try {
        const alias = ALIASES[detail.step];
        const wanted = alias ? alias[0] : detail.step;
        const idx = wanted ? stepIndex(wanted) : -1;
        const voiceTab = alias?.[0] === 'voice' ? alias[1] : null;
        if (alias?.[1] && !voiceTab) this.route = alias[1];
        if (detail.reason === 'no-model') this.reason = 'no-model';
        if (this.isOpen) { if (voiceTab) this.voice.tab = voiceTab; if (idx >= 0) { this.resume(); this.show(idx); } return; }
        this.reason = detail.reason === 'no-model' ? 'no-model' : 'manual';
        if (!alias?.[1] || voiceTab) this.route = 'free';
        this.free = freshFree(); this.prov = freshProv(); this.key = freshKey(); this.local = freshLocal();
        this.stopVoice(); this.voice = freshVoice(); if (voiceTab) this.voice.tab = voiceTab;
        this.stopTelegram(); this.tg = freshTelegram();
        this.stopLogin(); this.login = freshLogin(); this.cancelClaude(); this.claude = freshClaude(); this.choice = null;
        const active = document.activeElement;
        this.returnFocus = active && active !== document.body && active !== this ? active : null;
        this.isOpen = true; this.peek = false;
        this.setAttribute('open', ''); this.layer.hidden = false; this.pill.hidden = true;
        window.addEventListener('keydown', this.onKey, true);
        window.addEventListener('resize', this.onReflow);
        window.addEventListener('scroll', this.onReflow, true);
        document.addEventListener('focusin', this.onFocusIn);
        if (window.ResizeObserver) { this.ro = new ResizeObserver(this.onReflow); this.ro.observe(this.card); }
        this.timer = setInterval(() => this.tick(), 500);
        this.show(idx >= 0 ? idx : (this.reason === 'no-model' ? stepIndex('connect') : 0));
        this.focusCard();
      } catch (error) { console.error('vesper-tour:', error); }
    }
    close(finished = false) {
      if (!this.isOpen) return;
      const step = STEPS[this.i]?.id;
      this.shadowRoot.querySelectorAll('input[type=password]').forEach(input => { input.value = ''; });
      this.kept = {};
      this.stopLogin(true);
      this.cancelClaude();
      this.stopVoice(); this.stopTelegram();
      this.teardown();
      this.isOpen = false; this.peek = false;
      this.removeAttribute('open'); this.layer.hidden = true; this.pill.hidden = true; this.ring.hidden = true;
      store.set(DONE_KEY, '1');
      const back = this.returnFocus && this.returnFocus.isConnected ? this.returnFocus : document.querySelector('[data-tour="guide"]');
      this.returnFocus = null;
      try { back?.focus?.({ preventScroll: true }); } catch { /* focus target may be gone */ }
      window.dispatchEvent(new CustomEvent('vesper:tour-closed', { detail: { finished, step } }));
    }
    teardown() {
      window.removeEventListener('keydown', this.onKey, true);
      window.removeEventListener('resize', this.onReflow);
      window.removeEventListener('scroll', this.onReflow, true);
      document.removeEventListener('focusin', this.onFocusIn);
      document.removeEventListener('close', this.onDialogClose, true);
      this.ro?.disconnect(); this.ro = null;
      clearInterval(this.timer); this.timer = null; cancelAnimationFrame(this.raf);
    }
    focusCard() { try { this.card.focus({ preventScroll: true }); } catch { /* ignore */ } }
    announce(text) { const sr = this.$('.sr'); sr.textContent = ''; setTimeout(() => { sr.textContent = text; }, 30); }

    // Hide the tour so the user can use something it just opened; the pill (or the dialog closing) brings it back.
    suspend() {
      this.peek = true; this.layer.hidden = true; this.pill.hidden = false;
      this.$('.resume').textContent = `▸ Resume tour · ${this.i + 1} / ${STEPS.length}`;
      document.addEventListener('close', this.onDialogClose, true);
    }
    resume() {
      if (!this.peek) return;
      this.peek = false; this.layer.hidden = false; this.pill.hidden = true;
      document.removeEventListener('close', this.onDialogClose, true);
      this.place(); this.focusCard();
    }

    // ---- navigation ------------------------------------------------------------------------
    next() { if (this.i >= STEPS.length - 1) this.close(true); else this.show(this.i + 1); }
    prev() { if (this.i > 0) this.show(this.i - 1); }
    show(idx) {
      this.i = clamp(idx, 0, STEPS.length - 1);
      const step = STEPS[this.i];
      this.$('.kicker').textContent = step.kicker; this.$('h2').textContent = titleOf(step);
      this.$('[data-act=back]').disabled = this.i === 0;
      this.$('[data-act=next]').textContent = step.next || 'Next';
      this.$('.count').textContent = `${this.i + 1} / ${STEPS.length}`;
      this.$('.count').setAttribute('aria-label', `Step ${this.i + 1} of ${STEPS.length}`);
      this.$('.dots').innerHTML = STEPS.map((_, n) => `<i class="${n === this.i ? 'on' : n < this.i ? 'past' : ''}"></i>`).join('');
      this.renderBody(true);
      this.card.classList.remove('enter'); void this.card.offsetWidth; this.card.classList.add('enter');
      this.revealTarget();
      this.place();
      this.announce(`Step ${this.i + 1} of ${STEPS.length}: ${titleOf(step)}`);
      if (step.id === 'hermes' && !this.hermes.result && !this.hermes.busy) this.testHermes();
      if (step.id === 'connect') this.loadRoute();
      if (step.id === 'voice') this.loadVoice();
      if (step.id === 'telegram') this.loadTelegram();
    }
    loadRoute() {
      if (this.route === 'free') this.loadFree();
      else if (this.route === 'local') this.scanLocal();
      else this.loadProviders();
    }
    revealTarget() {
      const found = this.resolveTarget(STEPS[this.i]);
      if (found) return;
      for (const name of STEPS[this.i].targets || []) {
        const el = document.querySelector(`[data-tour="${name}"]`);
        try { if (el && el.getClientRects().length) el.scrollIntoView({ block: 'center', inline: 'center' }); } catch { /* ignore */ }
      }
    }

    // ---- spotlight and placement -----------------------------------------------------------
    resolveTarget(step) {
      for (const name of step.targets || []) {
        try {
          const el = document.querySelector(`[data-tour="${name}"]`);
          if (!el) continue;
          const rect = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          if (rect.width < 2 || rect.height < 2 || style.display === 'none' || style.visibility === 'hidden') continue;
          if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
          return { el, rect, radius: parseFloat(style.borderTopLeftRadius) || 0 };
        } catch { /* a bad target must never break the tour */ }
      }
      return null;
    }
    schedulePlace() { cancelAnimationFrame(this.raf); this.raf = requestAnimationFrame(() => this.place()); }
    tick() {
      if (!this.isOpen || this.peek) return;
      const t = this.resolveTarget(STEPS[this.i]);
      const sig = t ? [t.el, t.rect.left, t.rect.top, t.rect.width, t.rect.height, innerWidth, innerHeight].join('|') : 'none|' + innerWidth + innerHeight;
      if (sig !== this.sig) this.place();
    }
    place() {
      if (!this.isOpen || this.peek) return;
      try {
        const card = this.card, ring = this.ring;
        const target = this.resolveTarget(STEPS[this.i]);
        this.sig = target ? [target.el, target.rect.left, target.rect.top, target.rect.width, target.rect.height, innerWidth, innerHeight].join('|') : 'none|' + innerWidth + innerHeight;
        card.classList.remove('center', 'dock-top'); card.style.left = card.style.top = ''; card.style.maxHeight = '';
        this.shield.classList.toggle('spot', !!target);
        ring.hidden = !target;
        const mobile = this.mq.matches;
        if (!target) { if (!mobile) card.classList.add('center'); return; }
        const pad = 6, r = target.rect;
        const box = { l: r.left - pad, t: r.top - pad, r: r.right + pad, b: r.bottom + pad };
        const w = box.r - box.l, h = box.b - box.t;
        Object.assign(ring.style, { left: box.l + 'px', top: box.t + 'px', width: w + 'px', height: h + 'px',
          borderRadius: clamp(target.radius + pad, 10, 999) + 'px' });
        if (mobile) { card.classList.toggle('dock-top', r.top + r.height / 2 > innerHeight / 2); return; }
        const vw = innerWidth, vh = innerHeight, M = 12, G = 16;
        const cw = Math.min(560, vw - 32);
        card.style.maxHeight = (vh - 2 * M) + 'px';
        const nat = card.offsetHeight;
        const tall = h > w * 1.8;
        const spots = {
          below: () => ({ room: vh - box.b - G - M, x: clamp(box.l + w / 2 - cw / 2, M, vw - cw - M), y: () => box.b + G }),
          above: () => ({ room: box.t - G - M, x: clamp(box.l + w / 2 - cw / 2, M, vw - cw - M), y: hh => box.t - G - hh }),
          right: () => ({ room: vh - 2 * M, side: vw - box.r - G - M, x: box.r + G, y: hh => clamp(box.t + h / 2 - hh / 2, M, vh - hh - M) }),
          left: () => ({ room: vh - 2 * M, side: box.l - G - M, x: box.l - G - cw, y: hh => clamp(box.t + h / 2 - hh / 2, M, vh - hh - M) })
        };
        // Put the card where it covers the least: below top-bar controls, above bottom-bar ones,
        // beside anything on a side rail.
        const cx = box.l + w / 2, cy = box.t + h / 2;
        const order = cy < vh * 0.18 ? ['below', 'above', 'right', 'left']
          : cy > vh * 0.82 ? ['above', 'below', 'right', 'left']
          : tall || cx < vw * 0.3 ? ['right', 'left', 'below', 'above']
          : cx > vw * 0.7 ? ['left', 'right', 'below', 'above'] : ['below', 'above', 'right', 'left'];
        const fits = (s, need) => (s.side === undefined ? s.room >= need : s.side >= cw);
        let pick = null;
        for (const pass of [nat, 240]) {
          for (const name of order) { const s = spots[name](); if (fits(s, pass)) { pick = s; break; } }
          if (pick) break;
        }
        if (!pick) { card.classList.add('center'); return; }
        card.style.maxHeight = pick.room + 'px';
        const used = Math.min(nat, pick.room);
        card.style.left = pick.x + 'px'; card.style.top = pick.y(used) + 'px';
      } catch (error) { console.error('vesper-tour place:', error); this.ring.hidden = true; this.card.classList.add('center'); }
    }

    // ---- input -----------------------------------------------------------------------------
    keydown(event) {
      if (!this.isOpen || this.peek) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.close(); return; }
      if (event.key === 'Tab') { this.trap(event); return; }
      const active = this.shadowRoot.activeElement;
      const submit = { key: () => this.connectFree(), 'cloud-key': () => this.connectKey(), 'local-url': () => this.checkLocal(), 'claude-code': () => this.submitClaude(),
        'el-key': () => this.connectEleven(), 'hf-token': () => this.startCloning(), 'tg-token': () => this.checkTelegram() }[active?.dataset?.fk];
      if (event.key === 'Enter' && submit && !event.isComposing) {
        event.preventDefault(); event.stopPropagation(); submit(); return;
      }
      if ((event.key === 'ArrowRight' || event.key === 'ArrowLeft') && !event.altKey && !event.ctrlKey && !event.metaKey) {
        if (/^(INPUT|SELECT|TEXTAREA)$/.test(active?.tagName || '')) return;   // the arrows belong to the field
        const dir = event.key === 'ArrowRight' ? 1 : -1;
        event.preventDefault(); event.stopPropagation();
        if (active?.getAttribute('role') === 'tab') this.cycleTab(active.parentElement.dataset.tabs, dir);
        else if (dir > 0) this.next(); else this.prev();
      }
    }
    // Arrow keys move between the tabs of the focused strip (connect routes or cloud providers).
    cycleTab(kind, dir) {
      const route = kind === 'route', list = route ? ROUTES : kind === 'voice' ? VOICE_TABS : this.keyTabs(), n = list.length;
      if (!n) return;
      const at = list.findIndex(x => x.id === (route ? this.route : kind === 'voice' ? this.voice.tab : this.keyTab()));
      this.selectTab(kind, list[(at + dir + n) % n].id);
      this.$(`[data-tabs=${kind}] [aria-selected=true]`)?.focus();
    }
    selectTab(kind, id) {
      if (kind === 'route') { this.route = id; this.free.error = null; this.renderBody(false); this.loadRoute(); return; }
      if (kind === 'voice') { this.voice.tab = id; this.renderBody(false); if (id === 'eleven') this.loadVoices(); return; }
      const more = this.keyProviders().filter(p => !p.featured);
      this.key = { ...freshKey(), pid: id === 'more' ? (more[0]?.id || '') : id };
      this.renderBody(false);
    }
    trap(event) {
      const nodes = [...this.card.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href],[tabindex="0"]')].filter(n => n.getClientRects().length);
      if (!nodes.length) { event.preventDefault(); return; }
      const first = nodes[0], last = nodes[nodes.length - 1], active = this.shadowRoot.activeElement;
      if (event.shiftKey && (!active || active === first || active === this.card)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (!active || active === last)) { event.preventDefault(); first.focus(); }
    }
    click(event) {
      const el = event.target.closest('[data-act]');
      if (!el) return;
      const act = el.dataset.act;
      this.lastFk = el.dataset.fk || '';
      try {
        if (act === 'close' || act === 'backdrop') this.close();
        else if (act === 'next') this.next();
        else if (act === 'back') this.prev();
        else if (act === 'resume') this.resume();
        else if (act === 'copy') this.copy(el.dataset.copy || '', el);
        else if (act === 'hermes-test') this.testHermes();
        else if (act === 'route') this.selectTab('route', el.dataset.id);
        else if (act === 'provider') this.selectTab('provider', el.dataset.id);
        else if (act === 'providers-reload') { this.prov = freshProv(); this.loadProviders(); }
        else if (act === 'key-connect') this.connectKey();
        else if (act === 'key-again') { this.key = { ...freshKey(), pid: this.key.pid }; this.renderBody(false); }
        else if (act === 'login-start') this.startLogin(el.dataset.id);
        else if (act === 'claude-start') this.startClaude();
        else if (act === 'pick-login') this.pickLogin(el.dataset.id);
        else if (act === 'choice-use') this.useConnected();
        else if (act === 'choice-again') { const id = this.choice?.pid; this.choice = null; if (id === 'claude') this.startClaude(); else this.startLogin(id); }
        else if (act === 'choice-back') { this.choice = null; this.renderBody(false); }
        else if (act === 'claude-submit') this.submitClaude();
        else if (act === 'claude-cancel') { this.cancelClaude(); this.claude = freshClaude(); this.renderBody(false); }
        else if (act === 'login-cancel') { this.stopLogin(true); this.login = freshLogin(); this.renderBody(false); }
        else if (act === 'local-scan') { this.local = freshLocal(); this.scanLocal(); }
        else if (act === 'local-use') this.useLocal(el.dataset.url);
        else if (act === 'local-check') this.checkLocal();
        else if (act === 'local-connect') this.connectLocal();
        else if (act === 'local-other') { this.local.url = ''; this.local.models = null; this.local.error = null; this.renderBody(false); this.$('[data-fk=local-url]')?.focus(); }
        else if (act === 'model-picker') { ui('model-picker'); this.suspend(); }
        else if (act === 'refresh') this.refreshModels(el);
        else if (act === 'free-connect') this.connectFree();
        else if (act === 'free-reload') { this.free.loaded = false; this.loadFree(); }
        else if (act === 'voice-tab') this.selectTab('voice', el.dataset.id);
        else if (act === 'voice-reload') { this.voice.data = null; this.voice.error = null; this.loadVoice(); }
        else if (act === 'hf-download') this.startCloning();
        else if (act === 'el-connect') this.connectEleven();
        else if (act === 'el-newkey') { this.voice.newKey = true; this.voice.elError = null; this.renderBody(false); this.$('[data-fk=el-key]')?.focus(); }
        else if (act === 'el-save') this.saveVoices();
        else if (act === 'el-use') { ui('voice-engine', { engine: 'elevenlabs' }); this.voice.using = true; this.renderBody(false); }
        else if (act === 'tg-check') this.checkTelegram();
        else if (act === 'tg-pick') this.connectTelegram(el.dataset.id);
        else if (act === 'tg-again') this.watchTelegram(true);
        else if (act === 'tg-back') { this.stopTelegram(); this.tg = { ...freshTelegram(), status: this.tg.status, replacing: this.tg.replacing }; this.renderBody(false); }
        else if (act === 'tg-replace') { this.tg.replacing = true; this.tg.note = ''; this.renderBody(false); this.$('[data-fk=tg-token]')?.focus(); }
        else if (act === 'tg-reload') { this.tg = freshTelegram(); this.loadTelegram(); }
      } catch (error) { console.error('vesper-tour action:', error); }
    }

    // ---- clipboard -------------------------------------------------------------------------
    async copy(text, button) {
      let ok = false;
      try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; } } catch { /* fall back below */ }
      if (!ok) ok = this.legacyCopy(text, button);
      const original = button.dataset.label || (button.dataset.label = button.textContent);
      button.textContent = ok ? 'Copied' : 'Failed';
      button.classList.toggle('done', ok);
      this.announce(ok ? 'Copied to clipboard' : 'Copy is not available here. Select the text and copy it manually.');
      clearTimeout(button._t);
      button._t = setTimeout(() => { button.textContent = original; button.classList.remove('done'); }, 1700);
    }
    // For plain-http LAN pages, where navigator.clipboard is undefined.
    legacyCopy(text, button) {
      const attempt = host => {
        const area = document.createElement('textarea');
        area.value = text; area.setAttribute('readonly', ''); area.setAttribute('aria-hidden', 'true');
        area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0;';
        host.appendChild(area);
        let ok = false;
        try {
          this.copying = true; area.focus({ preventScroll: true }); area.select(); area.setSelectionRange(0, text.length);
          ok = document.execCommand('copy');
        } catch { ok = false; }
        area.remove(); this.copying = false;
        return ok;
      };
      const ok = attempt(this.shadowRoot) || attempt(document.body);
      try { button.focus({ preventScroll: true }); } catch { /* ignore */ }
      return ok;
    }

    // ---- free setup (OpenRouter) -----------------------------------------------------------
    // Loads the free model list and whether a key is already saved. A failed list is not fatal:
    // the backend picks its recommended model when none is sent.
    async loadFree() {
      const f = this.free;
      if (f.loading || f.loaded || f.result) return;
      f.loading = true; f.listError = false; this.paintFree();
      const [list, status] = await Promise.all([api('/vesper/setup/openrouter-free/models'), api('/vesper/setup/status')]);
      if (f !== this.free) return;
      f.loading = false; f.loaded = true;
      f.keySet = !!status.data?.openrouter_key_set;
      const models = Array.isArray(list.data?.models) ? list.data.models.filter(m => m && m.id) : [];
      if (list.ok && models.length) {
        f.models = models;
        f.recommended = models.some(m => m.id === list.data.recommended) ? list.data.recommended : models[0].id;
        if (!models.some(m => m.id === f.selected)) f.selected = f.recommended;
      } else { f.models = []; f.listError = true; }
      this.paintFree();
    }
    async connectFree() {
      const f = this.free;
      if (f.busy || f.result) return;
      const input = this.$('[data-fk=key]');
      let key = (input?.value || '').trim();
      if (!key && !f.keySet) {
        f.error = { code: 'missing_key', message: 'Paste your OpenRouter key first.' };
        this.paintFree(); input?.focus(); return;
      }
      const body = {};
      if (key) body.api_key = key;
      if (f.selected) body.model = f.selected;
      f.busy = true; f.error = null; this.paintFree();
      const res = await api('/vesper/setup/openrouter-free', body, 45000);
      key = ''; body.api_key = undefined;
      f.busy = false;
      const data = res.data;
      if (res.ok && data?.ok && data.model) {
        if (input) input.value = '';
        f.keySet = true;
        f.result = { model: String(data.model), provider: String(data.provider || 'openrouter'), limits: String(data.limits || '') };
        ui('refresh-models');
        ui('setup-complete', { model: f.result.model, provider: f.result.provider });
        if (f === this.free && this.isOpen) {
          this.announce(`Connected: ${f.result.model}`);
          this.paintFree();
          this.$('[data-act=next]')?.focus({ preventScroll: true });
        }
        return;
      }
      const code = res.network ? 'network' : (data?.error?.code || '');
      const message = res.network ? 'Check your connection and try again.'
        : res.status === 401 ? 'The dashboard key was rejected. Reload the page and try again.'
        : (typeof data?.error?.message === 'string' && data.error.message) || `Something went wrong (HTTP ${res.status}).`;
      f.error = { code, message };
      // A rejected key is dropped; a transient failure keeps it so Retry needs no re-typing.
      if (code === 'invalid_key' && input) input.value = '';
      if (code === 'invalid_model') { f.selected = ''; f.loaded = false; this.loadFree(); }
      if (f === this.free && this.isOpen) {
        this.paintFree();
        this.announce(`${FREE_ERRORS[code] || 'Could not connect.'} ${message}`);
        (['invalid_key', 'missing_key'].includes(code) ? this.$('[data-fk=key]') : this.$('[data-free=go]'))?.focus({ preventScroll: true });
      }
    }
    // Updates the free form in place so a key being typed is never wiped by a re-render.
    paintFree() {
      const f = this.free;
      if (!this.isOpen || STEPS[this.i].id !== 'connect' || this.route !== 'free') return;
      if (f.result) { this.renderBody(false); return; }
      const set = (selector, html) => { const el = this.$(selector); if (el) el.innerHTML = html; };
      set('[data-free=models]', this.freeModelsHtml());
      set('[data-free=msg]', this.freeMsgHtml());
      if (f.error) this.$('[data-free=msg]')?.scrollIntoView({ block: 'nearest' });
      const go = this.$('[data-free=go]');
      if (go) { go.disabled = f.busy; go.setAttribute('aria-busy', String(f.busy)); go.innerHTML = this.freeGoHtml(); }
      const label = this.$('[data-free=keylabel]'); if (label) label.textContent = this.freeKeyLabel();
      const key = this.$('[data-fk=key]');
      if (key) {
        key.readOnly = f.busy; key.placeholder = this.freeKeyHint();
        if (['invalid_key', 'missing_key'].includes(f.error?.code)) key.setAttribute('aria-invalid', 'true'); else key.removeAttribute('aria-invalid');
      }
      this.schedulePlace();
    }
    refreshModels(button) {
      ui('refresh-models');
      const original = button.dataset.label || (button.dataset.label = button.textContent);
      button.textContent = 'Requested';
      this.announce('Asked the dashboard to refresh its model list.');
      clearTimeout(button._t);
      button._t = setTimeout(() => { button.textContent = original; }, 1700);
    }

    // ---- rendering -------------------------------------------------------------------------
    renderBody(reset) {
      const step = STEPS[this.i];
      const active = this.shadowRoot.activeElement;
      const focusKey = active?.dataset?.fk || (!active || active === this.card ? this.lastFk : '');
      const scroll = reset ? 0 : this.bodyEl.scrollTop;
      // Typed values (a key being pasted, an address) survive the re-render a status change causes.
      this.kept = this.kept || {};
      this.bodyEl.querySelectorAll('input[data-keep]').forEach(input => { this.kept[input.dataset.fk] = input.value; });
      this.bodyEl.innerHTML = this['body_' + step.id]();
      this.bodyEl.querySelectorAll('input[data-keep]').forEach(input => { input.value = this.kept[input.dataset.fk] || ''; });
      this.bodyEl.scrollTop = scroll;
      const tab = this.$('.tabs .tab[aria-selected=true]'), strip = tab?.parentElement;
      if (strip && strip.scrollWidth > strip.clientWidth) {
        const t = tab.getBoundingClientRect(), b = strip.getBoundingClientRect();
        if (t.right > b.right) strip.scrollLeft += t.right - b.right + 8; else if (t.left < b.left) strip.scrollLeft -= b.left - t.left + 8;
      }
      if (focusKey) this.$(`[data-fk="${focusKey}"]`)?.focus({ preventScroll: true });
      this.schedulePlace();
    }
    body_welcome() {
      return `<p>${who()} chats, remembers, uses tools and runs scheduled tasks. To think, it needs a <b>model</b>: the AI that does the thinking.</p>
        <p>This guide connects one for you, then helps you pick a voice and reach ${who()} from Telegram, if you like. There's nothing to install or type in a terminal, and the model takes about two minutes.</p>
        <p class="muted">Reopen it any time from the GUIDE button.</p>`;
    }

    body_hermes() {
      const h = this.hermes, st = h.state, r = h.result;
      const where = !st ? `${who()} runs on <b>Hermes Agent</b>, Nous Research's open-source agent: it does the thinking, remembering and tool use.`
        : st.mode === 'managed' ? `${who()} runs on <b>Hermes Agent</b> ${st.hermes?.version ? `${escape(st.hermes.version)} ` : ''}that JARVIS installed in this container${st.updates?.auto ? ' and keeps updated' : ''}.`
        : `${who()} runs on <b>your own Hermes Agent</b> at <code class="k">${escape(st.hermes?.url || '')}</code>.`;
      const rows = (r?.checks || []).map(c => {
        const cls = c.ok === true ? 'ok' : c.ok === false ? 'no' : 'info';
        return `<li class="${cls}"><span class="mk">${c.ok === true ? '✓' : c.ok === false ? '✗' : '•'}</span><span>${escape(c.label)}${c.detail ? ` <small class="muted">${escape(c.detail)}</small>` : ''}${c.hint && c.ok !== true ? `<small class="hint">${escape(c.hint)}</small>` : ''}</span></li>`;
      }).join('');
      const verdict = h.busy ? '' : !r ? '' : r.ok
        ? `<div class="callout ok" role="status"><b>The connection works.</b></div>`
        : `<div class="callout err" role="status"><b>Something is wrong with the connection.</b> The first ✗ below says what to do.</div>`;
      const settings = h.launcher ? `<a class="ext" href="/jarvis/" target="_blank" rel="noopener">Connection settings ↗</a>` : '';
      return `<p>${where}</p>${verdict}<ul class="checks">${rows}</ul>
        <div class="actions"><button type="button" class="btn${r && r.ok ? '' : ' primary'}" data-act="hermes-test" data-fk="hermes-test" ${h.busy ? 'disabled' : ''} aria-busy="${h.busy}">${h.busy ? '<span class="spin"></span>Testing…' : 'Test connection'}</button>${settings}</div>`;
    }
    // The launcher (JARVIS's container front door) runs the full check; a dashboard opened straight on a Hermes host
    // has no launcher, so then only the connector's own check is asked.
    async testHermes() {
      this.hermes = { ...this.hermes, busy: true };
      this.renderBody(false);
      const auth = { Authorization: 'Bearer ' + (document.body?.dataset?.vesperKey || '') };
      let result = null, launcher = false, st = null;
      try {
        const res = await fetch('/jarvis/api/test', { method: 'POST', cache: 'no-store',
          headers: { ...auth, 'X-JARVIS': '1', 'Content-Type': 'application/json' }, body: '{}' });
        if (res.ok) { result = await res.json(); launcher = true; }
        const sres = launcher ? await fetch('/jarvis/api/state', { cache: 'no-store' }) : null;
        if (sres?.ok) st = await sres.json();
      } catch { /* no launcher in front of this Hermes */ }
      if (!result) {
        const res = await api('/vesper/connector/check');
        const d = res.data || {};
        result = { ok: res.ok, checks: [
          { label: 'Hermes answers', ok: res.status !== 0, detail: res.status ? `HTTP ${res.status}` : 'not reachable' },
          { label: 'The JARVIS connector is loaded', ok: res.ok, detail: d.version ? `version ${d.version}` : '',
            hint: res.ok ? '' : 'Restart the Hermes gateway (hermes gateway restart).' },
          ...(d.isolation ? [{ label: 'Plugins run inside Hermes (in_process)', ok: d.isolation === 'in_process', detail: d.isolation }] : []),
          ...(d.hermes_version ? [{ label: 'Hermes version', ok: null, detail: d.hermes_version }] : [])
        ] };
      }
      this.hermes = { busy: false, result, launcher, state: st };
      if (STEPS[this.i]?.id === 'hermes') this.renderBody(false);
    }

    body_connect() {
      const lead = this.reason === 'no-model' && !this.free.result
        ? `<div class="lead"><b>No model is connected yet.</b> ${who()} cannot answer until one is.</div>` : '';
      const routes = ROUTES.map(r => `<button type="button" class="route" role="tab" id="route-${r.id}" data-act="route" data-id="${r.id}" data-fk="route-${r.id}" aria-selected="${r.id === this.route}" aria-controls="route-panel" tabindex="${r.id === this.route ? 0 : -1}"><b>${r.name}</b><small>${r.hint}</small></button>`).join('');
      const panel = { free: () => this.panelFree(), login: () => this.panelLogin(), key: () => this.panelKey(), local: () => this.panelLocal() }[this.route]();
      return `${lead}<div class="routes" role="tablist" aria-label="Ways to connect a model" data-tabs="route">${routes}</div>
        <div id="route-panel" role="tabpanel" aria-labelledby="route-${this.route}">${panel}</div>`;
    }
    panelFree() {
      const f = this.free;
      if (f.result) {
        return `<div class="callout ok" role="status"><b>Connected: ${escape(f.result.model)}</b>${f.result.limits ? `<br>${escape(f.result.limits)}` : ''}</div>`;
      }
      return `<p class="tight">OpenRouter is free to sign up for. Its free models have daily request limits.</p>
        <p><a class="ext" href="https://openrouter.ai/keys" target="_blank" rel="noopener noreferrer">Get a free key at openrouter.ai/keys ↗</a></p>
        <div class="field"><label class="lbl" for="free-key" data-free="keylabel">${this.freeKeyLabel()}</label>
          <input id="free-key" data-fk="key" type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
            placeholder="${this.freeKeyHint()}" ${f.busy ? 'readonly' : ''}></div>
        <div class="field" data-free="models">${this.freeModelsHtml()}</div>
        <div data-free="msg">${this.freeMsgHtml()}</div>
        <div class="actions"><button type="button" class="btn primary" data-act="free-connect" data-fk="free-go" data-free="go" aria-busy="${f.busy}" ${f.busy ? 'disabled' : ''}>${this.freeGoHtml()}</button></div>`;
    }
    freeKeyLabel() { return `OpenRouter key${this.free.keySet ? ' · already saved' : ''}`; }
    freeKeyHint() { return this.free.keySet ? 'Leave blank to use the saved key' : 'sk-or-…'; }
    freeGoHtml() { return this.free.busy ? '<span class="spin"></span>Connecting…' : 'Connect'; }
    freeMsgHtml() {
      const e = this.free.error;
      if (!e) return '';
      const title = FREE_ERRORS[e.code];
      return `<div class="callout err" role="alert">${title ? `<b>${escape(title)}</b> ` : ''}${escape(e.message)}</div>`;
    }
    freeModelsHtml() {
      const f = this.free;
      const label = '<label class="lbl" for="free-model">Free model</label>';
      if (f.loading) return `${label}<div class="wait" id="free-model"><span class="spin"></span>Loading models…</div>`;
      if (!f.models.length) {
        return f.listError
          ? `${label}<p class="note" id="free-model">Could not load the list, so the recommended model will be used. <button type="button" class="linkbtn" data-act="free-reload">Try again</button></p>` : label;
      }
      const options = f.models.map(m => {
        const ctx = ctxLabel(m.context_length);
        return `<option value="${escape(m.id)}"${m.id === f.selected ? ' selected' : ''}>${escape(m.name || m.id)}${ctx ? ` · ${ctx}` : ''}${m.id === f.recommended ? ' · recommended' : ''}</option>`;
      }).join('');
      return `${label}<select id="free-model" data-fk="model" ${f.busy ? 'disabled' : ''}>${options}</select>`;
    }
    // ---- shared by the sign-in, API-key and local routes -----------------------------------
    connected(result, via) {
      return `<div class="callout ok" role="status"><b>Connected: ${escape(result.model)}</b>${via ? ` · ${escape(via)}` : ''}<br>
        ${who()} uses it from your next message. You can pick another model any time with the model button.</div>`;
    }
    finish(result, via) {
      ui('refresh-models');
      ui('setup-complete', { model: result.model, provider: result.provider });
      if (!this.isOpen) return;
      this.announce(`Connected: ${result.model}`);
      this.renderBody(false);
      this.$('[data-act=next]')?.focus({ preventScroll: true });
    }
    errorHtml(error) { return error ? `<div class="callout err" role="alert">${escape(error)}</div>` : ''; }
    waitHtml(text) { return `<div class="wait" role="status"><span class="spin"></span>${escape(text)}</div>`; }
    async loadProviders() {
      const p = this.prov;
      if (p.loading || p.data) return;
      p.loading = true; p.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/providers');
      if (p !== this.prov) return;
      p.loading = false;
      if (res.ok && Array.isArray(res.data?.keys)) p.data = res.data; else p.error = problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    providersGate() {
      const p = this.prov;
      if (p.error) return `${this.errorHtml(p.error)}<div class="actions"><button type="button" class="btn small" data-act="providers-reload">Try again</button></div>`;
      if (!p.data) return this.waitHtml('Loading providers…');
      return '';
    }

    // ---- sign in (device code) ---------------------------------------------------------------
    panelLogin() {
      const L = this.login, C = this.claude, gate = this.providersGate();
      if (gate) return gate;
      const claudeReady = !!this.prov.data.claude?.installed;
      if (C.result) return this.connected(C.result, 'Claude');
      if (C.sid) return this.claudeSteps();
      if (C.installing) return this.claudeInstalling();
      if (this.choice) return this.choiceHtml();
      const pick = this.prov.data.logins.find(x => x.id === L.pid);
      if (L.result) return this.connected(L.result, pick?.name);
      if (L.start) {
        const code = escape(L.start.user_code);
        return `<p class="tight">Signing in to <b>${escape(pick?.name || '')}</b>.</p>
          <ol class="steps">
            <li><div>Open the sign-in page and log in.</div>
              <div class="actions"><a class="btn primary" href="${escape(L.start.verification_url)}" target="_blank" rel="noopener noreferrer" data-fk="login-open">Open sign-in page ↗</a></div></li>
            <li><div>If it asks for a code, enter this one:</div>
              <div class="code"><pre class="usercode" aria-label="Sign-in code">${code}</pre><button type="button" class="copy" data-act="copy" data-copy="${code}" aria-label="Copy the code">Copy</button></div></li>
            <li><div>Approve the request. This guide carries on by itself.</div></li>
          </ol>
          ${L.error ? this.errorHtml(L.error) : this.waitHtml('Waiting for you to approve…')}
          <div class="actions"><button type="button" class="btn small" data-act="login-cancel">${L.error ? 'Back' : 'Cancel'}</button></div>`;
      }
      const claudeOn = !!this.prov.data.claude?.connected;
      const claudeRow = `<button type="button" class="pick" data-act="pick-login" data-id="claude" data-fk="login-claude" ${C.busy || L.busy ? 'disabled' : ''}>
          <b>Claude</b><small>Your Claude Pro or Max plan${claudeReady || claudeOn ? '' : ' · sets up Claude Code first (about 110 MB, once)'}</small>${claudeOn ? '<span class="badge">Connected</span>' : ''}</button>`;
      const rows = claudeRow + this.prov.data.logins.map(x => `<button type="button" class="pick" data-act="pick-login" data-id="${escape(x.id)}" data-fk="login-${escape(x.id)}" ${L.busy ? 'disabled' : ''}>
          <b>${escape(x.name)}</b><small>${escape(x.uses)}</small>${x.connected ? '<span class="badge">Connected</span>' : ''}</button>`).join('');
      return `<p class="tight">Use a plan you already pay for. Pick one and sign in on its own website.</p>
        <div class="picks">${rows}</div>${L.busy || C.busy ? this.waitHtml('Starting the sign-in…') : ''}${this.errorHtml(L.error || C.error)}
`;
    }
    // A sign-in row that's already connected offers to use it as it is, instead of signing in all over again.
    pickLogin(id) {
      const d = this.prov.data;
      const row = id === 'claude' ? { name: 'Claude', connected: !!d?.claude?.connected } : d?.logins.find(x => x.id === id);
      if (!row) return;
      if (row.connected) { this.choice = { pid: id, name: row.name, busy: false, error: null, result: null }; this.renderBody(false); return; }
      if (id === 'claude') this.startClaude(); else this.startLogin(id);
    }
    choiceHtml() {
      const Ch = this.choice, name = escape(Ch.name);
      if (Ch.result) return this.connected(Ch.result, Ch.name);
      return `<div class="callout ok" role="status"><b>${name} is already connected.</b></div>
        <p>Use it now, or sign in again, for example with a different account.</p>
        ${this.errorHtml(Ch.error)}
        <div class="actions"><button type="button" class="btn primary" data-act="choice-use" data-fk="choice-use" ${Ch.busy ? 'disabled aria-busy="true"' : ''}>${Ch.busy ? '<span class="spin"></span>Switching…' : `Use ${name}`}</button>
          <button type="button" class="btn small" data-act="choice-again" data-fk="choice-again">Sign in again</button>
          <button type="button" class="btn small" data-act="choice-back" data-fk="choice-back">Back</button></div>`;
    }
    async useConnected() {
      const Ch = this.choice;
      if (!Ch || Ch.busy) return;
      Ch.busy = true; Ch.error = null; this.renderBody(false);
      const path = Ch.pid === 'claude' ? '/vesper/setup/claude/use' : `/vesper/setup/login/${encodeURIComponent(Ch.pid)}/use`;
      const res = await api(path, {}, 60000);
      if (Ch !== this.choice) return;
      Ch.busy = false;
      if (res.ok && res.data?.ok) { Ch.result = { model: res.data.model, provider: res.data.provider }; this.finish(Ch.result); return; }
      Ch.error = problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    claudeSteps() {
      const C = this.claude;
      return `<p class="tight">Signing in to <b>Claude</b> with your plan.</p>
        <ol class="steps">
          <li><div>Open Claude's sign-in page and approve the sign-in.</div>
            <div class="actions"><a class="btn primary" href="${escape(C.url)}" target="_blank" rel="noopener noreferrer" data-fk="claude-open">Open Claude sign-in ↗</a></div></li>
          <li><div><label for="claude-code">Claude then shows a code. Copy it, paste it here and press Connect.</label></div>
            <input id="claude-code" data-fk="claude-code" data-keep type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
              placeholder="Code from Claude" ${C.submitting ? 'readonly' : ''} ${C.error ? 'aria-invalid="true"' : ''}></li>
        </ol>
        ${this.errorHtml(C.error)}
        <div class="actions"><button type="button" class="btn primary" data-act="claude-submit" data-fk="claude-go" ${C.submitting ? 'disabled aria-busy="true"' : ''}>${C.submitting ? '<span class="spin"></span>Connecting…' : 'Connect'}</button>
          <button type="button" class="btn small" data-act="claude-cancel">Cancel</button></div>
        <p class="note">Uses your plan's limits. Anthropic allows apps like this one on Pro and Max plans.</p>`;
    }
    // Claude sign-in runs through Anthropic's Claude Code, which JARVIS downloads the first time it's needed.
    claudeInstalling() {
      const I = this.claude.installing, mb = n => Math.round((n || 0) / 1048576);
      const pct = I.total ? Math.min(100, Math.round(100 * I.done / I.total)) : 0;
      return `<p class="tight">Getting <b>Claude Code</b> ready, Anthropic's app that signs this dashboard in to your Claude plan. This only happens once.</p>
        <div class="bar" role="progressbar" aria-label="Downloading Claude Code" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
        ${this.waitHtml(I.total ? `Downloading… ${mb(I.done)} of ${mb(I.total)} MB` : 'Starting the download…')}
        <p class="note">The sign-in carries on by itself when it's done.</p>`;
    }
    async startClaude() {
      const C = this.claude;
      if (C.busy || C.sid || C.installing) return;
      if (!this.prov.data?.claude?.installed) {
        C.installing = { done: 0, total: 0 }; C.error = null; this.renderBody(false);
        this.watchInstall(C, await api('/vesper/setup/claude/install', {}, 30000));
        return;
      }
      C.busy = true; C.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/claude/start', {}, 60000);
      if (C !== this.claude) { if (res.data?.session_id) api(`/vesper/setup/claude/${encodeURIComponent(res.data.session_id)}`, undefined, 10000, 'DELETE'); return; }
      C.busy = false;
      if (res.ok && res.data?.url) { C.sid = res.data.session_id; C.url = res.data.url; this.announce('Open Claude sign-in, approve, then paste the code here'); }
      else C.error = problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    async submitClaude() {
      const C = this.claude;
      if (!C.sid || C.submitting) return;
      const field = this.$('[data-fk=claude-code]');
      const code = (field?.value || '').trim();
      if (!code) { C.error = 'Paste the code from Claude first.'; this.renderBody(false); this.$('[data-fk=claude-code]')?.focus(); return; }
      C.submitting = true; C.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/claude/submit', { session_id: C.sid, code }, 90000);
      if (C !== this.claude) return;
      C.submitting = false;
      const input = this.$('[data-fk=claude-code]'); if (input) input.value = '';
      this.kept['claude-code'] = '';
      if (res.ok && res.data?.ok) {
        C.sid = ''; C.result = { model: res.data.model, provider: 'anthropic' };
        if (this.prov.data?.claude) this.prov.data.claude.connected = true;
        this.finish(C.result);
        return;
      }
      // The server ends a sign-in once a code was tried: start over from the list.
      const message = problem(res);
      Object.assign(C, freshClaude(), { error: message });
      if (this.isOpen) this.renderBody(false);
    }
    watchInstall(C, res) {
      if (C !== this.claude) return;
      const st = res.data || {};
      if (res.ok && st.installed) {
        C.installing = null;
        if (this.prov.data?.claude) this.prov.data.claude.installed = true;
        this.startClaude();
        return;
      }
      if (!res.ok || st.state === 'error') {
        C.installing = null; C.error = (res.ok && st.error) || problem(res);
        if (this.isOpen) this.renderBody(false);
        return;
      }
      C.installing = { done: st.done, total: st.total };
      if (this.isOpen) this.renderBody(false);
      C.timer = setTimeout(async () => this.watchInstall(C, await api('/vesper/setup/claude/status', undefined, 15000)), 1000);
    }
    cancelClaude() {
      const C = this.claude;
      clearTimeout(C?.timer);
      if (C?.sid && !C.result) api(`/vesper/setup/claude/${encodeURIComponent(C.sid)}`, undefined, 10000, 'DELETE');
    }
    async startLogin(pid) {
      const L = this.login;
      if (L.busy || L.start) return;
      L.pid = pid; L.busy = true; L.error = null; this.renderBody(false);
      const res = await api(`/vesper/setup/login/${encodeURIComponent(pid)}/start`, {}, 30000);
      if (L !== this.login) return;
      L.busy = false;
      if (!res.ok || !res.data?.session_id) { L.error = problem(res); this.renderBody(false); return; }
      L.start = res.data;
      this.renderBody(false);
      this.announce(`Open the sign-in page and enter the code ${res.data.user_code}`);
      this.schedulePoll(L);
    }
    schedulePoll(L) {
      const wait = Math.max(2, Math.min(10, Number(L.start.poll_interval) || 5)) * 1000;
      L.timer = setTimeout(() => this.pollLogin(L), wait);
    }
    async pollLogin(L) {
      if (L !== this.login || !L.start) return;
      const res = await api(`/vesper/setup/login/${encodeURIComponent(L.pid)}/poll/${encodeURIComponent(L.start.session_id)}`, undefined, 60000);
      if (L !== this.login || !L.start) return;
      const status = res.data?.status;
      if (res.ok && status === 'approved' && res.data.model) {
        L.result = { model: res.data.model, provider: res.data.provider || L.pid };
        L.start = null;
        this.prov.data?.logins.forEach(x => { if (x.id === L.pid) x.connected = true; });
        this.finish(L.result);
        return;
      }
      if (res.ok && status === 'pending') { this.schedulePoll(L); return; }
      if (res.network) { this.schedulePoll(L); return; }   // a blip: keep waiting
      L.error = (res.ok && (res.data.error_message || LOGIN_STATUS[status])) || problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    // Stop polling; with cancel, also tell the gateway so a late approval can't complete the sign-in.
    stopLogin(cancel = false) {
      const L = this.login;
      if (!L) return;
      clearTimeout(L.timer);
      if (cancel && L.start && !L.result) api(`/vesper/setup/login/${encodeURIComponent(L.pid)}/${encodeURIComponent(L.start.session_id)}`, undefined, 10000, 'DELETE');
    }

    // ---- API key -------------------------------------------------------------------------------
    keyProviders() { return this.prov.data?.keys || []; }
    keyTabs() {
      const featured = this.keyProviders().filter(p => p.featured).map(p => ({ id: p.id, name: p.name }));
      return this.keyProviders().some(p => !p.featured) ? [...featured, { id: 'more', name: 'More' }] : featured;
    }
    keyTab() { return this.keyProviders().find(p => p.id === this.key.pid)?.featured === false ? 'more' : this.key.pid; }
    panelKey() {
      const K = this.key, gate = this.providersGate();
      if (gate) return gate;
      const all = this.keyProviders();
      const p = all.find(x => x.id === K.pid) || all[0];
      if (!p) return '<p>No providers are available.</p>';
      if (K.pid !== p.id) K.pid = p.id;
      const tab = this.keyTab();
      const tabs = this.keyTabs().map(x => `<button type="button" class="tab" role="tab" id="tab-${escape(x.id)}" data-act="provider" data-id="${escape(x.id)}" data-fk="tab-${escape(x.id)}" aria-selected="${x.id === tab}" aria-controls="provider-panel" tabindex="${x.id === tab ? 0 : -1}">${escape(x.name)}</button>`).join('');
      const more = tab === 'more'
        ? `<div class="field"><label class="lbl" for="key-more">Provider</label><select id="key-more" data-fk="key-more">${all.filter(x => !x.featured).map(x => `<option value="${escape(x.id)}"${x.id === p.id ? ' selected' : ''}>${escape(x.name)}</option>`).join('')}</select></div>` : '';
      const body = K.result ? this.connected(K.result, p.name) + '<div class="actions"><button type="button" class="btn small" data-act="key-again">Use a different key</button></div>' : `
        <ol class="steps">
          <li><div>${p.signup_url ? `Get an API key from ${escape(p.name)}: <a class="ext" href="${escape(p.signup_url)}" target="_blank" rel="noopener noreferrer">${escape(hostOf(p.signup_url))} ↗</a>` : `Get an API key from your ${escape(p.name)} account.`}</div>
            ${p.hint ? `<p class="note warn">${escape(p.hint)}</p>` : ''}</li>
          <li><div><label for="cloud-key">Paste it here and press Connect.</label></div>
            <input id="cloud-key" data-fk="cloud-key" data-keep type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
              placeholder="${escape(p.name)} API key" ${K.busy ? 'readonly' : ''} ${K.error ? 'aria-invalid="true"' : ''}></li>
        </ol>
        ${p.connected ? '<p class="note">Already connected. A new key replaces the saved one.</p>' : ''}
        ${this.errorHtml(K.error)}
        <div class="actions"><button type="button" class="btn primary" data-act="key-connect" data-fk="key-go" ${K.busy ? 'disabled aria-busy="true"' : ''}>${K.busy ? '<span class="spin"></span>Checking the key…' : 'Connect'}</button></div>
        <p class="note">The key is saved on your own server only. Providers bill you for what you use.</p>`;
      return `<div class="tabs" role="tablist" aria-label="Providers" data-tabs="provider">${tabs}</div>
        <div id="provider-panel" role="tabpanel" aria-labelledby="tab-${escape(tab)}">${more}${body}</div>`;
    }
    async connectKey() {
      const K = this.key;
      if (K.busy || K.result) return;
      const input = this.$('[data-fk=cloud-key]');
      let key = (input?.value || '').trim();
      if (!key) { K.error = 'Paste the key first.'; this.renderBody(false); this.$('[data-fk=cloud-key]')?.focus(); return; }
      K.busy = true; K.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/key', { provider: K.pid, api_key: key }, 60000);
      key = '';
      if (K !== this.key) return;
      K.busy = false;
      // renderBody re-reads kept fields from the page, so a key that's done with is cleared in the field itself.
      const field = this.$('[data-fk=cloud-key]');
      if (res.ok && res.data?.ok) {
        if (field) field.value = '';
        K.result = { model: res.data.model, provider: res.data.provider };
        this.prov.data?.keys.forEach(x => { if (x.id === K.pid) x.connected = true; });
        this.finish(K.result);
        return;
      }
      K.error = problem(res);
      if (res.data?.error?.code === 'invalid_key' && field) field.value = '';
      if (this.isOpen) { this.renderBody(false); this.$('[data-fk=cloud-key]')?.focus({ preventScroll: true }); }
    }

    // ---- local server ----------------------------------------------------------------------------
    panelLocal() {
      const L = this.local;
      if (L.result) return this.connected(L.result, hostOf(L.url));
      const intro = '<p class="tight">Runs free, private models on a computer you own, using <b>Ollama</b> or <b>LM Studio</b>.</p>';
      if (L.scanning) return intro + this.waitHtml('Looking for a model server on your network…');
      if (L.url && L.models) {
        const options = L.models.map(m => `<option value="${escape(m)}"${m === L.model ? ' selected' : ''}>${escape(m)}</option>`).join('');
        return `${intro}<div class="callout ok" role="status"><b>Found a model server</b> at ${escape(hostOf(L.url))}.</div>
          ${L.models.length ? `<div class="field"><label class="lbl" for="local-model">Model</label><select id="local-model" data-fk="local-model" ${L.busy ? 'disabled' : ''}>${options}</select></div>`
            : '<p class="note warn">It has no models yet. Download one in Ollama or LM Studio, then press Search again.</p>'}
          ${this.errorHtml(L.error)}
          <div class="actions">${L.models.length ? `<button type="button" class="btn primary" data-act="local-connect" data-fk="local-go" ${L.busy ? 'disabled aria-busy="true"' : ''}>${L.busy ? '<span class="spin"></span>Connecting…' : 'Connect'}</button>` : ''}
            <button type="button" class="btn small" data-act="local-other">Use another server</button></div>
          <p class="note">Bigger models answer better but need a powerful computer. Hermes works best with a context window of 64K or more.</p>`;
      }
      const found = L.found.length ? `<p class="tight">Found on your network:</p><div class="picks">${L.found.map(f => `<button type="button" class="pick" data-act="local-use" data-url="${escape(f.base_url)}" data-fk="local-${escape(f.base_url)}"><b>${escape(f.name)}</b><small>${escape(hostOf(f.base_url))} · ${f.models.length} model${f.models.length === 1 ? '' : 's'}</small></button>`).join('')}</div>`
        : `${L.scanned ? '<div class="callout warn"><b>No model server found yet.</b></div>' : ''}
          <ol class="steps">
            <li><div>Install <a class="ext" href="https://ollama.com/download" target="_blank" rel="noopener noreferrer">Ollama ↗</a> on a computer at home, and download a model in it.</div></li>
            <li><div>If that isn't this server itself, open Ollama's settings and turn on <b>Expose Ollama to the network</b>.</div></li>
            <li><div>Press <b>Search again</b>, or type that computer's address below.</div></li>
          </ol>`;
      return `${intro}${found}
        <div class="field"><label class="lbl" for="local-url">Server address</label>
          <input id="local-url" data-fk="local-url" data-keep type="text" inputmode="url" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="192.168.1.20 or http://192.168.1.20:11434" ${L.checking ? 'readonly' : ''}></div>
        ${this.errorHtml(L.error)}
        <div class="actions"><button type="button" class="btn primary" data-act="local-check" data-fk="local-check" ${L.checking ? 'disabled aria-busy="true"' : ''}>${L.checking ? '<span class="spin"></span>Checking…' : 'Check address'}</button>
          <button type="button" class="btn small" data-act="local-scan" data-fk="local-scan">Search again</button></div>`;
    }
    async scanLocal() {
      const L = this.local;
      if (L.scanning || L.scanned || L.result) return;
      L.scanning = true; this.renderBody(false);
      const res = await api('/vesper/setup/local/scan', undefined, 30000);
      if (L !== this.local) return;
      L.scanning = false; L.scanned = true;
      L.found = res.ok && Array.isArray(res.data?.found) ? res.data.found : [];
      if (!res.ok) L.error = problem(res);
      if (L.found.length === 1) { this.useLocal(L.found[0].base_url); return; }
      if (this.isOpen) this.renderBody(false);
    }
    useLocal(url) {
      const L = this.local, hit = L.found.find(f => f.base_url === url);
      if (!hit) return;
      L.url = hit.base_url; L.models = hit.models; L.model = hit.models[0] || ''; L.error = null;
      this.renderBody(false);
    }
    async checkLocal() {
      const L = this.local;
      if (L.checking) return;
      const address = (this.$('[data-fk=local-url]')?.value || '').trim();
      if (!address) { L.error = 'Type the address first.'; this.renderBody(false); this.$('[data-fk=local-url]')?.focus(); return; }
      L.checking = true; L.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/local/probe', { base_url: address }, 30000);
      if (L !== this.local) return;
      L.checking = false;
      if (res.ok && Array.isArray(res.data?.models)) { L.url = res.data.base_url; L.models = res.data.models; L.model = res.data.models[0] || ''; }
      else L.error = res.data?.error?.code === 'not_reachable'
        ? 'Nothing answered at that address. Check that Ollama is running there and allows network connections.' : problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    async connectLocal() {
      const L = this.local;
      if (L.busy || !L.url || !L.model) return;
      L.busy = true; L.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/local', { base_url: L.url, model: L.model }, 45000);
      if (L !== this.local) return;
      L.busy = false;
      if (res.ok && res.data?.ok) { L.result = { model: res.data.model, provider: res.data.provider }; this.finish(L.result); return; }
      L.error = problem(res);
      if (this.isOpen) this.renderBody(false);
    }

    // ---- voice ---------------------------------------------------------------------------------
    // Pocket TTS speaks on this server out of the box; its voice-cloning model (Hugging Face, gated) makes it speak in
    // the persona's own voice. ElevenLabs is the online option, with the account's voices picked per persona.
    async loadVoice() {
      const V = this.voice;
      if (V.loading || V.data) return;
      V.loading = true; V.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/voice');
      if (V !== this.voice) return;
      V.loading = false;
      if (res.ok && res.data?.pocket) V.data = res.data; else V.error = problem(res);
      if (V.data?.pocket?.download?.state === 'downloading') this.watchCloning();
      if (V.tab === 'eleven') this.loadVoices();
      if (this.isOpen) this.renderBody(false);
    }
    stopVoice() { clearTimeout(this.voice?.timer); }
    body_voice() {
      const V = this.voice;
      const intro = `<p class="tight">${who()} answers out loud. It already has a voice that runs on this server. Both options here are optional.</p>`;
      if (V.error) return `${intro}${this.errorHtml(V.error)}<div class="actions"><button type="button" class="btn small" data-act="voice-reload">Try again</button></div>`;
      if (!V.data) return intro + this.waitHtml('Checking the voices…');
      const tabs = VOICE_TABS.map(t => `<button type="button" class="tab" role="tab" id="vtab-${t.id}" data-act="voice-tab" data-id="${t.id}" data-fk="vtab-${t.id}" aria-selected="${t.id === V.tab}" aria-controls="voice-panel" tabindex="${t.id === V.tab ? 0 : -1}">${t.name}${t.id === 'eleven' && V.data.elevenlabs.connected ? ' ✓' : t.id === 'server' && (V.data.pocket.cloning || V.data.pocket.voice_pack) ? ' ✓' : ''}</button>`).join('');
      return `${intro}<div class="tabs" role="tablist" aria-label="Voices" data-tabs="voice">${tabs}</div>
        <div id="voice-panel" role="tabpanel" aria-labelledby="vtab-${V.tab}">${V.tab === 'eleven' ? this.panelEleven() : this.panelPocket()}</div>
        <p class="note">Switch voices any time under <b>VOICE</b> in the Vitals panel.</p>`;
    }
    panelPocket() {
      const V = this.voice, P = V.data.pocket, dl = P.download || {};
      if (!P.installed) return `<p>This copy of ${who()} doesn't include Pocket TTS, so it speaks with Piper, a plain voice that also runs here.</p>`;
      if (P.cloning) return `<div class="callout ok" role="status"><b>${who()} speaks in its own voice.</b> Pocket TTS copies the persona's voice right here on your server.</div>`;
      if (P.voice_pack) return `<div class="callout ok" role="status"><b>${who()} speaks in its own voice.</b> This JARVIS came with its voices, and Pocket TTS speaks them right here on your server.</div>`;
      if (dl.state === 'downloading') {
        const pct = dl.total ? Math.min(100, Math.round(100 * dl.done / dl.total)) : 0;
        return `<p class="tight">Downloading the voice-cloning model. This happens once.</p>
          <div class="bar" role="progressbar" aria-label="Downloading the voice-cloning model" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
          ${this.waitHtml(dl.total ? `${mb(dl.done)} of ${mb(dl.total)} MB` : 'Starting the download…')}
          <p class="note">You can carry on with the guide. The new voice is used as soon as it's done.</p>`;
      }
      const saved = !!V.data.huggingface?.token_saved;
      return `<p class="tight">Right now <b>Pocket TTS</b> speaks with a stock voice. To make it sound like ${who()}, it needs Kyutai's free voice-cloning model, which Hugging Face shares once you accept its terms.</p>
        <ol class="steps">
          <li><div>Sign in to Hugging Face (it's free) and accept the terms on the model page: <a class="ext" href="${escape(P.terms_url)}" target="_blank" rel="noopener noreferrer">huggingface.co/kyutai/pocket-tts ↗</a></div></li>
          <li><div>Make an access token with <b>Read</b> access: <a class="ext" href="${escape(P.tokens_url)}" target="_blank" rel="noopener noreferrer">huggingface.co/settings/tokens ↗</a></div></li>
          <li><div><label for="hf-token">Paste the token here and press Download.</label></div>
            <input id="hf-token" data-fk="hf-token" data-keep type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
              placeholder="${saved ? 'Leave blank to use the saved token' : 'hf_…'}" ${V.hfBusy ? 'readonly' : ''} ${V.hfError ? 'aria-invalid="true"' : ''}></li>
        </ol>
        ${this.errorHtml(V.hfError || (dl.state === 'error' ? dl.error : ''))}
        <div class="actions"><button type="button" class="btn primary" data-act="hf-download" data-fk="hf-go" ${V.hfBusy ? 'disabled aria-busy="true"' : ''}>${V.hfBusy ? '<span class="spin"></span>Checking…' : `Download · ${mb(P.size)} MB`}</button></div>
        <p class="note">The token is saved on your own server only.</p>`;
    }
    async startCloning() {
      const V = this.voice;
      if (V.hfBusy || !V.data) return;
      const field = this.$('[data-fk=hf-token]');
      let token = (field?.value || '').trim();
      if (!token && !V.data.huggingface?.token_saved) { V.hfError = 'Paste your Hugging Face token first.'; this.renderBody(false); this.$('[data-fk=hf-token]')?.focus(); return; }
      V.hfBusy = true; V.hfError = null; this.renderBody(false);
      const res = await api('/vesper/setup/voice/pocket', token ? { hf_token: token } : {}, 45000);
      token = '';
      if (V !== this.voice) return;
      V.hfBusy = false;
      const input = this.$('[data-fk=hf-token]');
      if (res.ok && res.data?.pocket) {
        if (input) input.value = '';
        this.kept['hf-token'] = '';
        V.data = res.data;
        this.watchCloning();
      } else {
        V.hfError = problem(res);
        if (res.data?.error?.code === 'invalid_token' && input) input.value = '';
      }
      if (this.isOpen) this.renderBody(false);
    }
    watchCloning() {
      const V = this.voice;
      clearTimeout(V.timer);
      V.timer = setTimeout(async () => {
        const res = await api('/vesper/setup/voice', undefined, 15000);
        if (V !== this.voice) return;
        if (res.ok && res.data?.pocket) V.data = res.data;
        const st = V.data.pocket;
        if (st.download?.state === 'downloading' || (!res.ok && res.network)) this.watchCloning();
        else if (st.cloning) { ui('voice-refresh'); this.announce(`${who()} now speaks in its own voice.`); }
        if (this.isOpen && STEPS[this.i].id === 'voice') this.renderBody(false);
      }, 1500);
    }
    panelEleven() {
      const V = this.voice, E = V.data.elevenlabs;
      if (E.connected && !V.newKey) {
        const P = window.VESPER_PERSONAS || {};
        const list = V.voices || [];
        const rows = V.voicesLoading ? this.waitHtml('Loading your voices…') : !list.length
          ? `<p class="note">${escape(V.elNote || 'No voices were listed for this account, so ElevenLabs uses its default voice.')}</p>`
          : PERSONA_IDS.map(id => {
            const value = V.chosen?.[id] ?? '';
            const options = [`<option value="">ElevenLabs default</option>`, ...list.map(v => `<option value="${escape(v.id)}"${v.id === value ? ' selected' : ''}>${escape(v.name)}${v.category && v.category !== 'premade' ? ` · ${escape(v.category)}` : ''}</option>`)].join('');
            return `<div class="field"><label class="lbl" for="el-voice-${id}">${escape(P[id]?.short || id)}</label><select id="el-voice-${id}" data-fk="el-voice-${id}" ${V.saving ? 'disabled' : ''}>${options}</select></div>`;
          }).join('') + `${this.errorHtml(V.saveError)}<div class="actions"><button type="button" class="btn primary" data-act="el-save" data-fk="el-save" ${V.saving ? 'disabled aria-busy="true"' : ''}>${V.saving ? '<span class="spin"></span>Saving…' : V.saved ? 'Saved ✓' : 'Save voices'}</button></div>`;
        return `<div class="callout ok" role="status"><b>ElevenLabs is connected.</b></div>
          <p>Pick the voice each persona speaks with. Voices you made in ElevenLabs are listed first.</p>
          ${rows}
          <div class="actions">${V.using ? '<span class="note">Using ElevenLabs now.</span>' : '<button type="button" class="btn small" data-act="el-use" data-fk="el-use">Speak with ElevenLabs now</button>'}
            <button type="button" class="btn small" data-act="el-newkey" data-fk="el-newkey">Use a different key</button></div>`;
      }
      return `<p class="tight">ElevenLabs has the most natural voices, online. Its free plan includes 10,000 characters of speech a month.</p>
        <ol class="steps">
          <li><div>Sign in to ElevenLabs and create an API key: <a class="ext" href="${escape(E.keys_url)}" target="_blank" rel="noopener noreferrer">elevenlabs.io/app/settings/api-keys ↗</a></div></li>
          <li><div><label for="el-key">Paste it here and press Connect.</label></div>
            <input id="el-key" data-fk="el-key" data-keep type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
              placeholder="ElevenLabs API key" ${V.elBusy ? 'readonly' : ''} ${V.elError ? 'aria-invalid="true"' : ''}></li>
        </ol>
        ${this.errorHtml(V.elError)}
        <div class="actions"><button type="button" class="btn primary" data-act="el-connect" data-fk="el-go" ${V.elBusy ? 'disabled aria-busy="true"' : ''}>${V.elBusy ? '<span class="spin"></span>Checking the key…' : 'Connect'}</button></div>
        <p class="note">The key is saved on your own server only. When the month's characters run out, the voice on this server takes over.</p>`;
    }
    // Each persona starts on the voice picked for it before, else its own voice from personas.js when this account has it.
    pickVoices(list, saved) {
      const P = window.VESPER_PERSONAS || {}, ids = new Set(list.map(v => v.id));
      return Object.fromEntries(PERSONA_IDS.map(id => {
        const own = P[id]?.voice?.elevenlabs;
        return [id, saved?.[id] && ids.has(saved[id]) ? saved[id] : own && ids.has(own) ? own : ''];
      }));
    }
    async loadVoices() {
      const V = this.voice;
      if (!V.data?.elevenlabs?.connected || V.voices || V.voicesLoading) return;
      V.voicesLoading = true; this.renderBody(false);
      const res = await api('/vesper/setup/voice/elevenlabs/voices', undefined, 30000);
      if (V !== this.voice) return;
      V.voicesLoading = false;
      V.voices = res.ok && Array.isArray(res.data?.voices) ? res.data.voices : [];
      V.elNote = res.ok ? (res.data.unavailable ? "ElevenLabs didn't list this account's voices just now, so each persona uses its default voice." : '') : problem(res);
      V.chosen = this.pickVoices(V.voices, res.data?.persona_voices);
      if (this.isOpen) this.renderBody(false);
    }
    async connectEleven() {
      const V = this.voice;
      if (V.elBusy) return;
      const field = this.$('[data-fk=el-key]');
      let key = (field?.value || '').trim();
      if (!key) { V.elError = 'Paste the key first.'; this.renderBody(false); this.$('[data-fk=el-key]')?.focus(); return; }
      V.elBusy = true; V.elError = null; this.renderBody(false);
      const res = await api('/vesper/setup/voice/elevenlabs', { api_key: key }, 45000);
      key = '';
      if (V !== this.voice) return;
      V.elBusy = false;
      const input = this.$('[data-fk=el-key]');
      if (res.ok && res.data?.ok) {
        if (input) input.value = '';
        this.kept['el-key'] = '';
        V.data.elevenlabs.connected = true; V.data.engines.elevenlabs = true; V.newKey = false;
        V.voices = Array.isArray(res.data.voices) ? res.data.voices : [];
        V.elNote = res.data.voices_note || '';
        V.chosen = this.pickVoices(V.voices, res.data.persona_voices);
        ui('voice-refresh');
        this.announce('ElevenLabs is connected.');
      } else {
        V.elError = problem(res);
        if (res.data?.error?.code === 'invalid_key' && input) input.value = '';
      }
      if (this.isOpen) this.renderBody(false);
    }
    async saveVoices() {
      const V = this.voice;
      if (V.saving || !V.chosen) return;
      V.saving = true; V.saveError = null; this.renderBody(false);
      const voices = Object.fromEntries(PERSONA_IDS.map(id => [id, V.chosen[id] || null]));
      const res = await api('/vesper/setup/voice/elevenlabs/voices', { voices }, 20000, 'PUT');
      if (V !== this.voice) return;
      V.saving = false;
      if (res.ok && res.data?.ok) { V.saved = true; this.announce('Voices saved.'); } else V.saveError = problem(res);
      if (this.isOpen) this.renderBody(false);
    }

    // ---- Telegram --------------------------------------------------------------------------------
    async loadTelegram() {
      const T = this.tg;
      if (T.loading || T.status) return;
      T.loading = true; T.loadError = null; this.renderBody(false);
      const res = await api('/vesper/setup/telegram', undefined, 30000);
      if (T !== this.tg) return;
      T.loading = false;
      if (res.ok && res.data) T.status = res.data; else T.loadError = problem(res);
      if (this.isOpen) this.renderBody(false);
    }
    stopTelegram() { clearTimeout(this.tg?.findTimer); clearTimeout(this.tg?.waitTimer); }
    body_telegram() {
      const T = this.tg, St = T.status;
      const intro = `<p class="tight">Chat with ${who()} from anywhere in the Telegram app, and get scheduled results there. Optional.</p>`;
      if (T.loadError) return `${intro}${this.errorHtml(T.loadError)}<div class="actions"><button type="button" class="btn small" data-act="tg-reload">Try again</button></div>`;
      if (!St) return intro + this.waitHtml('Checking Telegram…');
      const botLink = bot => bot?.link ? `<a class="btn small" href="${escape(bot.link)}" target="_blank" rel="noopener noreferrer" data-fk="tg-open">Open @${escape(bot.username)} ↗</a>` : '';
      if (T.result) {
        const live = St.running;
        return `<div class="callout ok" role="status"><b>Connected: @${escape(T.result.bot.username)}</b><br>${who()} sent you a message there.</div>
          ${live ? '' : T.result.restart_needed ? `<p class="note warn">Saved. Restart ${who()} to start it.</p>` : this.waitHtml('Starting Telegram…')}
          <div class="actions">${botLink(T.result.bot)}</div>`;
      }
      if (St.configured && !T.replacing) {
        const bot = St.bot;
        return `${intro}<div class="callout ${St.running ? 'ok' : 'warn'}" role="status"><b>${St.running ? 'Connected' : 'Saved but not running'}${bot ? `: @${escape(bot.username)}` : ''}</b>${St.running ? '' : St.error ? `<br>${escape(St.error)}` : ''}</div>
          ${T.note ? `<p class="note" role="status">${escape(T.note)}</p>` : ''}
          <div class="actions">${botLink(bot)}<button type="button" class="btn small" data-act="tg-replace" data-fk="tg-replace">Use a different bot</button></div>`;
      }
      if (!T.setup) {
        return `${intro}<ol class="steps">
            <li><div>In Telegram, open <a class="ext" href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather ↗</a>, send <code class="k">/newbot</code> and answer its two questions: a name, then a username ending in “bot”.</div></li>
            <li><div><label for="tg-token">BotFather replies with a token. Paste it here and press Check.</label></div>
              <input id="tg-token" data-fk="tg-token" data-keep type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"
                placeholder="123456789:AA…" ${T.busy ? 'readonly' : ''} ${T.error ? 'aria-invalid="true"' : ''}></li>
          </ol>
          ${this.errorHtml(T.error)}
          <div class="actions"><button type="button" class="btn primary" data-act="tg-check" data-fk="tg-go" ${T.busy ? 'disabled aria-busy="true"' : ''}>${T.busy ? '<span class="spin"></span>Checking…' : 'Check'}</button>
            ${T.replacing ? '<button type="button" class="btn small" data-act="tg-back" data-fk="tg-cancel">Cancel</button>' : ''}</div>
          <p class="note">Only you can talk to the bot: ${who()} answers just the account you pick next.</p>`;
      }
      const bot = T.setup.bot;
      const picks = T.senders.length
        ? `<p class="tight">Is this you?</p><div class="picks">${T.senders.map(s => `<button type="button" class="pick" data-act="tg-pick" data-id="${escape(s.id)}" data-fk="tg-pick-${escape(s.id)}" ${T.busy ? 'disabled' : ''}><b>${escape(s.name)}</b><small>${s.username ? '@' + escape(s.username) : 'Telegram account'}</small></button>`).join('')}</div>`
        : T.stale ? `<div class="callout warn"><b>No message yet.</b> Press Start in the bot's chat, then check again.</div><div class="actions"><button type="button" class="btn small" data-act="tg-again" data-fk="tg-again">Check again</button></div>`
        : this.waitHtml('Waiting for your message…');
      return `<div class="callout ok" role="status"><b>Found your bot: @${escape(bot.username)}</b></div>
        <ol class="steps">
          <li><div>Open it in Telegram and press <b>Start</b> (or send it any message).</div><div class="actions">${botLink(bot).replace('btn small', 'btn primary')}</div></li>
          <li><div>${who()} spots your message here, so it knows it's you.</div></li>
        </ol>
        ${picks}${this.errorHtml(T.error)}${T.busy ? this.waitHtml('Connecting…') : ''}
        <div class="actions"><button type="button" class="btn small" data-act="tg-back" data-fk="tg-back">Change bot</button></div>`;
    }
    async checkTelegram() {
      const T = this.tg;
      if (T.busy) return;
      const field = this.$('[data-fk=tg-token]');
      let token = (field?.value || '').trim();
      if (!token) { T.error = 'Paste the token from BotFather first.'; this.renderBody(false); this.$('[data-fk=tg-token]')?.focus(); return; }
      T.busy = true; T.error = null; this.renderBody(false);
      const res = await api('/vesper/setup/telegram/check', { token }, 30000);
      token = '';
      if (T !== this.tg) return;
      T.busy = false;
      const input = this.$('[data-fk=tg-token]');
      if (res.ok && res.data?.setup_id && res.data.already_connected && T.status?.running) {
        // The gateway is already reading this bot; there's nothing to set up (and no Start message the guide could see).
        if (input) input.value = '';
        this.kept['tg-token'] = '';
        T.replacing = false; T.note = `That's the bot ${who()} already uses.`;
        this.announce(T.note);
      } else if (res.ok && res.data?.setup_id) {
        if (input) input.value = '';
        this.kept['tg-token'] = '';
        T.setup = { id: res.data.setup_id, bot: res.data.bot };
        this.announce(`Found your bot, @${res.data.bot.username}. Open it in Telegram and press Start.`);
        this.watchTelegram(true);
      } else {
        T.error = problem(res);
        if (res.data?.error?.code === 'invalid_token' && input) input.value = '';
      }
      if (this.isOpen) this.renderBody(false);
    }
    // While the guide waits on the Start message: ask every 3 s, for three minutes.
    watchTelegram(restart = false) {
      const T = this.tg;
      if (!T.setup) return;
      clearTimeout(T.findTimer);
      if (restart) { T.findUntil = Date.now() + 180000; T.stale = false; T.error = null; this.renderBody(false); }
      T.findTimer = setTimeout(async () => {
        if (T !== this.tg || !T.setup || T.result) return;
        const res = await api('/vesper/setup/telegram/find', { setup_id: T.setup.id }, 20000);
        if (T !== this.tg || !T.setup || T.result) return;
        const before = T.senders.length;
        if (res.ok && Array.isArray(res.data?.senders)) { T.senders = res.data.senders; T.error = null; }
        else if (!res.network) {
          T.error = problem(res);
          if (res.data?.error?.code === 'setup_expired') { T.setup = null; T.senders = []; }
          if (this.isOpen) this.renderBody(false);
          return;
        }
        if (T.senders.length !== before) this.announce(T.senders.length === 1 ? `Message from ${T.senders[0].name}. Is this you?` : 'New messages. Pick yourself.');
        if (Date.now() < T.findUntil) this.watchTelegram(); else T.stale = !T.senders.length;
        if (this.isOpen && STEPS[this.i].id === 'telegram') this.renderBody(false);
      }, 3000);
    }
    async connectTelegram(userId) {
      const T = this.tg;
      if (T.busy || !T.setup) return;
      T.busy = true; T.error = null; clearTimeout(T.findTimer); this.renderBody(false);
      const res = await api('/vesper/setup/telegram/connect', { setup_id: T.setup.id, user_id: userId, persona: personaId() }, 60000);
      if (T !== this.tg) return;
      T.busy = false;
      if (res.ok && res.data?.ok) {
        T.result = res.data; T.setup = null;
        T.status = { ...(T.status || {}), configured: true, running: false };
        this.announce(`Connected to @${res.data.bot.username}.`);
        if (res.data.started) this.waitTelegramRunning(Date.now() + 45000);
      } else { T.error = problem(res); this.watchTelegram(); }
      if (this.isOpen) this.renderBody(false);
    }
    waitTelegramRunning(until) {
      const T = this.tg;
      T.waitTimer = setTimeout(async () => {
        const res = await api('/vesper/setup/telegram', undefined, 20000);
        if (T !== this.tg) return;
        if (res.ok && res.data) T.status = res.data;
        if (!T.status.running && Date.now() < until) this.waitTelegramRunning(until);
        else if (!T.status.running) T.result.restart_needed = true;
        if (this.isOpen && STEPS[this.i].id === 'telegram') this.renderBody(false);
      }, 2000);
    }

    body_model() {
      return `<p>The button in the top bar shows the model this chat uses. Click it to see every connected model.</p>
        <p class="muted">Picking one changes this chat only.</p>
        <div class="actions"><button type="button" class="btn primary" data-act="model-picker">Open the model picker</button></div>
        <p class="note info">The tour hides while you pick. Press “Resume tour” to come back.</p>`;
    }

    body_around() {
      const say = ['take a note', 'play some music', 'run a speed test'].map(s => `<span class="chip">“${s}”</span>`).join('');
      return `<dl class="kv"><dt>Sidebar</dt><dd>The icon rail opens panels, including <b>Notes</b> for quick text and voice notes.</dd>
        <dt>Voice</dt><dd>Press the <b>mic</b> and talk. ${who()} answers aloud. <kbd>Esc</kbd> interrupts.</dd>
        <dt>Commands</dt><dd><kbd>⌘K</kbd> or <kbd>Ctrl</kbd> + <kbd>K</kbd> searches every action.</dd></dl>
        <p class="muted">Try saying:</p><div class="chips">${say}</div>`;
    }

    body_done() {
      return `<p>That is the tour. Reopen it any time from the <b>GUIDE</b> button.</p>
        <p>Something not working? Open the <b>Telemetry</b> panel and press <b>RUN DIAGNOSTICS</b>.</p>`;
    }
  }
  customElements.define('vesper-tour', VesperTour);
})();
