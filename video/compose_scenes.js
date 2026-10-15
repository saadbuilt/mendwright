// The later acts, declared as data and drawn by one engine on top of compose.js.
// Act 6  dismiss=True on a popup nobody registered.
// Act 7  popups that need judgement: an upsell, a "Dismiss to cancel" prompt, a ZIP re-entry.
// Act 8  steps in any order (heal.step / run_steps).
// Act 9  Selenium.
// Every log line, outcome and timing shown comes from D (real runs); see render.py.
'use strict';

// ---------- engine ----------
const SCENES = [];
const CARD_LEN = 1.9;                               // an act's title card, in story seconds
const actLayer = el('div', '', null, document.body, 'position:absolute;inset:0;pointer-events:none;z-index:3');

function sceneStatus(t, kind, text, tout) {
  const col = kind === 'ok' ? C.green : kind === 'bad' ? C.amber : C.blue;
  const ic = kind === 'ok' ? '✓' : kind === 'bad' ? '⊘' : '';
  const e = el('div', '', `${kind === 'run' ? `<span class="spin" style="width:14px;height:14px;border-radius:50%;border:2.5px solid rgba(90,169,255,.25);border-top-color:${C.blue}"></span>` : `<span style="font-size:18px">${ic}</span>`}${text}`, stage, `background:${col}1f;border:1px solid ${col}55;color:${kind === 'run' ? '#cfe3ff' : col}`);
  e.id = 'status';
  statusEls.push({ t, tout, kind, e, spin: $('.spin', e) });
}

function scene(def) {
  const sc = Object.assign({ font: 16.5, lh: LH, url: 'northwind.example/checkout', marks: [], hl: [], clicks: [], fills: [],
    boxes: [], chips: [], timers: [], halo: [], cursor: null, run: [], caps: [], status: [], inserts: {} }, def);
  sc.body = sc.t0 + (sc.title ? CARD_LEN : 0);
  sc.lineEls = sc.lines.map((html, i) => {
    const ln = el('div', 'ln mono', `<span class="g"></span><span class="c"><span class="cw">${html}</span></span><span class="st"></span>`, code, `font-size:${sc.font}px;height:${sc.lh}px`);
    return { root: ln, g: $('.g', ln), cw: $('.cw', ln), st: $('.st', ln), i };
  });
  sc.frames = (sc.frames || []).map(f => Object.assign({ e: el('iframe', '', null, content) }, f));
  sc.frames.forEach(f => { f.e.srcdoc = f.html; });
  for (const [g0, g1, ents] of (sc.runGroups || [[sc.body, sc.t1, sc.run]])) {
    const g = el('div', 'grp mono', null, tbody);
    groupEls.push({ t0: g0, t1: g1, g, ents: ents.map(([t, cls, html]) => ({ t, e: el('div', 'ent ' + cls, html, g) })) });
  }
  sc.caps.forEach(([a, b, h]) => capEls.push({ a, b, e: el('div', 'cap', h, document.body) }));
  sc.status.forEach(s => sceneStatus(...s));
  if (sc.title) {
    sc.card = el('div', '', `<div style="font-size:16px;letter-spacing:.16em;text-transform:uppercase;color:${sc.title.col || C.purple};font-weight:700">${sc.title.kicker || ''}</div>
      <div style="font-size:64px;font-weight:700;letter-spacing:-.025em;margin-top:14px">${sc.title.text}</div>
      <div class="mono" style="font-size:24px;color:var(--ink2);margin-top:18px">${sc.title.sub || ''}</div>`, actLayer,
      'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;background:rgba(6,8,13,.72);backdrop-filter:blur(14px);visibility:hidden');
  }
  sc.boxEls = sc.boxes.map(() => box());
  sc.chipEls = sc.chips.map(c => chip(c.html, c.style || ''));
  sc.timerEls = sc.timers.map(tm => timer(tm.col || C.amber));
  if (sc.hud) sc.hudEl = hud(sc.hud.rows);
  SCENES.push(sc);
  return sc;
}

