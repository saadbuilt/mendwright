// Clips A, B, C: cookie banners, popups, and a popup where declining isn't safe.
// Each is a cut of the same composition (window.CUT). Every log fragment, outcome, time and
// argument shown comes from D (the real runs in the capture JSON); see render.py.
// Times are story seconds: the clip's title holds LEAD s longer, so the cut is LEAD longer.
'use strict';
const SM = D.SM;
const quoteLabel = s => '“' + s + '”';
const sameScript = (a, b) => JSON.stringify([a.open_kwargs, a.click, a.click_kwargs]) === JSON.stringify([b.open_kwargs, b.click, b.click_kwargs]);

// ---------- clip A: cookie banners ----------
function cutA() {
  const B = D.B, Cr = D.Crun, SEL = D.SEL;
  if (!D.B_banner_up_after || D.C_banner_up_after || !D.C_ok) throw new Error('banner runs: wrong outcomes');
  if (!B.error.includes('intercepts pointer events')) throw new Error('B: the banner did not take the click');
  clipTitle(1.6, 'Cookie banners', 'cookies="reject"');
  const T = { fail: 8.8, code: 11.2, rerun: 14.4, sel: 20.6, end: 27.0 };
  frame({ id: 'a1', html: D.pages.v1, t: [-1, T.sel], popup: '#cc-wrap', pop: [[2.0, T.rerun + .1], [T.rerun + .35, 16.6]], reloads: [T.rerun] });
  reload(T.rerun);
  url(0, 'northwind.example/checkout');
  const sr = scriptRows();
  const blk = codeBlock({ t0: 0, t1: T.sel, rows: [
    ...openRows('page', { cookies: 'reject' }, 'page').map((r, i) => Object.assign(r, { at: [[0, i]], appear: 11.4 + i * .5, type: [11.4 + i * .5, 11.9 + i * .5], tint: [11.4, 13.6] })),
    ...sr.map((html, i) => ({ id: 'L' + (i + 1), html, n: [[0, i + 1], [11.3, i + 2]], at: [[0, i], [11.3, i + 2]] }))] });
  badge(0, 'plain Playwright', C.grey);
  badge(T.code, 'Playwright + mendwright', C.purple);
  badge(T.sel, 'Selenium + mendwright', C.purple);
  // the site adds a banner; plain Playwright's click lands on it, for 30 s
  focus(1.6, 'page');
  cap(1.8, 5.2, 'The site adds a cookie banner.');
  beat(3.4, 'A banner rises');
  const keys1 = [[5.8, 0], [8.4, B.seconds]];
  const s30 = Math.round(B.seconds);
  // the receipt: the line of the real traceback that names the exception
  const tline = `${B.error_type}: ${B.error.split('\n')[0]}`;
  run({ t0: 5.0, t1: T.code, cmd: [5.2, '$ python checkout.py'], keys: keys1, clockCol: [[8.4, C.red]],
    stamp: { t: T.fail, kind: 'bad', html: `✕ Failed after ${s30} s` },
    receipts: [{ t: T.fail, text: verbatim(B.error_line, tline) }] });
  blk.hl.push([0, 'L1', 0], [5.2, 'L1', 1], [T.code, 'L1', 0]);
  blk.marks.push([T.fail, 'L1', '✕', C.red, T.code]);
  curOn(5.25, 7.0); cur(5.2, 'a1:#email', -60, 4);
  [5.8, 6.2, 6.6].forEach(t => click(t, 'a1:#email', C.red));
  // the speed badge by the clock says it is sped up; the callout says only what happens
  speedOf(keys1, 5.8, 8.4);
  callout({ at: 'a1:#email', place: 'below-of', of: 'a1:#cc-wrap > div', t: [6.4, T.code], sets: [
    { t: 6.4, ic: '⊘', col: C.red, h: 'The banner takes the click', s: `Playwright retries for ${s30} s` }] });
  beat(7.2, 'A the banner takes the click');
  cap(7.6, 11.0, 'Plain Playwright <s>can’t click through it</s>.');
  focus(T.fail, 'run');
  beat(9.8, 'A failed after 30 s');
  // one line
  focus(T.code, 'code');
  cap(11.4, 14.4, 'Add one line: <em>cookies="reject"</em>.');
  beat(13.2, 'A one line');
  // the rerun: mendwright rejects the banner, then the script carries on
  focus(T.rerun, 'page');
  const c = Cr.steps, cs = [0];
  c.forEach(x => cs.push(cs[cs.length - 1] + x));
  const kC = [[14.6, 0], [16.4, Cr.log_at[0]], [17.0, cs[1]], [17.2, cs[2]], [17.4, cs[3]], [17.8, cs[4]], [18.0, Cr.seconds]];
  run({ t0: T.rerun, t1: T.sel, cmd: [14.5, '$ python checkout.py'], keys: kC,
    stamp: { t: 18.0, kind: 'ok', html: '✓ Passed · banner rejected' },
    receipts: [{ t: 18.0, text: verbatim(Cr.log[0], Cr.log[0]) }] });
  const reject = 'a1:#cc-wrap button';
  cap(14.6, 18.6, 'It <em>rejects the banner</em>, then carries on.');
  callout({ at: 'a1:#email', place: 'below-of', of: 'a1:#cc-wrap > div', t: [15.8, 16.7], sets: [
    { t: 15.8, ic: '▸', col: C.purple, h: 'Cookie banner in the way', s: `mendwright clicks ${quoteLabel('Reject all')}` }] });
  curOn(15.85, 18.0); cur(15.8, reject, -10, 4); haloOn(15.85, 16.7);
  click(16.4, reject, C.purple); press(16.4, reject);
  beat(16.2, 'A mendwright rejects the banner');
  cur(16.6, 'a1:#email', -60, 4); click(17.0, 'a1:#email', C.white); fill(17.05, 17.25, 'a1:#email', 'ada@example.com', T.sel);
  cur(17.05, 'a1:#accept-terms', 2, 3); click(17.4, 'a1:#accept-terms', C.white); check(17.4, 'a1:#accept-terms', T.sel);
  cur(17.45, 'a1:#continue', -40, 4); click(17.8, 'a1:#continue', C.white); press(17.8, 'a1:#continue');
  blk.hl.push([14.6, 'L1', 1], [17.1, 'L2', 1], [17.3, 'L3', 1], [17.6, 'L4', 1], [18.0, 'L4', 0]);
  blk.marks.push([17.0, 'L1', '✓', C.green, T.sel], [17.2, 'L2', '✓', C.green, T.sel], [17.4, 'L3', '✓', C.green, T.sel], [17.8, 'L4', '✓', C.green, T.sel]);
  focus(18.0, 'run');
  beat(19.4, 'A passed, banner rejected');
  // the same on Selenium
  if (SEL.banner_up_after || !SEL.terms_checked || SEL.mendwright.some(s => !s.ok)) throw new Error('Selenium: wrong outcome');
  frame({ id: 's1', html: D.pages.v1, t: [T.sel, T.end + 3], popup: '#cc-wrap', pop: [[T.sel - 1, 22.8]] });
  const sblk = codeBlock({ t0: T.sel, t1: T.end + 3, rows: [
    ...openRows('driver', { cookies: 'reject' }, 'driver'),
    { id: 'f1', n: 2, html: O_('driver') + P_('.') + F_('find_element') + P_('(') },
    { id: 'f2', n: '', html: IND(4) + O_('By') + P_('.') + O_('ID') + P_(', ') + Str(JSON.stringify(SEL.mendwright[0].value)) + P_(').') + F_('click') + P_('()') },
    { id: 'more', n: '', html: `<span class="k-fold">⋯ ${SEL.mendwright.length - 1} more steps</span>` }] });
  focus(T.sel, 'code');
  cap(20.8, 25.0, 'Same on Selenium: one line, same result.');
  const sm = SEL.mendwright.map(s => s.seconds), ss = [0];
  sm.forEach(x => ss.push(ss[ss.length - 1] + x));
  const kS = [[22.4, 0], [23.2, ss[1]], [23.6, ss[2]], [24.0, ss[3]], [24.4, SEL.mendwright_seconds]];
  // plain Selenium's failure is the previous run; the receipt under the stamp is this run's
  run({ t0: T.sel, t1: T.end + 3, cmd: [20.8, '$ python checkout.py'],
    prev: [22.0, `plain Selenium · ${verbatim(SEL.plain_line, SEL.plain_type)}`], keys: kS,
    stamp: { t: 24.4, kind: 'ok', html: '✓ Passed · banner rejected' },
    receipts: [{ t: 24.4, text: verbatim(SEL.log, SEL.log) }] });
  focus(22.2, 'page');
  curOn(22.25, 24.2); cur(22.2, 's1:#cc-wrap button', -10, 4); haloOn(22.25, 22.9);
  click(22.6, 's1:#cc-wrap button', C.purple); press(22.6, 's1:#cc-wrap button');
  cur(22.85, 's1:#email', -60, 4); click(23.2, 's1:#email', C.white);
  cur(23.25, 's1:#accept-terms', 2, 3); click(23.6, 's1:#accept-terms', C.white); check(23.6, 's1:#accept-terms');
  cur(23.65, 's1:#continue', -40, 4); click(24.0, 's1:#continue', C.white); press(24.0, 's1:#continue');
  sblk.hl.push([22.4, 'f1', 1], [23.4, 'more', 1], [24.4, 'more', 0]);
  sblk.marks.push([23.2, 'f1', '✓', C.green, T.end + 3], [24.0, 'more', '✓', C.green, T.end + 3]);
  beat(23.0, 'A Selenium: banner rejected');
  focus(24.4, 'run');
  beat(25.8, 'A Selenium passed');
  endStrip(T.end);
}

