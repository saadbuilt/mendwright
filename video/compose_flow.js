// The flow act: what mendwright does when the steps change, not the selectors.
// F1  dismiss=True: a popup it was never told about is declined (Escape, then "No thanks").
// F2  optional=True: a step that isn't on the page is skipped.
// F3  the page guard: a step that moved to another page is not "repaired" onto a lookalike.
// Uses the helpers and elements of compose.js; every text shown comes from D.F (real runs).
'use strict';
const F = D.F;
const FLOW_END = S.s7;      // the Selenium beat follows

// ---------- the flow script ----------
const FLINES = {
  M0: O_('heal') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('page') + P_(', ') + O_('dismiss') + P_('=') + '<span class="k-fn">True</span>' + P_(')'),
  M1: O_('page') + ' ' + P_('=') + ' ' + O_('heal') + P_('.') + O_('page'),
  M2: O_('page') + P_('.') + F_('goto') + P_('(') + Str('"https://northwind.example/checkout"') + P_(')'),
  M3: O_('heal') + P_('.') + F_('locator') + P_('(') + Str('"promo"') + P_(', ') + Str('"#promo"') + P_(', ') + O_('optional') + P_('=') + '<span class="k-fn">True</span>' + P_(').') + F_('click') + P_('()'),
  M4: O_('page') + P_('.') + F_('click') + P_('(') + Str('"#email"') + P_(')'),
  M5: O_('page') + P_('.') + F_('click') + P_('(') + Str('"#continue"') + P_(')'),
};
const flowEls = {};
Object.keys(FLINES).forEach((id, i) => {
  const ln = el('div', 'ln mono', `<span class="g">${i + 1}</span><span class="c"><span class="cw">${FLINES[id]}</span></span><span class="st"></span>`, code, 'font-size:16.5px');
  flowEls[id] = { root: ln, st: $('.st', ln), i };
});
const fy = id => 14 + flowEls[id].i * LH;

// ---------- the pages ----------
const f3 = el('iframe', '', null, content); f3.srcdoc = F.pages.v2_nl;
const f4 = el('iframe', '', null, content); f4.srcdoc = F.pages.shipping;
const urlEl = $('.url', br);
const URL1 = urlEl.innerHTML, URL2 = URL1.replace('checkout', 'checkout/shipping');

// ---------- timeline (story seconds) ----------
const T = {
  in: S.s6, load: S.s6 + .8, pop: S.s6 + 1.6, run1: S.s6 + 2.0,
  goto1: S.s6 + 2.3, promo: S.s6 + 2.6, skip1: S.s6 + 4.0,
  email: S.s6 + 4.6, block: S.s6 + 5.2, hud: S.s6 + 5.4, esc: S.s6 + 5.85, noThanks: S.s6 + 6.75,
  gone: S.s6 + 6.85, emailOk: S.s6 + 7.75, cont: S.s6 + 8.0, contOk: S.s6 + 8.45, pass1: S.s6 + 8.75,
  split: S.s6 + 10.0, url2: S.s6 + 10.5, run2: S.s6 + 11.0, goto2: S.s6 + 11.3, skip2: S.s6 + 12.0,
  email2: S.s6 + 12.4, cont2: S.s6 + 12.9, ghost: S.s6 + 13.3, wait2: [S.s6 + 13.45, S.s6 + 14.5],
  look: S.s6 + 14.6, guard: S.s6 + 14.8, stop: S.s6 + 15.6,
};

