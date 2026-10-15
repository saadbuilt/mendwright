// mendwright explainer: a deterministic composition. render(t) draws the frame at t seconds
// as a pure function of t, so every frame can be captured at an exact time.
// One composition, five cuts (window.CUT): 'main' and the clips 'A'..'D'. Every time below is
// a real second of the cut. Motion: critically damped springs, materialize transitions.
// Every selector, log fragment, outcome and run time on screen comes from window.DATA,
// which render.py builds from the capture JSON of real runs.
'use strict';
const D = window.DATA;
const CUT = window.CUT || 'main';

// ---------- motion math ----------
function spring(t, resp = 0.42, damp = 1) {
  if (t <= 0) return 0;
  const w = 2 * Math.PI / resp;
  if (damp >= 1) return 1 - (1 + w * t) * Math.exp(-w * t);
  const wd = w * Math.sqrt(1 - damp * damp);
  return 1 - Math.exp(-damp * w * t) * (Math.cos(wd * t) + (damp * w / wd) * Math.sin(wd * t));
}
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lin = (t, t0, t1) => clamp((t - t0) / (t1 - t0));
const lerp = (a, b, p) => a + (b - a) * p;
const easeIO = p => p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
function pres(t, tin, tout = 1e9, ri = 0.42, ro = 0.34) {
  return spring(t - tin, ri) * (1 - clamp(spring(t - tout, ro)));
}
// Additive spring track: each key re-targets from wherever the value is.
function track(t, keys, resp = 0.5, damp = 1) {
  let v = keys[0][1];
  for (let i = 1; i < keys.length; i++) v += (keys[i][1] - keys[i - 1][1]) * spring(t - keys[i][0], keys[i][2] || resp, damp);
  return v;
}
function trackN(t, keys, resp, damp) {
  const n = keys[0][1].length, out = [];
  for (let j = 0; j < n; j++) out.push(track(t, keys.map(k => [k[0], k[1][j], k[2]]), resp, damp));
  return out;
}
function mat(e, p, o = {}) {
  const { dy = 22, dx = 0, s0 = .97, blur = 12, origin } = o;
  if (p <= 0.002) { if (e.style.visibility !== 'hidden') e.style.visibility = 'hidden'; return; }
  e.style.visibility = 'inherit';
  e.style.opacity = clamp(p).toFixed(4);
  if (origin) e.style.transformOrigin = origin;
  e.style.transform = `translate(${((1 - p) * dx).toFixed(2)}px,${((1 - p) * dy).toFixed(2)}px) scale(${lerp(s0, 1, p).toFixed(4)})`;
  const b = (1 - clamp(p)) * blur;
  e.style.filter = b > 0.05 ? `blur(${b.toFixed(2)}px)` : 'none';
}
// piecewise-linear interpolation of [[x, y], ...]
function pw(keys, x) {
  if (x <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) if (x <= keys[i][0]) return lerp(keys[i - 1][1], keys[i][1], lin(x, keys[i - 1][0], keys[i][0]));
  return keys[keys.length - 1][1];
}

// ---------- DOM helpers ----------
const $ = (s, r = document) => r.querySelector(s);
function el(tag, cls, html, parent, style) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  if (style) e.style.cssText = style;
  (parent || document.body).appendChild(e);
  return e;
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------- layout (1920x1080; the same in every cut) ----------
const LAYOUT = {
  ED: { x: 40, y: 56, w: 640, h: 400 }, TERM: { x: 40, y: 476, w: 640, h: 454 }, BR: { x: 712, y: 56, w: 1168, h: 874 },
  LH: 50, HEAD: 56, CHROME: 46, ROW0: 12, Z: 1.4, CARD_AT: [16, 120],
};
const { ED, TERM, BR, LH, Z } = LAYOUT;
const CO = { x: BR.x, y: BR.y + LAYOUT.CHROME, w: BR.w, h: BR.h - LAYOUT.CHROME };   // browser content on the stage
const C = { green: '#3fd47a', red: '#ff6b6b', blue: '#5aa9ff', purple: '#b98cff', amber: '#f2c14e', grey: '#9aa4b2', white: '#ffffff' };

// ---------- static DOM ----------
const bg = el('div', '', null, document.body); bg.id = 'bg';
const g1 = el('div', 'glow', null, bg, 'width:900px;height:700px;left:-200px;top:-260px;background:rgba(124,92,255,.30)');
const g2 = el('div', 'glow', null, bg, 'width:900px;height:700px;right:-260px;bottom:-300px;background:rgba(40,160,255,.20)');
el('div', '', null, bg).id = 'grid';
const stage = el('div', '', null, document.body); stage.id = 'stage';

const ed = el('div', 'panel', `<div class="phead"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="t">checkout.py</span></div><div class="code"><div id="hl"></div></div><div class="veil"></div>`, stage,
  `left:${ED.x}px;top:${ED.y}px;width:${ED.w}px;height:${ED.h}px`);
ed.id = 'ed';
const code = $('.code', ed), hl = $('#hl', ed), edHead = $('.phead', ed);
const term = el('div', 'panel', `<div class="phead"><span class="dot" style="background:rgba(255,255,255,.18)"></span><span class="t">Run</span></div><div class="veil"></div>`, stage,
  `left:${TERM.x}px;top:${TERM.y}px;width:${TERM.w}px;height:${TERM.h}px`);
term.id = 'term';
const br = el('div', 'panel', `<div class="chrome"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="nav">‹ ›</span><div class="url"><span class="lock"></span><span class="u">northwind.example/checkout</span></div></div><div id="prog"></div><div class="content"><div id="ov"></div></div><div class="veil"></div>`, stage,
  `left:${BR.x}px;top:${BR.y}px;width:${BR.w}px;height:${BR.h}px`);
br.id = 'br';
const content = $('.content', br), ov = $('#ov', br), prog = $('#prog', br), urlEl = $('.url .u', br);
const VEIL = { code: $('.veil', ed), run: $('.veil', term), page: $('.veil', br) };
const ann = el('div', '', `<svg width="1920" height="1080"></svg>`, stage); ann.id = 'ann';
const annSvg = $('svg', ann);
const sveil = el('div', '', null, stage); sveil.id = 'sveil';
const band = el('div', '', null, document.body); band.id = 'band';
const cursor = el('div', '', `<svg width="36" height="36" viewBox="0 0 30 30"><path d="M5 3 L5 24 L10.5 18.8 L14.2 27 L18 25.3 L14.4 17.3 L22 17.3 Z" fill="#fff" stroke="#11151d" stroke-width="1.6" stroke-linejoin="round"/></svg>`, ov); cursor.id = 'cursor';
const halo = el('div', '', null, ov); halo.id = 'halo';

// ---------- code tokens ----------
const P_ = s => `<span class="k-p">${esc(s)}</span>`, O_ = s => `<span class="k-obj">${esc(s)}</span>`,
  F_ = s => `<span class="k-fn">${esc(s)}</span>`, MW = s => `<span class="k-mw">${esc(s)}</span>`,
  Str = (s, k) => `<span class="k-str${k ? ' sel' : ''}"${k ? ` data-k="${k}"` : ''}>${esc(s)}</span>`,
  KW = s => `<span class="k-fn">${esc(s)}</span>`, FOLD = `<span class="k-fold">  ⋯</span>`;
const IND = n => ' '.repeat(n);
// Python source for a captured keyword argument's value (the scripts' own quoting: "...")
const pyv = v => v === true ? 'True' : v === false ? 'False' : v === null ? 'None' : typeof v === 'number' ? String(v) : JSON.stringify(v);
const kwArg = (k, v) => `<span style="color:${C.purple}">${esc(k)}=${esc(pyv(v))}</span>`;
// heal = mendwright.open(page, k=v, ...): on one row when it is short, else wrapped
function openRows(obj, kwargs, tail) {
  const kws = Object.entries(kwargs || {});
  const one = O_(obj) + ' ' + P_('=') + ' ' + MW('mendwright') + P_('.') + F_('open') + P_('(') + O_(obj === 'heal' ? 'page' : obj);
  const end = P_(')') + (tail ? P_('.') + O_(tail) : '');
  if (!kws.length) return [{ id: 'o1', n: 1, html: one + end }];
  return [{ id: 'o1', n: 1, html: O_(obj) + ' ' + P_('=') + ' ' + MW('mendwright') + P_('.') + F_('open') + P_('(') },
    { id: 'o2', n: '', html: IND(4) + O_(obj === 'heal' ? 'page' : obj) + P_(', ') + kws.map(([k, v]) => kwArg(k, v)).join(P_(', ')) + end }];
}
// heal.page.click("sel", k=v): the selector, then the captured keyword arguments on a row below
function clickRows(sel, kwargs, n, id = 'c') {
  const kws = Object.entries(kwargs || {}), head = O_('heal') + P_('.') + O_('page') + P_('.') + F_('click') + P_('(') + Str(JSON.stringify(sel));
  if (!kws.length) return [{ id: id + '1', n, html: head + P_(')') }];
  return [{ id: id + '1', n, html: head + P_(',') },
    { id: id + '2', n: '', html: IND('heal.page.click('.length) + kws.map(([k, v]) => O_(k) + P_('=') + KW(pyv(v))).join(P_(', ')) + P_(')') }];
}

// ---------- the timeline: every cut declares its items here, in real seconds ----------
const TL = {
  dur: 0, beats: [], code: [], frames: [], runs: [], caps: [], callouts: [], tags: [], boxes: [], clicks: [],
  cur: [], curOn: [], halo: [], focus: [], fills: [], checks: [], press: [], urls: [], badges: [], reloads: [],
  keycaps: [], pstamps: [], montage: [], stageIn: 0, stageOut: 1e9, title: null, end: null, ctitle: null, estrip: null,
  lead: 0,      // a clip's title holds this long (real s) before the story's clock starts
};
const beat = (t, name, real) => TL.beats.push({ t, name, real: !!real });
const cap = (a, b, html) => TL.caps.push({ a, b, html });
const focus = (t, zone) => TL.focus.push([t, zone]);
const url = (t, u) => TL.urls.push([t, u]);
const click = (t, at, col) => TL.clicks.push({ t, at, col });
const cur = (t, at, dx = 0, dy = 0, edge) => TL.cur.push({ t, at, dx, dy, edge });
const curOn = (a, b) => TL.curOn.push([a, b]);
const haloOn = (a, b) => TL.halo.push([a, b]);
const fill = (a, b, at, text, until = 1e9) => TL.fills.push({ a, b, at, text, until });
const check = (t, at, until = 1e9) => TL.checks.push({ t, at, until });
const press = (t, at) => TL.press.push({ t, at });
const box = o => TL.boxes.push(Object.assign({ pad: 7, dashed: false }, o));
const tag = o => TL.tags.push(Object.assign({ zone: 'page', place: 'right' }, o));
const callout = o => TL.callouts.push(Object.assign({ zone: 'page', place: 'auto' }, o));
const keycap = o => TL.keycaps.push(o);
const pstamp = o => TL.pstamps.push(o);
const badge = (t, html, col) => TL.badges.push({ t, html, col });
const reload = t => TL.reloads.push(t);
const montage = (a, b) => TL.montage.push([a, b]);
function codeBlock(o) { const b = Object.assign({ marks: [], hl: [], tok: {} }, o); TL.code.push(b); return b; }
function frame(o) { const f = Object.assign({ pop: [], reloads: [] }, o); TL.frames.push(f); return f; }
function run(o) { const r = Object.assign({ receipts: [] }, o); TL.runs.push(r); return r; }

