// mendwright explainer: a deterministic composition. render(t) draws the frame at t seconds
// as a pure function of t, so every frame can be captured at an exact time.
// Motion follows Apple's fluid-interface rules: critically damped springs by default
// (a little bounce only on completion pops), additive springs so a new target never
// jumps, materialize (blur + scale + opacity) for glass, and enter/exit on the same path.
'use strict';
const D = window.DATA;

// ---------- motion math ----------
// The story is laid out in story seconds; it plays K times slower. Springs keep their real
// response (SS = 1/K while drawing), so the pace slows down but no motion turns sluggish.
const K = 1.4;
let SS = 1;
function spring(t, resp = 0.42, damp = 1) {
  if (t <= 0) return 0;
  resp *= SS;
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
// Additive spring track: each key re-targets from wherever the value is, keeping velocity.
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
  e.style.visibility = 'visible';
  e.style.opacity = clamp(p).toFixed(4);
  if (origin) e.style.transformOrigin = origin;
  e.style.transform = `translate(${((1 - p) * dx).toFixed(2)}px,${((1 - p) * dy).toFixed(2)}px) scale(${lerp(s0, 1, p).toFixed(4)})`;
  const b = (1 - clamp(p)) * blur;
  e.style.filter = b > 0.05 ? `blur(${b.toFixed(2)}px)` : 'none';
}
const typed = (s, t, t0, t1) => s.slice(0, Math.round(s.length * lin(t, t0, t1)));

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
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------- layout ----------
const ED = { x: 64, y: 150, w: 664, h: 300 };
const TERM = { x: 64, y: 474, w: 664, h: 458 };
const BR = { x: 764, y: 150, w: 1092, h: 782 };
const CO = { x: BR.x, y: BR.y + 46 };           // browser content origin on the stage
const LH = 36;
const C = { green: '#3fd47a', red: '#ff6b6b', blue: '#5aa9ff', purple: '#b98cff', amber: '#f2c14e' };

// ---------- timeline (seconds) ----------
const S = { title: 0, s1: 3.4, s2: 9.8, s3: 19.6, s4: 29.0, s5: 48.6, s6: 53.6, s7: 65.6, s7b: 74.4, s7c: 85.6,
            s8: 95.6, s9: 113.6, end: 121.6, total: 129.0 };
window.DURATION = S.total * K;

// ---------- build ----------
const bg = el('div', '', null, document.body); bg.id = 'bg';
const g1 = el('div', 'glow', null, bg, 'width:900px;height:700px;left:-200px;top:-260px;background:rgba(124,92,255,.32)');
const g2 = el('div', 'glow', null, bg, 'width:900px;height:700px;right:-260px;bottom:-300px;background:rgba(40,160,255,.22)');
const g3 = el('div', 'glow', null, bg, 'width:600px;height:500px;left:700px;top:520px;background:rgba(63,212,122,.08)');
el('div', '', null, bg).id = 'grid';

const stage = el('div', '', null, document.body); stage.id = 'stage';

// editor
const ed = el('div', 'panel', `<div class="phead"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="t">checkout.py</span><span class="r">Python</span></div><div class="code"><div id="hl"></div></div>`, stage, `left:${ED.x}px;top:${ED.y}px;width:${ED.w}px;height:${ED.h}px`);
ed.id = 'ed';
const code = $('.code', ed), hl = $('#hl', ed);
const P_ = s => `<span class="k-p">${s}</span>`, O_ = s => `<span class="k-obj">${s}</span>`,
  F_ = s => `<span class="k-fn">${s}</span>`, Str = (s, k) => `<span class="k-str${k ? ' sel' : ''}"${k ? ` data-k="${k}"` : ''}>${esc(s)}</span>`;
const LINES = {
  L0: O_('page') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('page') + P_(', ') + O_('cookies') + P_('=') + Str('"reject"') + P_(').') + O_('page'),
  L1: O_('page') + P_('.') + F_('click') + P_('(') + Str('"#email"', 'email') + P_(')'),
  L2: O_('page') + P_('.') + O_('keyboard') + P_('.') + F_('type') + P_('(') + Str('"ada@example.com"') + P_(')'),
  L3: O_('page') + P_('.') + F_('check') + P_('(') + Str('"#accept-terms"', 'terms') + P_(')'),
  L4: O_('page') + P_('.') + F_('click') + P_('(') + Str('"#continue"', 'cont') + P_(')'),
};
const L0_TEXT = 'page = mendwright.open(page, cookies="reject").page';
const lineEls = {};
for (const id of Object.keys(LINES)) {
  const ln = el('div', 'ln mono', `<span class="g"></span><span class="c"><span class="cw">${LINES[id]}</span></span><span class="st"></span>`, code);
  lineEls[id] = { root: ln, g: $('.g', ln), c: $('.c', ln), cw: $('.cw', ln), st: $('.st', ln) };
}
const T_INS = 20.2;                          // the mendwright line goes in
const BASE = { L0: 0, L1: 0, L2: 1, L3: 2, L4: 3 };
function lineIdx(id, t) {
  if (id === 'L0') return 0;
  return BASE[id] + spring(t - T_INS, 0.5);
}
const lineY = (id, t) => 14 + lineIdx(id, t) * LH;   // inside .code

// run panel
const term = el('div', 'panel', `<div class="phead"><span class="dot" style="background:rgba(255,255,255,.18)"></span><span class="t">Run</span><span class="r mono">python checkout.py</span></div><div class="body"></div>`, stage, `left:${TERM.x}px;top:${TERM.y}px;width:${TERM.w}px;height:${TERM.h}px`);
term.id = 'term';
const tbody = $('.body', term);

// browser
const br = el('div', 'panel', `<div class="chrome"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="nav">‹ ›</span><div class="url"><span class="lock"></span>northwind.example/checkout</div></div><div id="prog"></div><div class="content"></div>`, stage, `left:${BR.x}px;top:${BR.y}px;width:${BR.w}px;height:${BR.h}px`);
br.id = 'br';
const content = $('.content', br), prog = $('#prog', br);
const f1 = el('iframe', '', null, content); f1.srcdoc = D.pages.v1;
const f2 = el('iframe', '', null, content); f2.srcdoc = D.pages.v2;

// overlay
const ov = el('div', '', null, stage); ov.id = 'ov';

