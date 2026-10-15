// Steps in any order: the checkout asks for the card first now; heal.run_steps() runs
// whichever registered step is on the page. Then a step the script doesn't know stops the
// run. Pages, order, outcomes and times come from D.ST (capture_steps.json, real runs).
'use strict';
const ST = D.ST;
// The decorators as the script registers them (capture_steps.json 'steps'): the name, when=
// folded to …, and any other argument marked with … (nothing is dropped silently).
const stepRows = () => ST.steps.map(([name, when, kw]) => ({ id: name, html: KW('@') + O_('heal') + P_('.') + F_('step') + P_('(') + Str(`"${name}"`) + P_(', ') + O_('when') + P_('=…') + (Object.keys(kw).length ? P_(', …') : '') + P_(')') + FOLD }));
const stepLog = (run, re) => { const i = run.log.findIndex(l => re.test(l)); if (i < 0) throw new Error('no log line ' + re); return { line: msgOf(run.log[i]), at: run.log_at[i] }; };
// clip D
function orderDiagram(t, warnRow) {
  const script = ST.today.ran, site = ST.switched.ran;     // the order the script registers, the order the site asks
  return { rows: [{ lb: 'your script:', chips: script, tone: 'grey', t: t }, warnRow || { lb: 'the site now:', chips: site, tone: 'amber', t: t + 1.2 }], lit: [] };
}
function stepFrames(prefix, flow, times) {
  // times: { '/checkout': [in, out], ... }
  const P = ST.pages[flow];
  for (const [path, tt] of Object.entries(times)) {
    frame({ id: prefix + path.split('/').pop(), html: P[path], t: tt });
    url(tt[0], 'northwind.example' + path);
  }
}

// montage beat M3 (brief): the order diagram, the pages, one stamp
window.montageSteps = function montageSteps(t0) {
  const t1 = t0 + 4.6, sw = ST.switched;
  montage(t0, t1);
  if (!sw.ok || sw.ran[0] !== 'payment') throw new Error('steps: switched run');
  const T = { pay: t0, ship: t0 + 1.6, rev: t0 + 2.4, done: t0 + 3.0 };
  stepFrames('m3', 'switched', { '/checkout': [T.pay, T.ship], '/checkout/shipping': [T.ship, T.rev], '/checkout/review': [T.rev, T.done], '/checkout/done': [T.done, t1] });
  // heal is defined on the row above the steps, as in clip D
  codeBlock({ t0, t1, rows: [{ ...openRows('heal', ST.open_kwargs)[0], n: 1 },
    { id: 'st', n: '', html: `<span class="k-fold">⋯ ${ST.steps.length} × @heal.step(…)</span>` },
    { id: 'rs', n: 2, html: O_('heal') + P_('.') + F_('run_steps') + P_('()') }] });
  const dg = orderDiagram(t0 + .1);
  dg.rows[1].t = t0 + .1;
  dg.lit = [[t0 + .8, 'payment'], [T.ship, 'shipping'], [T.rev, 'review']];
  dg.done = T.done;
  run({ t0, t1, diagram: dg });
  // deliberate departure from "one focal zone", as in clip D: the order diagram's lit chip
  // and the page are read together
  focus(t0, 'run+page');
  cap(t0 + .2, t1 - .2, 'Steps reordered? It follows the site.');
  beat(t0 + 1.2, 'M3 payment first');
  fill(t0 + .8, t0 + 1.1, 'm3checkout:#card-number', '4242 4242 4242 4242');
  curOn(t0 + .9, T.done); cur(t0 + .9, 'm3checkout:#next', 60, 4); cur(T.ship + .1, 'm3shipping:#next', 60, 4); cur(T.rev + .05, 'm3review:.go', 60, 4);
  click(t0 + 1.3, 'm3checkout:#next', C.white); press(t0 + 1.3, 'm3checkout:#next');
  fill(T.ship + .05, T.ship + .3, 'm3shipping:#ship-name', 'Ada Lovelace');
  click(T.ship + .5, 'm3shipping:#next', C.white); press(T.ship + .5, 'm3shipping:#next');
  click(T.rev + .4, 'm3review:.go', C.white); press(T.rev + .4, 'm3review:.go');
  pstamp({ t: [t0 + 3.4, t1], kind: 'ok', html: `✓ ran ${sw.ran.join(' → ')}`, frame: 'm3done' });
  beat(t0 + 4.0, 'M3 ran in the site’s order');
  return t1;
};