// ---------- formatting and receipts (verbatim pieces of real log lines) ----------
const secs = s => (+s).toFixed(2);
function verbatim(line, part) {
  if (!line || !line.includes(part)) throw new Error('receipt is not in the log line: ' + part);
  return part;
}
// "a … b": both pieces verbatim and in this order in the real line
function elide(line, a, b) {
  const i = line.indexOf(a), j = line.indexOf(b, i + a.length);
  if (i < 0 || j < 0) throw new Error('elision pieces are not in the log line: ' + a + ' / ' + b);
  return a + ' … ' + b;
}
const msgOf = l => l.replace(/^(INFO|WARNING|ERROR) /, '');
// The speed of a run's clock between story times a and b: round(real / shown).
function speedOf(keys, a, b) {
  const r = (pw(keys, b) - pw(keys, a)) / (b - a);
  if (r <= 1.5) throw new Error('not sped up between ' + a + ' and ' + b);
  return Math.round(r);
}

// ---------- the main cut's data, from the real runs ----------
const XP = D.xpaths;
const SCRIPT = D.script;                              // [[verb, arg], ...] exactly as capture.py ran it
const TIMEOUT = D.D_timeout_ms / 1000;                // mendwright's default wait before it repairs
const GB = Math.round(D.model_bytes / 1e9);           // the local model's size on disk
function scriptRows() {
  const row = ([verb, arg], k) => verb === 'type'
    ? O_('page') + P_('.') + O_('keyboard') + P_('.') + F_('type') + P_('(') + Str(JSON.stringify(arg)) + P_(')')
    : O_('page') + P_('.') + F_(verb) + P_('(') + Str(JSON.stringify(arg), k) + P_(')');
  const keys = { '#email': 'email', '#accept-terms': 'terms', '#continue': 'cont' };
  return SCRIPT.map(s => row(s, keys[s[1]]));
}
const OPEN_ROW = O_('page') + ' ' + P_('=') + ' ' + MW('mendwright') + P_('.') + F_('open') + P_('(') + O_('page') + P_(').') + O_('page');

// "Two checkboxes. Memory isn't sure which.": in the real run the fingerprint tier found two
// elements that score alike, the model was asked to pick between 2 candidates, and the page
// has exactly two checkboxes (the two amber boxes).
function twoCandidates() {
  const note = D.D_fingerprint_notes.find(n => n.key === SCRIPT[2][1]);
  if (!note || note.applied || !/two elements score alike/.test(note.note)) throw new Error('B4: the fingerprint tier did not find two alike elements: ' + JSON.stringify(note));
  const prop = D.D_proposed.find(x => x.key === SCRIPT[2][1]);
  if (!prop || !/candidate \d+ of 2\b/.test(prop.reasoning)) throw new Error('B4: the model did not pick between 2 candidates: ' + JSON.stringify(prop));
  if (D.D_checked.length !== 2) throw new Error('B4: the page does not have two checkboxes');
  return note.note;
}

// ---------- B0: the hook (also the poster frame) ----------
function hookCard(t0, t1) {
  if (D.D_heals[0].source !== 'fingerprint') throw new Error('the hook says "from memory, no AI": D_heals[0] is not a fingerprint repair');
  TL.title = { t0, t1 };
  beat(t0, 'B0 poster');
  beat(t0 + 1.4, 'B0 selector swaps');
  beat(t0 + 2.9, 'B0 found from memory');
}
// ---------- B8: the end card ----------
function endCard(t0, t1) { TL.end = { t0, t1 }; beat(t0 + 3.2, 'B8 end card'); }
// The clip title: on a plate, over the stage with every zone veiled (the cut's first focus
// key is 'none'). It holds LEAD s longer than the story's t1, so the story starts LEAD late.
const LEAD = .8;
function clipTitle(t1, title, api) {
  TL.ctitle = { t0: 0, t1, title, api }; TL.stageIn = -2; TL.lead = LEAD;
  focus(0, 'none');
  beat(1.2, 'title', true);
}
// The end strip: on a plate, every zone veiled; the last run's stamp and receipts and the
// page's annotations leave as it comes in, so nothing competes with the wordmark.
function endStrip(t0) {
  TL.estrip = { t0, t1: t0 + 2.4 }; TL.dur = t0 + 2.4;
  focus(t0, 'none');
  for (const x of [...TL.runs, ...TL.callouts]) x.t1 = Math.min(x.t1, t0);
  for (const x of [...TL.tags, ...TL.boxes, ...TL.pstamps, ...TL.keycaps]) x.t[1] = Math.min(x.t[1], t0);
  beat(t0 + 1.2, 'end strip');
  return t0 + 2.4;
}

