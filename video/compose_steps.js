// Act 8: steps in any order. The checkout asks for the card first now; heal.run_steps()
// runs whichever registered step is on the page. Then a step the script doesn't know
// stops the run. Texts and outcomes come from D.ST (capture_steps.json, real runs).
'use strict';
window.stepsAct = function stepsAct() {
  const ST = D.ST, sw = ST.switched, ex = ST.extra, t0 = S.s8, b = t0 + CARD_LEN;
  const pick = (log, re) => (log.find(l => re.test(l)) || '').replace(/^(INFO|WARNING|ERROR) /, '');
  const D_ = s => `<span class="k-fn">${s}</span>`;
  const dec = (name, when, extra) => D_('@') + O_('heal') + P_('.') + F_('step') + P_('(') + Str(`"${name}"`) + P_(', ') + O_('when') + P_('=') + Str(when) + (extra || '') + P_(')');
  const def = name => '<span class="k-mw">def</span> ' + F_(name) + P_('(') + O_('page') + P_('):');
  const ind = '    ';
  const lines = [
    O_('heal') + ' ' + P_('=') + ' <span class="k-mw">mendwright</span>' + P_('.') + F_('open') + P_('(') + O_('page') + P_(', ') + O_('dismiss') + P_('=') + D_('True') + P_(')'),
    dec('shipping', '"#ship-name"'),
    def('shipping'),
    ind + O_('page') + P_('.') + F_('fill') + P_('(') + Str('"#ship-name"') + P_(', ') + Str('"Ada Lovelace"') + P_(')'),
    ind + O_('page') + P_('.') + F_('click') + P_('(') + Str('"#next"') + P_(')'),
    dec('payment', '"#card-number"'),
    def('payment'),
    ind + O_('page') + P_('.') + F_('fill') + P_('(') + Str('"#card-number"') + P_(', ') + Str('"4242 4242 4242 4242"') + P_(')'),
    ind + O_('page') + P_('.') + F_('click') + P_('(') + Str('"#next"') + P_(')'),
    dec('review', `'text="Place order"'`, P_(', ') + O_('last') + P_('=') + D_('True')),
    def('review'),
    ind + O_('page') + P_('.') + F_('click') + P_('(') + Str(`'text="Place order"'`) + P_(')'),
    O_('page') + P_('.') + F_('goto') + P_('(') + Str('"https://northwind.example/checkout"') + P_(')'),
    O_('heal') + P_('.') + F_('run_steps') + P_('()'),
  ];
  const P1 = ST.pages.switched, P2 = ST.pages.extra;
  // when each page shows (story seconds)
  const T = { pay: b + 1.0, ship: b + 5.3, rev: b + 7.7, done: b + 9.1, again: b + 10.4, prot: b + 12.0, stop: b + 13.6 };
  const URL = t => 'northwind.example' + (t >= T.prot ? '/checkout/protection' : t >= T.again ? '/checkout'
    : t >= T.done ? '/checkout/done' : t >= T.rev ? '/checkout/review' : t >= T.ship ? '/checkout/shipping' : '/checkout');
  const ran = (name) => pick(sw.log, new RegExp(`ran step '${name}'`));
  const changed = pick(sw.log, /order/i);
  scene({
    t0, t1: S.s9, title: { kicker: 'New in 0.7.0', text: 'Steps in any order', sub: 'heal.step()  +  heal.run_steps()', col: C.green },
    font: 15, lh: 29, layout: { edH: 520, termTop: 694, termH: 238 },
    lines,
    frames: [
      { id: 'pay', html: P1['/checkout'], in: T.pay, out: T.ship },
      { id: 'ship', html: P1['/checkout/shipping'], in: T.ship, out: T.rev },
      { id: 'rev', html: P1['/checkout/review'], in: T.rev, out: T.done },
      { id: 'done', html: P1['/checkout/done'], in: T.done, out: T.again },
      { id: 'pay2', html: P2['/checkout'], in: T.again, out: T.prot },
      { id: 'prot', html: P2['/checkout/protection'], in: T.prot },
    ],
    urlAt: URL,
    reloadsAll: [T.ship, T.rev, T.done, T.again, T.prot],
    hl: [[b, 12, 0], [b + 2.2, 12, 1], [b + 2.8, 13, 1], [b + 3.2, 5, 1], [b + 3.6, 7, 1], [b + 4.7, 8, 1],
      [b + 5.6, 1, 1], [b + 6.0, 3, 1], [b + 7.1, 4, 1], [b + 8.0, 9, 1], [b + 8.4, 11, 1], [b + 9.4, 13, 1],
      [T.again + .3, 13, 1], [T.again + .6, 5, 1], [T.prot, 13, 1]],
    hlBad: [T.stop, S.s9],
    marks: [[b + 2.5, 12, '✓', C.green], [b + 5.1, 5, '✓', C.green, T.again], [b + 7.5, 1, '✓', C.green, T.again],
      [b + 8.9, 9, '✓', C.green, T.again], [b + 9.4, 13, '✓', C.green, T.again], [T.stop, 13, '⊘', C.amber]],
    cursorOn: [b + 3.4, b + 9.2],
    cursor: [[b + 3.6, 'pay:#card-number'], [b + 4.7, 'pay:#next', 0, 4], [b + 6.0, 'ship:#ship-name'], [b + 7.1, 'ship:#next', 0, 4],
      [b + 8.4, 'rev:.go', 0, 4]],
    clicks: [[b + 3.8, 'pay:#card-number', '#fff'], [b + 5.0, 'pay:#next', '#fff'], [b + 6.2, 'ship:#ship-name', '#fff'],
      [b + 7.4, 'ship:#next', '#fff'], [b + 8.8, 'rev:.go', '#fff']],
    press: [[b + 5.0, 'pay:#next'], [b + 7.4, 'ship:#next'], [b + 8.8, 'rev:.go']],
    fills: [[b + 3.9, b + 4.6, 'pay:#card-number', '4242 4242 4242 4242'], [b + 6.3, b + 6.9, 'ship:#ship-name', 'Ada Lovelace'],
      [T.again + .7, T.again + 1.2, 'pay2:#card-number', '4242 4242 4242 4242']],
    boxes: [
      { at: 'pay:#card-number', col: C.green, t: [b + 3.3, b + 5.0], pad: 5 },
      { at: 'ship:#ship-name', col: C.green, t: [b + 5.7, b + 7.4], pad: 5 },
      { at: 'prot:.card', col: C.amber, dashed: true, t: [T.stop, S.s9 - .2], pad: 6 },
    ],
    chips: [
      { at: [560, 118], html: 'your script: <b>shipping → payment → review</b>', place: 'right', style: 'background:rgba(14,18,27,.88);color:#cfd6df;font-size:14px;border:1px solid rgba(255,255,255,.12)', t: [b + .6, T.again] },
      { at: [560, 158], html: 'the site now: <b>payment → shipping → review</b>', place: 'right', style: `background:${C.amber};color:#1b1405;font-size:14px`, t: [b + 1.8, T.again] },
      { at: 'prot:.card', html: 'not a step the script knows · stopped', place: 'below', style: `background:${C.amber};color:#1b1405;font-size:14px`, t: [T.stop + .1, S.s9 - .2] },
    ],
    timers: [{ at: 'prot:h2', t: [T.prot + .3, T.stop], col: C.amber, text: f => `looking for a known step · ${Math.round(6 * f)} s of 6` }],
    runGroups: [[b, T.again - .1, [
      [b + 2.2, 'cmd', '$ python checkout.py'],
      [b + 5.1, 'flow', `<span class="i">⇄</span><span class="tag-log">log</span>${esc(changed || ran('payment'))}`],
      [b + 7.5, 'ok', `<span class="i">✓</span>${esc(ran('shipping') || "ran 'shipping'")}`],
      [b + 8.9, 'ok', `<span class="i">✓</span>${esc(ran('review') || "ran 'review'")}`],
      [b + 9.4, 'sum', `<span style="color:${C.green}">Passed</span> · ran ${esc((sw.ran || []).join(' → '))}`],
    ]], [T.again, S.s9, [
      [T.again + .2, 'cmd', '$ python checkout.py   # the site added a step'],
      [T.stop, 'err', `<b>${esc(ex.error_type || 'AttentionRequired')}</b>: ${esc((ex.error || '').split('\n')[0])}`],
    ]]],
    caps: [
      [b + .2, b + 2.6, 'Your script registers each step. The site decides the order.'],
      [b + 2.7, T.again - .1, 'The site now asks for the card first. <em>run_steps()</em> runs whichever step is on the page.'],
      [T.again, S.s9 - .3, 'A step it doesn’t know <u>stops the run and names the page</u>. It never guesses.'],
    ],
    status: [[b + 2.2, 'run', 'Running', b + 9.4], [b + 9.4, 'ok', 'Passed · steps in a new order', T.again],
      [T.again + .2, 'run', 'Running', T.stop], [T.stop, 'bad', 'Stopped · an unknown step', S.s9 - .2]],
  });
};