// ---------- clip B: popups you didn't plan for ----------
// Both popups run the same two lines, on a fresh store (nothing remembered):
//   heal = mendwright.open(page, dismiss=True)
//   heal.page.click("input[name=email]")
function cutB() {
  const F1 = D.F.F1, u = SM.upsell, em = F1.steps[0];
  if (F1.steps.length !== 1 || !em.ok || F1.popup_up_after || F1.sign_up !== 'never clicked') throw new Error('F1: wrong outcome');
  if (!F1.store_fresh) throw new Error('F1: the newsletter run did not start from a fresh store');
  if (!sameScript(F1, u)) throw new Error('clip B: the two popup runs ran different scripts');
  if (!u.ok || u.page.clicked.length !== 1) throw new Error('upsell: wrong outcome');
  clipTitle(1.6, 'Popups you didn’t plan for', 'dismiss=True');
  const T = { up: 14.3, end: 20.8 };
  frame({ id: 'nl', html: D.F.pages.v2_nl, t: [-1, T.up], popup: '#nl', pop: [[1.8, 9.8]] });
  url(0, 'northwind.example/checkout');
  // one script for both halves
  const blk = codeBlock({ t0: 0, t1: T.end + 3, rows: [...openRows('heal', F1.open_kwargs), ...clickRows(F1.click, F1.click_kwargs, 2)] });
  const field = 'input[name=email]';
  if (F1.click !== field) throw new Error('F1: the script clicks ' + F1.click);
  focus(1.6, 'page');
  cap(1.8, 5.8, 'A popup mendwright was never told about.');
  beat(3.0, 'B a popup nobody registered');
  const di = em.log.findIndex(l => /dismissed a popup/.test(l));
  const kF = [[3.0, 0], [9.6, em.log_at[di]], [10.8, F1.seconds]];
  run({ t0: 2.8, t1: T.up, cmd: [2.9, '$ python checkout.py'], keys: kF,
    stamp: { t: 11.8, kind: 'ok', html: `✓ Passed · ${quoteLabel('Sign up')} never clicked` },
    receipts: [{ t: 11.8, text: elide(msgOf(em.log[di]), 'dismissed a popup', 'with “No thanks”, after Escape did not close it') }] });
  blk.hl.push([0, 'c1', 0], [3.0, 'c1', 1], [11.8, 'c1', 0]);
  blk.marks.push([10.8, 'c1', '✓', C.green, T.up]);
  curOn(3.3, 4.2); cur(3.2, 'nl:' + field, -90, 4);
  click(3.8, 'nl:' + field, C.red);
  const card = 'nl:#nl > div';
  callout({ at: card, place: 'below-of', of: card, t: [4.6, 10.6], sets: [
    { t: 4.6, ic: '⊘', col: C.amber, h: 'A popup is in the way', s: '1 · Escape' },
    { t: 7.2, ic: '⊘', col: C.amber, h: 'A popup is in the way', s: '1 · Escape: still there' },
    { t: 9.6, ic: '⊘', col: C.amber, h: 'A popup is in the way', s: '2 · “No thanks”' }] });
  keycap({ at: card, text: 'esc', t: [4.6, 6.6] });
  beat(5.4, 'B Escape first');
  cap(6.0, 10.2, 'Escape first. Then “No thanks”. <s>Never “Sign up”.</s>');
  box({ at: 'nl:.nl-yes', col: C.red, dashed: true, t: [8.4, 9.8], pad: 6 });
  tag({ at: 'nl:.nl-yes', html: 'never', col: C.red, solid: true, t: [8.4, 9.8], clear: card, leader: true, pad: 6 });
  beat(8.9, 'B never Sign up');
  curOn(9.0, 11.3); cur(8.95, 'nl:.nl-no', -8, 4); haloOn(9.0, 9.9);
  click(9.6, 'nl:.nl-no', C.purple); press(9.6, 'nl:.nl-no');
  cur(10.3, 'nl:' + field, -90, 4); click(10.8, 'nl:' + field, C.white);
  focus(11.8, 'run');
  beat(13.0, 'B passed, never Sign up');
  // the upsell: a paid button is never clicked (the same script, run again)
  const ud = u.log.findIndex(l => /dismissed a popup/.test(l));
  frame({ id: 'u', html: SM.pages.upsell, t: [T.up, T.end + 3], popup: '#pop', pop: [[T.up + .2, 17.2]] });
  focus(T.up, 'page');
  cap(14.5, 18.8, 'A paid “Skip the line”? <s>Never.</s> <u>“No thanks”.</u>');
  beat(15.2, 'B upsell popup');
  const kU = [[14.6, 0], [17.0, u.log_at[ud]], [17.6, u.seconds]];
  run({ t0: T.up, t1: T.end + 3, cmd: [14.4, '$ python checkout.py'], keys: kU,
    stamp: { t: 18.2, kind: 'ok', html: `✓ Passed · clicked ${quoteLabel(u.page.clicked[0])}` } });
  const paid = 'u:#pop button:nth-of-type(1)', no = 'u:#pop button:nth-of-type(2)', ucard = 'u:#pop > div';
  box({ at: paid, col: C.red, dashed: true, t: [15.7, 17.2], pad: 6 });
  // under the popup card, off its right edge; the leader runs up outside the card to the
  // paid button, so it never points at "No thanks"
  tag({ at: paid, html: 'costs money · never', col: C.red, solid: true, t: [15.7, 17.2], place: 'below-of', of: ucard, elbow: 'right', leader: true, pad: 6 });
  beat(16.3, 'B costs money: never');
  curOn(16.45, 17.9); cur(16.4, no, -8, 4); haloOn(16.45, 17.3);
  click(17.0, no, C.purple); press(17.0, no);
  cur(17.25, 'u:' + field, -90, 4); click(17.6, 'u:' + field, C.white);
  blk.hl.push([T.up, 'c1', 0], [14.6, 'c1', 1], [18.2, 'c1', 0]);
  blk.marks.push([17.6, 'c1', '✓', C.green, T.end + 3]);
  focus(18.2, 'run');
  beat(19.4, 'B clicked No thanks');
  endStrip(T.end);
}

