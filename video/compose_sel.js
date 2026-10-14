// The Selenium beat: driver.find_element(...).click() with the cookie banner up.
// Plain Selenium dies with ElementClickInterceptedException; with
// mendwright.open(driver, cookies="reject").driver the banner is rejected first.
// Every text shown comes from D.SEL (real Selenium runs).
'use strict';
const SEL = D.SEL;

const SLINES = {
  N0: O_('driver') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('driver') + P_(', ') + O_('cookies') + P_('=') + Str('"reject"') + P_(').') + O_('driver'),
  N1: O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"email"') + P_(').') + F_('click') + P_('()'),
  N2: O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"accept-terms"') + P_(').') + F_('click') + P_('()'),
  N3: O_('driver') + P_('.') + F_('find_element') + P_('(') + O_('By') + P_('.') + O_('ID') + P_(', ') + Str('"continue"') + P_(').') + F_('click') + P_('()'),
};
const selEls = {};
Object.keys(SLINES).forEach((id, i) => {
  const ln = el('div', 'ln mono', `<span class="g">${i + 1}</span><span class="c"><span class="cw">${SLINES[id]}</span></span><span class="st"></span>`, code, 'font-size:16.5px');
  selEls[id] = { root: ln, st: $('.st', ln), i };
});
const sy = id => 14 + selEls[id].i * LH;

const f5 = el('iframe', '', null, content); f5.srcdoc = D.pages.v1;
const runSel = el('div', '', `<span class="d" style="background:${C.blue};box-shadow:0 0 12px ${C.blue}"></span>Selenium <span style="color:var(--ink3);font-weight:500">+</span> <span style="color:#d9c4ff">mendwright</span>`, stage); runSel.id = 'run';

const U = {
  in: S.s7, page: S.s7 + .5, plain: S.s7 + 1.0, run: S.s7 + 2.0, email: S.s7 + 2.0, chip: S.s7 + 2.25,
  reject: S.s7 + 2.85, gone: S.s7 + 2.95, emailOk: S.s7 + 3.45, terms: S.s7 + 3.7, termsOk: S.s7 + 4.1,
  cont: S.s7 + 4.3, contOk: S.s7 + 4.7, pass: S.s7 + 4.95,
};

const ssec = s => `${(+s).toFixed(2)} s`;
const sm = SEL.mendwright;
const grpSel = el('div', 'grp mono', null, tbody);
groupEls.push({
  t0: U.in + .1, t1: S.end, g: grpSel, ents: [
    [U.plain - .1, 'cmd', '# plain Selenium, same page'],
    [U.plain, 'err', `<b>${esc(SEL.plain_type)}</b>: ${esc(SEL.plain_msg)}`],
    [U.gone, 'log', `<span class="i">▸</span><span class="tag-log">log</span>${esc(SEL.log)}`],
    [U.emailOk, 'ok', okRow('find_element(By.ID, "email").click()', ssec(sm[0].seconds))],
    [U.termsOk, 'ok', okRow('find_element(By.ID, "accept-terms").click()', ssec(sm[1].seconds))],
    [U.contOk, 'ok', okRow('find_element(By.ID, "continue").click()', ssec(sm[2].seconds))],
    [U.pass, 'sum', `<span style="color:${C.green}">Passed</span> · banner rejected`],
  ].map(([t, cls, html]) => ({ t, e: el('div', 'ent ' + cls, html, grpSel) })),
});
capEls.push({ a: U.in + .3, b: S.end - .2, e: el('div', 'cap', 'Same on Selenium: <s>find_element(...).click() died on the banner.</s> <u>Now it’s rejected first.</u>', document.body) });
[[U.run, 'run', 'Running', U.pass], [U.pass, 'ok', 'Passed', S.end - .2]].forEach(([t, kind, text, tout]) => {
  const col = kind === 'ok' ? C.green : C.blue;
  const e = el('div', '', `${kind === 'run' ? `<span class="spin" style="width:14px;height:14px;border-radius:50%;border:2.5px solid rgba(90,169,255,.25);border-top-color:${C.blue}"></span>` : '<span style="font-size:18px">✓</span>'}${text}`, stage, `background:${col}1f;border:1px solid ${col}55;color:${kind === 'run' ? '#cfe3ff' : col}`);
  e.id = 'status';
  statusEls.push({ t, tout, kind, e, spin: $('.spin', e) });
});

const sx = {};
let SR = null;
function measureSel() {
  const d5 = f5.contentDocument;
  d5.documentElement.style.overflow = 'hidden';
  sx.banner = d5.getElementById('cc-wrap'); sx.card = sx.banner.firstElementChild; sx.reject = sx.card.querySelector('button');
  sx.email = d5.getElementById('email'); sx.chk = d5.getElementById('accept-terms'); sx.cont = d5.getElementById('continue');
  SR = { email: rectOf(sx.email), chk: rectOf(sx.chk), cont: rectOf(sx.cont), reject: rectOf(sx.reject), card: rectOf(sx.card) };
  const em = [SR.email.x + 74, SR.email.cy + 3], rej = P(SR.reject, -10, 4), chk = P(SR.chk, 2, 3), cont = P(SR.cont, -30, 4);
  sx.cursor = [[0, [760, 560]], [U.email, em], [U.reject - .45, rej], [U.emailOk - .45, em], [U.terms, chk], [U.cont, cont], [U.pass + .6, [760, 540]]];
  ripple(U.email + .55, em, C.red);
  ripple(U.reject, rej, C.purple);
  ripple(U.emailOk, em, '#fff');
  ripple(U.termsOk, chk, '#fff');
  ripple(U.contOk, cont, '#fff');
  sx.chip = chip(`▸ cookie banner in the way · clicking “Reject all”`, `background:${C.amber};color:#1b1405;font-size:15.5px;box-shadow:0 10px 30px rgba(0,0,0,.35)`);
  sx.block = chip(`<span style="color:${C.red}">⊘</span>&nbsp; plain Selenium: element click intercepted`, `background:rgba(14,18,27,.9);border:1px solid ${C.red}66;color:#ffd3d3;font-size:15px;padding:8px 13px;border-radius:11px`);
}