// ---------- main cut: B1..B6 ----------
function mainStory() {
  const V1 = frame({ id: 'v1', html: D.pages.v1, t: [4.2, 11.7], popup: '#cc-wrap', pop: [] });   // today's site, no banner
  const V2 = frame({ id: 'v2', html: D.pages.v2, t: [11.75, 48.4], reloads: [41.4] });
  if (V2.html.match(/id="(email|accept-terms|continue)"/)) throw new Error('V2 still has an old id');
  url(4.2, 'northwind.example/checkout');
  const rows = scriptRows();
  const blk = codeBlock({
    t0: 4.2, t1: 48.4,
    rows: [{ id: 'L0', html: OPEN_ROW, n: 1, type: [4.8, 5.6], tint: [4.8, 7.2] },
      { id: 'L1', html: rows[0], n: 2 }, { id: 'L2', html: rows[1], n: 3 }, { id: 'L3', html: rows[2], n: 4 }, { id: 'L4', html: rows[3], n: 5 }],
  });
  // ----- B1: today; one line, and it remembers
  beat(4.6, 'B1 empty line 1');
  focus(4.4, 'code');
  cap(4.4, 7.8, 'Add <em>one line</em> to your script.');
  beat(5.9, 'B1 one line typed in');
  focus(6.2, 'page');
  // no clock here: B1 is about memory; the only small run time on screen is the next run's
  const M = D.M;
  if (M.steps.length !== SCRIPT.length) throw new Error('M: the run did not run the whole script');
  const fp = Object.values(M.fingerprints).filter(Boolean).length;
  if (fp !== 3) throw new Error('M: not all three elements were remembered');
  run({ t0: 6.2, t1: 11.7, cmd: [6.4, '$ python checkout.py'],
    stamp: { t: 9.9, kind: 'ok', html: `✓ Passed · ${fp} elements remembered` } });
  curOn(6.1, 9.8);
  cur(6.0, [CO.x + 560, CO.y + 520]); cur(6.25, 'v1:#email', -60, 4); cur(8.1, 'v1:#accept-terms', 2, 3); cur(8.8, 'v1:#continue', -40, 4); cur(9.5, [CO.x + 640, CO.y + 560]);
  click(6.8, 'v1:#email', C.white); fill(6.9, 7.45, 'v1:#email', 'ada@example.com');
  click(8.6, 'v1:#accept-terms', C.white); check(8.6, 'v1:#accept-terms');
  click(9.3, 'v1:#continue', C.white); press(9.3, 'v1:#continue');
  const REM = `<span style="color:${C.blue}">◆</span> remembered`;
  tag({ at: 'v1:#email', html: REM, col: C.blue, t: [6.8, 11.7], col0: 'v1:.card' });
  tag({ at: 'v1:label.chk', html: REM, col: C.blue, t: [8.75, 11.7], col0: 'v1:.card' });
  tag({ at: 'v1:#continue', html: REM, col: C.blue, t: [9.45, 11.7], col0: 'v1:.card' });
  blk.hl.push([4.2, 'L1', 0], [6.4, 'L1', 1], [7.0, 'L2', 1], [8.1, 'L3', 1], [8.9, 'L4', 1], [9.9, 'L4', 0]);
  blk.marks.push([6.8, 'L1', '✓', C.green, 11.7], [7.5, 'L2', '✓', C.green, 11.7], [8.6, 'L3', '✓', C.green, 11.7], [9.3, 'L4', '✓', C.green, 11.7]);
  cap(8.0, 11.4, 'It <i>remembers</i> each element it touches.');
  focus(9.9, 'run');
  beat(7.4, 'B1 email remembered');
  beat(10.6, 'B1 passed, 3 remembered');

  // ----- B2: the break
  cap(11.5, 14.9, 'Then the site redeploys.');
  beat(12.1, 'B2 redeploy crossfade');
  focus(12.8, 'code');
  blk.tok.email = [[13.2, 'broken'], [19.9, 'mem'], [42.0, 'saved']];
  blk.tok.terms = [[13.8, 'broken'], [30.2, 'ai'], [42.0, 'saved']];
  blk.tok.cont = [[14.4, 'broken'], [36.7, 'mem'], [42.0, 'saved']];
  blk.marks.push([13.2, 'L1', '✕', C.red, 20.6], [13.8, 'L3', '✕', C.red, 30.8], [14.4, 'L4', '✕', C.red, 37.0]);
  cap(15.0, 18.0, '<s>All three selectors are gone.</s>');
  beat(15.6, 'B2 three selectors broken');

  // ----- B3..B5: the first run after the redesign
  const d = D.Dsteps, s = [0];
  d.forEach(x => s.push(s[s.length - 1] + x.seconds));      // real step boundaries
  const found = i => s[i] + d[i].log_at[0];                  // when the repair's log line came
  const keys2 = [[16.6, 0], [18.4, TIMEOUT], [18.6, found(0)], [20.6, s[1]], [23.6, s[1]], [23.8, s[2]],
    [24.0, s[2]], [25.6, s[2] + TIMEOUT], [27.4, s[2] + TIMEOUT], [28.8, found(2)], [30.8, s[3]],
    [35.2, s[3]], [36.3, s[3] + TIMEOUT], [36.4, found(3)], [37.0, s[4]], [38.9, D.D_seconds]];
  focus(16.6, 'page');
  run({ t0: 16.6, t1: 41.4, cmd: [16.6, '$ python checkout.py'], keys: keys2,
    stamp: { t: 38.9, kind: 'ok', html: `✓ Passed · ${D.D_heals.length} repaired, checked, saved` } });
  blk.hl.push([16.6, 'L1', 1]);
  // B3 #email: mendwright waits its timeout, then finds the field from memory
  const sel = SCRIPT.map(x => x[1]);
  const WAIT = (a, b) => `${TIMEOUT} s timeout · ▶▶ sped up ×${speedOf(keys2, a, b)}`;
  callout({ at: 'v2:input[name=email]', t: [16.8, 23.8], leader: 18.6, sets: [
    { t: 16.8, ic: 'ring', ring: [16.8, 18.4], h: `Waiting for ${sel[0]}`, s: WAIT(16.6, 18.4) },
    { t: 18.6, ic: '◆', col: C.blue, h: `${sel[0]} found again`, s: XP.email, mono: true, typed: [18.7, 19.1], result: true },
  ] });
  box({ at: 'v2:input[name=email]', col: C.blue, t: [18.6, 23.8] });
  beat(17.6, 'B3 waiting for #email');
  cap(19.8, 23.4, 'Found <i>from memory</i>. No AI.');
  curOn(19.9, 21.3); cur(19.85, 'v2:input[name=email]', -90, 4);
  click(20.6, 'v2:input[name=email]', C.white); fill(20.7, 21.2, 'v2:input[name=email]', 'ada@example.com', 41.4);
  blk.marks.push([20.6, 'L1', '✓', C.green, 41.4]);
  beat(21.4, 'B3 found from memory');
  // B4 #accept-terms: memory can't tell two checkboxes apart, the local model picks
  blk.marks.push([23.8, 'L2', '✓', C.green, 41.4]);
  blk.hl.push([23.8, 'L3', 1]);
  callout({ at: 'v2:label.opt:nth-of-type(2)', t: [24.0, 25.8], sets: [
    { t: 24.0, ic: 'ring', ring: [24.0, 25.6], h: `Waiting for ${sel[2]}`, s: WAIT(24.0, 25.6) }] });
  beat(24.9, 'B4 waiting for #accept-terms');
  box({ at: 'v2:label.opt:nth-of-type(1)', col: C.amber, dashed: true, t: [25.8, 28.8], pad: 6 });
  box({ at: 'v2:label.opt:nth-of-type(2)', col: C.amber, dashed: true, t: [25.8, 28.8], pad: 6 });
  twoCandidates();
  cap(26.0, 29.5, 'Two checkboxes. Memory isn’t sure which.');
  beat(26.9, 'B4 two candidates');
  const modelS = D.model_seconds;
  callout({ at: 'v2:label.opt:nth-of-type(2)', t: [27.4, 34.6], leader: 28.8, sets: [
    { t: 27.4, ic: '★', col: C.purple, h: 'Asking the local model', s: `${GB} GB · runs on this machine`, shimmer: [27.4, 28.8] },
    { t: 28.8, ic: '★', col: C.purple, h: `Picked in ${modelS} s`, s: XP.terms, mono: true, typed: [28.9, 29.3], result: true },
  ] });
  beat(28.1, 'B4 asking the local model');
  box({ at: 'v2:label.opt:nth-of-type(2)', col: C.purple, t: [28.8, 34.6], pad: 6 });
  cap(30.0, 34.6, `So mendwright’s <em>${GB} GB local model</em> picks.`);
  curOn(30.25, 31.4); cur(30.2, 'v2:#opt-91c4', 2, 3);
  click(30.8, 'v2:#opt-91c4', C.white); check(30.8, 'v2:#opt-91c4', 41.4);
  blk.marks.push([30.8, 'L3', '✓', C.green, 41.4]);
  beat(30.95, 'B4 token purple, click');
  beat(31.8, 'B4 picked by the local model');
  // B5 #continue from memory, and the run passes
  blk.hl.push([34.8, 'L4', 1], [38.9, 'L4', 0]);
  callout({ at: 'v2:.btn-x82hd', t: [35.2, 41.4], leader: 36.4, sets: [
    { t: 35.2, ic: 'ring', ring: [35.2, 36.3], h: `Waiting for ${sel[3]}`, s: WAIT(35.2, 36.3) },
    { t: 36.4, ic: '◆', col: C.blue, h: `${sel[3]} found again`, result: true },
  ] });
  box({ at: 'v2:.btn-x82hd', col: C.blue, t: [36.4, 41.4] });
  beat(35.8, 'B5 waiting for #continue');
  beat(36.95, 'B5 #continue found again');
  curOn(36.45, 37.6); cur(36.4, 'v2:.btn-x82hd', 120, 4);
  click(37.0, 'v2:.btn-x82hd', C.white); press(37.0, 'v2:.btn-x82hd');
  blk.marks.push([37.0, 'L4', '✓', C.green, 41.4]);
  cap(37.6, 41.4, '<u>Checked on the live page</u>, then saved.');
  focus(38.9, 'run');
  beat(40.2, 'B5 passed, 3 repaired');

  // ----- B6: the next run
  reload(41.4);
  focus(41.4, 'page');
  cap(41.6, 48.0, 'Next run: saved fixes, <u>no waiting, no model.</u>');
  if (D.E_heals.length) throw new Error('E: the next run repaired something');
  const e = D.Esteps, Et = [42.0, 42.4, 42.7, 43.0, 43.6];
  let ea = 0;
  const keys3 = [[Et[0], 0]];
  e.forEach((x, i) => { ea += x; keys3.push([Et[i + 1], ea]); });
  keys3.push([43.8, D.E_seconds]);
  run({ t0: 41.4, t1: 48.4, cmd: [41.4, '$ python checkout.py'], prev: [41.4, `first run after the redesign · ${secs(D.D_seconds)} s`],
    keys: keys3, clockCol: [[44.2, C.green]], stamp: { t: 44.2, kind: 'ok', html: '✓ Passed' } });
  blk.hl.push([42.0, 'L1', 1], [42.7, 'L3', 1], [43.3, 'L4', 1], [44.0, 'L4', 0]);
  blk.marks.push([42.4, 'L1', '✓', C.green, 48.4], [42.7, 'L2', '✓', C.green, 48.4], [43.0, 'L3', '✓', C.green, 48.4], [43.6, 'L4', '✓', C.green, 48.4]);
  curOn(41.85, 44.0); cur(41.8, 'v2:input[name=email]', -90, 4); cur(42.55, 'v2:#opt-91c4', 2, 3); cur(43.15, 'v2:.btn-x82hd', 120, 4);
  click(42.4, 'v2:input[name=email]', C.white); fill(42.45, 42.7, 'v2:input[name=email]', 'ada@example.com');
  click(43.0, 'v2:#opt-91c4', C.white); check(43.0, 'v2:#opt-91c4');
  click(43.6, 'v2:.btn-x82hd', C.white); press(43.6, 'v2:.btn-x82hd');
  const SAVED = '✓ saved fix';
  box({ at: 'v2:input[name=email]', col: C.green, t: [42.4, 48.2] });
  box({ at: 'v2:label.opt:nth-of-type(2)', col: C.green, t: [43.0, 48.2], pad: 6 });
  box({ at: 'v2:.btn-x82hd', col: C.green, t: [43.6, 48.2] });
  tag({ at: 'v2:input[name=email]', html: SAVED, col: C.green, t: [42.8, 48.2], solid: true, col0: 'v2:.f9a1' });
  tag({ at: 'v2:label.opt:nth-of-type(2)', html: SAVED, col: C.green, t: [43.1, 48.2], solid: true, col0: 'v2:.f9a1' });
  tag({ at: 'v2:.btn-x82hd', html: SAVED, col: C.green, t: [43.7, 48.2], solid: true, col0: 'v2:.f9a1' });
  focus(44.2, 'run');
  beat(43.2, 'B6 saved fixes used');
  beat(46.0, 'B6 next run, no waiting');
}

// ---------- the montage (brief mode: one caption, one stamp, focal on the page) ----------
function montageCookies(t0) {
  const t1 = t0 + 4.2;
  montage(t0, t1);
  if (D.C_banner_up_after || !D.C_ok) throw new Error('C: the banner run did not pass');
  run({ t0, t1, cmd: [t0, '$ python checkout.py'] });
  frame({ id: 'm1', html: D.pages.v1, t: [t0, t1], popup: '#cc-wrap', pop: [[t0 - 1, t0 + 1.6]] });
  url(t0, 'northwind.example/checkout');
  codeBlock({ t0, t1, rows: openRows('page', { cookies: 'reject' }, 'page') });
  cap(t0 + .2, t1 - .2, 'Cookie banner? It clicks “Reject all”.');
  beat(t0 + .9, 'M1 cookie banner');
  curOn(t0 + .9, t0 + 1.9); cur(t0 + .9, 'm1:#cc-wrap button', -10, 4); haloOn(t0 + 1.0, t0 + 1.8);
  click(t0 + 1.3, 'm1:#cc-wrap button', C.purple); press(t0 + 1.3, 'm1:#cc-wrap button');
  pstamp({ t: [t0 + 2.2, t1], kind: 'ok', html: '✓ Passed · banner rejected', frame: 'm1' });
  beat(t0 + 3.4, 'M1 banner rejected');
  return t1;
}
function montagePopup(t0) {
  const t1 = t0 + 4.2, F1 = D.F.F1;
  montage(t0, t1);
  if (F1.sign_up !== 'never clicked' || F1.popup_up_after) throw new Error('F1: wrong outcome');
  run({ t0, t1, cmd: [t0, '$ python checkout.py'] });
  frame({ id: 'm2', html: D.F.pages.v2_nl, t: [t0, t1], popup: '#nl', pop: [[t0 + .2, t0 + 2.5]] });
  codeBlock({ t0, t1, rows: openRows('page', F1.open_kwargs, 'page') });
  cap(t0 + .2, t1 - .2, 'A popup? <u>“No thanks”</u>. <s>Never “Sign up”.</s>');
  box({ at: 'm2:.nl-yes', col: C.red, dashed: true, t: [t0 + 1.4, t0 + 2.5], pad: 6 });
  tag({ at: 'm2:.nl-yes', html: 'never', col: C.red, solid: true, t: [t0 + 1.4, t0 + 2.5], clear: 'm2:#nl > div', leader: true, pad: 6 });
  beat(t0 + 1.9, 'M2 popup: never Sign up');
  curOn(t0 + 1.6, t0 + 2.7); cur(t0 + 1.6, 'm2:.nl-no', -8, 4); haloOn(t0 + 1.7, t0 + 2.5);
  click(t0 + 2.2, 'm2:.nl-no', C.purple); press(t0 + 2.2, 'm2:.nl-no');
  pstamp({ t: [t0 + 3.0, t1], kind: 'ok', html: '✓ “Sign up” never clicked', frame: 'm2' });
  beat(t0 + 3.7, 'M2 never clicked');
  return t1;
}

// ---------- draw state ----------
let R = {};          // 'frame:css' -> { f, r (iframe px) }
let CW = 14;         // editor char width at 24px
const FR = {};       // frames by id
const resolved = new Map();