// top bar
const runPlain = el('div', '', `<span class="d" style="background:rgba(255,255,255,.45)"></span>Plain Playwright`, stage); runPlain.id = 'run';
const runMw = el('div', '', `<span class="d" style="background:${C.purple};box-shadow:0 0 12px ${C.purple}"></span>Playwright <span style="color:var(--ink3);font-weight:500">+</span> <span style="color:#d9c4ff">mendwright</span>`, stage); runMw.id = 'run';
const STEPS = ['Today', 'Banner', 'One line', 'Redesign', 'Next run', 'Popups', 'Judgement', 'Any order', 'Selenium'];
const stepsEl = el('div', '', STEPS.map(s => `<div class="s"><i></i><span>${s}</span></div>`).join(''), stage); stepsEl.id = 'steps';
const stepEls = [...stepsEl.children];

// ---------- data from the real runs ----------
const ERR = D.B_error_lines;                      // trimmed real call log
const MODEL_S = D.model_seconds;                  // e.g. "0.5"
const PICK = D.model_pick_label;                  // "I agree to the purchase terms"
const XP = D.xpaths;                              // {email, terms, cont}

// ---------- run panel entries ----------
// group: [t0, t1, entries[[t, cls, html]]]
const sec = s => `${(+s).toFixed(2)} s`;
const okRow = (txt, s) => `<span class="i">✓</span>${esc(txt)}<span style="float:right;color:var(--ink3)">${s}</span>`;
const GROUPS = [
  [4.9, S.s2, [
    [5.0, 'cmd', '$ python checkout.py'],
    [5.65, 'ok', okRow('page.click("#email")', sec(D.A[0]))],
    [6.4, 'ok', okRow('page.keyboard.type(...)', sec(D.A[1]))],
    [7.0, 'ok', okRow('page.check("#accept-terms")', sec(D.A[2]))],
    [7.65, 'ok', okRow('page.click("#continue")', sec(D.A[3]))],
    [7.9, 'sum', `<span style="color:${C.green}">Passed</span> · 4 steps`],
  ]],
  [S.s2 + .1, S.s3, [
    [11.45, 'cmd', '$ python checkout.py'],
    [14.7, 'err', ERR],
  ]],
  [S.s3 + .1, S.s4, [
    [21.95, 'cmd', '$ python checkout.py'],
    [23.0, 'log', `<span class="i">▸</span><span class="tag-log">log</span>${esc(D.C_log)}`],
    [24.1, 'ok', okRow('page.click("#email")', sec(D.C[0]))],
    [25.4, 'ok', okRow('page.check("#accept-terms")', sec(D.C[2]))],
    [26.0, 'ok', okRow('page.click("#continue")', sec(D.C[3]))],
    [26.25, 'sum', `<span style="color:${C.green}">Passed</span> · banner rejected`],
  ]],
  [S.s4 + .1, S.s5, [
    [33.0, 'cmd', '$ python checkout.py'],
    [35.2, 'log', `<span class="i">◆</span><span class="tag-log">log</span>${esc(D.D_logs[0])}`],
    [37.0, 'ok', okRow('page.click("#email")', sec(D.D[0]))],
    [41.35, 'log', `<span class="i">★</span><span class="tag-log">log</span>${esc(D.D_logs[2])}`],
    [43.1, 'ok', okRow('page.check("#accept-terms")', sec(D.D[2]))],
    [45.0, 'log', `<span class="i">◆</span><span class="tag-log">log</span>${esc(D.D_logs[3])}`],
    [46.3, 'ok', okRow('page.click("#continue")', sec(D.D[3]))],
    [46.55, 'sum', `<span style="color:${C.green}">Passed</span> · 3 selectors repaired and saved`],
  ]],
  [S.s5 + .1, S.s6, [
    [49.4, 'cmd', '$ python checkout.py'],
    [50.05, 'ok', okRow('page.click("#email")  · saved fix', sec(D.E[0]))],
    [50.8, 'ok', okRow('page.check("#accept-terms")  · saved fix', sec(D.E[2]))],
    [51.2, 'ok', okRow('page.click("#continue")  · saved fix', sec(D.E[3]))],
    [51.45, 'sum', `<span style="color:${C.green}">Passed</span> · ${D.E_total} s · no waiting, no model`],
  ]],
];
const groupEls = GROUPS.map(([t0, t1, ents]) => {
  const g = el('div', 'grp mono', null, tbody);
  return { t0, t1, g, ents: ents.map(([t, cls, html]) => ({ t, e: el('div', 'ent ' + cls, html, g) })) };
});

// ---------- overlay pieces ----------
const cursor = el('div', '', `<svg width="30" height="30" viewBox="0 0 30 30"><path d="M5 3 L5 24 L10.5 18.8 L14.2 27 L18 25.3 L14.4 17.3 L22 17.3 Z" fill="#fff" stroke="#11151d" stroke-width="1.6" stroke-linejoin="round"/></svg>`, ov); cursor.id = 'cursor';
const halo = el('div', '', null, ov, `position:absolute;width:64px;height:64px;border-radius:50%;background:radial-gradient(circle,rgba(185,140,255,.55),rgba(185,140,255,0) 70%)`);
const RIPPLES = [];
function ripple(t, at, color) { RIPPLES.push({ t, at, color, e: el('div', 'ripple', null, ov) }); }
const box = (style) => el('div', 'box', null, ov, style || '');
const chip = (html, style) => el('div', 'chip', html, ov, style || '');
const ffIcon = col => `<svg width="22" height="22" viewBox="0 0 22 22"><circle cx="11" cy="11" r="9" fill="none" stroke="rgba(255,255,255,.14)" stroke-width="2.5"/><circle class="arc" cx="11" cy="11" r="9" fill="none" stroke="${col}" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="56.55" stroke-dashoffset="56.55" transform="rotate(-90 11 11)"/></svg>`;
function timer(col) { const e = el('div', 'timer', `${ffIcon(col)}<span class="tx"></span><span class="ff" style="color:var(--ink3);font-size:13px;font-weight:700;letter-spacing:.04em">▶▶ SPED UP</span>`, ov); return { e, arc: $('.arc', e), tx: $('.tx', e) }; }

// HUDs
function hud(rows) {
  const h = el('div', 'hud', null, ov);
  const parts = rows.map(r => {
    if (r.xp != null) return { ...r, e: el('div', 'xp mono', '', h) };
    if (r.shimmer) return { ...r, e: el('div', 'shimmer', '<i></i>', h) };
    return { ...r, e: el('div', 'row', `<span class="ic" style="color:${r.col}">${r.ic}</span><span>${r.html}</span>`, h) };
  });
  return { h, parts };
}