// ---------- clip C: when declining isn't safe ----------
function cutC() {
  const a = SM.order_alone, r = SM.order_registered;
  if (a.ok || a.page.clicked.length || a.page.cancelled || !a.page.popup_up) throw new Error('order_alone: mendwright clicked something');
  if (!r.ok || r.page.clicked[0] !== 'Continue with order' || r.page.cancelled) throw new Error('order_registered: wrong outcome');
  if (!sameScript(a, r)) throw new Error('clip C: the two runs ran different scripts besides the interrupt');
  clipTitle(1.6, 'When declining isn’t safe', 'heal.interrupt()');
  const T = { code: 10.2, rerun: 13.4, end: 18.8 };
  frame({ id: 'o', html: SM.pages.order, t: [-1, T.end + 3], popup: '#pop', pop: [[1.8, 15.0]], reloads: [T.rerun] });
  reload(T.rerun);
  url(0, 'northwind.example/checkout');
  const [ik, isel] = r.interrupts[0];
  const cr = clickRows(a.click, a.click_kwargs, [[0, 2], [10.3, 3]]);
  cr.forEach((x, i) => { x.at = [[0, 2 + i], [10.3, 4 + i]]; });
  const blk = codeBlock({ t0: 0, t1: T.end + 3, rows: [
    ...openRows('heal', a.open_kwargs),
    { id: 'i1', n: 2, html: O_('heal') + P_('.') + F_('interrupt') + P_('(') + Str(JSON.stringify(ik)) + P_(','), at: [[0, 2]], appear: 10.4, type: [10.4, 10.9], tint: [10.4, 12.8] },
    { id: 'i2', n: '', html: IND(4) + Str("'" + isel + "'") + P_(')'), at: [[0, 3]], appear: 10.9, type: [10.9, 11.6], tint: [10.4, 12.8] },
    ...cr] });
  // before and after, as in clip A: the header says which script runs
  badge(0, 'without interrupt', C.grey);
  badge(T.code, 'with interrupt', C.purple);
  // "Dismiss" would cancel the order: mendwright clicks nothing and stops
  focus(1.6, 'page');
  cap(1.8, 5.4, 'Here “Dismiss” would cancel the order.');
  const dismiss = 'o:#pop button:nth-of-type(2)', cont = 'o:#pop button:nth-of-type(1)', ocard = 'o:#pop > div';
  // on Dismiss for as long as the popup is up; the label sits under the popup card
  box({ at: dismiss, col: C.red, dashed: true, t: [3.0, 15.0], pad: 5 });
  tag({ at: dismiss, html: 'cancels the order · never', col: C.red, solid: true, t: [3.0, 15.0], place: 'below-of', of: ocard, pad: 5 });
  beat(3.8, 'C Dismiss cancels the order');
  curOn(4.0, 4.8); cur(3.9, 'o:input[name=email]', -90, 4);
  click(4.4, 'o:input[name=email]', C.red);
  const dl = a.log.findIndex(l => /could not dismiss/.test(l));
  const kA = [[4.4, 0], [5.0, a.log_at[dl]], [6.6, a.seconds]];
  run({ t0: 1.6, t1: T.code, cmd: [2.0, '$ python checkout.py'], keys: kA, clockCol: [[6.6, C.amber]],
    stamp: { t: 6.8, kind: 'stop', html: '⊘ Stopped · nothing clicked' },
    receipts: [{ t: 6.8, text: verbatim(a.error_line, `${a.error_type}: ${a.error}`) },
      { t: 8.0, text: elide(msgOf(a.log[dl]), 'Register the button that closes it with heal.interrupt(', ')').replace(' … )', '…)') }] });
  blk.hl.push([0, 'c1', 0], [4.4, 'c1', 1], [T.code, 'c1', 0]);
  blk.marks.push([6.8, 'c1', '⊘', C.amber, T.code]);
  cap(5.6, 9.2, 'So it <b>clicks nothing</b> and stops.');
  focus(6.8, 'run');
  beat(7.4, 'C stopped, nothing clicked');
  beat(9.0, 'C register it with interrupt');
  // you decide, with one line; heal.page (the page the script acts on) is your page
  focus(T.code, 'code');
  cap(10.4, 13.4, 'You decide, with <em>one line</em>.');
  tag({ at: 'code:c1', zone: 'code', place: 'below', html: '↑ is your page', col: C.purple, t: [11.8, 13.4], pad: 0 });
  beat(12.4, 'C one interrupt line');
  // the rerun: mendwright clicks the registered button
  focus(T.rerun, 'page');
  const hi = r.log.findIndex(l => /handled interruption/.test(l));
  const kR = [[13.8, 0], [14.8, r.log_at[hi]], [15.6, r.seconds]];
  run({ t0: T.rerun, t1: T.end + 3, cmd: [13.5, '$ python checkout.py'], keys: kR,
    stamp: { t: 16.2, kind: 'ok', html: `✓ Passed · clicked ${quoteLabel(r.page.clicked[0])}` },
    receipts: [{ t: 16.2, text: verbatim(msgOf(r.log[hi]), msgOf(r.log[hi]).replace(/^mendwright: /, '')) }] });
  cap(14.0, 18.6, 'The order goes on. “Dismiss” is never clicked.');
  // the purple cursor comes from above the popup straight down onto "Continue with order"
  curOn(13.9, 16.0); cur(13.6, ocard, 0, -60, 'top'); cur(14.2, cont, 0, 4); haloOn(13.9, 15.1);
  click(14.8, cont, C.purple); press(14.8, cont);
  cur(15.2, 'o:input[name=email]', -90, 4); click(15.6, 'o:input[name=email]', C.white);
  beat(14.6, 'C mendwright clicks Continue');
  blk.hl.push([13.6, 'c1', 1], [16.2, 'c1', 0]);
  blk.marks.push([15.6, 'c1', '✓', C.green, T.end + 3]);
  focus(16.2, 'run');
  beat(17.4, 'C passed, order continued');
  endStrip(T.end);
}

window.CUTS.A = cutA;
window.CUTS.B = cutB;
window.CUTS.C = cutC;
defineCut();