function rectOf(node) {
  const r = node.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height };
}
const grow = (r, p) => ({ x: r.x - p, y: r.y - p, w: r.w + 2 * p, h: r.h + 2 * p });
const cxy = r => [r.x + r.w / 2, r.y + r.h / 2];
// iframe px -> content px -> stage px
function toContent(f, r) { return { x: (r.x - f.cam.x) * Z, y: (r.y - f.cam.y) * Z, w: r.w * Z, h: r.h * Z }; }
function toStage(f, r) { const c = toContent(f, r); return { x: c.x + CO.x, y: c.y + CO.y, w: c.w, h: c.h }; }
function frameOf(at) { const i = at.indexOf(':'); return FR[at.slice(0, i)]; }
function nodeOf(at) {
  if (resolved.has(at)) return resolved.get(at);
  const i = at.indexOf(':'), f = FR[at.slice(0, i)];
  if (!f) throw new Error('no frame for ' + at);
  const n = f.doc.querySelector(at.slice(i + 1));
  if (!n) throw new Error('selector not found: ' + at);
  resolved.set(at, n);
  return n;
}
// a stage rect for an anchor: 'frame:css', 'code:rowId' or [x, y] (stage)
function SR(at, t) {
  if (Array.isArray(at)) return { x: at[0], y: at[1], w: 0, h: 0 };
  if (at.startsWith('code:')) return codeRect(at.slice(5), t);
  const f = frameOf(at);
  if (!R[at]) R[at] = rectOf(nodeOf(at));
  return toStage(f, R[at]);
}
const toCO = p => [p[0] - CO.x, p[1] - CO.y];

// ---------- build after the pages load ----------
const ROWS = [];     // every code row element
function buildCode() {
  for (const b of TL.code) {
    b.rowEls = {};
    b.rows.forEach((r, i) => {
      const ln = el('div', 'ln mono', `<span class="g">${Array.isArray(r.n) ? r.n[0][1] : r.n}</span><span class="c"><span class="cw">${r.html}</span></span><span class="st"></span>`, code);
      const o = { b, r, ln, g: $('.g', ln), cw: $('.cw', ln), st: $('.st', ln), len: ln.querySelector('.cw').textContent.length,
        at: r.at || [[0, i]], toks: [...ln.querySelectorAll('[data-k]')] };
      b.rowEls[r.id] = o;
      ROWS.push(o);
    });
  }
}
function rowIdx(o, t) { return track(t, o.at.map(([tt, i], k) => [tt, i, .5]), .5); }
function blockAt(t) {   // the code block on screen at t (the latest that started)
  let cur = null;
  for (const b of TL.code) if (t >= b.t0 - .3 && t < b.t1 + .3) cur = b;
  return cur;
}
function codeRect(id, t = 1e9) {
  for (const b of TL.code) if (b.rowEls && b.rowEls[id]) {
    const o = b.rowEls[id];
    let idx = o.at[0][1];
    for (const [tt, i] of o.at) if (t >= tt) idx = i;
    return { x: ED.x + 58, y: ED.y + LAYOUT.HEAD + LAYOUT.ROW0 + idx * LH, w: o.len * CW, h: LH };
  }
  throw new Error('no code row ' + id);
}

function measure() {
  for (const f of TL.frames) {
    f.doc = f.ifr.contentDocument;
    f.doc.documentElement.style.overflow = 'hidden';
    if (f.popup) {
      f.popEl = f.doc.querySelector(f.popup);
      if (!f.popEl) throw new Error('no popup ' + f.popup + ' in ' + f.id);
      f.popCard = f.popEl.firstElementChild;
      f.popEl.style.visibility = 'hidden';
    }
    // the page camera: the form card lands at content (16, 120)
    const card = f.doc.querySelector('.card, .f9a1');
    const r = rectOf(card);
    f.cam = f.cam || { x: r.x - LAYOUT.CARD_AT[0] / Z, y: r.y - LAYOUT.CARD_AT[1] / Z };
    f.pgw.style.transform = `scale(${Z}) translate(${(-f.cam.x).toFixed(2)}px,${(-f.cam.y).toFixed(2)}px)`;
  }
  // the redesign must break all three selectors
  if (FR.v2 && FR.v2.doc.querySelector('#email, #accept-terms, #continue')) throw new Error('V2 still matches an old selector: "no match" would be false');
  // editor char width
  const probe = el('span', 'mono', 'x'.repeat(40), code, 'position:absolute;visibility:hidden;font-size:24px;white-space:pre');
  CW = probe.getBoundingClientRect().width / 40; probe.remove();
  buildCode();
  buildRuns();
  buildAnn();
  buildPage();
  if (TL.title) buildTitle();
  if (TL.end) buildEnd();
  if (TL.ctitle) buildClipTitle();
  if (TL.estrip) buildEndStrip();
  for (const c of TL.caps) c.e = el('div', 'cap', c.html, document.body);
  TL.focus.sort((a, b) => a[0] - b[0]);
  if (!TL.focus.length || TL.focus[0][0] > 0) TL.focus.unshift([0, 'all']);
  checkFits();
}

// ---------- the browser: pages, boxes, ripples, cursor ----------
function makeFrames() {
  for (const f of TL.frames) {
    f.pgw = el('div', 'pgw', null, content);
    content.insertBefore(f.pgw, ov);
    f.ifr = el('iframe', '', null, f.pgw);
    f.ifr.srcdoc = f.html;
    FR[f.id] = f;
  }
}
function buildPage() {
  for (const b of TL.boxes) b.e = el('div', 'box', null, ov);
  for (const c of TL.clicks) c.e = el('div', 'ripple', null, ov);
  TL.curKeys = [[0, [CO.x + 600, CO.y + 560]]].concat(TL.cur.map(k => {
    const r = SR(k.at);
    // a wide input: point near its start, where the text goes; anything else: its centre
    const big = r.w > 300 && k.edge !== 'top' && !Array.isArray(k.at) && !k.at.startsWith('code:') && nodeOf(k.at).tagName === 'INPUT';
    const p = Array.isArray(k.at) ? k.at : [(big ? r.x + 110 : r.x + r.w / 2) + k.dx, (k.edge === 'top' ? r.y : r.y + r.h / 2) + k.dy];
    return [k.t, p];
  }));
  for (const c of TL.clicks) { const r = SR(c.at); c.p = Array.isArray(c.at) ? c.at : [r.x + r.w / 2, r.y + r.h / 2]; }
  TL.clicks.forEach((c, i) => {   // a click lands where the cursor is pointing, when the cursor is there
    const k = TL.curKeys.filter(x => x[0] <= c.t).pop();
    if (k && k[0] > c.t - 1.2) c.p = k[1];
  });
}
function setBox(e, r, { col, dashed = false, fill = 'transparent', glow = 0, p = 1, radius = 12 }) {
  if (p <= 0.002) { e.style.visibility = 'hidden'; return; }
  e.style.visibility = 'inherit';
  e.style.left = r.x + 'px'; e.style.top = r.y + 'px'; e.style.width = r.w + 'px'; e.style.height = r.h + 'px';
  e.style.border = `${dashed ? 3 : 3.5}px ${dashed ? 'dashed' : 'solid'} ${col}`;
  e.style.background = fill;
  e.style.borderRadius = radius + 'px';
  e.style.boxShadow = glow ? `0 0 0 ${glow}px ${col}33, 0 0 30px ${col}44` : 'none';
  e.style.opacity = clamp(p).toFixed(3);
  e.style.transform = `scale(${lerp(1.05, 1, clamp(p)).toFixed(4)})`;
}
function drawBrowser(t) {
  let pw_ = 0, po = 0, dip = 0;
  for (const r of TL.reloads) {
    if (t >= r && t < r + 1.0) { pw_ = easeIO(lin(t, r, r + .5)); po = 1 - lin(t, r + .55, r + .9); }
    dip = Math.max(dip, Math.sin(Math.PI * lin(t, r + .05, r + .5)) * .35);
  }
  prog.style.width = (pw_ * 100).toFixed(2) + '%'; prog.style.opacity = po.toFixed(3);
  let u = null;
  for (const [tt, s] of TL.urls) if (t >= tt) u = s;
  if (u != null && urlEl.textContent !== u) urlEl.textContent = u;
  for (const f of TL.frames) {
    const p = pres(t, f.t[0], f.t[1], .45, .35);
    let fdip = 0;
    for (const r of f.reloads) fdip = Math.max(fdip, Math.sin(Math.PI * lin(t, r + .05, r + .5)) * .35);
    if (p <= .002) { f.pgw.style.visibility = 'hidden'; continue; }
    f.pgw.style.visibility = 'inherit';
    f.pgw.style.opacity = (clamp(p) * (1 - fdip)).toFixed(4);
    if (f.popEl) {
      let np = 0;
      for (const [a, b] of f.pop) np = Math.max(np, pres(t, a, b, .5, .3));
      if (np <= .002) f.popEl.style.visibility = 'hidden';
      else {
        f.popEl.style.visibility = 'inherit'; f.popEl.style.opacity = clamp(np).toFixed(3);
        f.popCard.style.transform = `translateY(${((1 - np) * 40).toFixed(2)}px) scale(${lerp(.95, 1, np).toFixed(4)})`;
        f.popCard.style.filter = np < .995 ? `blur(${((1 - clamp(np)) * 8).toFixed(2)}px)` : 'none';
      }
    }
  }
  // page state
  const vals = new Map();
  for (const x of TL.fills) {
    const v = t < x.a || t >= x.until ? null : x.text.slice(0, Math.round(x.text.length * lin(t, x.a, x.b)));
    if (!vals.has(x.at) || v != null) vals.set(x.at, v);
  }
  for (const [at, v] of vals) { const n = nodeOf(at); const want = v || ''; if (n.value !== want) n.value = want; }
  const chk = new Map();
  for (const x of TL.checks) chk.set(x.at, (chk.get(x.at) || false) || (t >= x.t && t < x.until));
  for (const [at, v] of chk) nodeOf(at).checked = v;
  const prs = new Map();
  for (const x of TL.press) prs.set(x.at, Math.max(prs.get(x.at) || 0, Math.sin(Math.PI * lin(t, x.t, x.t + .24))));
  for (const [at, v] of prs) nodeOf(at).style.transform = `scale(${(1 - .05 * v).toFixed(4)})`;
  // boxes
  for (const b of TL.boxes) {
    const p = pres(t, b.t[0], b.t[1], .4, .3);
    if (p <= .002) { b.e.style.visibility = 'hidden'; continue; }
    const r = grow(SR(b.at), b.pad);
    setBox(b.e, { x: r.x - CO.x, y: r.y - CO.y, w: r.w, h: r.h }, { col: b.col, dashed: b.dashed,
      fill: b.dashed ? `${b.col}12` : `${b.col}14`, glow: b.dashed ? 0 : 4, p });
  }
  // cursor
  const [cx, cy] = toCO(trackN(t, TL.curKeys, .5));
  let cv = 0;
  for (const [a, b] of TL.curOn) cv = Math.max(cv, pres(t, a, b, .25, .25));
  cursor.style.left = (cx - 6) + 'px'; cursor.style.top = (cy - 4) + 'px';
  cursor.style.opacity = clamp(cv).toFixed(3);
  let push = 0;
  for (const c of TL.clicks) push = Math.max(push, Math.sin(Math.PI * lin(t, c.t - .06, c.t + .12)));
  cursor.style.transform = `scale(${(1 - .14 * push).toFixed(3)})`;
  let hp = 0;
  for (const [a, b] of TL.halo) hp = Math.max(hp, pres(t, a, b, .3, .3));
  halo.style.left = (cx - 42) + 'px'; halo.style.top = (cy - 42) + 'px'; halo.style.opacity = (hp * clamp(cv)).toFixed(3);
  for (const c of TL.clicks) {
    const p = lin(t, c.t, c.t + .65);
    if (p <= 0 || p >= 1) { c.e.style.visibility = 'hidden'; continue; }
    const e = 1 - Math.pow(1 - p, 3), rad = 8 + 40 * e;
    const [x, y] = toCO(c.p);
    c.e.style.visibility = 'inherit';
    c.e.style.left = (x - rad) + 'px'; c.e.style.top = (y - rad) + 'px';
    c.e.style.width = c.e.style.height = 2 * rad + 'px';
    c.e.style.borderColor = c.col; c.e.style.opacity = (.9 * (1 - p)).toFixed(3);
  }
}