// ---------- captions ----------
const CAPS = [
  [3.9, 9.5, 'Your Playwright script, on the site as it is today.'],
  [10.0, 12.75, 'Then the site adds a cookie banner.'],
  [12.85, 19.4, 'Plain Playwright <s>can’t click the email field</s> through it. After 30 s the run dies.'],
  [19.8, 22.35, 'Add <em>one line</em>.'],
  [22.45, 28.7, 'mendwright <em>rejects the banner</em>, then carries on.'],
  [29.2, 31.3, 'A week later, the site redeploys.'],
  [31.4, 35.0, 'New ids, new classes, reworded labels. <s>All three selectors are gone.</s>'],
  [35.1, 39.35, 'The email field is found again <u>from memory</u>. No AI.'],
  [39.45, 43.5, 'Too close to call from memory, so the <em>1 GB local model</em> picks. On your machine.'],
  [43.6, 48.4, 'Every fix is <u>checked on the live page</u>, then saved.'],
  [48.9, 53.4, 'Next run, the saved fixes are used directly. <u>No waiting, no model.</u>'],
];
const capEls = CAPS.map(([a, b, h]) => ({ a, b, e: el('div', 'cap', h, document.body) }));

// status pills: [t, kind, text]
const STATUS = [
  [5.0, 'run', 'Running'], [7.8, 'ok', 'Passed'], [S.s2, null],
  [11.5, 'run', 'Running'], [14.8, 'bad', 'Failed at line 1'], [S.s3, null],
  [21.9, 'run', 'Running'], [26.2, 'ok', 'Passed'], [S.s4, null],
  [33.0, 'run', 'Running'], [46.5, 'ok', 'Passed · 3 repaired'], [S.s5, null],
  [49.4, 'run', 'Running'], [51.5, 'ok', `Passed · ${D.E_total} s`], [S.s6 - .2, null],
];
const statusEls = [];
STATUS.forEach(([t, kind, text], i) => {
  if (!kind) return;
  const tout = STATUS[i + 1][0];
  const col = kind === 'ok' ? C.green : kind === 'bad' ? C.red : C.blue;
  const ic = kind === 'ok' ? '✓' : kind === 'bad' ? '✕' : '';
  const e = el('div', '', `${kind === 'run' ? `<span class="spin" style="width:14px;height:14px;border-radius:50%;border:2.5px solid rgba(90,169,255,.25);border-top-color:${C.blue}"></span>` : `<span style="font-size:18px">${ic}</span>`}${text}`, stage, `background:${col}1f;border:1px solid ${col}55;color:${kind === 'run' ? '#cfe3ff' : col}`);
  e.id = 'status';
  statusEls.push({ t, tout, kind, e, spin: $('.spin', e) });
});

// cards
const band = el('div', '', null, document.body); band.id = 'band';
const title = el('div', 'card', `<div class="wm"><b>mendwright</b></div><div class="tsub">Playwright &amp; Selenium selectors that repair themselves.</div><div class="tcode mono"><span class="k-obj">page</span><span class="k-p">.</span><span class="k-fn">click</span><span class="k-p">(</span><span class="old">"#email"</span><span class="neu">"//input[@name='email']"</span><span class="k-p">)</span><span class="ok">✓</span></div>`, document.body);
const [tWm, tSub, tCode] = title.children;
const tOld = $('.old', tCode), tNeu = $('.neu', tCode), tOk = $('.ok', tCode);
const T_OLD = '"#email"', T_NEU = `"//input[@name='email']"`;
const end = el('div', 'card', `<div class="wm" style="font-size:104px"><b>mendwright</b></div>
<div class="feat">
 <div><span class="ic" style="background:${C.blue}22;color:${C.blue}">◆</span>Fixes from memory first, with no AI.</div>
 <div><span class="ic" style="background:${C.purple}22;color:${C.purple}">★</span>A 1 GB local model for the hard ones. No API key.</div>
 <div><span class="ic" style="background:${C.green}22;color:${C.green}">✓</span>Every fix checked on the live page, then saved.</div>
 <div><span class="ic" style="background:${C.amber}22;color:${C.amber}">▸</span>Cookie banners and popups declined, never “Sign up”, never a cancel.</div>
 <div><span class="ic" style="background:${C.green}22;color:${C.green}">⇄</span>Checkout steps in any order, with heal.step() and run_steps().</div>
</div>
<div class="pills mono"><div class="pill"><span class="p">$</span>pip install mendwright</div><div class="pill"><span class="p">$</span>mendwright model install</div></div>
<div class="repo">github.com/saadbuilt/mendwright · Playwright &amp; Selenium · MIT</div>`, document.body);
const [eWm, eFeat, ePills, eRepo] = end.children;
const featRows = [...eFeat.children];

// ---------- measured after the pages load ----------
let R = null;     // rects in content coordinates
let doc1, doc2, banner, bCard, bReject, v1email, v1chk, v1cont, v2email, v2a, v2b, v2cont;
let selTok = {};  // editor selector tokens: offset of each in its line
let CW = 10.8;    // editor char width
let H = {};       // HUDs and other pieces needing rects
let CURSOR, CLICKS, GHOST;

function rectOf(node) {
  const r = node.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
}
function grow(r, p) { return { x: r.x - p, y: r.y - p, w: r.w + 2 * p, h: r.h + 2 * p }; }

function measure() {
  doc1 = f1.contentDocument; doc2 = f2.contentDocument;
  banner = doc1.getElementById('cc-wrap'); bCard = banner.firstElementChild; bReject = bCard.querySelector('button');
  v1email = doc1.getElementById('email'); v1chk = doc1.getElementById('accept-terms'); v1cont = doc1.getElementById('continue');
  v2email = doc2.querySelector('input[name=email]'); v2a = doc2.getElementById('opt-77a1'); v2b = doc2.getElementById('opt-91c4');
  v2cont = doc2.querySelector('.btn-x82hd');
  for (const d of [doc1, doc2]) { d.documentElement.style.overflow = 'hidden'; }
  bCard.style.willChange = 'transform,opacity,filter';
  R = {
    v1email: rectOf(v1email), v1chk: rectOf(v1chk), v1chkL: rectOf(v1chk.closest('label')), v1cont: rectOf(v1cont),
    reject: rectOf(bReject), card: rectOf(bCard),
    v2email: rectOf(v2email), v2a: rectOf(v2a.closest('label')), v2b: rectOf(v2b.closest('label')), v2bIn: rectOf(v2b),
    v2cont: rectOf(v2cont),
  };
  // editor char width and token offsets
  const probe = el('span', 'mono', 'x'.repeat(40), code, 'position:absolute;visibility:hidden;font-size:18px;white-space:pre');
  CW = probe.getBoundingClientRect().width / 40; probe.remove();
  for (const id of ['L1', 'L3', 'L4']) {
    const tok = $('.sel', lineEls[id].root);
    const lr = lineEls[id].root.getBoundingClientRect(), tr = tok.getBoundingClientRect();
    selTok[tok.dataset.k] = { line: id, dx: tr.left - lr.left, w: tr.width, h: tr.height, tok };
  }
  buildScenes();
  if (window.measureScenes) measureScenes();
}