// 'frame:css' -> the frame and the selector; the first frame by default
function ref(sc, s) {
  const i = s.indexOf(':');
  if (i > 0 && sc.frames.some(f => f.id === s.slice(0, i))) return [sc.frames.find(f => f.id === s.slice(0, i)), s.slice(i + 1)];
  return [sc.frames[0], s];
}
function measureScenes() {
  for (const sc of SCENES) {
    for (const f of sc.frames) {
      f.doc = f.e.contentDocument;
      f.doc.documentElement.style.overflow = 'hidden';
      if (f.popup) { f.pop = f.doc.querySelector(f.popup); f.popCard = f.pop.firstElementChild; f.popCard.style.willChange = 'transform,opacity,filter'; }
    }
    sc.R = {};
    sc.N = {};
    const want = new Set();
    const add = s => { if (typeof s === 'string') want.add(s); };
    (sc.cursor || []).forEach(k => add(k[1]));
    sc.clicks.forEach(k => add(k[1]));
    sc.fills.forEach(k => add(k[2]));
    sc.boxes.forEach(b => add(b.at));
    sc.chips.forEach(c => add(c.at));
    sc.timers.forEach(tm => add(tm.at));
    (sc.press || []).forEach(k => add(k[1]));
    (sc.checks || []).forEach(k => add(k[1]));
    for (const s of want) {
      const [f, css] = ref(sc, s);
      const n = f.doc.querySelector(css);
      if (!n) throw new Error('scene selector not found: ' + s);
      sc.N[s] = n;
      sc.R[s] = rectOf(n);
    }
    const pt = (at, dx = 0, dy = 0) => {
      if (Array.isArray(at)) return at;
      const r = sc.R[at];
      return [(r.w > 260 ? r.x + 80 : r.cx) + dx, r.cy + dy];
    };
    sc.pt = pt;
    if (sc.cursor) sc.cursorKeys = [[0, [760, 540]], ...sc.cursor.map(([t, at, dx, dy]) => [t, pt(at, dx, dy)])];
    sc.clicks.forEach(([t, at, col]) => ripple(t, pt(at), col));
  }
}

function sLineY(sc, i, t) {
  let y = i;
  for (const [tt, at] of Object.entries(sc.inserts)) if (i >= at) y += spring(t - +tt, .5) - 1;   // lines after an insert start one up
  return 14 + y * sc.lh;
}