// ---------- the editor ----------
const TOKTXT = { broken: '#ffb0b0', mem: '#9ccbff', ai: '#d4b2ff', saved: '#8ce8b0' };   // a token's text, tinted by its state
function drawEditor(t) {
  const blk = blockAt(t);
  for (const o of ROWS) {
    const b = o.b;
    const p = pres(t, b.t0, b.t1, .4, .3) * (o.r.appear != null ? spring(t - o.r.appear, .3) : 1);
    if (p <= .002) { o.ln.style.visibility = 'hidden'; continue; }
    o.ln.style.visibility = 'inherit';
    o.ln.style.opacity = clamp(p).toFixed(3);
    o.ln.style.top = (LAYOUT.ROW0 + rowIdx(o, t) * LH) + 'px';
    if (Array.isArray(o.r.n)) { let n = ''; for (const [tt, v] of o.r.n) if (t >= tt) n = v; if (o.g.textContent !== String(n)) o.g.textContent = n; }
    if (o.r.type) {
      const n = Math.round(o.len * lin(t, o.r.type[0], o.r.type[1]));
      o.cw.style.width = (n * CW + 1).toFixed(1) + 'px';
    }
    o.ln.style.background = o.r.tint ? `rgba(185,140,255,${(.16 * pres(t, o.r.tint[0], o.r.tint[1], .4, .9)).toFixed(3)})` : 'transparent';
    o.ln.style.borderRadius = '10px';
    // the result of the line
    let m = null;
    for (const mk of b.marks) if (mk[1] === o.r.id && t >= mk[0] && t < mk[4]) m = mk;
    if (!m) o.st.style.opacity = 0;
    else {
      o.st.textContent = m[2]; o.st.style.color = m[3];
      o.st.style.opacity = clamp(pres(t, m[0], m[4] - .15, .3, .25)).toFixed(3);
      o.st.style.transform = `scale(${lerp(.4, 1, spring(t - m[0], .36, .55)).toFixed(3)})`;
    }
    // selector tokens: red while broken, then the colour of the repair
    for (const tok of o.toks) {
      const ks = b.tok[tok.dataset.k] || [];
      let st = null, t0 = 0;
      for (const [tt, s] of ks) if (t >= tt) { st = s; t0 = tt; }
      const col = { broken: C.red, mem: C.blue, ai: C.purple, saved: C.green }[st];
      if (!col) { tok.style.textDecoration = 'none'; tok.style.background = 'transparent'; tok.style.color = ''; continue; }
      tok.style.textDecoration = `underline ${st === 'broken' ? 'wavy' : 'solid'} ${col}`;
      tok.style.textUnderlineOffset = '7px'; tok.style.textDecorationThickness = st === 'broken' ? '2px' : '3px';
      tok.style.textDecorationSkipInk = 'none';
      tok.style.color = TOKTXT[st];
      tok.style.background = `${col}${Math.round(0x40 * clamp(spring(t - t0, .4))).toString(16).padStart(2, '0')}`;
    }
  }
  // the current line
  if (blk && blk.hl.length) {
    const ys = blk.hl.map(([tt, id]) => [tt, LAYOUT.ROW0 + rowIdx(blk.rowEls[id], t) * LH]);
    hl.style.top = track(t, ys, .3) + 'px';
    hl.style.opacity = (clamp(track(t, blk.hl.map(([tt, , on]) => [tt, on]), .3)) * pres(t, blk.t0, blk.t1, .3, .3)).toFixed(3);
  } else hl.style.opacity = 0;
  // header badge (clip A)
  for (const x of TL.badges) {
    const nx = TL.badges[TL.badges.indexOf(x) + 1];
    mat(x.e, pres(t, x.t, nx ? nx.t : 1e9, .35, .25), { dy: 0, dx: 10, s0: .94, blur: 4 });
  }
}

// ---------- the run panel ----------
function buildRuns() {
  for (const r of TL.runs) {
    r.e = el('div', 'rf', null, term);
    term.insertBefore(r.e, VEIL.run);
    if (r.diagram) r.dg = buildDiagram(r.diagram, r.e);
    if (r.cmd) r.cmdE = el('div', 'cmd mono', esc(r.cmd[1]), r.e);
    if (r.prev) r.prevE = el('div', 'prev', esc(r.prev[1]), r.e);
    if (r.keys) {
      const row = el('div', 'crow', null, r.e);
      r.clockE = el('div', 'clock mono', '0.00 s', row);
      r.speedE = el('div', 'speed', '', row);
      r.segs = [];
      for (let i = 1; i < r.keys.length; i++) {
        const [a, x] = r.keys[i - 1], [b, y] = r.keys[i];
        const rate = (y - x) / (b - a);
        if (rate > 1.5) r.segs.push({ a, b, n: Math.round(rate) });
      }
    }
    if (r.stamp) {
      const col = { ok: C.green, bad: C.red, stop: C.amber }[r.stamp.kind];
      const parts = r.stamp.html.split(' · ');
      r.stampE = el('div', 'stamp', `<span style="color:${col}">${parts[0]}</span>${parts.length > 1 ? `<span style="color:var(--ink)"> · ${parts.slice(1).join(' · ')}</span>` : ''}`, r.e);
      // too long for one line: the outcome on the first, the detail on the second
      if (parts.length > 1 && r.stampE.getBoundingClientRect().height > 50)
        r.stampE.innerHTML = `<span style="color:${col}">${parts[0]}</span><span class="sd">${parts.slice(1).join(' · ')}</span>`;
    }
    if (r.receipts.length) {
      const box_ = el('div', 'receipts', null, r.e);
      r.recE = r.receipts.map(x => el('div', 'receipt mono' + (x.dim ? ' dim' : ''), esc(x.text), box_));
    }
  }
}
function buildDiagram(dg, parent) {
  const w = el('div', 'diagram', null, parent);
  dg.rowEls = dg.rows.map(row => {
    const e = el('div', 'drow', `<span class="lb">${esc(row.lb)}</span>`, w);
    const chips = row.chips.map((c, i) => {
      if (i) el('span', 'darr', '→', e);
      return { name: c, e: el('span', 'dchip', esc(c), e) };
    });
    return { row, e, chips };
  });
  return w;
}
function drawDiagram(dg, t) {
  dg.rowEls.forEach(({ row, e, chips }) => {
    mat(e, spring(t - row.t, .4), { dy: 8, blur: 4, s0: .98 });
    const tone = row.tone === 'amber' ? C.amber : C.grey;
    let lit = null, done = [];
    for (const [tt, name] of dg.lit) if (t >= tt) { if (lit) done.push(lit); lit = name; }
    if (dg.done != null && t >= dg.done) { if (lit) done.push(lit); lit = null; }
    chips.forEach(c => {
      const on = c.name === lit, was = done.includes(c.name);
      const warn = on && dg.warn && c.name === dg.warn;
      const col = warn ? C.amber : on ? (row.tone === 'amber' ? C.amber : '#eef2f7') : was ? C.green : tone;
      c.e.style.color = on && !warn && row.tone === 'amber' ? '#1b1405' : warn ? '#1b1405' : col;
      c.e.style.background = on ? (row.tone === 'amber' || warn ? col : 'rgba(255,255,255,.14)') : 'rgba(255,255,255,.04)';
      c.e.style.borderColor = on ? col : was ? `${C.green}88` : 'rgba(255,255,255,.12)';
    });
  });
}
function drawRuns(t) {
  for (const r of TL.runs) {
    const p = pres(t, r.t0, r.t1, .35, .3);
    mat(r.e, p, { dy: 0, blur: 6, s0: 1 });
    if (p <= .002) continue;
    if (r.dg) drawDiagram(r.diagram, t);
    if (r.cmdE) mat(r.cmdE, spring(t - r.cmd[0], .35), { dy: 6, blur: 4, s0: 1 });
    if (r.prevE) mat(r.prevE, spring(t - r.prev[0], .35), { dy: 6, blur: 4, s0: 1 });
    if (r.clockE) {
      const v = pw(r.keys, t);
      r.clockE.textContent = secs(v) + ' s';
      mat(r.clockE.parentNode, spring(t - r.keys[0][0], .35), { dy: 6, blur: 4, s0: 1 });
      let col = '#eef2f7';
      for (const [tt, c] of (r.clockCol || [])) if (t >= tt) col = c;
      r.clockE.style.color = col;
      let sp = 0, seg = null;
      for (const s of r.segs) { const q = pres(t, s.a - .05, s.b, .2, .2); if (q > sp) { sp = q; seg = s; } }
      if (seg) r.speedE.textContent = `▶▶ sped up ×${seg.n}`;
      r.speedE.style.opacity = clamp(sp).toFixed(3);
    }
    if (r.stampE) {
      const q = spring(t - r.stamp.t, .38, .7);
      mat(r.stampE, clamp(spring(t - r.stamp.t, .3)), { dy: 0, s0: .9, blur: 6 });
      if (q > .002) r.stampE.style.transform = `scale(${lerp(.9, 1, q).toFixed(4)})`;
      r.stampE.style.transformOrigin = '0 50%';
    }
    (r.recE || []).forEach((e, i) => mat(e, spring(t - r.receipts[i].t, .35), { dy: 8, blur: 4, s0: 1 }));
  }
}

