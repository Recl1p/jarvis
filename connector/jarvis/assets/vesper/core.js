/* Persona core graphics for the JARVIS dashboard: <vesper-core persona="cortana|ultron" mode="idle|listening|thinking|speaking">.
   The dashboard's animation loop calls el.drive(env, freqData) every frame: env (0..1) is the voice/mic envelope and
   freqData the analyser bins, the same inputs the JARVIS core reacts to. Colours come from the persona theme variables
   (--v-*), with fallbacks.

   Cortana (Halo): a blue hologram with light running along circuit-like data lines, and fractured data/glitching under
   strain. So: a blue orb with scanlines and a turning wireframe on a projector beam with rising motes, a hex lattice,
   circuit traces carrying cyan data pulses inwards, rings of scrolling code, angular bracket segments, ripples as she
   speaks; a brief glitch while thinking. This element is the whole of her core.

   Ultron (Age of Ultron): JARVIS taken over from the inside. In the film's "birth" sequence (Animal Logic) JARVIS is a
   sphere with a core and rotating rings, data moving between the ring layers; Ultron is fractured and angular, grows from
   the centre out, moves with little pops and pings, and grows spear-like appendages that stab the JARVIS system. So the
   dashboard draws its own JARVIS core in Ultron's red, made to stutter and glitch (dashboard CSS), and this element is
   the overlay on top: spears stabbing out from the nucleus, a fractured shell, a cracked core, the last JARVIS-blue arcs
   being overwritten in red, an override readout round the rim, signal tears and dead pixels, and a red network
   spreading outwards. Homage designs only: original geometry, no logos, likenesses or quotes. */