function drawScene(sc, t) {
  const live = t > sc.t0 - .7 && t < sc.t1 + 1.2;
  const out = spring(t - sc.t1, .4);
  // title card
  if (sc.card) mat(sc.card, pres(t, sc.t0, sc.t0 + CARD_LEN - .25, .5, .4), { dy: 0, s0: 1.03, blur: 16 });
  // editor lines
  sc.lineEls.forEach(L => {
    const ins = sc.inserts && Object.entries(sc.inserts).find(([, at]) => at === L.i && sc.insertLine === L.i);
    let p = spring(t - (sc.body - .1 + L.i * .06), .45) * (1 - out);
    if (sc.insertLine === L.i) p *= spring(t - sc.insertAt, .4);
    L.root.style.visibility = (!live || p <= .002) ? 'hidden' : 'visible';
    if (!live) return;
    L.root.style.top = sLineY(sc, L.i, t) + 'px';
    L.root.style.opacity = clamp(p).toFixed(3);
    L.root.style.transform = `translateY(${((1 - clamp(p)) * 8).toFixed(2)}px)`;
    L.g.textContent = String(L.i + 1 - (sc.insertLine != null && L.i > sc.insertLine && t < sc.insertAt ? 1 : 0));
    if (sc.insertLine === L.i) {
      const n = Math.round(L.cw.textContent.length * lin(t, sc.insertAt + .2, sc.insertAt + 1.1));
      L.cw.style.display = 'inline-block'; L.cw.style.overflow = 'hidden'; L.cw.style.verticalAlign = 'bottom';
      L.cw.style.width = (n * CW * sc.font / 18).toFixed(1) + 'px';
      L.root.style.background = `rgba(185,140,255,${(.14 * pres(t, sc.insertAt, sc.insertAt + 1.8, .4, .9)).toFixed(3)})`;
      L.root.style.borderRadius = '8px';
    }
    let m = null;
    for (const mk of sc.marks) if (mk[1] === L.i && t >= mk[0] && t < (mk[4] || sc.t1)) m = mk;
    if (!m) L.st.style.opacity = 0;
    else {
      L.st.textContent = m[2]; L.st.style.color = m[3];
      L.st.style.opacity = clamp(pres(t, m[0], (m[4] || sc.t1) - .15, .36, .25)).toFixed(3);
      L.st.style.transform = `scale(${lerp(.4, 1, spring(t - m[0], .36, .55)).toFixed(3)})`;
    }
  });
  if (sc.layout) {
    // a taller editor for this act, and a shorter run panel under it
    const k = live ? clamp(pres(t, sc.t0 + .8, sc.t1, .55, .45)) : 0;
    ed.style.height = lerp(ED.h, sc.layout.edH, k).toFixed(1) + 'px';
    term.style.top = lerp(TERM.y, sc.layout.termTop, k).toFixed(1) + 'px';
    term.style.height = lerp(TERM.h, sc.layout.termH, k).toFixed(1) + 'px';
  }
  if (!live) {
    sc.frames.forEach(f => mat(f.e, 0));
    [...sc.boxEls, ...sc.chipEls, ...sc.timerEls.map(x => x.e), sc.hudEl && sc.hudEl.h].forEach(e => { if (e) e.style.visibility = 'hidden'; });
    return false;
  }
  // current line
  if (sc.hl.length && t >= sc.body) {
    hl.style.top = track(t, sc.hl.map(([tt, i]) => [tt, sLineY(sc, i, t)]), .32) + 'px';
    hl.style.opacity = (clamp(track(t, sc.hl.map(([tt, , o]) => [tt, o]), .3)) * (1 - out)).toFixed(3);
    const bad = sc.hlBad && t >= sc.hlBad[0] && t < sc.hlBad[1];
    hl.style.background = bad ? 'rgba(242,193,78,.13)' : 'rgba(90,169,255,.12)';
    hl.style.boxShadow = bad ? 'inset 0 0 0 1px rgba(242,193,78,.45)' : 'inset 0 0 0 1px rgba(90,169,255,.28)';
  } else if (t >= sc.t0) { hl.style.opacity = 0; }
  // pages
  sc.frames.forEach(f => {
    const p = pres(t, f.in != null ? f.in : sc.body - .3, f.out != null ? f.out : sc.t1, .5, .45);
    const dip = (f.reloads || []).reduce((d, r) => Math.max(d, Math.sin(Math.PI * lin(t, r, r + .45)) * .3), 0);
    mat(f.e, p * (1 - dip), { dy: 0, s0: 1.015, blur: 10 });
    if (f.pop) {
      let np = 0;
      for (const [a, b] of f.popTimes) np = Math.max(np, pres(t, a, b == null ? 1e9 : b, .55, .45));
      if (np <= .002) f.pop.style.visibility = 'hidden';
      else {
        f.pop.style.visibility = 'visible'; f.pop.style.opacity = clamp(np).toFixed(3);
        f.popCard.style.transform = `translateY(${((1 - np) * 46).toFixed(2)}px) scale(${lerp(.94, 1, np).toFixed(4)})`;
        f.popCard.style.filter = np < .995 ? `blur(${((1 - clamp(np)) * 10).toFixed(2)}px)` : 'none';
      }
    }
  });
  for (const r of (sc.reloadsAll || [])) if (t >= r && t < r + 1) { prog.style.width = (easeIO(lin(t, r, r + .5)) * 100) + '%'; prog.style.opacity = 1 - lin(t, r + .55, r + .9); }
  if (t >= sc.t0) $('.url', br).innerHTML = `<span class="lock"></span>${sc.urlAt ? sc.urlAt(t) : sc.url}`;
  // typing into fields, checking boxes, pressing buttons
  sc.fills.forEach(([a, b, at, text, clear]) => {
    const n = sc.N[at];
    const v = t < a ? '' : text.slice(0, Math.round(text.length * lin(t, a, b)));
    const val = clear != null && t >= clear ? '' : v;
    if (n.value !== val) n.value = val;
  });
  (sc.checks || []).forEach(([tt, at, until]) => { sc.N[at].checked = t >= tt && t < (until || 1e9); });
  (sc.press || []).forEach(([tt, at]) => { sc.N[at].style.transform = `scale(${(1 - .05 * Math.sin(Math.PI * lin(t, tt, tt + .22))).toFixed(4)})`; });
  // cursor
  if (sc.cursorKeys && t >= sc.body - .3) {
    const [cx, cy] = trackN(t, sc.cursorKeys, .55);
    const cv = pres(t, sc.cursorOn[0], sc.cursorOn[1], .3, .3);
    cursor.style.left = (cx - 5) + 'px'; cursor.style.top = (cy - 3) + 'px';
    cursor.style.opacity = clamp(cv).toFixed(3);
    let hp = 0;
    for (const [a, b] of sc.halo) hp = Math.max(hp, pres(t, a, b, .35, .3));
    halo.style.left = (cx - 32) + 'px'; halo.style.top = (cy - 32) + 'px'; halo.style.opacity = (hp * clamp(cv)).toFixed(3);
  }
  // boxes, chips, timers, HUD
  sc.boxes.forEach((b, i) => setBox(sc.boxEls[i], grow(sc.R[b.at], b.pad || 6), { col: b.col, dashed: !!b.dashed,
    fill: b.fill || `${b.col}12`, glow: b.dashed ? 0 : 4, p: pres(t, b.t[0], b.t[1], .4, .3) }));
  sc.chips.forEach((c, i) => {
    const r = Array.isArray(c.at) ? { x: c.at[0], y: c.at[1], w: 0, h: 0, cy: c.at[1] } : sc.R[c.at];
    const [x, y] = c.place === 'above' ? [r.x, r.y - 36] : c.place === 'below' ? [r.x, r.y + r.h + 10] : [r.x + r.w + 16, r.cy - 15];
    place(sc.chipEls[i], x + (c.dx || 0), y + (c.dy || 0), pres(t, c.t[0], c.t[1], .4, .3), { dy: 8, blur: 5 });
  });
  sc.timers.forEach((tm, i) => {
    const r = sc.R[tm.at], f = lin(t, tm.t[0] + .1, tm.t[1] - .1);
    drawTimer(sc.timerEls[i], r.x + r.w + 20, r.cy - 22, pres(t, tm.t[0], tm.t[1] + .1, .35, .25), f, tm.text(f));
  });
  if (sc.hud) drawHud(sc.hudEl, t, sc.hud.x, sc.hud.y, sc.hud.t[0], sc.hud.t[1]);
  return true;
}