// ---------- annotations: callouts, tags, leaders, keycaps, page stamps ----------
const RING = `<svg width="30" height="30" viewBox="0 0 30 30"><circle cx="15" cy="15" r="11" fill="none" stroke="rgba(255,255,255,.16)" stroke-width="3.5"/><circle class="arc" cx="15" cy="15" r="11" fill="none" stroke="${C.amber}" stroke-width="3.5" stroke-linecap="round" stroke-dasharray="69.12" stroke-dashoffset="69.12" transform="rotate(-90 15 15)"/></svg>`;
function buildAnn() {
  const inner = { x: BR.x + 16, y: CO.y + 12, r: BR.x + BR.w - 16, b: BR.y + BR.h - 12 };
  for (const c of TL.callouts) {
    c.e = el('div', 'callout', null, ann);
    c.sets.forEach(s => {
      const ic = s.ic === 'ring' ? `<span class="ic">${RING}</span>` : s.ic ? `<span class="ic" style="color:${s.col || '#fff'}">${s.ic}</span>` : '';
      // a sub only ever breaks between its " · " parts
      const sub = s.s == null ? '' : s.mono ? esc(s.s) : s.s.split(' · ').map((x, i, a) => `<span style="white-space:nowrap">${esc(x)}${i < a.length - 1 ? ' ·' : ''}</span>`).join(' ');
      s.e = el('div', 'set', `<div class="h">${ic}<span>${esc(s.h)}</span></div>${s.s != null ? `<div class="s${s.mono ? ' mono' : ''}">${sub}</div>` : ''}${s.shimmer ? '<div class="shimmer"><i></i></div>' : ''}`, c.e);
      if (!s.ic) $('.h', s.e).style.gap = '0';
      if (s.ic === 'ring') s.arc = $('.arc', s.e);
      if (s.mono) s.sub = $('.s', s.e);
      s.shim = $('.shimmer', s.e);
      // the widths a set needs: its headline on one line, and its sub
      const h = $('.h', s.e);
      h.classList.add('one'); h.style.width = 'max-content';
      s.hw = Math.ceil(h.getBoundingClientRect().width);
      h.classList.remove('one'); h.style.width = '';
      s.w = Math.ceil(s.e.getBoundingClientRect().width) + 36;
      s.hgt = Math.ceil(s.e.getBoundingClientRect().height) + 32;
    });
    // as wide as the widest set needs, up to 420: a headline that fits on one line at that
    // width stays on one line, so the box keeps its height from set to set
    c.W = Math.min(420, Math.max(...c.sets.map(s => Math.max(s.w, s.hw + 36))));
    c.sets.forEach(s => {
      s.e.style.width = (c.W - 36) + 'px';
      if (s.hw <= c.W - 36) $('.h', s.e).classList.add('one');
      s.hgt = Math.ceil(s.e.getBoundingClientRect().height) + 32;
    });
    const T = SR(c.at);
    const H0 = Math.max(...c.sets.map(s => s.hgt));
    const freeRight = inner.r - (T.x + T.w + 44);
    let x, y, side;
    if (c.place === 'below-of') {
      const A = SR(c.of);
      x = A.x + A.w / 2 - c.W / 2; y = A.y + A.h + 22; side = 'below';
    } else if (c.place === 'right' || (c.place === 'auto' && freeRight >= 380)) {
      x = T.x + T.w + 44; y = T.y + T.h / 2 - c.sets[0].hgt / 2; side = 'right';
    } else if (inner.b - (T.y + T.h + 24) >= H0) {
      x = T.x; y = T.y + T.h + 24; side = 'below';
    } else { x = T.x; y = T.y - 24 - H0; side = 'above'; }
    x = clamp(x, inner.x, inner.r - c.W); y = clamp(y, inner.y, inner.b - H0);
    Object.assign(c, { x, y, side, T });
    c.e.style.left = x + 'px'; c.e.style.top = y + 'px'; c.e.style.width = c.W + 'px';
    if (c.leader != null) {
      c.line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      annSvg.appendChild(c.line);
      const col = c.sets.find(s => s.t >= c.leader - .01) || c.sets[c.sets.length - 1];
      c.lineCol = col.col || C.blue;
    }
  }
  for (const g of TL.tags) {
    const col = g.col;
    g.e = el('div', 'tag', g.html, ann, g.solid
      ? `background:${col};color:${col === C.green ? '#04210f' : col === C.red ? '#2a0606' : '#0b1220'}`
      : `background:rgba(14,18,27,.92);border:1.5px solid ${col}99;color:#eef2f7`);
    const w = g.e.getBoundingClientRect().width, h = g.e.getBoundingClientRect().height;
    const T = SR(g.at, g.t[0]), pad = g.pad != null ? g.pad : 7;
    const zr = g.zone === 'code' ? ED.x + ED.w - 12 : inner.r;
    // right of the element (and of col0 / clear: a column or a popup card it must clear),
    // else below it; 'below-of' sits under another element (a popup card), centred on the
    // target; 'left' sits left of the element (or of clear), dy moves it off an input
    let x = T.x + T.w + pad + 14, y = T.y + T.h / 2 - h / 2 + (g.dy || 0);
    if (g.col0) { const K = SR(g.col0); x = Math.max(x, K.x + K.w + 18); }
    if (g.clear && g.place === 'right') { const K = SR(g.clear); x = Math.max(x, K.x + K.w + 14); }
    if (g.place === 'below' || (g.place === 'right' && x + w > zr)) { x = g.align === 'center' ? T.x + T.w / 2 - w / 2 : T.x; y = T.y + T.h + pad + 10; g.side = 'below'; }
    if (g.place === 'below-of') {
      const K = SR(g.of);
      x = T.x + T.w / 2 - w / 2; y = K.y + K.h + 12;
      // elbow 'right': under the card's right edge; its leader runs up outside the card
      if (g.elbow === 'right') { g.ex = K.x + K.w + 24; x = g.ex + 48 - w; }
    }
    if (g.place === 'left') { x = (g.clear ? SR(g.clear).x : T.x - pad) - 14 - w; }
    if (g.place === 'above') { x = T.x; y = T.y - pad - 10 - h; }
    g.side = g.side || g.place;
    g.x = x; g.y = y; g.w = w; g.h = h; g.T = grow(T, pad);
    g.e.style.left = x + 'px'; g.e.style.top = y + 'px';
    if (g.leader) {    // a line from the tag to the element's box, in the tag's colour
      g.line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      annSvg.appendChild(g.line);
      const B = g.T, ly = clamp(y + h / 2, B.y + 10, B.y + B.h - 10);
      if (g.ex != null) g.pts = [[g.ex, y], [g.ex, B.y + B.h / 2], [B.x + B.w, B.y + B.h / 2]];
      else if (g.side === 'below-of') g.pts = [[x + w / 2, y], [x + w / 2, B.y + B.h]];
      else g.pts = g.side === 'left' ? [[x + w, ly], [B.x, ly]] : [[x, ly], [B.x + B.w, ly]];
      g.line.setAttribute('points', g.pts.map(p => p.map(v => v.toFixed(1)).join(',')).join(' '));
      g.line.setAttribute('fill', 'none'); g.line.setAttribute('stroke', col); g.line.setAttribute('stroke-width', '2.5');
      g.line.setAttribute('stroke-linejoin', 'round');
    }
  }
  for (const k of TL.keycaps) {
    // above the popup card's top-right corner, right-aligned with it, clear of the page form
    k.e = el('div', 'keycap mono', esc(k.text), ann);
    const A = SR(k.at), w = k.e.getBoundingClientRect().width, h = k.e.getBoundingClientRect().height;
    k.x = A.x + A.w - w; k.y = A.y - h - 12; k.w = w; k.h = h;
    k.e.style.left = k.x + 'px'; k.e.style.top = k.y + 'px';
  }
  for (const s of TL.pstamps) {
    const col = { ok: C.green, bad: C.red, stop: C.amber }[s.kind];
    s.e = el('div', 'pstamp', `<span style="color:${col}">${s.html}</span>`, ann);
    const f = FR[s.frame];
    s.e.style.left = (CO.x + 16) + 'px';
    s.e.style.top = (BR.y + BR.h - 24 - s.e.getBoundingClientRect().height) + 'px';
  }
  for (const b of TL.badges) b.e = el('div', 'badge', `<span class="d" style="background:${b.col}"></span>${b.html}`, edHead,
    `background:${b.col}22;border:1px solid ${b.col}66;color:${b.col === C.grey ? '#dfe6ee' : b.col}`);
}
function annDim(zone, t) { return 1 - .45 * veilAt(zone, t); }
function drawAnn(t) {
  for (const c of TL.callouts) {
    const p = pres(t, c.t[0], c.t[1], .4, .3);
    if (p <= .002) { c.e.style.visibility = 'hidden'; if (c.line) c.line.style.opacity = 0; continue; }
    mat(c.e, p, { dx: c.side === 'right' ? 18 : 0, dy: c.side === 'right' ? 0 : 14, s0: .96, blur: 10 });
    c.e.style.opacity = (clamp(p) * annDim(c.zone, t)).toFixed(3);
    c.e.style.height = track(t, c.sets.map((s, i) => [i ? s.t : c.t[0], s.hgt]), .35) + 'px';
    c.sets.forEach((s, i) => {
      const nx = c.sets[i + 1];
      const q = pres(t, i ? s.t : c.t[0] - 1, nx ? nx.t - .05 : 1e9, .32, .18);
      s.e.style.opacity = clamp(q).toFixed(3);
      s.e.style.transform = `translateY(${((1 - clamp(spring(t - s.t, .32))) * (i ? 8 : 0)).toFixed(2)}px)`;
      s.e.style.visibility = q <= .002 ? 'hidden' : 'inherit';
      if (s.arc) s.arc.setAttribute('stroke-dashoffset', (69.12 * (1 - lin(t, s.ring[0], s.ring[1]))).toFixed(2));
      if (s.sub && s.typed) s.sub.textContent = s.s.slice(0, Math.round(s.s.length * lin(t, s.typed[0], s.typed[1]))) || ' ';
      if (s.shim) {
        const sh = s.shim.firstChild, qq = lin(t, s.shimmer[0], s.shimmer[1]);
        sh.style.left = (-40 + 140 * ((qq * 2.2) % 1)) + '%';
        s.shim.style.opacity = (1 - lin(t, s.shimmer[1], s.shimmer[1] + .2)).toFixed(3);
      }
    });
    if (c.line) {
      // the leader: from the callout's near edge to the target's box, once the element is found
      const T = grow(c.T, 7), h = parseFloat(c.e.style.height);
      let x1, y1, x2, y2;
      if (c.side === 'right') { x1 = c.x; y1 = clamp(T.y + T.h / 2, c.y + 18, c.y + h - 18); x2 = T.x + T.w; y2 = T.y + T.h / 2; }
      else if (c.side === 'below') { x1 = clamp(T.x + T.w / 2, c.x + 24, c.x + c.W - 24); y1 = c.y; x2 = x1; y2 = T.y + T.h; }
      else { x1 = clamp(T.x + T.w / 2, c.x + 24, c.x + c.W - 24); y1 = c.y + h; x2 = x1; y2 = T.y; }
      const L = Math.hypot(x2 - x1, y2 - y1), q = clamp(spring(t - c.leader, .35)) * (1 - clamp(spring(t - c.t[1], .3)));
      Object.entries({ x1, y1, x2, y2 }).forEach(([k, v]) => c.line.setAttribute(k, v.toFixed(1)));
      c.line.setAttribute('stroke', c.lineCol); c.line.setAttribute('stroke-width', '2');
      c.line.setAttribute('stroke-dasharray', L.toFixed(1)); c.line.setAttribute('stroke-dashoffset', (L * (1 - q)).toFixed(1));
      c.line.style.opacity = (t >= c.leader ? .7 * annDim(c.zone, t) : 0).toFixed(3);
    }
  }
  for (const g of TL.tags) {
    const p = pres(t, g.t[0], g.t[1], .35, .3);
    const vert = g.side === 'below' || g.side === 'below-of';
    mat(g.e, p, { dx: vert ? 0 : g.side === 'left' ? 12 : -12, dy: vert ? -8 : 0, s0: .9, blur: 6 });
    if (p > .002) g.e.style.opacity = (clamp(p) * annDim(g.zone, t)).toFixed(3);
    if (g.line) g.line.style.opacity = (.8 * clamp(p) * annDim(g.zone, t)).toFixed(3);
  }
  for (const k of TL.keycaps) {
    const p = pres(t, k.t[0], k.t[1], .3, .3);
    mat(k.e, p, { dy: 10, s0: .9, blur: 4 });
    const pr = Math.sin(Math.PI * lin(t, k.t[0] + .35, k.t[0] + .6));
    if (p > .002) k.e.style.transform += ` translateY(${(3 * pr).toFixed(2)}px)`;
  }
  for (const s of TL.pstamps) mat(s.e, pres(t, s.t[0], s.t[1], .35, .3), { dy: 14, s0: .92, blur: 8 });
}