// ---------- run panel, captions, status ----------
const fsec = s => `${(+s).toFixed(2)} s`;
const FGROUPS = [
  [T.run1 - .1, T.split, [
    [T.run1, 'cmd', '$ python checkout.py'],
    [T.goto1, 'ok', okRow('page.goto(".../checkout")', fsec(F.goto_s))],
    [T.skip1, 'flow', `<span class="i">↷</span><span class="tag-log">log</span>${esc(F.promo_log)}`],
    [T.gone - .05, 'log', `<span class="i">▸</span><span class="tag-log">log</span>${esc(F.dismiss_log)}`],
    [T.emailOk, 'ok', okRow('page.click("#email")', fsec(F.email_s))],
    [T.contOk, 'ok', okRow('page.click("#continue")', fsec(F.cont_s))],
    [T.pass1, 'sum', `<span style="color:${C.green}">Passed</span> · popup declined, promo skipped`],
  ]],
  [T.split + .1, FLOW_END, [
    [T.run2, 'cmd', '$ python checkout.py'],
    [T.goto2, 'ok', okRow('page.goto(...) → /checkout/shipping', fsec(F.F3.goto_s))],
    [T.skip2, 'flow', `<span class="i">↷</span>skipped 'promo' (optional)`],
    [T.email2 + .3, 'ok', okRow('page.click("#email")', fsec(F.F3.email_s))],
    [T.stop, 'err', `<b>${esc(F.F3.guard_type)}</b>: ${esc(F.F3.guard_msg)}`],
  ]],
];
FGROUPS.forEach(([t0, t1, ents]) => {
  const g = el('div', 'grp mono', null, tbody);
  groupEls.push({ t0, t1, g, ents: ents.map(([t, cls, html]) => ({ t, e: el('div', 'ent ' + cls, html, g) })) });
});
[
  [T.in + .3, T.promo + .5, 'Flows change too: popups, steps that come and go, steps that move.'],
  [T.promo + .6, T.email + .5, '<em>optional=True</em>: a step that isn’t on the page is skipped, not repaired.'],
  [T.email + .6, T.split - .1, '<em>dismiss=True</em>: an unknown popup is declined. Escape first, then “No thanks”. <s>Never “Sign up”.</s>'],
  [T.split, T.cont2, 'Then the site splits checkout into steps. The button moves to the next page.'],
  [T.cont2 + .1, FLOW_END - .2, 'A step that moved isn’t “repaired” onto a lookalike. <u>It stops and says why.</u>'],
].forEach(([a, b, h]) => capEls.push({ a, b, e: el('div', 'cap', h, document.body) }));
[
  [T.run1, 'run', 'Running', T.pass1], [T.pass1, 'ok', 'Passed', T.split],
  [T.run2, 'run', 'Running', T.stop], [T.stop, 'bad', 'Stopped · the step moved', FLOW_END - .2],
].forEach(([t, kind, text, tout]) => {
  const col = kind === 'ok' ? C.green : kind === 'bad' ? C.amber : C.blue;
  const ic = kind === 'ok' ? '✓' : kind === 'bad' ? '⊘' : '';
  const e = el('div', '', `${kind === 'run' ? `<span class="spin" style="width:14px;height:14px;border-radius:50%;border:2.5px solid rgba(90,169,255,.25);border-top-color:${C.blue}"></span>` : `<span style="font-size:18px">${ic}</span>`}${text}`, stage, `background:${col}1f;border:1px solid ${col}55;color:${kind === 'run' ? '#cfe3ff' : col}`);
  e.id = 'status';
  statusEls.push({ t, tout, kind, e, spin: $('.spin', e) });
});

// ---------- overlay pieces ----------
const fx = {};
let FR = null;