function drawScenes(t) {
  // the run chip: Selenium in its act, hidden before
  const sel = SCENES.find(s => s.runChip === 'sel');
  const selOn = sel ? pres(t, sel.t0 + .2, S.end, .4, .4) : 0;
  mat(runSel, selOn, { dy: -10, blur: 6 });
  if (!SCENES.length || t < S.s6 - .7) {
    for (const sc of SCENES) drawScene(sc, t);
    return;
  }
  // the earlier acts leave: their script, their pages
  const gone = spring(t - S.s6, .45);
  for (const id of Object.keys(lineEls)) {
    const L = lineEls[id];
    L.root.style.opacity = ((+L.root.style.opacity || (id === 'L0' ? 0 : 1)) * (1 - gone)).toFixed(3);
  }
  mat(f1, 0);
  f2.style.opacity = (+f2.style.opacity * (1 - gone)).toFixed(3);
  for (const sc of SCENES) drawScene(sc, t);
  if (sel) runMw.style.opacity = (+runMw.style.opacity * (1 - selOn)).toFixed(3);
}
const runSel = el('div', '', `<span class="d" style="background:${C.blue};box-shadow:0 0 12px ${C.blue}"></span>Selenium <span style="color:var(--ink3);font-weight:500">+</span> <span style="color:#d9c4ff">mendwright</span>`, stage); runSel.id = 'run';
runSel.style.visibility = 'hidden';            // shown only in its act (drawScenes)

// ---------- helpers for the definitions ----------
const sx = s => `${(+s).toFixed(2)} s`;
const logRow = (ic, text) => `<span class="i">${ic}</span><span class="tag-log">log</span>${esc(text)}`;
const msgOf = l => l.replace(/^(INFO|WARNING|ERROR) /, '');
const KW = s => `<span class="k-fn">${s}</span>`;