const P = (r, dx = 0, dy = 0) => [r.cx + dx, r.cy + dy];
function buildScenes() {
  const rest1 = [700, 560], rest2 = [760, 520];
  const em1 = [R.v1email.x + 74, R.v1email.cy + 3], chk1 = P(R.v1chk, 2, 3), cont1 = P(R.v1cont, -30, 4);
  const rej = P(R.reject, -10, 4);
  const em2 = [R.v2email.x + 80, R.v2email.cy + 3], chk2 = P(R.v2bIn, 2, 3), cont2 = [R.v2cont.x + 150, R.v2cont.cy + 4];
  CURSOR = [
    [0, rest1],
    [5.0, em1], [6.4, chk1], [7.05, cont1], [8.2, rest2],
    [11.6, em1], [15.2, rest1],
    [22.3, rej], [23.6, em1], [24.9, chk1], [25.5, cont1], [26.6, rest2],
    [33.2, em1], [36.55, em2], [37.75, chk1], [42.6, chk2], [43.6, cont1], [45.95, cont2], [47.0, rest2],
    [49.55, em2], [50.4, chk2], [50.8, cont2], [51.7, rest2],
  ];
  CLICKS = [
    [5.6, em1, '#fff'], [6.95, chk1, '#fff'], [7.6, cont1, '#fff'],
    [12.3, em1, C.red], [12.75, em1, C.red], [13.2, em1, C.red], [13.65, em1, C.red], [14.1, em1, C.red],
    [22.95, rej, C.purple], [24.1, em1, '#fff'], [25.4, chk1, '#fff'], [26.0, cont1, '#fff'],
    [37.0, em2, '#fff'], [43.1, chk2, '#fff'], [46.3, cont2, '#fff'],
    [50.05, em2, '#fff'], [50.8, chk2, '#fff'], [51.2, cont2, '#fff'],
  ];
  for (const [t, at, col] of CLICKS) ripple(t, at, col);

  // S2: blocked click
  H.block = chip(`<span style="color:${C.red}">⊘</span>&nbsp; <span class="mono" style="font-weight:500">&lt;div class="cookie-consent"&gt;</span> intercepts pointer events`,
    `background:rgba(14,18,27,.9);border:1px solid ${C.red}66;color:#ffd3d3;font-size:16px;padding:9px 14px;border-radius:11px;box-shadow:0 14px 40px rgba(0,0,0,.45)`);
  H.t30 = timer(C.red);
  // S3: banner chip
  H.bchip = chip(`▸ cookie banner in the way · clicking “Reject all”`, `background:${C.amber};color:#1b1405;font-size:15.5px;box-shadow:0 10px 30px rgba(0,0,0,.35)`);
  // S4: selector chips that leave the page for the code
  H.selChips = ['email', 'terms', 'cont'].map(k => chip(`<span class="mono">${{ email: '#email', terms: '#accept-terms', cont: '#continue' }[k]}</span>`,
    `font-size:16px;padding:4px 10px;border-radius:8px`));
  H.ghosts = ['email', 'terms', 'cont'].map(() => box());
  H.ghostLbl = ['#email', '#accept-terms', '#continue'].map(s => chip(`<span class="mono">${s}</span> &nbsp;not on the page`,
    `background:rgba(20,10,14,.9);border:1px solid ${C.red}88;color:#ffc4c4;font-size:14px;padding:4px 10px;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.3)`));
  H.t10 = [timer(C.amber), timer(C.amber), timer(C.amber)];
  H.amber = [box(), box()];
  H.hudE = hud([
    { ic: '◆', col: C.blue, html: '<b>Repaired from memory.</b> <span class="sub">No AI.</span>', t: 35.15 },
    { xp: XP.email, t: 35.4, t1: 35.95 },
    { ic: '✓', col: C.green, html: 'Checked on the live page: <b>1 visible textbox</b>', t: 36.15 },
  ]);
  H.hudT = hud([
    { ic: '?', col: C.amber, html: 'Two checkboxes, reworded label. <span class="sub">Too close to call from memory.</span>', t: 39.5 },
    { ic: '★', col: C.purple, html: `<b>Asking the local model</b> <span class="sub">1 GB · runs on this machine</span>`, t: 40.35 },
    { shimmer: true, t: 40.35, t1: 41.25 },
    { ic: '', col: C.purple, html: `Picked <b>“${esc(PICK)}”</b> <span class="sub">in ${MODEL_S} s</span>`, t: 41.3 },
    { xp: XP.terms, t: 41.5, t1: 41.95 },
    { ic: '✓', col: C.green, html: 'Checked on the live page: <b>1 visible checkbox</b>', t: 42.15 },
  ]);
  H.hudC = hud([
    { ic: '◆', col: C.blue, html: '<b>Repaired from memory.</b> <span class="sub">No AI.</span>', t: 44.95 },
    { xp: XP.cont, t: 45.05, t1: 45.6 },
    { ic: '✓', col: C.green, html: 'Checked on the live page: <b>1 visible button</b>', t: 45.75 },
  ]);
  H.saved = [R.v2email, R.v2b, R.v2cont].map(() => chip(`✓ saved fix`, `background:${C.green};color:#04210f;font-size:14px;padding:3px 10px`));
  H.savedBox = [box(), box(), box()];
}