function measureFlow() {
  const d3 = f3.contentDocument, d4 = f4.contentDocument;
  for (const d of [d3, d4]) d.documentElement.style.overflow = 'hidden';
  fx.nl = d3.getElementById('nl'); fx.nlCard = fx.nl.firstElementChild;
  fx.yes = d3.querySelector('.nl-yes'); fx.no = d3.querySelector('.nl-no');
  fx.email3 = d3.querySelector('input[name=email]'); fx.cont3 = d3.querySelector('.btn-x82hd');
  fx.email4 = d4.querySelector('input[name=email]'); fx.cont4 = d4.querySelector('.btn-q71');
  fx.nlCard.style.willChange = 'transform,opacity,filter';
  FR = {
    email3: rectOf(fx.email3), cont3: rectOf(fx.cont3), card: rectOf(fx.nlCard), yes: rectOf(fx.yes), no: rectOf(fx.no),
    email4: rectOf(fx.email4), cont4: rectOf(fx.cont4),
  };
  const em3 = [FR.email3.x + 80, FR.email3.cy + 3], no = P(FR.no, -6, 4), cont3 = [FR.cont3.x + 150, FR.cont3.cy + 4];
  const em4 = [FR.email4.x + 80, FR.email4.cy + 3], old = [FR.cont3.x + 150, FR.cont3.cy + 4];
  fx.cursor = [[0, [760, 520]], [T.email, em3], [T.noThanks - .45, no], [T.emailOk - .45, em3], [T.cont, cont3],
    [T.cont + 1.0, [760, 520]], [T.email2, em4], [T.cont2, old], [T.stop + .4, [760, 560]]];
  ripple(T.block, em3, C.red);
  ripple(T.noThanks, no, C.purple);
  ripple(T.emailOk, em3, '#fff');
  ripple(T.contOk - .05, cont3, '#fff');
  ripple(T.email2 + .3, em4, '#fff');
  fx.esc = chip('<span class="mono" style="font-size:15px">esc</span>', `background:rgba(14,18,27,.9);border:1px solid rgba(255,255,255,.25);color:#fff;padding:6px 12px;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.4)`);
  fx.never = chip('never clicks this', `background:${C.red};color:#2a0606;font-size:13.5px;padding:3px 9px`);
  fx.neverBox = box();
  fx.skip = chip(`<span class="mono">#promo</span> &nbsp;not on the page · optional, skipped`, `background:rgba(20,16,6,.9);border:1px solid ${C.amber}88;color:#ffe4a3;font-size:14px;padding:5px 11px;border-radius:8px`);
  fx.tPromo = timer(C.amber);
  fx.tPromo2 = timer(C.amber);
  fx.hudD = hud([
    { ic: '⊘', col: C.amber, html: `<b>A popup is in the way</b> <span class="sub">${esc(F.popup_label)}</span>`, t: T.hud },
    { ic: '1', col: C.ink3 || '#8b949e', html: 'Escape <span class="sub">· still there</span>', t: T.esc + .25 },
    { ic: '2', col: C.green, html: '“No thanks” <span class="sub">· gone</span>', t: T.gone },
    { ic: '✕', col: C.red, html: 'Never “Sign up”, never types, never accepts', t: T.gone + .3 },
  ]);
  fx.ghost = box(); fx.ghostLbl = chip(`<span class="mono">#continue</span> &nbsp;not on this page`, `background:rgba(20,10,14,.9);border:1px solid ${C.red}88;color:#ffc4c4;font-size:14px;padding:4px 10px;border-radius:8px`);
  fx.tCont = timer(C.amber);
  fx.look = box(); fx.lookTag = chip('looks similar · not clicked', `background:${C.amber};color:#1b1405;font-size:14px;padding:3px 10px`);
  fx.hudG = hud([
    { ic: '⊘', col: C.amber, html: '<b>Not repaired: the step moved</b>', t: T.guard },
    { ic: '', col: C.amber, html: `<span class="sub">${esc(F.F3.guard_short)}</span>`, t: T.guard + .25 },
    { ic: '✓', col: C.green, html: 'No lookalike clicked, no model asked', t: T.guard + .6 },
  ]);
}