// ---------- act 6: a popup nobody registered ----------
{
  const F1 = D.F.F1, st = Object.fromEntries(F1.steps.map(s => [s.step, s]));
  const t0 = S.s6, b = t0 + CARD_LEN;
  scene({
    t0, t1: S.s7, title: { kicker: 'New in 0.7.0', text: 'Popups nobody registered', sub: 'mendwright.open(page, dismiss=True)' },
    lines: [
      O_('page') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('page') + P_(', ') + O_('dismiss') + P_('=') + KW('True') + P_(').') + O_('page'),
      O_('page') + P_('.') + F_('goto') + P_('(') + Str('"https://northwind.example/checkout"') + P_(')'),
      O_('page') + P_('.') + F_('click') + P_('(') + Str('"#email"') + P_(')'),
      O_('page') + P_('.') + F_('click') + P_('(') + Str('"#continue"') + P_(')'),
    ],
    frames: [{ id: 'nl', html: D.F.pages.v2_nl, popup: '#nl', popTimes: [[b + .9, b + 6.2]], in: b - .3 }],
    hl: [[b, 1, 0], [b + 1.4, 1, 1], [b + 2.0, 2, 1], [b + 7.6, 3, 1], [b + 8.5, 3, 0]],
    marks: [[b + 1.7, 1, '✓', C.green], [b + 7.3, 2, '✓', C.green], [b + 8.2, 3, '✓', C.green]],
    cursorOn: [b + 1.8, b + 9.2],
    cursor: [[b + 2.0, 'nl:input[name=email]'], [b + 5.1, 'nl:.nl-no', -6, 4], [b + 6.8, 'nl:input[name=email]'], [b + 7.6, 'nl:.btn-x82hd', 60, 2]],
    clicks: [[b + 2.7, 'nl:input[name=email]', C.red], [b + 5.8, 'nl:.nl-no', C.purple], [b + 7.3, 'nl:input[name=email]', '#fff'], [b + 8.1, 'nl:.btn-x82hd', '#fff']],
    press: [[b + 5.8, 'nl:.nl-no']],
    halo: [[b + 5.0, b + 6.1]],
    boxes: [{ at: 'nl:.nl-yes', col: C.red, dashed: true, t: [b + 3.6, b + 6.1], pad: 5 }],
    chips: [
      { at: 'nl:.nl-yes', html: 'never clicks this', place: 'above', dx: 300, style: `background:${C.red};color:#2a0606;font-size:13.5px;padding:3px 9px`, t: [b + 3.7, b + 6.1] },
      { at: 'nl:#nl > div', html: '<span class="mono" style="font-size:15px">esc</span>', place: 'above', dx: 196, style: 'background:rgba(14,18,27,.9);border:1px solid rgba(255,255,255,.25);color:#fff;padding:6px 12px;border-radius:8px', t: [b + 3.9, b + 4.5] },
    ],
    hud: { x: 600, y: 14, t: [b + 3.0, b + 9.4], rows: [
      { ic: '⊘', col: C.amber, html: '<b>A popup is in the way</b> <span class="sub">“Get 10% off your first order”</span>', t: b + 3.0 },
      { ic: '1', col: '#8b949e', html: 'Escape <span class="sub">· still there</span>', t: b + 4.5 },
      { ic: '2', col: C.green, html: '“No thanks” <span class="sub">· gone</span>', t: b + 6.1 },
      { ic: '✕', col: C.red, html: 'Never “Sign up”, never types, never accepts', t: b + 6.7 },
    ] },
    run: [
      [b + 1.3, 'cmd', '$ python checkout.py'],
      [b + 1.7, 'ok', okRow('page.goto(".../checkout")', sx(st.goto.seconds))],
      [b + 6.2, 'log', logRow('▸', msgOf(st['#email'].log.find(l => /popup/.test(l))))],
      [b + 7.3, 'ok', okRow('page.click("#email")', sx(st['#email'].seconds))],
      [b + 8.2, 'ok', okRow('page.click("#continue")', sx(st['#continue'].seconds))],
      [b + 8.5, 'sum', `<span style="color:${C.green}">Passed</span> · popup declined, “Sign up” never clicked`],
    ],
    caps: [
      [b - .1, b + 2.9, 'The checkout now shows a popup mendwright was never told about.'],
      [b + 3.0, S.s7 - .3, '<em>dismiss=True</em>: Escape first. Still there? Then “No thanks”. <s>Never “Sign up”.</s>'],
    ],
    status: [[b + 1.3, 'run', 'Running', b + 8.6], [b + 8.6, 'ok', 'Passed · popup declined', S.s7 - .2]],
  });
}