// ---------- per-frame drawing ----------
function setBox(e, r, { col, dashed = false, fill = 'transparent', glow = 0, p = 1, radius = 10 }) {
  if (p <= 0.002) { e.style.visibility = 'hidden'; return; }
  e.style.visibility = 'visible';
  e.style.left = r.x + 'px'; e.style.top = r.y + 'px'; e.style.width = r.w + 'px'; e.style.height = r.h + 'px';
  e.style.border = `${dashed ? 2 : 2.5}px ${dashed ? 'dashed' : 'solid'} ${col}`;
  e.style.background = fill;
  e.style.borderRadius = radius + 'px';
  e.style.boxShadow = glow ? `0 0 0 ${glow}px ${col}33, 0 0 30px ${col}44` : 'none';
  e.style.opacity = clamp(p).toFixed(3);
  e.style.transform = `scale(${lerp(1.06, 1, clamp(p)).toFixed(4)})`;
}
function place(e, x, y, p, o) { e.style.left = x + 'px'; e.style.top = y + 'px'; mat(e, p, o); }
function rectLerp(a, b, p) { return { x: lerp(a.x, b.x, p), y: lerp(a.y, b.y, p), w: lerp(a.w, b.w, p), h: lerp(a.h, b.h, p) }; }
function drawTimer(tm, x, y, p, frac, text) {
  place(tm.e, x, y, p, { dy: 10, s0: .9 });
  tm.arc.setAttribute('stroke-dashoffset', (56.55 * (1 - frac)).toFixed(2));
  tm.tx.textContent = text;
}

function render(t) {
  if (!R) return;
  // background drift
  g1.style.transform = `translate(${Math.sin(t * .21) * 60}px,${Math.cos(t * .17) * 40}px)`;
  g2.style.transform = `translate(${Math.cos(t * .19) * 70}px,${Math.sin(t * .23) * 50}px)`;
  g3.style.transform = `translate(${Math.sin(t * .13) * 90}px,0)`;

  // ---- title card
  const tOut = 3.05;
  // The first frame already shows the wordmark: it is the poster frame.
  mat(tWm, 1 - clamp(spring(t - tOut, .36)), { dy: 0, s0: .94, blur: 24 });
  if (t < tOut + .6) tWm.style.transform += ` scale(${lerp(1, 1.025, lin(t, 0, 3)).toFixed(4)})`;
  mat(tSub, pres(t, .35, tOut, .55), { dy: 18, blur: 10 });
  mat(tCode, pres(t, .8, tOut, .55), { dy: 18, blur: 10 });
  const cw30 = CW * 30 / 18;
  const strike = clamp(spring(t - 1.35, .3)), gone = clamp(spring(t - 1.8, .35));
  tOld.style.color = `rgb(${Math.round(lerp(165, 255, strike))},${Math.round(lerp(214, 107, strike))},${Math.round(lerp(255, 107, strike))})`;
  tOld.style.textDecoration = strike > .5 ? 'line-through 3px' : 'none';
  tOld.style.width = (T_OLD.length * cw30 * (1 - gone)).toFixed(1) + 'px';
  tOld.style.opacity = (1 - gone).toFixed(3);
  tNeu.style.width = (Math.round(T_NEU.length * lin(t, 1.95, 2.4)) * cw30).toFixed(1) + 'px';
  tOk.style.opacity = clamp(spring(t - 2.5, .3)).toFixed(3);
  tOk.style.transform = `scale(${lerp(.3, 1, spring(t - 2.5, .36, .55)).toFixed(3)})`;

  // ---- stage presence + camera
  const stIn = t < S.end ? 1 : 0;
  const sp = spring(t - S.s1, .6) * (1 - spring(t - S.end, .5));
  const camS = track(t, [[0, 1], [12.0, 1.13, .9], [14.45, 1, .9], [39.25, 1.13, .9], [43.25, 1, .9]]);
  const fx = track(t, [[0, 960], [12.0, CO.x + R.v1email.x + 120, .9], [39.25, CO.x + 560, .9]]);
  const fy = track(t, [[0, 540], [12.0, CO.y + R.v1email.cy, .9], [39.25, CO.y + R.v2b.cy - 10, .9]]);
  const endS = lerp(.985, 1, sp);
  stage.style.transform = `translate(${fx}px,${fy}px) scale(${(camS * endS).toFixed(4)}) translate(${-fx}px,${-fy}px)`;
  stage.style.opacity = clamp(sp).toFixed(3);
  stage.style.filter = sp < .995 ? `blur(${((1 - clamp(sp)) * 14).toFixed(2)}px)` : 'none';
  stage.style.visibility = sp < .002 ? 'hidden' : 'visible';
  if (sp < .002) { drawCards(t); drawCaps(t); return; }
  mat(br, spring(t - S.s1 - .05, .6), { dy: 40, s0: .96, blur: 18 });
  mat(ed, spring(t - S.s1 - .15, .6), { dy: 40, s0: .96, blur: 18 });
  mat(term, spring(t - S.s1 - .25, .6), { dy: 40, s0: .96, blur: 18 });

  // ---- top bar
  mat(runPlain, pres(t, S.s1 + .4, 21.0), { dy: -10, blur: 6 });
  mat(runMw, pres(t, 21.05, S.end), { dy: -10, blur: 6 });
  mat(stepsEl, spring(t - S.s1 - .5, .6), { dy: -10, blur: 6 });
  const scene = t < S.s2 ? 0 : t < S.s3 ? 1 : t < S.s4 ? 2 : t < S.s5 ? 3 : t < S.s6 ? 4 : t < S.s7 ? 5 : t < S.s8 ? 6 : t < S.s9 ? 7 : 8;
  const sceneStart = [S.s1, S.s2, S.s3, S.s4, S.s5, S.s6, S.s7, S.s8, S.s9][scene];
  stepEls.forEach((s, i) => {
    const on = i === scene, done = i < scene;
    const p = on ? spring(t - sceneStart, .4) : 0;
    s.style.color = on ? `rgba(238,242,247,${lerp(.4, .95, p)})` : done ? 'rgba(238,242,247,.55)' : '';
    const dot = s.firstChild;
    const col = [C.blue, C.amber, C.purple, C.purple, C.purple, C.green, C.amber, C.green, C.blue][i];
    dot.style.background = on ? col : done ? 'rgba(255,255,255,.5)' : 'rgba(255,255,255,.18)';
    dot.style.boxShadow = on ? `0 0 ${lerp(0, 14, p)}px ${col}` : 'none';
    dot.style.transform = `scale(${on ? lerp(1, 1.35, spring(t - sceneStart, .35, .6)) : 1})`;
  });
  for (const s of statusEls) {
    const p = pres(t, s.t, s.tout, .36, .3);
    s.e.style.right = '64px';
    mat(s.e, p, { dy: 0, s0: s.kind === 'run' ? .92 : .8, blur: 6 });
    if (p > .002 && s.kind !== 'run') s.e.style.transform = `scale(${lerp(.8, 1, spring(t - s.t, .38, .62)).toFixed(4)})`;
    if (s.spin) s.spin.style.transform = `rotate(${(t * 420) % 360}deg)`;
  }

  drawEditor(t);
  drawRun(t);
  drawBrowser(t);
  drawOverlay(t);
  if (window.drawScenes) drawScenes(t);
  drawCaps(t);
  drawCards(t);
}