function drawFlow(t) {
  if (!FR) return;
  const on = spring(t - T.in, .45);
  // the earlier script leaves, the flow script comes in
  for (const id of Object.keys(lineEls)) {
    const L = lineEls[id];
    const base = id === 'L0' ? +L.root.style.opacity || 0 : 1;
    L.root.style.opacity = (base * (1 - on)).toFixed(3);
  }
  Object.entries(flowEls).forEach(([id, L]) => {
    const p = spring(t - (T.in + .25 + L.i * .07), .45);
    L.root.style.top = fy(id) + 'px';
    const q = p * (1 - spring(t - FLOW_END, .4));
    L.root.style.opacity = clamp(q).toFixed(3);
    L.root.style.transform = `translateY(${((1 - p) * 8).toFixed(2)}px)`;
    L.root.style.visibility = (p * (1 - spring(t - FLOW_END, .4))) <= .002 ? 'hidden' : 'visible';
  });
  if (t < T.in) { mat(f3, 0); mat(f4, 0); hideFlow(); return; }
  if (t >= FLOW_END + 1.2) { mat(f3, 0); mat(f4, 0); hideFlow(); return; }

  // current line
  const HLF = [[T.in, 'M2', 0], [T.goto1 - .3, 'M2', 1], [T.promo, 'M3', 1], [T.email, 'M4', 1], [T.cont, 'M5', 1],
    [T.pass1, 'M5', 0], [T.run2, 'M2', 1], [T.goto2 + .3, 'M3', 1], [T.email2, 'M4', 1], [T.cont2, 'M5', 1]];
  hl.style.top = track(t, HLF.map(([tt, id]) => [tt, fy(id)]), .32) + 'px';
  hl.style.opacity = clamp(track(t, HLF.map(([tt, , o]) => [tt, o]), .3)).toFixed(3);
  const stopped = t >= T.stop;
  hl.style.background = stopped ? 'rgba(242,193,78,.13)' : 'rgba(90,169,255,.12)';
  hl.style.boxShadow = stopped ? 'inset 0 0 0 1px rgba(242,193,78,.45)' : 'inset 0 0 0 1px rgba(90,169,255,.28)';
  const FMARKS = {
    M0: [], M1: [],
    M2: [[T.goto1, '✓', C.green, T.split], [T.goto2, '✓', C.green, FLOW_END]],
    M3: [[T.skip1, '↷', C.amber, T.split], [T.skip2, '↷', C.amber, FLOW_END]],
    M4: [[T.emailOk, '✓', C.green, T.split], [T.email2 + .3, '✓', C.green, FLOW_END]],
    M5: [[T.contOk, '✓', C.green, T.split], [T.stop, '⊘', C.amber, FLOW_END]],
  };
  for (const [id, L] of Object.entries(flowEls)) {
    let m = null;
    for (const mk of FMARKS[id]) if (t >= mk[0] && t < mk[3]) m = mk;
    if (!m) { L.st.style.opacity = 0; continue; }
    L.st.textContent = m[1]; L.st.style.color = m[2];
    L.st.style.opacity = clamp(pres(t, m[0], m[3] - .15, .36, .25)).toFixed(3);
    L.st.style.transform = `scale(${lerp(.4, 1, spring(t - m[0], .36, .55)).toFixed(3)})`;
  }

  // pages: the shop with the popup, then the split checkout
  const reload1 = Math.sin(Math.PI * lin(t, T.load, T.load + .45)) * .3;
  const p3 = spring(t - T.load, .5) * (1 - spring(t - T.url2, .5));
  const p4 = spring(t - (T.url2 + .15), .5) * (1 - spring(t - FLOW_END, .5));
  mat(f2, (1 - spring(t - T.load, .5)), { dy: 0, s0: 1.015, blur: 10 });
  mat(f3, p3 * (1 - reload1), { dy: 0, s0: 1.015, blur: 10 });
  mat(f4, p4, { dy: 0, s0: 1.015, blur: 10 });
  if (t >= T.load && t < T.load + 1) { prog.style.width = (easeIO(lin(t, T.load, T.load + .5)) * 100) + '%'; prog.style.opacity = 1 - lin(t, T.load + .55, T.load + .9); }
  if (t >= T.url2 && t < T.url2 + 1) { prog.style.width = (easeIO(lin(t, T.url2, T.url2 + .5)) * 100) + '%'; prog.style.opacity = 1 - lin(t, T.url2 + .55, T.url2 + .9); }
  urlEl.innerHTML = t >= T.url2 ? URL2 : URL1;
  // popup in, then declined along the way it came
  const np = pres(t, T.pop, T.gone, .55, .45);
  if (np <= .002) fx.nl.style.visibility = 'hidden';
  else {
    fx.nl.style.visibility = 'visible'; fx.nl.style.opacity = clamp(np).toFixed(3);
    fx.nlCard.style.transform = `translateY(${((1 - np) * 46).toFixed(2)}px) scale(${lerp(.94, 1, np).toFixed(4)})`;
    fx.nlCard.style.filter = np < .995 ? `blur(${((1 - clamp(np)) * 10).toFixed(2)}px)` : 'none';
  }
  const pr = Math.sin(Math.PI * lin(t, T.noThanks, T.noThanks + .2));
  fx.no.style.transform = `scale(${(1 - .05 * pr).toFixed(4)})`;
  fx.no.style.background = t > T.noThanks - .5 && t < T.gone + .2 ? `rgba(185,140,255,${(.18 * pres(t, T.noThanks - .5, T.gone, .3, .2)).toFixed(3)})` : 'none';
  fx.no.style.borderRadius = '8px';
  fx.email3.value = t >= T.emailOk + .1 && t < T.split ? 'ada@example.com'.slice(0, Math.round(15 * lin(t, T.emailOk + .1, T.emailOk + .5))) : '';
  fx.cont3.style.transform = `scale(${(1 - .03 * Math.sin(Math.PI * lin(t, T.contOk - .05, T.contOk + .2))).toFixed(4)})`;
  fx.email4.value = t >= T.email2 + .4 ? 'ada@example.com'.slice(0, Math.round(15 * lin(t, T.email2 + .4, T.email2 + .8))) : '';

  // cursor (takes over from compose.js in this act)
  const [cx, cy] = trackN(t, fx.cursor, .55);
  const cvis = pres(t, T.email - .3, T.cont + 1.2, .3, .3) + pres(t, T.email2 - .3, T.stop + .8, .3, .3);
  cursor.style.left = (cx - 5) + 'px'; cursor.style.top = (cy - 3) + 'px';
  cursor.style.opacity = clamp(cvis).toFixed(3);
  const hp = pres(t, T.noThanks - .55, T.gone + .1, .35, .3) * clamp(cvis);
  halo.style.left = (cx - 32) + 'px'; halo.style.top = (cy - 32) + 'px'; halo.style.opacity = hp.toFixed(3);

  // F2: optional step skipped
  const fPromo = lin(t, T.promo + .1, T.skip1 - .1);
  drawTimer(fx.tPromo, 560, 30, pres(t, T.promo, T.skip1, .35, .25), fPromo, `waiting for #promo · ${Math.round(10 * fPromo)} s of 10`);
  const fPromo2 = lin(t, T.goto2 + .35, T.skip2 - .1);
  drawTimer(fx.tPromo2, 560, 30, pres(t, T.goto2 + .3, T.skip2, .35, .25), fPromo2, `waiting for #promo · ${Math.round(10 * fPromo2)} s of 10`);
  place(fx.skip, 560, 30, pres(t, T.skip1, T.email, .4, .3), { dy: 8, blur: 5 });

  // F1: the popup is declined
  place(fx.esc, FR.card.x + FR.card.w / 2 - 28, FR.card.y - 52, pres(t, T.esc - .1, T.esc + .55, .3, .25), { dy: 8, s0: .8, blur: 5 });
  setBox(fx.neverBox, grow(FR.yes, 5), { col: C.red, dashed: true, fill: 'rgba(255,107,107,.06)', p: pres(t, T.hud + .3, T.gone, .4, .3) });
  place(fx.never, FR.yes.x + FR.yes.w - 128, FR.yes.y - 30, pres(t, T.hud + .35, T.gone, .4, .3), { dy: 6, blur: 4 });
  drawHud(fx.hudD, t, 600, 14, T.hud, T.cont);

  // F3: the page guard
  const g = grow(FR.cont3, 6);
  setBox(fx.ghost, g, { col: C.red, dashed: true, fill: 'rgba(255,107,107,.07)', p: pres(t, T.ghost, FLOW_END - .3, .4, .3) });
  place(fx.ghostLbl, g.x, g.y - 34, pres(t, T.ghost + .1, FLOW_END - .3, .4, .3), { dy: 8, blur: 5 });
  const fC = lin(t, T.wait2[0] + .1, T.wait2[1] - .1);
  drawTimer(fx.tCont, g.x + g.w + 20, g.y + 2, pres(t, T.wait2[0], T.wait2[1] + .1, .35, .25), fC, `waiting for #continue · ${Math.round(10 * fC)} s of 10`);
  setBox(fx.look, grow(FR.cont4, 6), { col: C.amber, dashed: true, fill: 'rgba(242,193,78,.07)', p: pres(t, T.look, FLOW_END - .3, .4, .3) });
  place(fx.lookTag, FR.cont4.x + FR.cont4.w - 190, FR.cont4.y + FR.cont4.h + 14, pres(t, T.look + .1, FLOW_END - .3, .4, .3), { dy: 6, blur: 4 });
  drawHud(fx.hudG, t, 600, 14, T.guard, FLOW_END - .3);
}

function hideFlow() {
  for (const e of [fx.esc, fx.never, fx.skip, fx.ghostLbl, fx.lookTag, fx.hudD && fx.hudD.h, fx.hudG && fx.hudG.h,
    fx.tPromo && fx.tPromo.e, fx.tPromo2 && fx.tPromo2.e, fx.tCont && fx.tCont.e]) if (e) e.style.visibility = 'hidden';
  for (const b of [fx.neverBox, fx.ghost, fx.look]) if (b) b.style.visibility = 'hidden';
  if (fx.nl) fx.nl.style.visibility = 'hidden';
}
window.measureFlow = measureFlow;
window.drawFlow = drawFlow;