// ---------- act 7: popups that need judgement ----------
const SM = D.SM;
const L_open = O_('heal') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('page') + P_(', ') + O_('dismiss') + P_('=') + KW('True') + P_(')');
const L_click = O_('heal') + P_('.') + O_('page') + P_('.') + F_('click') + P_('(') + Str('"input[name=email]"') + P_(')');
const L_intr = (k, s, extra) => O_('heal') + P_('.') + F_('interrupt') + P_('(') + Str(`"${k}"`) + P_(', ') + Str(`"${s}"`) + (extra || '') + P_(')');
{
  // 7a: the upsell
  const u = SM.upsell, t0 = S.s7, b = t0 + CARD_LEN;
  scene({
    t0, t1: S.s7b, title: { kicker: 'dismiss=True', text: 'Popups that need judgement', sub: 'what it clicks, and what it won’t', col: C.amber },
    lines: [L_open, L_click],
    frames: [{ id: 'u', html: SM.pages.upsell, popup: '#pop', popTimes: [[b + .4, b + 4.5]], in: b - .3 }],
    hl: [[b, 1, 0], [b + 1.0, 1, 1], [b + 6.1, 1, 0]],
    marks: [[b + 5.6, 1, '✓', C.green]],
    cursorOn: [b + .9, b + 6.6],
    cursor: [[b + 1.0, 'u:input[name=email]'], [b + 3.6, 'u:#pop button:nth-of-type(2)', -6, 4], [b + 5.0, 'u:input[name=email]']],
    clicks: [[b + 1.7, 'u:input[name=email]', C.red], [b + 4.3, 'u:#pop button:nth-of-type(2)', C.purple], [b + 5.6, 'u:input[name=email]', '#fff']],
    press: [[b + 4.3, 'u:#pop button:nth-of-type(2)']],
    halo: [[b + 3.5, b + 4.6]],
    boxes: [{ at: 'u:#pop button:nth-of-type(1)', col: C.red, dashed: true, t: [b + 2.2, b + 4.5], pad: 5 }],
    chips: [{ at: 'u:#pop button:nth-of-type(1)', html: 'costs money: never', place: 'above', dx: 290, style: `background:${C.red};color:#2a0606;font-size:13.5px;padding:3px 9px`, t: [b + 2.3, b + 4.5] }],
    hud: { x: 600, y: 14, t: [b + 2.0, b + 6.8], rows: [
      { ic: '⊘', col: C.amber, html: '<b>A popup is in the way</b> <span class="sub">“Skip the queue?”</span>', t: b + 2.0 },
      { ic: '✕', col: C.red, html: '“Skip the line – $9.99” <span class="sub">· a price: never clicked</span>', t: b + 2.6 },
      { ic: '✓', col: C.green, html: '“No thanks” <span class="sub">· clicked, popup gone</span>', t: b + 4.6 },
    ] },
    run: [
      [b + .9, 'cmd', '$ python checkout.py'],
      [b + 4.6, 'log', logRow('▸', msgOf(u.log.find(l => /popup/.test(l))))],
      [b + 5.6, 'ok', okRow('heal.page.click(...)', sx(u.seconds))],
      [b + 5.9, 'sum', `<span style="color:${C.green}">Passed</span> · clicked: ${esc(u.page.clicked.map(c => '“' + c + '”').join(', '))}`],
    ],
    caps: [[b + .2, S.s7b - .3, 'A paid “Skip the line” is <s>never clicked</s>. <u>“No thanks” is.</u>']],
    status: [[b + .9, 'run', 'Running', b + 5.9], [b + 5.9, 'ok', 'Passed', S.s7b - .2]],
  });
}
{
  // 7b: "Dismiss to cancel" - left alone; then the script says what it wants
  const a = SM.order_alone, r = SM.order_registered, t0 = S.s7b, b = t0, ins = t0 + 6.0, b2 = ins + 1.6;
  scene({
    t0, t1: S.s7c,
    lines: [L_open, O_('heal') + P_('.') + F_('interrupt') + P_('(') + Str('"order"') + P_(', ') + Str(`'text="Continue with order"'`) + P_(')'), L_click],
    insertLine: 1, insertAt: ins, inserts: { [ins]: 1 },
    frames: [{ id: 'o', html: SM.pages.order, popup: '#pop', popTimes: [[b + .6, b2 + 1.6], [ins + .9, b2 + 1.6]], in: b - .3, reloads: [ins + .5] }],
    reloadsAll: [ins + .5],
    hl: [[b, 2, 0], [b + 1.2, 2, 1], [b + 5.0, 2, 0], [b2, 2, 1], [b2 + 3.0, 2, 0]],
    hlBad: [b + 3.9, ins],
    marks: [[b + 3.9, 2, '⊘', C.amber, ins], [b2 + 2.6, 2, '✓', C.green]],
    cursorOn: [b + 1.1, b2 + 3.4],
    cursor: [[b + 1.2, 'o:input[name=email]'], [b + 4.6, [760, 560]], [b2 + .3, 'o:#pop button:nth-of-type(1)', 0, 4], [b2 + 2.0, 'o:input[name=email]']],
    clicks: [[b + 1.8, 'o:input[name=email]', C.red], [b2 + 1.0, 'o:#pop button:nth-of-type(1)', C.purple], [b2 + 2.6, 'o:input[name=email]', '#fff']],
    press: [[b2 + 1.0, 'o:#pop button:nth-of-type(1)']],
    halo: [[b2 + .2, b2 + 1.3]],
    boxes: [
      { at: 'o:#pop > div > div:nth-of-type(2)', col: C.amber, dashed: true, t: [b + 2.6, b + 5.6], pad: 4 },
      { at: 'o:#pop button:nth-of-type(2)', col: C.red, dashed: true, t: [b + 2.9, b + 5.6], pad: 4 },
    ],
    chips: [{ at: 'o:#pop button:nth-of-type(2)', html: 'cancels the order: never', place: 'below', dx: 120, style: `background:${C.red};color:#2a0606;font-size:13.5px;padding:3px 9px`, t: [b + 3.0, b + 5.6] }],
    hud: { x: 600, y: 14, t: [b + 2.2, b + 5.8], rows: [
      { ic: '⊘', col: C.amber, html: '<b>A popup is in the way</b> <span class="sub">“Your order is on hold”</span>', t: b + 2.2 },
      { ic: '!', col: C.amber, html: 'Its text says “Dismiss” cancels the order', t: b + 2.8 },
      { ic: '✓', col: C.green, html: 'Left alone: <b>nothing clicked</b>, it stops and says why', t: b + 3.6 },
    ] },
    run: [
      [b + 1.1, 'cmd', '$ python checkout.py'],
      [b + 3.8, 'log', logRow('⊘', msgOf(a.log.find(l => /popup/.test(l))))],
      [ins + 1.4, 'cmd', '$ python checkout.py   # with the interrupt line'],
      [b2 + 1.1, 'log', logRow('▸', msgOf(r.log.find(l => /interruption/.test(l))))],
      [b2 + 2.6, 'ok', okRow('heal.page.click(...)', sx(r.seconds))],
      [b2 + 2.9, 'sum', `<span style="color:${C.green}">Passed</span> · clicked: ${esc(r.page.clicked.map(c => '“' + c + '”').join(', '))}`],
    ],
    caps: [
      [b + .4, ins - .1, 'Here “Dismiss” would cancel the order. So it <u>clicks nothing</u>, stops, and says why.'],
      [ins, S.s7c - .3, 'You decide with <em>one line</em>. The order goes on, and “Dismiss” is never clicked.'],
    ],
    status: [[b + 1.1, 'run', 'Running', b + 3.9], [b + 3.9, 'bad', 'Stopped · popup left alone', ins + .2],
      [b2 - .2, 'run', 'Running', b2 + 2.9], [b2 + 2.9, 'ok', 'Passed · order continued', S.s7c - .2]],
  });
}
{
  // 7c: re-enter a detail: two registered interruptions fill it in mid-step
  const z = SM.zip_registered, t0 = S.s7c, b = t0;
  const zl = z.log.filter(l => /interruption/.test(l));
  scene({
    t0, t1: S.s8, font: 15, lh: 34,
    lines: [L_open, L_intr('zip', '#zip', P_(', ') + O_('action') + P_('=') + Str('"fill"') + P_(', ') + O_('args') + P_('=(') + Str('"10115"') + P_(',)')),
      L_intr('zip_ok', '#zip-ok'), L_click],
    frames: [{ id: 'z', html: SM.pages.zip, popup: '#pop', popTimes: [[b + .8, b + 5.2]], in: b - .3 }],
    hl: [[b, 3, 0], [b + 1.6, 3, 1], [b + 2.6, 1, 1], [b + 4.4, 2, 1], [b + 5.4, 3, 1], [b + 6.6, 3, 0]],
    marks: [[b + 3.6, 1, '✓', C.green], [b + 5.0, 2, '✓', C.green], [b + 6.2, 3, '✓', C.green]],
    cursorOn: [b + 1.5, b + 7.0],
    cursor: [[b + 1.6, 'z:input[name=email]'], [b + 2.6, 'z:#zip', -40, 2], [b + 4.4, 'z:#zip-ok', 0, 4], [b + 5.6, 'z:input[name=email]']],
    clicks: [[b + 2.9, 'z:#zip', C.purple], [b + 4.9, 'z:#zip-ok', C.purple], [b + 6.2, 'z:input[name=email]', '#fff']],
    press: [[b + 4.9, 'z:#zip-ok']],
    halo: [[b + 2.5, b + 5.1]],
    fills: [[b + 3.0, b + 3.6, 'z:#zip', z.page.zip || '10115']],
    boxes: [{ at: 'z:#zip', col: C.blue, t: [b + 2.7, b + 5.1], pad: 4 }],
    hud: { x: 600, y: 14, t: [b + 2.2, b + 6.9], rows: [
      { ic: '▸', col: C.blue, html: '<b>“Confirm your ZIP code”</b> <span class="sub">registered as two steps</span>', t: b + 2.2 },
      { ic: '1', col: C.green, html: 'fill <span class="mono">#zip</span> with 10115', t: b + 3.6 },
      { ic: '2', col: C.green, html: 'click <span class="mono">#zip-ok</span> · popup gone', t: b + 5.0 },
    ] },
    run: [
      [b + 1.5, 'cmd', '$ python checkout.py'],
      [b + 3.6, 'log', logRow('▸', msgOf(zl[0]))],
      [b + 5.0, 'log', logRow('▸', msgOf(zl[1]))],
      [b + 6.2, 'ok', okRow('heal.page.click(...)', sx(z.seconds))],
      [b + 6.5, 'sum', `<span style="color:${C.green}">Passed</span> · ZIP re-entered mid-step`],
    ],
    caps: [
      [b + .5, b + 2.5, 'Mid-checkout, the site asks you to re-enter your ZIP code.'],
      [b + 2.6, S.s8 - .3, 'Register the answer once. mendwright <u>fills it in whenever it pops up</u>.'],
    ],
    status: [[b + 1.5, 'run', 'Running', b + 6.5], [b + 6.5, 'ok', 'Passed', S.s8 - .2]],
  });
}