function drawEditor(t) {
  // line numbers and positions
  const ins = clamp(spring(t - T_INS, .5));
  for (const id of Object.keys(lineEls)) {
    const L = lineEls[id];
    L.root.style.top = lineY(id, t) + 'px';
    const n = Math.round(lineIdx(id, t)) + 1;
    L.g.textContent = id === 'L0' ? '1' : String(t >= T_INS + .2 ? BASE[id] + 2 : BASE[id] + 1);
  }
  // the inserted line types in
  const L0 = lineEls.L0;
  const shown = Math.round(L0_TEXT.length * lin(t, T_INS + .25, T_INS + 1.25));
  L0.root.style.opacity = t < T_INS ? 0 : clamp(spring(t - T_INS, .35)).toFixed(3);
  L0.cw.style.display = 'inline-block'; L0.cw.style.overflow = 'hidden'; L0.cw.style.verticalAlign = 'bottom';
  L0.cw.style.width = (shown * CW).toFixed(1) + 'px';
  L0.root.style.background = t > T_INS ? `rgba(185,140,255,${(0.14 * pres(t, T_INS, 22.0, .4, .9)).toFixed(3)})` : 'transparent';
  L0.root.style.borderRadius = '8px';
  // selector tokens: red while broken, then the colour of the repair
  const tokState = {
    email: [[32.55, 'broken'], [35.3, 'mem'], [S.s5 + .1, 'saved']],
    terms: [[32.65, 'broken'], [41.3, 'ai'], [S.s5 + .1, 'saved']],
    cont: [[32.75, 'broken'], [44.95, 'mem'], [S.s5 + .1, 'saved']],
  };
  for (const k of Object.keys(selTok)) {
    let st = null, t0 = 0;
    for (const [tt, s] of tokState[k]) if (t >= tt) { st = s; t0 = tt; }
    if (t >= S.s6) st = null;
    const tok = selTok[k].tok;
    const col = st === 'broken' ? C.red : st === 'mem' ? C.blue : st === 'ai' ? C.purple : st === 'saved' ? C.green : null;
    if (!col) { tok.style.textDecoration = 'none'; tok.style.background = 'transparent'; continue; }
    tok.style.textDecoration = `underline ${st === 'broken' ? 'wavy' : 'solid'} ${col}`;
    tok.style.textUnderlineOffset = '6px'; tok.style.textDecorationThickness = '2px';
    tok.style.background = `${col}${Math.round(0x22 * clamp(spring(t - t0, .4))).toString(16).padStart(2, '0')}`;
  }
  // current-line highlight
  const HL = [[0, 'L1', 0], [5.0, 'L1', 1], [6.35, 'L2', 1], [6.6, 'L3', 1], [7.1, 'L4', 1], [7.9, 'L4', 0],
    [11.5, 'L1', 1], [16.5, 'L1', 0],
    [21.9, 'L1', 1], [24.2, 'L2', 1], [24.85, 'L3', 1], [25.5, 'L4', 1], [26.3, 'L4', 0],
    [33.0, 'L1', 1], [37.3, 'L2', 1], [37.7, 'L3', 1], [43.5, 'L4', 1], [46.5, 'L4', 0],
    [49.4, 'L1', 1], [50.1, 'L2', 1], [50.4, 'L3', 1], [50.8, 'L4', 1], [51.5, 'L4', 0]];
  const yk = HL.map(([tt, id]) => [tt, lineY(id, t)]);
  const ok = HL.map(([tt, , on]) => [tt, on]);
  hl.style.top = track(t, yk, .32) + 'px';
  const failed = t >= 14.7 && t < S.s3;
  const op = clamp(track(t, ok, .3));
  hl.style.opacity = op.toFixed(3);
  hl.style.background = failed ? 'rgba(255,107,107,.14)' : 'rgba(90,169,255,.12)';
  hl.style.boxShadow = failed ? 'inset 0 0 0 1px rgba(255,107,107,.4)' : 'inset 0 0 0 1px rgba(90,169,255,.28)';
  // per-line results
  const MARKS = {
    L1: [[5.65, '✓', C.green, S.s2], [14.7, '✕', C.red, S.s3], [24.1, '✓', C.green, S.s4], [37.0, '✓', C.green, S.s5], [50.05, '✓', C.green, S.s6]],
    L2: [[6.35, '✓', C.green, S.s2], [24.8, '✓', C.green, S.s4], [37.6, '✓', C.green, S.s5], [50.4, '✓', C.green, S.s6]],
    L3: [[6.95, '✓', C.green, S.s2], [25.4, '✓', C.green, S.s4], [43.1, '✓', C.green, S.s5], [50.8, '✓', C.green, S.s6]],
    L4: [[7.6, '✓', C.green, S.s2], [26.0, '✓', C.green, S.s4], [46.3, '✓', C.green, S.s5], [51.2, '✓', C.green, S.s6]],
    L0: [],
  };
  for (const id of Object.keys(lineEls)) {
    const L = lineEls[id];
    let m = null;
    for (const mk of MARKS[id]) if (t >= mk[0] && t < mk[3]) m = mk;
    if (!m) { L.st.style.opacity = 0; continue; }
    L.st.textContent = m[1]; L.st.style.color = m[2];
    const p = pres(t, m[0], m[3] - .15, .36, .25);
    L.st.style.opacity = clamp(p).toFixed(3);
    L.st.style.transform = `scale(${lerp(.4, 1, spring(t - m[0], .36, .55)).toFixed(3)})`;
  }
}

function drawRun(t) {
  for (const g of groupEls) {
    const gp = pres(t, g.t0, g.t1 - .25, .3, .3);
    mat(g.g, gp, { dy: 0, blur: 6, s0: 1 });
    if (gp <= .002) continue;
    for (const en of g.ents) {
      const p = spring(t - en.t, .38);
      en.e.style.display = t >= en.t ? 'block' : 'none';
      if (t >= en.t) { en.e.style.opacity = clamp(p).toFixed(3); en.e.style.transform = `translateY(${((1 - p) * 8).toFixed(2)}px)`; }
    }
  }
}