// ---------- focus ----------
function zoneOn(zone, z) { return z === 'all' || z.split('+').includes(zone); }
function veilAt(zone, t) {
  if (zone === 'none') return 0;
  return clamp(track(t, TL.focus.map(([tt, z]) => [tt, zoneOn(zone, z) ? 0 : 1]), .35));
}
function drawFocus(t) {
  for (const z of ['code', 'run', 'page']) VEIL[z].style.opacity = veilAt(z, t).toFixed(3);
}

// ---------- captions ----------
function drawCaps(t) {
  for (const c of TL.caps) mat(c.e, pres(t, c.a, c.b, .4, .28), { dy: 10, blur: 8, s0: .99 });
}

// ---------- cards ----------
let TC = {};
function buildTitle() {
  const T_OLD = JSON.stringify(SCRIPT[0][1]), T_NEU = JSON.stringify(XP.email);
  const e = el('div', 'card', `<div class="wm"><b>mendwright</b></div><div class="tsub">Playwright &amp; Selenium selectors that repair themselves.</div>
    <div class="tcode mono"><span class="k-obj">page</span><span class="k-p">.</span><span class="k-fn">click</span><span class="k-p">(</span><span class="old">${esc(T_OLD)}</span><span class="neu">${esc(T_NEU)}</span><span class="k-p">)</span><span class="ok">✓</span></div>
    <div class="tnote"><div style="color:${C.red}">after a redesign: no match</div><div style="color:${C.blue}">◆ found at run time, from memory · no AI</div></div>`, document.body);
  TC = { e, wm: e.children[0], sub: e.children[1], code: e.children[2], note: e.children[3], old: $('.old', e), neu: $('.neu', e), ok: $('.ok', e),
    n1: e.children[3].children[0], n2: e.children[3].children[1], lo: T_OLD.length, ln: T_NEU.length };
}
function drawTitle(t) {
  const { t0, t1 } = TL.title;
  const out = clamp(spring(t - (t1 - .4), .3));
  if (t > t1 + .4) { TC.e.style.visibility = 'hidden'; return; }
  TC.e.style.visibility = 'inherit';
  TC.e.style.opacity = (1 - out).toFixed(3);
  TC.e.style.filter = out > .01 ? `blur(${(out * 16).toFixed(2)}px)` : 'none';
  TC.e.style.transform = `scale(${lerp(1, .97, out).toFixed(4)})`;
  const cw = CW * 40 / 24;
  const strike = clamp(spring(t - .8, .25)), gone = clamp(spring(t - 1.1, .3));
  TC.old.style.color = strike > .02 ? `rgb(${Math.round(lerp(165, 255, strike))},${Math.round(lerp(214, 107, strike))},${Math.round(lerp(255, 107, strike))})` : '#a5d6ff';
  TC.old.style.textDecoration = strike > .5 ? `line-through 3px ${C.red}` : `underline wavy ${C.red}`;
  TC.old.style.textDecorationThickness = strike > .5 ? '3px' : '2px';
  TC.old.style.width = (TC.lo * cw * (1 - gone)).toFixed(1) + 'px';
  TC.old.style.opacity = (1 - gone).toFixed(3);
  TC.neu.style.width = (Math.round(TC.ln * lin(t, 1.2, 1.7)) * cw).toFixed(1) + 'px';
  TC.ok.style.opacity = clamp(spring(t - 1.9, .3)).toFixed(3);
  TC.ok.style.transform = `scale(${lerp(.3, 1, spring(t - 1.9, .36, .55)).toFixed(3)})`;
  const sw = clamp(spring(t - 2.2, .4));
  TC.n1.style.opacity = (1 - sw).toFixed(3); TC.n2.style.opacity = sw.toFixed(3);
  TC.n2.style.transform = `translateY(${((1 - sw) * 8).toFixed(2)}px)`;
}
let EC = {};
function buildEnd() {
  const e = el('div', 'card', `<div class="wm" style="font-size:112px"><b>mendwright</b></div>
<div class="feat">
 <div><span class="ic" style="background:${C.blue}22;color:${C.blue}">◆</span>Repairs from memory first. No AI.</div>
 <div><span class="ic" style="background:${C.purple}22;color:${C.purple}">★</span>A ${GB} GB local model for the hard ones. No API key.</div>
 <div><span class="ic" style="background:${C.green}22;color:${C.green}">✓</span>Every fix checked on the live page, then saved.</div>
 <div><span class="ic" style="background:${C.amber}22;color:${C.amber}">▸</span>Cookie banners, popups, steps in any order.</div>
</div>
<div class="pills mono"><div class="pill"><span class="p">$</span>pip install mendwright</div><div class="pill"><span class="p">$</span>mendwright model install</div></div>
<div class="repo">github.com/saadbuilt/mendwright · Playwright &amp; Selenium · MIT</div>`, document.body);
  EC = { e, wm: e.children[0], feats: [...e.children[1].children], pills: e.children[2], repo: e.children[3] };
}
function drawEnd(t) {
  const t0 = TL.end.t0;
  if (t < t0 - .1) { EC.e.style.visibility = 'hidden'; return; }
  EC.e.style.visibility = 'inherit';
  mat(EC.wm, spring(t - t0, .6), { dy: 0, s0: .94, blur: 22 });
  EC.feats.forEach((r, i) => mat(r, spring(t - (t0 + .5 + i * .4), .5), { dy: 16, blur: 8 }));
  mat(EC.pills, spring(t - (t0 + 2.3), .5), { dy: 14, blur: 8 });
  mat(EC.repo, spring(t - (t0 + 2.7), .5), { dy: 10, blur: 6 });
}
let CT = null, ES = null;
function buildClipTitle() {
  const c = TL.ctitle;
  CT = el('div', 'card', `<div class="plate"><div class="ctitle">${c.title}</div><div class="capi mono">${esc(c.api)}</div></div>`, document.body);
}
function buildEndStrip() {
  ES = el('div', 'card estrip', `<div class="plate"><div class="wm"><b>mendwright</b></div><div class="pi mono"><span style="color:var(--ink3)">$ </span>pip install mendwright</div></div>`, document.body);
}

// ---------- layout checks: nothing cropped, nothing overlapping ----------
const PROBLEMS = [];
function checkFits() {
  const fits = (r, z, what) => {
    if (r.x < z.x - .5 || r.y < z.y - .5 || r.x + r.w > z.x + z.w + .5 || r.y + r.h > z.y + z.h + .5) PROBLEMS.push(what + ' is cropped: ' + JSON.stringify([r.x, r.y, r.w, r.h].map(Math.round)));
  };
  const BRZ = { x: BR.x, y: CO.y, w: BR.w, h: CO.h };
  for (const c of TL.callouts) {
    fits({ x: c.x, y: c.y, w: c.W, h: Math.max(...c.sets.map(s => s.hgt)) }, BRZ, 'callout ' + c.sets[0].h);
    c.sets.forEach(s => { if (s.e.scrollWidth > c.W - 36 + 1) PROBLEMS.push('callout text too wide: ' + s.h + ' / ' + (s.s || '')); });
    const T = grow(c.T, 7), H = Math.max(...c.sets.map(s => s.hgt));
    if (c.x < T.x + T.w && c.x + c.W > T.x && c.y < T.y + T.h && c.y + H > T.y) PROBLEMS.push('callout covers its target: ' + c.sets[0].h);
  }
  for (const g of TL.tags) {
    fits({ x: g.x, y: g.y, w: g.w, h: g.h }, g.zone === 'code' ? ED : BRZ, 'tag ' + g.html.replace(/<[^>]+>/g, ''));
    const what = 'tag ' + g.html.replace(/<[^>]+>/g, '');
    const hit = r => g.x < r.x + r.w && g.x + g.w > r.x && g.y < r.y + r.h && g.y + g.h > r.y;
    if (g.zone === 'code') for (const o of ROWS) {     // a code tag never covers the text of a line that shows meanwhile
      if (!(o.b.t0 < g.t[1] && o.b.t1 > g.t[0]) || (o.r.appear != null && o.r.appear >= g.t[1])) continue;
      let idx = o.at[0][1];
      for (const [tt, i] of o.at) if (g.t[0] >= tt) idx = i;
      const y = ED.y + LAYOUT.HEAD + LAYOUT.ROW0 + idx * LH;
      const txt = o.cw.textContent, lead = txt.length - txt.trimStart().length;       // only its indent may be covered
      const x0 = ED.x + 58 + lead * CW, x1 = ED.x + 58 + txt.trimEnd().length * CW;
      if (g.y < y + LH - 8 && g.y + g.h > y + 8 && g.x < x1 && g.x + g.w > x0) PROBLEMS.push('code tag covers row ' + o.r.id);
    }
    if (g.zone === 'page') {
      const f = frameOf(g.at);
      // never over its own element: it labels it, from beside it
      if (hit(g.T)) PROBLEMS.push(what + ' covers the element it labels');
      // never over the popup card while the popup is up
      if (f.popCard && f.pop.some(([a, b]) => a < g.t[1] && b > g.t[0]) && hit(toStage(f, rectOf(f.popCard)))) PROBLEMS.push(what + ' overlaps the popup card');
      // never over an input of its page
      for (const n of f.doc.querySelectorAll('input, button')) {
        if (n === nodeOf(g.at) || n.closest('label') === nodeOf(g.at) || nodeOf(g.at).contains(n)) continue;
        if (n.type === 'checkbox' || f.popCard && f.popCard.contains(n) && !f.pop.some(([a, b]) => a < g.t[1] && b > g.t[0])) continue;
        if (hit(toStage(f, rectOf(n)))) PROBLEMS.push(what + ' covers an input: over ' + (n.textContent || n.name || n.id).trim());
      }
      // never under a callout that shows at the same time
      for (const c of TL.callouts) if (c.t[0] < g.t[1] && c.t[1] > g.t[0] && hit({ x: c.x, y: c.y, w: c.W, h: Math.max(...c.sets.map(s => s.hgt)) })) PROBLEMS.push(what + ' overlaps the callout ' + c.sets[0].h);
    }
  }
  for (const k of TL.keycaps) {     // a keycap is clear of the page's inputs
    const f = frameOf(k.at);
    for (const n of f.doc.querySelectorAll('input, button')) {
      const r = grow(toStage(f, rectOf(n)), 10);
      if (k.x < r.x + r.w && k.x + k.w > r.x && k.y < r.y + r.h && k.y + k.h > r.y) PROBLEMS.push('keycap ' + k.text + ' touches an input: ' + (n.textContent || n.name || n.id).trim());
    }
  }
  for (const r of TL.runs) {
    const kids = [...r.e.querySelectorAll('.cmd, .prev, .crow, .stamp, .receipt, .drow')];
    const H = Math.max(...kids.map(k => k.getBoundingClientRect().bottom)) - r.e.getBoundingClientRect().top, top = 56;
    if (top + H > TERM.h - 12) PROBLEMS.push('run panel overflows by ' + Math.round(top + H - TERM.h + 4) + 'px at ' + r.t0);
    if (r.e.scrollWidth > r.e.clientWidth + 1) PROBLEMS.push('run panel too wide at ' + r.t0);
    if (r.dg) r.diagram.rowEls.forEach(x => { if (x.e.scrollWidth > x.e.clientWidth + 1 || x.e.getBoundingClientRect().right > TERM.x + TERM.w - 20) PROBLEMS.push('diagram row too wide: ' + x.row.lb); });
    if (r.stampE && r.stampE.getBoundingClientRect().height > 44 * 2 + 1) PROBLEMS.push('stamp takes more than 2 lines: ' + r.stamp.html);
  }
  for (const o of ROWS) if (o.len > 37) PROBLEMS.push('code row longer than 37 chars: ' + o.ln.textContent);
  for (const o of ROWS) { const w = o.len * CW; if (58 + w > ED.w - 44) PROBLEMS.push('code row does not fit: ' + o.ln.querySelector('.cw').textContent); }
  for (const c of TL.caps) {
    const txt = c.e.textContent;
    if (txt.length > 48) PROBLEMS.push('caption longer than 48 chars: ' + txt);
    const rg = document.createRange(); rg.selectNodeContents(c.e);
    c.w = rg.getBoundingClientRect().width;
    if (c.w > 1920 - 2 * 80) PROBLEMS.push('caption too wide: ' + txt);
  }
}