// ---------- act 8: steps in any order (built when the real run exists) ----------
if (D.ST && window.stepsAct) window.stepsAct();

// ---------- act 9: Selenium ----------
{
  const SELd = D.SEL, sm = SELd.mendwright, t0 = S.s9, b = t0 + CARD_LEN;
  scene({
    t0, t1: S.end, runChip: 'sel', title: { kicker: 'Same API', text: 'Selenium too', sub: 'mendwright.open(driver).driver', col: C.blue },
    lines: [
      O_('driver') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('driver') + P_(', ') + O_('cookies') + P_('=') + Str('"reject"') + P_(').') + O_('driver'),
      O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"email"') + P_(').') + F_('click') + P_('()'),
      O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"accept-terms"') + P_(').') + F_('click') + P_('()'),
      O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"continue"') + P_(').') + F_('click') + P_('()'),
    ],
    frames: [{ id: 's', html: D.pages.v1, popup: '#cc-wrap', popTimes: [[b - .4, b + 3.4]], in: b - .3 }],
    hl: [[b, 1, 0], [b + 1.4, 1, 1], [b + 4.0, 2, 1], [b + 4.8, 3, 1], [b + 5.7, 3, 0]],
    marks: [[b + 3.8, 1, '✓', C.green], [b + 4.6, 2, '✓', C.green], [b + 5.4, 3, '✓', C.green]],
    cursorOn: [b + 1.3, b + 6.2],
    cursor: [[b + 1.4, 's:#email'], [b + 2.4, 's:#cc-wrap button', -10, 4], [b + 3.4, 's:#email'], [b + 4.0, 's:#accept-terms'], [b + 4.8, 's:#continue', -30, 4]],
    clicks: [[b + 1.9, 's:#email', C.red], [b + 3.1, 's:#cc-wrap button', C.purple], [b + 3.8, 's:#email', '#fff'], [b + 4.6, 's:#accept-terms', '#fff'], [b + 5.4, 's:#continue', '#fff']],
    press: [[b + 3.1, 's:#cc-wrap button'], [b + 5.4, 's:#continue']],
    checks: [[b + 4.6, 's:#accept-terms']],
    halo: [[b + 2.3, b + 3.4]],
    chips: [
      { at: 's:#email', html: `<span style="color:${C.red}">⊘</span>&nbsp; plain Selenium: element click intercepted`, place: 'above', dx: 120, style: `background:rgba(14,18,27,.9);border:1px solid ${C.red}66;color:#ffd3d3;font-size:15px;padding:8px 13px;border-radius:11px`, t: [b + .2, b + 1.4] },
      { at: 's:#cc-wrap > div', html: '▸ cookie banner in the way · clicking “Reject all”', place: 'above', style: `background:${C.amber};color:#1b1405;font-size:15.5px`, t: [b + 1.9, b + 3.3] },
    ],
    run: [
      [b - .1, 'cmd', '# plain Selenium, same page'],
      [b + .1, 'err', `<b>${esc(SELd.plain_type)}</b>: ${esc(SELd.plain_msg)}`],
      [b + 3.2, 'log', logRow('▸', SELd.log)],
      [b + 3.8, 'ok', okRow('find_element(By.ID, "email").click()', sx(sm[0].seconds))],
      [b + 4.6, 'ok', okRow('find_element(By.ID, "accept-terms").click()', sx(sm[1].seconds))],
      [b + 5.4, 'ok', okRow('find_element(By.ID, "continue").click()', sx(sm[2].seconds))],
    ],
    caps: [[b - .1, S.end - .3, 'Same on Selenium: <s>find_element(...).click() died on the banner.</s> <u>Now it’s rejected first.</u>']],
    status: [[b + 1.3, 'run', 'Running', b + 5.7], [b + 5.7, 'ok', 'Passed · banner rejected', S.end - .2]],
  });
}

window.measureScenes = measureScenes;
window.drawScenes = drawScenes;