// clip D: "Checkout steps in any order"
function cutD() {
  const sw = ST.switched, ex = ST.extra;
  if (!sw.ok || ex.ok || ex.error_type !== 'AttentionRequired') throw new Error('steps: wrong outcomes');
  if (sw.ran.join() === ST.today.ran.join()) throw new Error('steps: the site did not change the order');
  clipTitle(1.6, 'Checkout steps in any order', 'heal.step() · heal.run_steps()');
  // the real script: open() with the arguments it ran with, the three steps, goto, run_steps()
  const open = openRows('heal', ST.open_kwargs);
  let n = 0;
  const blk = codeBlock({ t0: 0, t1: 25.5, rows: [
    ...open.map(r => Object.assign(r, { n: r.n === '' ? '' : ++n })),
    ...stepRows().map(r => Object.assign(r, { n: ++n })),
    { id: 'g', n: ++n, html: O_('heal') + P_('.') + O_('page') + P_('.') + F_('goto') + P_('(…)') },
    { id: 'rs', n: ++n, html: O_('heal') + P_('.') + F_('run_steps') + P_('()') }] });
  // ----- the site asks for the card first; run_steps() follows the page
  const T = { pay: 4.4, ship: 5.8, rev: 8.0, done: 9.3, stamp: 9.9, again: 12.6 };
  stepFrames('d1', 'switched', { '/checkout': [-1, T.ship], '/checkout/shipping': [T.ship, T.rev], '/checkout/review': [T.rev, T.done], '/checkout/done': [T.done, T.again] });
  // The order diagram (run panel) and the page are read together here: the lit chip follows
  // the page. This is a deliberate departure from "one focal zone": run+page from T.pay on.
  // Before that the diagram alone is focal, and the code is veiled.
  focus(1.6, 'run'); focus(T.pay, 'run+page');
  cap(1.8, 6.0, 'The site now asks for the card first.');
  beat(3.6, 'D the order diagram');
  const dg = orderDiagram(1.8);
  dg.lit = [[T.pay, 'payment'], [T.ship, 'shipping'], [T.rev, 'review']];
  dg.done = T.done;
  const lp = stepLog(sw, /ran step 'payment'/), ls = stepLog(sw, /ran step 'shipping'/), lr = stepLog(sw, /ran step 'review'/);
  const changed = stepLog(sw, /changed the order/);
  run({ t0: 0, t1: T.again, diagram: dg, keys: [[T.pay, 0], [T.ship, lp.at], [T.rev, ls.at], [T.done, lr.at], [T.stamp, sw.seconds]],
    stamp: { t: T.stamp, kind: 'ok', html: '✓ Passed · in the site’s new order' },
    receipts: [{ t: T.stamp, text: verbatim(changed.line, "ran 'payment' before 'shipping': the site changed the order of the steps") }] });
  blk.hl.push([0, 'rs', 0], [4.0, 'rs', 1], [T.pay, 'payment', 1], [T.ship, 'shipping', 1], [T.rev, 'review', 1], [T.done, 'rs', 1], [T.stamp, 'rs', 0]);
  blk.marks.push([T.stamp, 'rs', '✓', C.green, T.again]);
  curOn(4.45, 5.7);
  cur(4.4, 'd1checkout:#card-number', -50, 4); cur(5.0, 'd1checkout:#next', 60, 4);
  click(4.8, 'd1checkout:#card-number', C.white); fill(4.85, 5.15, 'd1checkout:#card-number', '4242 4242 4242 4242');
  click(5.4, 'd1checkout:#next', C.white); press(5.4, 'd1checkout:#next');
  beat(5.2, 'D payment first');
  cap(6.6, 10.8, '<em>run_steps()</em> runs whichever step is on the page.');
  curOn(6.85, 9.0);
  cur(6.8, 'd1shipping:#ship-name', -50, 4); cur(7.35, 'd1shipping:#next', 60, 4);
  click(7.2, 'd1shipping:#ship-name', C.white); fill(7.25, 7.5, 'd1shipping:#ship-name', 'Ada Lovelace');
  click(7.7, 'd1shipping:#next', C.white); press(7.7, 'd1shipping:#next');
  cur(8.3, 'd1review:.go', 60, 4);
  click(8.9, 'd1review:.go', C.white); press(8.9, 'd1review:.go');
  beat(7.4, 'D shipping second');
  focus(T.stamp, 'run');
  beat(11.6, 'D passed in the new order');
  // ----- a step the script doesn't know: it stops and names the page
  const T2 = { prot: 14.6, stamp: 16.8 };
  const prot = ex.final_url.replace(/^https?:\/\/[^/]+/, '');
  reload(T.again);
  stepFrames('d2', 'extra', { '/checkout': [T.again, T2.prot], [prot]: [T2.prot, 25.5] });
  focus(T.again, 'run+page');
  // how long run_steps() waited for a known step: the run's own timeout, as the error says
  if (!ex.error.includes(`within ${ex.timeout_ms} ms`)) throw new Error('extra: the wait is not the run timeout');
  const wait = ex.timeout_ms / 1000;
  const lp2 = stepLog(ex, /ran step '(\w+)'/), first = lp2.line.match(/ran step '(\w+)'/)[1];
  const keys = [[12.8, 0], [T2.prot, lp2.at], [15.0, lp2.at], [16.4, ex.seconds]];
  const unknown = prot.split('/').pop() + ' ?';
  const dg2 = { rows: [{ lb: 'your script:', chips: ST.today.ran, tone: 'grey', t: T.again },
    { lb: 'the site now:', chips: [first, unknown], tone: 'amber', t: T.again }],
    lit: [[12.8, first], [T2.prot, unknown]], warn: unknown };
  run({ t0: T.again, t1: 25.5, diagram: dg2, keys, clockCol: [[16.4, C.amber]],
    stamp: { t: T2.stamp, kind: 'stop', html: `⊘ Stopped · ${ex.error_type}` },
    receipts: [{ t: T2.stamp, text: verbatim(ex.error, `no step showed on ${prot} within ${ex.timeout_ms} ms`) }] });
  blk.hl.push([T.again, 'rs', 1], [T2.stamp, 'rs', 0]);
  blk.marks.push([T2.stamp, 'rs', '⊘', C.amber, 25.5]);
  cap(12.8, 17.4, 'Then the checkout changes again.');
  curOn(13.25, 14.5);
  cur(13.2, 'd2checkout:#card-number', -50, 4); cur(13.85, 'd2checkout:#next', 60, 4);
  click(13.6, 'd2checkout:#card-number', C.white); fill(13.65, 13.9, 'd2checkout:#card-number', '4242 4242 4242 4242');
  click(14.2, 'd2checkout:#next', C.white); press(14.2, 'd2checkout:#next');
  callout({ at: `d2${prot.split('/').pop()}:.card`, t: [15.0, T2.stamp + .2], zone: 'page', sets: [
    { t: 15.0, ic: 'ring', ring: [15.0, 16.4], h: 'Looking for a known step', s: `${wait} s timeout · ▶▶ sped up ×${speedOf(keys, 15.0, 16.4)}` }] });
  beat(15.7, 'D looking for a known step');
  box({ at: `d2${prot.split('/').pop()}:.card`, col: C.amber, dashed: true, t: [16.4, 25.5], pad: 8 });
  focus(T2.stamp, 'run');
  cap(18.0, 22.6, 'It <b>stops and names the page</b>. It never guesses.');
  beat(18.8, 'D stopped, names the page');
  endStrip(22.8);
}
window.CUTS.D = cutD;