function setVal(input, v) { if (input.value !== v) input.value = v; }
function focusRing(input, on, col) {
  input.style.boxShadow = on > .01 ? `0 0 0 ${(3.5 * on).toFixed(2)}px ${col}` : 'none';
}
function drawBrowser(t) {
  // reloads
  const RL = [S.s2 + .05, S.s3 + .1, S.s5 + .1];
  let pw = 0, po = 0;
  for (const r of RL) {
    if (t >= r && t < r + 1.0) { pw = easeIO(lin(t, r, r + .5)); po = 1 - lin(t, r + .55, r + .9); }
  }
  prog.style.width = (pw * 100).toFixed(2) + '%'; prog.style.opacity = po.toFixed(3);
  let dip = 0;
  for (const r of RL) dip = Math.max(dip, Math.sin(Math.PI * lin(t, r + .05, r + .5)) * .35);

  // which page: V1 until the redesign, then V2
  const out1 = spring(t - 30.4, .6), in2 = spring(t - 30.6, .6);
  mat(f1, (1 - out1) * (1 - dip), { dy: 0, s0: 1.015, blur: 10 });
  if (t >= 30.4 && out1 < .999) f1.style.transform = `scale(${lerp(1, .985, out1)})`;
  mat(f2, in2 * (1 - (t > S.s5 ? dip : 0)), { dy: 0, s0: 1.015, blur: 10 });

  // V1 state
  const em1 = t < S.s2 ? typed('ada@example.com', t, 5.75, 6.3) : (t >= S.s3 && t < S.s4) ? typed('ada@example.com', t, 24.25, 24.75) : '';
  setVal(v1email, em1);
  focusRing(v1email, pres(t, 5.6, 6.45, .3, .3) + pres(t, 24.1, 24.9, .3, .3), 'rgba(90,169,255,.45)');
  v1chk.checked = (t >= 6.95 && t < S.s2) || (t >= 25.4 && t < S.s4);
  const press1 = Math.sin(Math.PI * lin(t, 7.6, 7.85)) + Math.sin(Math.PI * lin(t, 26.0, 26.25));
  v1cont.style.transform = `scale(${(1 - .04 * press1).toFixed(4)})`;
  // banner: in for S2, reload in S3, rejected by mendwright
  const bp = pres(t, 10.4, S.s3 + .12, .55, .25) + pres(t, S.s3 + .55, 23.05, .55, .5);
  if (bp <= .002) banner.style.visibility = 'hidden';
  else {
    banner.style.visibility = 'visible';
    banner.style.opacity = clamp(bp).toFixed(3);
    const cp = clamp(bp);
    bCard.style.transform = `translateY(${((1 - bp) * 46).toFixed(2)}px) scale(${lerp(.94, 1, bp).toFixed(4)})`;
    bCard.style.filter = cp < .995 ? `blur(${((1 - cp) * 10).toFixed(2)}px)` : 'none';
  }
  const pr = Math.sin(Math.PI * lin(t, 22.95, 23.15));
  bReject.style.transform = `scale(${(1 - .05 * pr).toFixed(4)})`;
  bReject.style.boxShadow = t > 22.3 && t < 23.2 ? `0 0 0 ${(4 * pres(t, 22.4, 23.05, .3, .2)).toFixed(2)}px rgba(185,140,255,.55)` : 'none';

  // V2 state
  const em2 = (t >= S.s4 && t < S.s5) ? typed('ada@example.com', t, 37.1, 37.55) : t >= S.s5 + .3 ? typed('ada@example.com', t, 50.1, 50.35) : '';
  setVal(v2email, em2);
  focusRing(v2email, pres(t, 37.0, 37.7, .3, .3) + pres(t, 50.05, 50.45, .3, .3), 'rgba(90,169,255,.45)');
  v2b.checked = (t >= 43.1 && t < S.s5) || t >= 50.8;
  const press2 = Math.sin(Math.PI * lin(t, 46.3, 46.55)) + Math.sin(Math.PI * lin(t, 51.2, 51.45));
  v2cont.style.transform = `scale(${(1 - .03 * press2).toFixed(4)})`;
}