function drawSel(t) {
  if (!SR) return;
  const on = spring(t - U.in, .45);
  Object.entries(selEls).forEach(([id, L]) => {
    const p = spring(t - (U.in + .25 + L.i * .07), .45);
    L.root.style.top = sy(id) + 'px';
    L.root.style.opacity = clamp(p).toFixed(3);
    L.root.style.transform = `translateY(${((1 - p) * 8).toFixed(2)}px)`;
    L.root.style.visibility = p <= .002 ? 'hidden' : 'visible';
  });
  mat(runSel, pres(t, U.in + .1, S.end), { dy: -10, blur: 6 });
  if (t < U.in) { mat(f5, 0); sx.chip.style.visibility = 'hidden'; sx.block.style.visibility = 'hidden'; return; }
  // the mendwright chip of the Playwright acts gives way to the Selenium one
  runMw.style.opacity = (+runMw.style.opacity * (1 - on)).toFixed(3);
  $('.url', br).innerHTML = $('.url', br).innerHTML.replace('checkout/shipping', 'checkout');

  const HLS = [[U.in, 'N1', 0], [U.run, 'N1', 1], [U.terms, 'N2', 1], [U.cont, 'N3', 1], [U.pass, 'N3', 0]];
  hl.style.top = track(t, HLS.map(([tt, id]) => [tt, sy(id)]), .32) + 'px';
  hl.style.opacity = clamp(track(t, HLS.map(([tt, , o]) => [tt, o]), .3)).toFixed(3);
  hl.style.background = 'rgba(90,169,255,.12)';
  hl.style.boxShadow = 'inset 0 0 0 1px rgba(90,169,255,.28)';
  const SM = { N0: [], N1: [[U.emailOk, '✓']], N2: [[U.termsOk, '✓']], N3: [[U.contOk, '✓']] };
  for (const [id, L] of Object.entries(selEls)) {
    const m = SM[id].find(mk => t >= mk[0]);
    if (!m) { L.st.style.opacity = 0; continue; }
    L.st.textContent = m[1]; L.st.style.color = C.green;
    L.st.style.opacity = clamp(pres(t, m[0], S.end - .15, .36, .25)).toFixed(3);
    L.st.style.transform = `scale(${lerp(.4, 1, spring(t - m[0], .36, .55)).toFixed(3)})`;
  }

  mat(f5, spring(t - U.page, .5), { dy: 0, s0: 1.015, blur: 10 });
  const bp = 1 - clamp(spring(t - U.gone, .5));
  if (bp <= .002) sx.banner.style.visibility = 'hidden';
  else {
    sx.banner.style.visibility = 'visible'; sx.banner.style.opacity = bp.toFixed(3);
    sx.card.style.transform = `translateY(${((1 - bp) * 46).toFixed(2)}px) scale(${lerp(.94, 1, bp).toFixed(4)})`;
    sx.card.style.filter = bp < .995 ? `blur(${((1 - bp) * 10).toFixed(2)}px)` : 'none';
  }
  sx.reject.style.transform = `scale(${(1 - .05 * Math.sin(Math.PI * lin(t, U.reject, U.reject + .2))).toFixed(4)})`;
  sx.chk.checked = t >= U.termsOk;
  sx.cont.style.transform = `scale(${(1 - .04 * Math.sin(Math.PI * lin(t, U.contOk, U.contOk + .25))).toFixed(4)})`;
  sx.email.style.boxShadow = t >= U.emailOk && t < U.terms ? '0 0 0 3.5px rgba(90,169,255,.45)' : 'none';

  const [cx, cy] = trackN(t, sx.cursor, .55);
  const cvis = pres(t, U.email - .3, U.pass + .8, .3, .3);
  cursor.style.left = (cx - 5) + 'px'; cursor.style.top = (cy - 3) + 'px';
  cursor.style.opacity = clamp(cvis).toFixed(3);
  const hp = pres(t, U.reject - .55, U.gone + .1, .35, .3) * clamp(cvis);
  halo.style.left = (cx - 32) + 'px'; halo.style.top = (cy - 32) + 'px'; halo.style.opacity = hp.toFixed(3);
  place(sx.block, SR.email.x + 150, SR.email.y - 64, pres(t, U.plain, U.chip - .1, .4, .3), { dy: 10, blur: 6 });
  place(sx.chip, SR.card.x + 8, SR.card.y - 50, pres(t, U.chip, U.gone + .2, .4, .3), { dy: 10, blur: 6 });
}
window.measureSel = measureSel;
window.drawSel = drawSel;