// ---------- audit (spec 2.6, 2.7): returns the violations ----------
window.audit = function audit() {
  const out = PROBLEMS.slice();
  const plain = h => h.replace(/<[^>]+>/g, '');
  const inMontage = t => TL.montage.some(([a, b]) => t >= a - .01 && t < b);
  // new text elements, with what kind they are
  const texts = [];
  for (const c of TL.caps) texts.push({ t: c.a, kind: 'caption', what: plain(c.html) });
  const seenTag = [];
  for (const g of TL.tags) {
    const txt = plain(g.html);
    const repeat = seenTag.some(s => s.txt === txt && s.t1 > g.t[0]);     // the same label again, while the first shows
    seenTag.push({ txt, t1: g.t[1] });
    if (!repeat) texts.push({ t: g.t[0], kind: 'tag', what: txt });
  }
  for (const c of TL.callouts) c.sets.forEach(s => texts.push({ t: s.t, kind: 'callout', what: s.h, result: !!s.result }));
  for (const r of TL.runs) {
    if (r.stamp) texts.push({ t: r.stamp.t, kind: 'stamp', what: plain(r.stamp.html), result: true, bad: r.stamp.kind !== 'ok' });
    // a receipt shown with its stamp is part of that one result
    for (const x of r.receipts) texts.push({ t: x.t, kind: 'receipt', what: x.text, partOf: !!r.stamp && Math.abs(x.t - r.stamp.t) < .01 });
  }
  for (const s of TL.pstamps) texts.push({ t: s.t[0], kind: 'stamp', what: plain(s.html), result: true });
  for (const k of TL.keycaps) texts.push({ t: k.t[0], kind: 'keycap', what: k.text, partOf: true });
  texts.sort((a, b) => a.t - b.t);
  const f = x => x.toFixed(2);
  // at most one new text element per 1.2 s (a keycap shows with its callout)
  const counted = texts.filter(x => !x.partOf);
  for (let i = 1; i < counted.length; i++) {
    const a = counted[i - 1], b = counted[i];
    if (b.t - a.t < 1.2 - 1e-6) out.push(`two text onsets ${f(b.t - a.t)} s apart: ${a.kind} "${a.what}" at ${f(a.t)}, ${b.kind} "${b.what}" at ${f(b.t)}`);
  }
  // caption durations, overlaps, and changes away from clicks and other text onsets
  const caps = TL.caps.slice().sort((a, b) => a.a - b.a);
  caps.forEach((c, i) => {
    const words = plain(c.html).split(/\s+/).filter(Boolean).length;
    const need = Math.max(2.5, words / 2.5 + 1);
    if (c.b - c.a < need - 1e-6) out.push(`caption "${plain(c.html)}" shows ${f(c.b - c.a)} s, needs ${f(need)}`);
    if (i && caps[i - 1].b > c.a + 1e-6) out.push(`captions overlap at ${f(c.a)}`);
    for (const edge of [c.a, c.b]) {
      for (const k of TL.clicks) if (Math.abs(k.t - edge) < .6 - 1e-6) out.push(`caption "${plain(c.html)}" changes at ${f(edge)}, ${f(Math.abs(k.t - edge))} s from a click at ${f(k.t)}`);
      for (const x of texts) if (x.kind !== 'caption' && Math.abs(x.t - edge) < .6 - 1e-6) out.push(`caption "${plain(c.html)}" changes at ${f(edge)}, ${f(Math.abs(x.t - edge))} s from ${x.kind} "${x.what}" at ${f(x.t)}`);
    }
  });
  // after a result: no new text (captions narrate it; receipts are part of it) for 2.5 s, 1.2 s in the montage
  for (const r of texts.filter(x => x.result)) {
    const hold = inMontage(r.t) ? 1.2 : 2.5;
    for (const x of texts) if (x !== r && x.kind !== 'caption' && x.kind !== 'receipt' && x.t > r.t + 1e-6 && x.t < r.t + hold - 1e-6)
      out.push(`${x.kind} "${x.what}" at ${f(x.t)} breaks the ${hold} s hold after "${r.what}" at ${f(r.t)}`);
  }
  // failures are held no longer than 2.5 s: something moves on by then
  for (const r of texts.filter(x => x.bad)) {
    const next = [...TL.focus.map(x => x[0]), ...texts.map(x => x.t), ...TL.clicks.map(x => x.t)].filter(x => x > r.t + 1e-6).sort((a, b) => a - b)[0];
    if (next == null || next - r.t > 2.5 + 1e-6) out.push(`failure "${r.what}" at ${f(r.t)} is held ${next == null ? 'to the end' : f(next - r.t) + ' s'}`);
  }
  return out;
};

// ---------- per-frame drawing ----------
function render(rt) {
  if (!window.ready && !window.__building) return;
  g1.style.transform = `translate(${Math.sin(rt * .21) * 60}px,${Math.cos(rt * .17) * 40}px)`;
  g2.style.transform = `translate(${Math.cos(rt * .19) * 70}px,${Math.sin(rt * .23) * 50}px)`;
  // story time: in a clip, the title holds TL.lead s before the story starts
  const t = TL.lead ? Math.max(0, rt - TL.lead) : rt;
  // the stage: in after the hook (main), out for the end card
  const sp = spring(t - TL.stageIn, .6) * (1 - spring(t - TL.stageOut, .5));
  stage.style.opacity = clamp(sp).toFixed(3);
  stage.style.filter = sp < .995 ? `blur(${((1 - clamp(sp)) * 14).toFixed(2)}px)` : 'none';
  stage.style.transform = `scale(${lerp(.985, 1, clamp(sp)).toFixed(4)})`;
  stage.style.visibility = sp < .002 ? 'hidden' : 'visible';
  band.style.opacity = clamp(sp).toFixed(3);
  if (sp >= .002) {
    drawFocus(t);
    drawEditor(t);
    drawRuns(t);
    drawBrowser(t);
    drawAnn(t);
  }
  // clip title (70%) and end strip (80%): the stage veiled on top of every panel's veil, no
  // blur; the title and the strip sit on a solid plate
  let sv = 0;
  if (TL.ctitle) { const p = pres(rt, -1, TL.ctitle.t1 + TL.lead, .3, .35); sv = Math.max(sv, .875 * p); mat(CT, p, { dy: 0, s0: .97, blur: 0 }); }
  if (TL.estrip) { const p = spring(t - TL.estrip.t0, .45); sv = Math.max(sv, p); mat(ES, p, { dy: 16, s0: .97, blur: 0 }); }
  sveil.style.opacity = clamp(sv).toFixed(3);
  drawCaps(t);
  if (TL.title) drawTitle(t);
  if (TL.end) drawEnd(t);
}
window.render = render;
window.ready = false;

// ---------- cuts ----------
function cutMain() {
  hookCard(0, 4.2);
  TL.stageIn = 4.2;
  mainStory();
  let t = montageCookies(48.4);
  t = montagePopup(t);
  t = window.montageSteps(t);
  focus(48.4, 'page');
  endCard(61.4, 69.0);
  TL.stageOut = 61.4;
  TL.dur = 69.0;
}
window.CUTS = { main: cutMain };
function defineCut() {
  const f = window.CUTS[CUT];
  if (!f) throw new Error('unknown cut ' + CUT);
  f();
  // DURATION and BEATS are in real seconds of the cut (a clip's story starts TL.lead late)
  window.DURATION = TL.dur + TL.lead;
  window.LEAD = TL.lead;
  window.BEATS = TL.beats.map(b => ({ t: +(b.t + (b.real ? 0 : TL.lead)).toFixed(3), name: b.name })).sort((a, b) => a.t - b.t);
  makeFrames();
}
window.defineCut = defineCut;
window.addEventListener('load', async () => {
  // every face the cut uses, loaded before anything is measured
  const loads = [];
  for (const w of [400, 500, 600, 650, 700]) loads.push(document.fonts.load(`${w} 32px Inter`), document.fonts.load(`${w} 24px "Cascadia Code"`));
  for (const f of TL.frames) for (const w of [400, 600, 700]) loads.push(f.ifr.contentDocument.fonts.load(`${w} 16px Inter`));
  await Promise.all(loads).catch(() => {});
  await document.fonts.ready;
  for (const f of TL.frames) await f.ifr.contentDocument.fonts.ready;
  window.__building = true;
  measure();
  render(0);
  window.ready = true;
});