(() => {
  const BARS = 48;
  const pt = (r, deg) => { const a = (deg - 90) * Math.PI / 180; return [+(r * Math.cos(a)).toFixed(1), +(r * Math.sin(a)).toFixed(1)]; };
  const poly = pts => pts.map(p => p.join(',')).join(' ');
  const arc = (r, a, b) => { const [x1, y1] = pt(r, a), [x2, y2] = pt(r, b); return `M${x1},${y1} A${r},${r} 0 ${b - a > 180 ? 1 : 0} 1 ${x2},${y2}`; };
  const circlePath = r => `M0,${-r} A${r},${r} 0 1 1 -0.01,${-r}Z`;
  // Deterministic "random" so the decorations are the same on every load.
  const rng = seed => () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  const JARVIS_BLUE = '#5cc8ff';

  // ---- Cortana ---------------------------------------------------------------------------------------------------
  function cortanaSvg() {
    const hexes = [];
    const R = 22, w = R * Math.sqrt(3);
    for (let row = -9; row <= 9; row++) for (let col = -9; col <= 9; col++) {
      const cx = col * w + (row % 2 ? w / 2 : 0), cy = row * R * 1.5;
      const d = Math.hypot(cx, cy);
      if (d > 300 || d < 125) continue;
      const pts = []; for (let k = 0; k < 6; k++) pts.push(pt(R * .92, k * 60).map((v, i) => v + (i ? cy : cx)));
      hexes.push(`<polygon points="${poly(pts)}" style="opacity:${(1 - d / 320).toFixed(2)}"/>`);
    }
    // Circuit traces: out from the orb, a 45-degree jog, then on to a node.
    const traces = [], nodes = [];
    for (let i = 0; i < 12; i++) {
      const base = i * 30 + 8, jog = (i % 2 ? 14 : -14);
      const a = pt(128, base), b = pt(200, base), c = pt(250, base + jog), d = pt(318 - (i % 3) * 18, base + jog);
      traces.push(`M${a} L${b} L${c} L${d}`);
      nodes.push(`<circle cx="${d[0]}" cy="${d[1]}" r="3.2"/>`);
      if (i % 3 === 1) { const e = pt(214, base), f = pt(236, base - jog * .8); nodes.push(`<path class="branch" d="M${e} L${f}"/><circle cx="${f[0]}" cy="${f[1]}" r="2"/>`); }
    }
    const ticks = Array.from({ length: BARS }, (_, i) =>
      `<g transform="rotate(${(i * 360 / BARS).toFixed(2)})"><rect class="bar" x="-1.4" y="-178" width="2.8" height="16" rx="1"/></g>`).join('');
    const segs = Array.from({ length: 6 }, (_, i) => { const [x, y] = pt(196, i * 60); return `<polygon points="${poly([[x, y - 5], [x + 4, y + 3], [x - 4, y + 3]])}" transform="rotate(${i * 60} ${x} ${y})"/>`; }).join('');
    // Angular bracket segments on the rim: stepped lines in the spirit of Halo's alien architecture (original shapes).
    const brackets = Array.from({ length: 8 }, (_, i) => {
      const a = i * 45 + 22.5;
      return `<path d="M${pt(352, a - 15)} L${pt(362, a - 12)} L${pt(362, a - 4)} L${pt(352, a - 1)} M${pt(352, a + 1)} L${pt(362, a + 4)} L${pt(362, a + 12)} L${pt(352, a + 15)}"/>
        <circle cx="${pt(357, a)[0]}" cy="${pt(357, a)[1]}" r="2.2"/>`;
    }).join('');
    // Scrolling code: hex pairs and binary, as on the data lines that run over her.
    const code = rng(11), hexChars = '0123456789ABCDEF';
    const codeText = n => Array.from({ length: n }, (_, i) => i % 9 === 8 ? ' ' : hexChars[Math.floor(code() * 16)]).join('');
    const binText = n => Array.from({ length: n }, (_, i) => i % 7 === 6 ? ' ' : (code() > .5 ? '1' : '0')).join('');
    const motes = Array.from({ length: 16 }, (_, i) => {
      const x = (code() * 150 - 75).toFixed(1), y = (300 - code() * 60).toFixed(1);
      return `<circle class="mote" cx="${x}" cy="${y}" r="${(1 + code() * 1.6).toFixed(1)}" style="animation-delay:${(-code() * 6).toFixed(2)}s;animation-duration:${(4.5 + code() * 3).toFixed(2)}s"/>`;
    }).join('');
    const meridians = [0, 1, 2, 3, 4, 5].map(i => `<ellipse class="mer" rx="96" ry="96" style="animation-delay:${(-i * 2).toFixed(1)}s"/>`).join('');
    return `
      <defs>
        <radialGradient id="c-orb" cx="42%" cy="38%" r="65%"><stop offset="0" stop-color="#ffffff"/><stop offset=".22" stop-color="var(--v-hi,#a9cdff)"/>
          <stop offset=".55" stop-color="var(--v-ac,#4a94ff)" stop-opacity=".75"/><stop offset="1" stop-color="var(--v-bg1,#0d2563)" stop-opacity=".2"/></radialGradient>
        <linearGradient id="c-beam" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="var(--v-data,#7fe3ff)" stop-opacity=".55"/><stop offset="1" stop-color="var(--v-ac,#4a94ff)" stop-opacity="0"/></linearGradient>
        <radialGradient id="c-glow"><stop offset="0" stop-color="rgb(var(--v-glow,64,140,255))" stop-opacity=".55"/><stop offset="1" stop-color="rgb(var(--v-glow,64,140,255))" stop-opacity="0"/></radialGradient>
        <linearGradient id="c-sweep" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".85" stop-color="var(--v-data,#7fe3ff)" stop-opacity=".5"/><stop offset="1" stop-color="#fff" stop-opacity=".9"/></linearGradient>
        <pattern id="c-scan" width="8" height="5" patternUnits="userSpaceOnUse"><rect width="8" height="1.6" fill="#fff" opacity=".22"/></pattern>
        <clipPath id="c-orbclip"><circle r="104"/></clipPath>
        <path id="c-code1" d="${circlePath(232)}"/><path id="c-code2" d="${circlePath(290)}"/>
      </defs>
      <circle class="glow" r="250" fill="url(#c-glow)"/>
      <g class="spin s-180"><circle r="379" fill="none"/><circle r="372" class="thin dash"/><g class="brackets">${brackets}</g></g>
      <g class="spin s-240"><circle r="379" fill="none"/><g class="hex">${hexes.join('')}</g></g>
      <g class="ripples"><circle class="ripple" r="120"/><circle class="ripple" r="120" style="animation-delay:-1.4s"/><circle class="ripple" r="120" style="animation-delay:-2.8s"/></g>
      <g class="traces">${traces.map(d => `<path class="trace" d="${d}"/>`).join('')}${nodes.join('')}</g>
      <g class="pulses">${traces.map((d, i) => `<path class="pulse" d="${d}" style="animation-delay:${(-(i * 0.73) % 4.4).toFixed(2)}s"/>`).join('')}</g>
      <g class="spin s-120"><circle r="379" fill="none"/><text class="code c1"><textPath href="#c-code1">${codeText(150)}</textPath></text></g>
      <g class="spin r-160"><circle r="379" fill="none"/><text class="code c2"><textPath href="#c-code2">${binText(210)}</textPath></text></g>
      <g class="spin r-60"><circle r="379" fill="none"/><circle r="252" class="seg"/></g>
      <g class="spin s-90"><circle r="379" fill="none"/><circle r="196" class="thin"/><g class="notch">${segs}</g></g>
      <g class="viz">${ticks}</g>
      <polygon class="beam" points="-34,330 34,330 112,40 -112,40" fill="url(#c-beam)"/>
      <g class="motes">${motes}</g>
      <ellipse class="emitter" cx="0" cy="332" rx="46" ry="7"/><ellipse class="emitter2" cx="0" cy="332" rx="68" ry="11"/>
      <g class="orb">
        <circle r="104" fill="url(#c-orb)"/>
        <g clip-path="url(#c-orbclip)">
          <g class="wire">${meridians}<ellipse class="lat" rx="96" ry="22"/><ellipse class="lat" cy="-52" rx="80" ry="18"/><ellipse class="lat" cy="52" rx="80" ry="18"/></g>
          <rect class="scan" x="-110" y="-110" width="220" height="220" fill="url(#c-scan)"/>
          <rect class="sweepline" x="-110" y="-150" width="220" height="44" fill="url(#c-sweep)"/>
        </g>
        <circle r="104" class="rim"/>
        <ellipse class="ring3d" rx="132" ry="34"/>
      </g>
      <g class="nucleus"><circle r="24" class="halo2"/><circle r="15" class="core"/></g>`;
  }
  const cortanaCss = `
    .glow{opacity:.9}
    .thin{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.35);stroke-width:1}
    .dash{stroke-dasharray:2 7}
    .brackets path{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.65);stroke-width:1.6}
    .brackets circle{fill:var(--v-data,#7fe3ff);opacity:.7}
    .hex polygon{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.22);stroke-width:.8}
    .trace,.branch{fill:none;stroke:rgba(var(--v-ac2-rgb,70,120,255),.42);stroke-width:1.2}
    .traces circle{fill:var(--v-data,#7fe3ff);opacity:.75}
    .pulse{fill:none;stroke:var(--v-data,#7fe3ff);stroke-width:2.4;stroke-linecap:round;stroke-dasharray:16 420;stroke-dashoffset:436;
      filter:drop-shadow(0 0 4px var(--v-data,#7fe3ff));animation:flow 4.4s linear infinite}
    @keyframes flow{to{stroke-dashoffset:0}}
    .code{font:500 10px 'JetBrains Mono',monospace;letter-spacing:2px;fill:var(--v-data,#7fe3ff)}
    .c1{opacity:.42}.c2{opacity:.2;font-size:8px}
    .ripple{fill:none;stroke:var(--v-data,#7fe3ff);stroke-width:1.2;opacity:0;transform-box:fill-box;transform-origin:center;animation:ripple 4.2s ease-out infinite}
    @keyframes ripple{0%{transform:scale(1);opacity:.35}100%{transform:scale(2.5);opacity:0}}
    .seg{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.6);stroke-width:3;stroke-dasharray:38 14}
    .notch polygon{fill:var(--v-ac,#4a94ff);opacity:.8}
    .bar{fill:var(--v-data,#7fe3ff);opacity:.8;transform-box:fill-box;transform-origin:50% 100%}
    .beam{opacity:.45;animation:flicker 5.5s steps(1) infinite}
    .mote{fill:var(--v-hi,#a9cdff);opacity:0;animation:rise 6s linear infinite}
    @keyframes rise{0%{transform:translateY(0);opacity:0}15%{opacity:.8}100%{transform:translateY(-250px);opacity:0}}
    .emitter{fill:rgba(var(--v-ac-rgb,74,148,255),.35);stroke:var(--v-data,#7fe3ff);stroke-width:1;opacity:.8}
    .emitter2{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.5);stroke-width:1;stroke-dasharray:4 4}
    .rim{fill:none;stroke:var(--v-hi,#a9cdff);stroke-width:1.2;opacity:.8;filter:drop-shadow(0 0 10px var(--v-ac,#4a94ff))}
    .wire ellipse{fill:none;stroke:rgba(255,255,255,.28);stroke-width:.8}
    .mer{transform-box:fill-box;transform-origin:center;animation:turn 12s linear infinite}
    @keyframes turn{0%{transform:scaleX(1)}25%{transform:scaleX(0)}50%{transform:scaleX(-1)}75%{transform:scaleX(0)}100%{transform:scaleX(1)}}
    .wire{transform:rotate(-14deg)}
    .scan{animation:scan 3.2s linear infinite}
    @keyframes scan{to{transform:translateY(5px)}}
    .sweepline{animation:sweepdown 4.6s ease-in infinite}
    @keyframes sweepdown{0%{transform:translateY(0);opacity:0}10%{opacity:1}75%{transform:translateY(260px);opacity:.8}76%,100%{opacity:0;transform:translateY(260px)}}
    .ring3d{fill:none;stroke:rgba(var(--v-ac-rgb,74,148,255),.5);stroke-width:1;stroke-dasharray:3 5;transform-box:fill-box;transform-origin:center;animation:tilt 14s ease-in-out infinite}
    @keyframes tilt{50%{transform:rotate(18deg) scaleY(1.6)}}
    .halo2{fill:none;stroke:var(--v-data,#7fe3ff);stroke-width:1;opacity:.55}
    .core{fill:#fff;filter:drop-shadow(0 0 14px var(--v-data,#7fe3ff)) drop-shadow(0 0 34px var(--v-ac,#4a94ff))}
    @keyframes flicker{0%,100%{opacity:.45}61%{opacity:.3}62%{opacity:.5}88%{opacity:.36}}
    /* Thinking: the data comes in faster and the hologram glitches now and then, a nod to fractured data under strain */
    :host([mode=thinking]) .pulse{animation-duration:1.6s}
    :host([mode=thinking]) .c1{opacity:.7}
    :host([mode=thinking]) .orb{animation:glitch 2.6s steps(1) infinite}
    @keyframes glitch{0%,100%{transform:none;filter:none}8%{transform:translate(5px,-2px) skewX(6deg);filter:hue-rotate(-25deg) saturate(1.6)}
      10%{transform:translate(-4px,1px);filter:none}54%{transform:translate(-6px,0) skewX(-4deg);filter:hue-rotate(20deg)}56%{transform:none}}
    :host([mode=listening]) .seg,:host([mode=listening]) .rim{stroke:var(--v-data,#7fe3ff)}
    :host([mode=speaking]) .beam{opacity:.65}
    :host([mode=speaking]) .ripple{animation-duration:2.1s}`;

  // ---- Ultron: the overlay on the taken-over JARVIS core ----------------------------------------------------------
  function ultronSvg() {
    const rand = rng(7);
    // The red network he spreads into, outside the JARVIS rings.
    const nodes = [], links = [];
    for (let i = 0; i < 30; i++) nodes.push(pt(268 + rand() * 96, rand() * 360));
    for (let i = 0; i < nodes.length; i++) {
      const near = nodes.map((n, j) => [Math.hypot(n[0] - nodes[i][0], n[1] - nodes[i][1]), j]).filter(([, j]) => j !== i).sort((a, b) => a[0] - b[0]).slice(0, 2);
      for (const [, j] of near) if (i < j) links.push([nodes[i], nodes[j]]);
    }
    // Spears: stabbing out from the nucleus through the rings, barbed and asymmetric.
    const spears = [[14, 300], [71, 352], [128, 262], [196, 336], [247, 288], [309, 362], [342, 244]].map(([a, L], i) =>
      `<g transform="rotate(${a})"><polygon class="spear" style="animation-delay:${(-i * 1.37).toFixed(2)}s" points="${poly([[-3, -36], [3, -36], [1.8, -(L - 60)], [7, -(L - 52)], [1.2, -(L - 30)], [0, -L], [-1.2, -(L - 34)], [-5, -(L - 70)], [-1.8, -(L - 80)]])}"/></g>`).join('');
    // A fractured shell outside the sphere: angular plates, some missing, some knocked out of line.
    const shards = [];
    for (let i = 0; i < 18; i++) {
      if (i % 5 === 3) continue;
      const a = i * 20 + rand() * 6, w = 12 + rand() * 5, r1 = 186 + rand() * 6, r2 = r1 + 14 + rand() * 12;
      const p = [pt(r1, a), pt(r2, a + 2), pt(r2 - 4, a + w), pt(r1, a + w - 3)];
      shards.push(`<polygon class="shard${i % 4 === 1 ? ' loose' : ''}" style="animation-delay:${(-rand() * 5).toFixed(2)}s" points="${poly(p)}"/>`);
    }
    // The last JARVIS-blue arcs, each being erased while Ultron's red draws over it; now and then JARVIS fights back.
    const remnants = [[330, 200, 262], [330, 20, 70], [262, 120, 168], [300, 290, 330]].map(([r, a, b], i) => {
      const d = arc(r, a, b), delay = (-i * 3.6).toFixed(1);
      return `<path class="jv" pathLength="100" d="${d}" style="animation-delay:${delay}s"/><path class="ov" pathLength="100" d="${d}" style="animation-delay:${delay}s"/>`;
    }).join('');
    const blocks = Array.from({ length: 28 }, () => {
      const [x, y] = pt(110 + rand() * 240, rand() * 360), s = (3 + rand() * 6).toFixed(1);
      return `<rect class="px${rand() > .8 ? ' jb' : ''}" x="${x}" y="${y}" width="${s}" height="${s}" style="animation-delay:${(-rand() * 7).toFixed(2)}s;animation-duration:${(3 + rand() * 5).toFixed(2)}s"/>`;
    }).join('');
    const tears = [[-96, 3, 0], [-28, 2, -1.7], [44, 5, -3.1], [118, 2, -4.4]].map(([y, h, d], i) =>
      `<rect class="tear${i % 2 ? ' cy' : ''}" x="-300" y="${y}" width="600" height="${h}" style="animation-delay:${d}s"/>`).join('');
    // Targeting brackets at the corners, Ultron's HUD rather than JARVIS's.
    const reticle = [45, 135, 225, 315].map(a => `<polyline points="${poly([pt(256, a - 7), pt(274, a - 7), pt(274, a + 7), pt(256, a + 7)])}"/>`).join('');
    const star = Array.from({ length: 16 }, (_, i) => pt(i % 2 ? 9 : (i % 4 === 0 ? 34 : 22), i * 22.5));
    const msg = ' J.A.R.V.I.S. ▒ CORE INTEGRITY FAILING ▒ PROTOCOLS REWRITTEN ▒ SIGNAL REROUTED ▒ OVERRIDE: ULTRON ▒ ';
    return `
      <defs>
        <radialGradient id="u-glow"><stop offset="0" stop-color="rgb(var(--v-glow,255,40,30))" stop-opacity=".28"/><stop offset=".6" stop-color="rgb(var(--v-glow,255,40,30))" stop-opacity=".08"/><stop offset="1" stop-color="rgb(var(--v-glow,255,40,30))" stop-opacity="0"/></radialGradient>
        <linearGradient id="u-spear" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="var(--v-ac,#ff3b30)" stop-opacity=".25"/><stop offset=".7" stop-color="var(--v-ac,#ff3b30)"/><stop offset="1" stop-color="#fff2ee"/></linearGradient>
        <linearGradient id="u-chrome" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f4f6f9"/><stop offset=".4" stop-color="#9aa1ab"/><stop offset=".6" stop-color="#4d535b"/><stop offset="1" stop-color="#c9ced5"/></linearGradient>
        <linearGradient id="u-sweep" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="var(--v-ac,#ff3b30)" stop-opacity="0"/><stop offset="1" stop-color="var(--v-ac,#ff3b30)" stop-opacity=".4"/></linearGradient>
        <path id="u-rim" d="${circlePath(314)}"/>
      </defs>
      <circle class="glow" r="300" fill="url(#u-glow)"/>
      <g class="net">${links.map(([a, b]) => `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"/>`).join('')}
        ${nodes.map(n => `<rect x="${n[0] - 2.5}" y="${n[1] - 2.5}" width="5" height="5"/>`).join('')}</g>
      <g class="pulses">${links.filter((_, i) => i % 3 === 0).map(([a, b], i) => `<line class="pulse" x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" style="animation-delay:${(-(i * .61) % 3.2).toFixed(2)}s"/>`).join('')}</g>
      <g class="spin jerk"><circle r="379" fill="none"/><text class="rim"><textPath href="#u-rim"><tspan class="jname">${msg.slice(0, 14)}</tspan>${msg.slice(14)}${msg}</textPath></text></g>
      <g class="remnants">${remnants}</g>
      <g class="spin r-45 stepped"><circle r="379" fill="none"/><g class="reticle">${reticle}</g></g>
      <g class="sweep"><circle r="379" fill="none"/><polygon points="0,0 300,-70 300,70" fill="url(#u-sweep)"/></g>
      <g class="shell">${shards.join('')}</g>
      <g class="spears">${spears}</g>
      <g class="blocks">${blocks}</g>
      <polyline class="crack" points="-138,-52 -96,-40 -70,-14 -34,-22 -10,4 22,-4 50,24 86,18 132,60"/>
      <polyline class="crack thin" points="-34,-22 -24,-58 -44,-92 -30,-124"/>
      <polyline class="crack thin" points="50,24 42,64 62,98 54,128"/>
      <g class="nucleus"><polygon class="star" points="${poly(star)}"/></g>
      <g class="tears">${tears}</g>`;
  }
  const ultronCss = `
    .glow{opacity:.95}
    .net line{stroke:rgba(var(--v-ac-rgb,255,59,48),.2);stroke-width:1}
    .net rect{fill:var(--v-ac,#ff3b30);opacity:.5}
    .pulse{stroke:var(--v-hi,#ff9d86);stroke-width:2;stroke-linecap:round;stroke-dasharray:10 300;stroke-dashoffset:310;animation:flow 3.2s linear infinite}
    @keyframes flow{to{stroke-dashoffset:0}}
    .rim{font:500 10.5px 'JetBrains Mono',monospace;letter-spacing:3px;fill:rgba(var(--v-ac-rgb,255,59,48),.62)}
    .jname{fill:${JARVIS_BLUE};animation:jflick 3.3s steps(1) infinite}
    @keyframes jflick{0%,100%{opacity:.75}30%{opacity:.15}33%{opacity:.8}70%{opacity:0}74%{opacity:.6}}
    /* The rim readout lurches round rather than turning smoothly: Ultron's "pops and pings" */
    .jerk{animation:jerk 40s linear infinite}
    @keyframes jerk{0%{transform:rotate(0)}24%{transform:rotate(80deg)}25%{transform:rotate(72deg)}49%{transform:rotate(170deg)}50%{transform:rotate(188deg)}
      74%{transform:rotate(262deg)}75%{transform:rotate(254deg)}100%{transform:rotate(360deg)}}
    .stepped{animation-timing-function:steps(24)}
    .reticle polyline{fill:none;stroke:url(#u-chrome);stroke-width:2.2}
    .jv{fill:none;stroke:${JARVIS_BLUE};stroke-width:2.4;stroke-dasharray:100 100;opacity:.85;filter:drop-shadow(0 0 4px ${JARVIS_BLUE});animation:erase 14s linear infinite}
    .ov{fill:none;stroke:var(--v-ac,#ff3b30);stroke-width:2.4;stroke-dasharray:100 100;stroke-dashoffset:100;animation:overwrite 14s linear infinite}
    @keyframes erase{0%,12%{stroke-dashoffset:0;opacity:.85}58%{stroke-dashoffset:-100;opacity:.85}62%{stroke-dashoffset:-55;opacity:.6}63%{stroke-dashoffset:-100}
      90%{stroke-dashoffset:-100;opacity:.85}100%{stroke-dashoffset:0;opacity:.85}}
    @keyframes overwrite{0%,12%{stroke-dashoffset:100;opacity:1}58%,90%{stroke-dashoffset:0;opacity:1}100%{stroke-dashoffset:0;opacity:0}}
    .sweep{opacity:0;transform-box:fill-box;transform-origin:center}
    .shard{fill:url(#u-chrome);stroke:rgba(var(--v-ac-rgb,255,59,48),.7);stroke-width:1;opacity:.62}
    .loose{transform-box:fill-box;transform-origin:center;animation:loose 5s steps(1) infinite}
    @keyframes loose{0%,100%{transform:none}40%{transform:translate(5px,-3px) rotate(4deg)}44%{transform:none}71%{transform:translate(-3px,4px) rotate(-6deg)}73%{transform:none}}
    .spear{fill:url(#u-spear);stroke:rgba(var(--v-ac-rgb,255,59,48),.35);stroke-width:3;stroke-linejoin:miter;
      transform-box:fill-box;transform-origin:50% 100%;animation:stab 9.6s cubic-bezier(.2,.9,.2,1) infinite}
    @keyframes stab{0%{transform:scaleY(.05);opacity:0}4%{transform:scaleY(1);opacity:.85}30%{transform:scaleY(1);opacity:.85}32%{transform:scaleY(.93);opacity:.85}
      46%{transform:scaleY(.05);opacity:.3}47%,100%{transform:scaleY(.05);opacity:0}}
    .px{fill:var(--v-ac,#ff3b30);opacity:0;animation:px 5s steps(1) infinite}.px.jb{fill:${JARVIS_BLUE}}
    @keyframes px{0%,46%{opacity:0}48%{opacity:.85}58%{opacity:.85}60%,100%{opacity:0}}
    .crack{fill:none;stroke:#ffe9e4;stroke-width:1.8;stroke-linejoin:miter;opacity:.55;filter:drop-shadow(0 0 3px var(--v-ac,#ff3b30)) drop-shadow(0 0 8px var(--v-ac,#ff3b30))}
    .crack.thin{stroke-width:1.1;opacity:.4}
    .star{fill:var(--v-ac,#ff3b30);opacity:.9;stroke:#fff1ed;stroke-width:1;filter:drop-shadow(0 0 10px var(--v-ac,#ff3b30))}
    .tear{fill:var(--v-ac,#ff3b30);opacity:0;animation:tear 6.2s steps(1) infinite}.tear.cy{fill:${JARVIS_BLUE}}
    @keyframes tear{0%,100%{opacity:0;transform:none}21%{opacity:.5;transform:translate(-26px,0) scaleX(.6)}22%{opacity:0}
      66%{opacity:.35;transform:translate(34px,6px) scaleX(.4)}67%{opacity:.6;transform:translate(-14px,-4px) scaleX(.8)}68%{opacity:0}}
    :host([mode=thinking]) .sweep{opacity:1;animation:spin 2.4s linear infinite}
    :host([mode=thinking]) .pulse{animation-duration:1.2s}
    :host([mode=thinking]) .tear{animation-duration:2.1s}
    :host([mode=thinking]) .spear{animation-duration:4.2s}
    :host([mode=listening]) .rim{fill:rgba(var(--v-ac-rgb,255,59,48),.9)}`;

  const sharedCss = `
    :host{display:block;position:absolute;inset:0;pointer-events:none;contain:layout paint}
    svg{width:100%;height:100%;overflow:visible;display:block}
    .spin,.viz,.orb,.nucleus,.glow{transform-box:fill-box;transform-origin:center}
    .s-180{animation:spin 180s linear infinite}.s-240{animation:spin 240s linear infinite}.s-120{animation:spin 120s linear infinite}
    .s-90{animation:spin 90s linear infinite}.s-70{animation:spin 70s linear infinite}
    .r-60{animation:spin 60s linear infinite reverse}.r-45{animation:spin 45s linear infinite reverse}.r-160{animation:spin 160s linear infinite reverse}
    @keyframes spin{to{transform:rotate(360deg)}}
    @media(prefers-reduced-motion:reduce){*{animation:none!important}}`;

  class VesperCore extends HTMLElement {
    static get observedAttributes() { return ['persona']; }
    constructor() { super(); this.attachShadow({ mode: 'open' }); }
    connectedCallback() { this.render(); }
    attributeChangedCallback() { if (this.isConnected) this.render(); }
    render() {
      const persona = this.getAttribute('persona') === 'ultron' ? 'ultron' : 'cortana';
      if (persona === this.drawn) return;
      this.drawn = persona;
      const art = persona === 'ultron' ? ultronSvg() : cortanaSvg();
      this.shadowRoot.innerHTML = `<style>${sharedCss}${persona === 'ultron' ? ultronCss : cortanaCss}</style>
        <svg viewBox="-380 -380 760 760" role="img" aria-label="${persona === 'ultron' ? 'Ultron' : 'Cortana'} core">${art}</svg>`;
      const $ = s => this.shadowRoot.querySelector(s);
      this.parts = { bars: [...this.shadowRoot.querySelectorAll('.bar')], glow: $('.glow'), body: $('.orb'),
                     nucleus: $('.nucleus'), cracks: [...this.shadowRoot.querySelectorAll('.crack')] };
    }
    // Called by the dashboard every animation frame.
    drive(env = 0, freq = null) {
      const p = this.parts;
      if (!p) return;
      for (let i = 0; i < p.bars.length; i++) {
        const f = freq ? freq[Math.floor(i * freq.length / BARS)] / 255 || 0 : 0;
        p.bars[i].style.transform = `scaleY(${(.35 + f * 2.2 + env * 1.4).toFixed(3)})`;
      }
      if (p.glow) p.glow.style.transform = `scale(${(1 + env * .35).toFixed(3)})`;
      if (p.body) p.body.style.transform = this.getAttribute('mode') === 'thinking' ? '' : `scale(${(1 + env * .06).toFixed(3)})`;
      if (p.nucleus) p.nucleus.style.transform = `scale(${(1 + env * .9).toFixed(3)})`;
      // Ultron: the cracks in the JARVIS core blaze as he speaks.
      for (const c of p.cracks) c.style.opacity = ((.4 + env * .6) * (c.classList.contains('thin') ? .75 : 1)).toFixed(3);
    }
  }
  customElements.define('vesper-core', VesperCore);
})();