function drawOverlay(t) {
  // cursor
  const [cx, cy] = trackN(t, CURSOR, .55);
  const cvis = pres(t, 4.8, 9.4, .3, .3) + pres(t, 11.35, 16.3, .3, .3) + pres(t, 22.1, 27.0, .3, .3) + pres(t, 33.0, 47.4, .3, .3) + pres(t, 49.35, 52.2, .3, .3);
  cursor.style.left = (cx - 5) + 'px'; cursor.style.top = (cy - 3) + 'px';
  cursor.style.opacity = clamp(cvis).toFixed(3);
  let press = 0;
  for (const r of RIPPLES) press = Math.max(press, Math.sin(Math.PI * lin(t, r.t - .06, r.t + .12)));
  cursor.style.transform = `scale(${(1 - .14 * press).toFixed(3)})`;
  const hp = pres(t, 22.15, 23.25, .35, .3) * clamp(cvis);
  halo.style.left = (cx - 32) + 'px'; halo.style.top = (cy - 32) + 'px'; halo.style.opacity = hp.toFixed(3);
  for (const r of RIPPLES) {
    const p = lin(t, r.t, r.t + .65);
    if (p <= 0 || p >= 1) { r.e.style.visibility = 'hidden'; continue; }
    const e = 1 - Math.pow(1 - p, 3), rad = 6 + 34 * e;
    r.e.style.visibility = 'visible';
    r.e.style.left = (r.at[0] - rad) + 'px'; r.e.style.top = (r.at[1] - rad) + 'px';
    r.e.style.width = r.e.style.height = 2 * rad + 'px';
    r.e.style.borderColor = r.color; r.e.style.opacity = (.85 * (1 - p)).toFixed(3);
  }

  // S2: the banner swallows the click
  place(H.block, R.v1email.x + 150, R.v1email.y - 64, pres(t, 12.4, 15.0, .4, .3), { dy: 10, blur: 6 });
  const f30 = lin(t, 12.6, 14.55);
  drawTimer(H.t30, R.v1email.x + 150, R.v1email.y - 118, pres(t, 12.55, 15.0, .4, .3), f30,
    f30 < 1 ? `Playwright retries the click · ${Math.round(30 * f30)} s` : 'Timeout 30000ms exceeded');

  // S3: mendwright handles the banner
  place(H.bchip, R.card.x + 8, R.card.y - 50, pres(t, 22.2, 23.15, .4, .3), { dy: 10, blur: 6 });

  // S4: the selectors leave the page and land on the code
  const chipsAt = [R.v1email, R.v1chkL, R.v1cont];
  ['email', 'terms', 'cont'].forEach((k, i) => {
    const c = H.selChips[i], tk = selTok[k];
    const t0 = 29.55 + i * .12, tRed = 31.0 + i * .08, tFly = 31.95 + i * .1;
    const from = [chipsAt[i].x + chipsAt[i].w + 16, chipsAt[i].cy - 16];
    if (k === 'terms') from[0] = chipsAt[i].x + chipsAt[i].w + 16;
    const ly = lineY(tk.line, t);
    const to = [ED.x + 1 + tk.dx - 7 - CO.x, ED.y + 1 + 62 + ly + 3 - CO.y];  // tk.dx already includes the gutter
    const fp = spring(t - tFly, .6);
    c.style.left = lerp(from[0], to[0], fp) + 'px'; c.style.top = lerp(from[1], to[1], fp) + 'px';
    const red = clamp(spring(t - tRed, .35));
    c.style.background = `rgba(${Math.round(lerp(90, 255, red))},${Math.round(lerp(169, 107, red))},${Math.round(lerp(255, 107, red))},.92)`;
    c.style.color = '#0b0f16';
    c.style.textDecoration = red > .5 ? 'line-through 2px rgba(11,15,22,.7)' : 'none';
    mat(c, pres(t, t0, tFly + .72, .4, .25), { dy: 8, s0: .85, blur: 6 });
  });

  // ghosts, repairs and HUDs
  const G = [
    { i: 0, old: R.v1email, neu: R.v2email, tIn: 33.6, tTimer: [33.75, 35.0], tFix: 35.3, tOut: 37.6, col: C.blue, pad: 6 },
    { i: 1, old: R.v1chkL, neu: R.v2b, tIn: 38.15, tTimer: [38.3, 39.4], tFix: 41.3, tOut: 43.55, col: C.purple, pad: 7 },
    { i: 2, old: R.v1cont, neu: R.v2cont, tIn: 43.95, tTimer: [44.05, 44.8], tFix: 44.95, tOut: 46.8, col: C.blue, pad: 6 },
  ];
  for (const g of G) {
    const p = pres(t, g.tIn, g.tOut, .4, .3);
    const mp = spring(t - g.tFix, .55, .82);
    const r = rectLerp(grow(g.old, g.pad), grow(g.neu, g.pad), mp);
    const fixed = t >= g.tFix;
    setBox(H.ghosts[g.i], r, { col: fixed ? g.col : C.red, dashed: !fixed, fill: fixed ? `${g.col}12` : 'rgba(255,107,107,.07)', glow: fixed ? 4 : 0, p });
    const lp = pres(t, g.tIn + .1, g.tFix - .05, .4, .25);
    place(H.ghostLbl[g.i], g.old.x - g.pad, g.old.y - g.pad - 34, lp, { dy: 8, blur: 5 });
    const [a, b] = g.tTimer, f = lin(t, a + .1, b - .1);
    drawTimer(H.t10[g.i], g.old.x + g.old.w + 24, g.old.cy - 22, pres(t, a, b + .1, .35, .25), f,
      `waiting for ${['#email', '#accept-terms', '#continue'][g.i]} · ${Math.round(10 * f)} s of 10`);
  }
  // amber candidates for the checkbox
  [R.v2a, R.v2b].forEach((r, i) => setBox(H.amber[i], grow(r, 6), { col: C.amber, dashed: true, fill: 'rgba(242,193,78,.07)', p: pres(t, 39.5 + i * .08, 41.35, .4, .3) }));
  drawHud(H.hudE, t, 590, R.v2email.y - 40, 35.1, 37.45);
  drawHud(H.hudT, t, 590, R.v2a.y - 92, 39.45, 43.4);
  drawHud(H.hudC, t, 590, R.v2cont.y - 120, 44.9, 46.8);

  // S5: saved fixes
  [[R.v2email, 50.0], [R.v2b, 50.75], [R.v2cont, 51.15]].forEach(([r, tt], i) => {
    const p = pres(t, tt, 52.6, .35, .3);
    setBox(H.savedBox[i], grow(r, 6), { col: C.green, fill: 'rgba(63,212,122,.07)', glow: 3, p });
    place(H.saved[i], r.x + r.w + 18, r.cy - 14, p, { dx: -10, dy: 0, blur: 5 });
  });
}

function drawHud(hd, t, x, y, tIn, tOut) {
  place(hd.h, x, y, pres(t, tIn, tOut, .45, .3), { dx: 24, dy: 0, s0: .97, blur: 12, origin: '0 50%' });
  for (const part of hd.parts) {
    const p = spring(t - part.t, .38);
    part.e.style.opacity = clamp(p).toFixed(3);
    part.e.style.transform = `translateY(${((1 - p) * 6).toFixed(2)}px)`;
    if (part.xp != null) part.e.textContent = typed(part.xp, t, part.t, part.t1) || ' ';
    if (part.shimmer) {
      const s = part.e.firstChild, q = lin(t, part.t, part.t1);
      s.style.left = (-40 + 140 * ((q * 2.2) % 1)) + '%';
      part.e.style.opacity = (clamp(p) * (1 - lin(t, part.t1, part.t1 + .2))).toFixed(3);
    }
  }
}

function drawCaps(t) {
  for (const c of capEls) mat(c.e, pres(t, c.a, c.b, .45, .3), { dy: 12, blur: 8, s0: .99 });
}

function drawCards(t) {
  const e0 = S.end + .5;
  mat(eWm, spring(t - e0, .6), { dy: 0, s0: .94, blur: 22 });
  featRows.forEach((r, i) => mat(r, spring(t - (e0 + .7 + i * .22), .5), { dy: 16, blur: 8 }));
  mat(ePills, spring(t - (e0 + 1.85), .5), { dy: 14, blur: 8 });
  mat(eRepo, spring(t - (e0 + 2.2), .5), { dy: 10, blur: 6 });
}

const drawStory = render;   // a top-level function is window.render: keep the original
window.render = t => { SS = 1 / K; drawStory(t / K); };
window.ready = false;
window.addEventListener('load', () => { measure(); window.render(0); window.ready = true; });
