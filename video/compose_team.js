// The team beats of the main cut: the repairs become a pull request, mendwright check before
// the nightly run, and the TypeScript package with the repairs the Python run saved. Every line
// on screen comes from D.T (capture_team.json: real runs of mendwright 0.10.1 and of the
// TypeScript package 0.1.0), checked in render.py team_data() and with verbatim() here.
'use strict';
const TM = D.T;
const MONO = '"Cascadia Code","Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace';
// a page compose.js builds: the shop pages' font, and the same Inter as the stage
const builtPage = (css, body) => `<!doctype html><html><head><meta charset="utf-8"><style>*{box-sizing:border-box}html,body{overflow:hidden}
body{margin:0;font-family:Inter,"Inter Variable","Segoe UI",system-ui,sans-serif}${css}</style>${D.fontHead || ''}</head><body>${body}</body></html>`;
const wholeLine = (text, line) => {
  if (!text.split(/\r?\n/).includes(line)) throw new Error('not a line of the output: ' + line);
  return line;
};
const plainOf = html => html.replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

// ---------- the pull request: the Action's title, and the diff mendwright fix wrote ----------
function prPage() {
  const h = TM.diff[0].match(/^@@ -(\d+),\d+ \+(\d+),\d+ @@$/);
  if (!h) throw new Error('PR: the diff has no hunk header');
  let o = +h[1], n = +h[2];
  const rows = [];
  for (const l of TM.diff.slice(1)) {
    const k = l[0], code = l.slice(1), on = k === '+' ? '' : o++, nn = k === '-' ? '' : n++;
    if (!code.trim()) continue;       // the hunk's blank first line: left out, its number counted
    rows.push(`<div class="dl ${k === '-' ? 'del' : k === '+' ? 'add' : 'ctx'}"><span class="no">${on}</span><span class="no">${nn}</span><span class="sg">${k === ' ' ? '' : k}</span><span class="cd">${esc(code)}</span></div>`);
  }
  const css = `body{background:#fff;color:#000}
.card{position:absolute;left:22px;top:22px;width:790px;background:#fff;border:1px solid #b5b5b5;border-radius:12px;padding:20px 22px 22px}
h1{font-size:25px;line-height:32px;margin:0;font-weight:700;letter-spacing:-.01em}
.ok{margin-top:10px;font-size:17px;line-height:24px;font-weight:600} .ok i{font-style:normal;color:${PGC.green}}
.diff{margin-top:18px;border:1px solid #b5b5b5;border-radius:10px;overflow:hidden}
.fn{border-bottom:1px solid #b5b5b5;padding:8px 14px;font:600 16px/22px ${MONO}}
.dl{display:flex;font:16.5px/28px ${MONO};white-space:pre;overflow:hidden}
.no{width:26px;flex:none;text-align:right;color:#6b6b6b;font-size:13px;padding-right:4px}
.sg{width:18px;flex:none;text-align:center;font-weight:700}
.del{color:${PGC.red}} .add{color:${PGC.green}} .ctx{color:#000}`;
  return builtPage(css, `<div class="card"><div class="head"><h1>${esc(TM.title)}</h1>
<div class="ok"><i>✓</i> Each one confirmed by a passing test</div></div>
<div class="diff"><div class="fn">${esc(TM.diffFile)}</div>${rows.join('')}</div></div>`);
}

window.pullRequestBeat = function pullRequestBeat(t0, blk) {
  const t1 = t0 + 5.6;
  if (TM.n !== D.D_heals.length) throw new Error('PR: the pull request does not write back every repair');
  frame({ id: 'pr', html: prPage(), t: [t0, t1], cam: { x: 0, y: 0 }, fit: '.card, .dl' });
  reload(t0); chrome(t0, 'Pull request'); chrome(t1, null);
  blk.t1 = t0;                      // the script's old selectors leave as the diff that replaces them comes in
  focus(t0, 'page');
  run({ t0, t1, cmd: [t0 + .1, '$ ' + TM.fixCmd] });
  // the card comes in whole, title and diff, as the page loads; then the caption names it
  reveal({ at: 'pr:.card', t: t0, what: TM.title + ', and the diff of ' + TM.diffFile, result: true });
  cap(t0 + 1.2, t0 + 5.4, 'Confirmed fixes come back as a <u>pull request</u>.');
  beat(t0 + 1.2, 'P1 the pull request');
  beat(t0 + 3.6, 'P2 three selectors written back');
  return t1;
};

// ---------- mendwright check, before the nightly run ----------
function checkPage() {
  const out = TM.checkOut, L = out.split(/\r?\n/);
  const RES = /^ {2}(would heal|needs model|ok \(heal\)|ok|wrong role|refused|not checked)( +)(\S+)( +)/;
  const row = l => {                 // the result, the key and the reason's first clause, verbatim
    const m = l.match(RES), rest = l.slice(m[0].length), cut = rest.search(/[.,](?=\s|$)/);
    if (cut < 0) throw new Error('check: a reason without a first clause: ' + l);
    verbatim(l, l.slice(0, m[0].length + cut + 1));
    return { kind: m[1], gap1: m[2], key: m[3], gap2: m[4], why: rest.slice(0, cut + 1) };
  };
  const rows = L.filter(l => RES.test(l)).map(row);
  const wh = rows.map((r, i) => r.kind === 'would heal' ? i : -1).filter(i => i >= 0);
  if (!wh.length || wh[wh.length - 1] - wh[0] !== wh.length - 1) throw new Error('check: the would-heal rows are not together');
  const line = r => `<div class="row">  <span class="${r.kind === 'would heal' ? 'wh-r' : 'nm-r'}">${esc(r.kind)}</span>${r.gap1}<span class="key"><i class="kh"></i>${esc(r.key)}</span>${r.gap2}${esc(r.why)} <span class="dim">…</span></div>`;
  const body = rows.map((r, i) => (i === wh[0] ? '<div class="wh"><i class="wo"></i>' : '') + line(r) + (i === wh[wh.length - 1] ? '</div>' : '')).join('');
  const head = wholeLine(out, L.find(l => l.startsWith('mendwright check: ')));
  const page = wholeLine(out, L.find(l => /^https?:\/\//.test(l)));
  const sum = wholeLine(out, L.find(l => / would heal from memory/.test(l))), exit = wholeLine(out, L.find(l => /^Exit \d/.test(l)));
  const css = `body{background:#000;color:#e6e6e6}
.card{position:absolute;left:0;top:0;width:1840px;padding:30px 40px;font:28px/42px ${MONO};white-space:pre}
.card div{overflow:hidden} .card .out,.card .wh{overflow:visible} .p{color:#8f8f8f} .c{color:#fff} .dim{color:#9a9a9a}
.wh{position:relative;display:inline-block;vertical-align:top} .wh div{overflow:visible}
.wo{position:absolute;left:-12px;right:-14px;top:-2px;bottom:-2px;border:3px solid #fff;border-radius:10px}
.key{position:relative} .kh{position:absolute;left:-6px;right:-6px;top:3px;bottom:3px;border-radius:6px;background:rgba(255,255,255,.16);box-shadow:inset 0 0 0 2px #fff;z-index:-1}
.wh-r,.nm-r{color:#fff;font-weight:700} .x{color:#fff}`;
  // .card keeps white space (pre): no white space between its lines
  return builtPage(css, `<div class="card"><div><span class="p">$ </span><span class="c">${esc(TM.checkCmd)}</span></div>`
    + `<div class="out"><div class="dim">${esc(head)}</div><div> </div><div>${esc(page)}</div>${body}<div> </div><div>${esc(sum)}</div><div class="x">${esc(exit)}</div></div></div>`);
}

// After the pull request, "3 keys broke" would read as the fixes failing: the caption says this
// is what the check says before that first run (the store as today's run left it, the
// redesigned pages), on a terminal as wide as the stage.
window.checkBeat = function checkBeat(t0) {
  const t1 = t0 + 6.4, K = t0 + 2.7, H = t0 + 3.9;
  frame({ id: 'ck', panel: 'wide', html: checkPage(), t: [t0, t1], cam: { x: 0, y: 0 }, fit: '.card > div:first-child, .out > div:not(.wh), .wh > .row' });
  widePanel(t0, t1);
  focus(t0, 'page');
  cap(t0 + .3, t0 + 4.5, 'Before that first run, <b>mendwright check</b> says:');
  reveal({ at: 'ck:.out', t: t0 + 1.5, what: 'mendwright check: what broke' });
  // the keys, then the two rows a run heals from memory
  reveal({ at: 'ck:.kh', all: true, text: false, t: K, t1: H, dy: 0 });
  reveal({ at: 'ck:.wo', text: false, t: H, dy: 0 });
  callout({ at: 'ck:.wh', place: 'right', dy: 56, t: [K, t1 - .1], sets: [
    { t: K, ic: '#', col: C.amber, h: 'A key is the name a selector’s fixes are saved under', s: 'here, the selector itself' },
    { t: H, ic: '◆', col: C.blue, h: 'That run heals these two from memory', s: 'the check itself saves nothing', result: true }] });
  beat(t0 + 2.2, 'K1 check, before that first run');
  beat(t0 + 3.4, 'K2 what a key is');
  beat(t0 + 5.0, 'K3 two heal from memory');
  return t1;
};

// ---------- the TypeScript package: the same store, the same repairs ----------
function tsRows() {
  const spec = TM.spec.split('\n');
  const S_ = (s, k) => Str(s, k);
  const call = (fn, arg, k, rest) => '  ' + KW('await') + ' ' + O_('page') + P_('.') + F_(fn) + P_('(') + S_(arg, k) + P_(rest || ');');
  const rows = [
    { id: 'i1', ln: 1, nomark: true, html: KW('import') + ' ' + P_('{ ') + O_('test') + P_(' }') + ' ' + KW('from') + ' ' + S_("'mendwright/test'") + P_(';') },
    { id: 't3', ln: 3, nomark: true, html: F_('test') + P_('(') + S_("'checkout'") + P_(', ') + KW('async') + ' ' + P_('({ ') + O_('page') + P_(' }) => {') },
    { id: 'g4', ln: 4, html: call('goto', "'/checkout'") },
    { id: 'f5', ln: 5, html: call('fill', "'#email'", 'email', ', …);'), fold: "'ada@example.com'" },
    { id: 'c6', ln: 6, html: call('check', "'#accept-terms'", 'terms') },
    { id: 'k7', ln: 7, html: call('click', "'#continue'", 'cont') }];
  for (const r of rows) {           // each row is the spec's line, or the line with an argument folded to …
    const shown = plainOf(r.html), real = spec[r.ln - 1];
    if ((r.fold ? shown.replace('…', r.fold) : shown) !== real) throw new Error('TS: row ' + r.ln + ' is not the spec: ' + shown);
    r.n = r.ln;
  }
  return rows;
}

window.typescriptBeat = function typescriptBeat(t0) {
  const t1 = t0 + 5.8;
  const keys = SCRIPT.filter(x => x[0] !== 'type').map(x => x[1]);
  const blk = codeBlock({ t0, t1, rows: tsRows() });
  const imp = blk.rows[0];
  Object.assign(imp, { type: [t0 + .3, t0 + .9], tint: [t0 + .3, t0 + 2.8] });
  edTitle(t0, 'checkout.spec.ts');
  blk.tok = { email: [[t0, 'saved']], terms: [[t0, 'saved']], cont: [[t0, 'saved']] };
  frame({ id: 'ts', html: D.pages.v2, t: [t0, t1] });
  reload(t0); url(t0, TM.tsUrl); chrome(t0, null);
  // the run: the list reporter's line for the test, and its summary
  const L = TM.tsOut.split(/\r?\n/);
  const tl = L.find(l => l.includes('checkout.spec.ts:') && l.includes('› checkout'));
  const res = verbatim(tl, tl.slice(tl.indexOf('checkout.spec.ts:'), tl.lastIndexOf(' (')));
  verbatim(L.find(l => /^\s*1 passed/.test(l)), '1 passed');
  run({ t0, t1, cmd: [t0 + .1, '$ ' + TM.tsCmd],
    stamp: { t: t0 + 3.2, kind: 'ok', html: '✓ 1 passed' }, receipts: [{ t: t0 + 3.2, text: res }] });
  focus(t0, 'code');
  cap(t0 + .2, t0 + 4.4, 'From <em>TypeScript</em> too: the same saved fixes.');
  beat(t0 + 1.0, 'T1 the import line');
  // the actions: the trace of the run says each used the repair the Python run saved
  const A = t0 + 1.4;
  focus(A, 'page');
  blk.hl.push([t0, 'i1', 0], [t0 + .6, 'i1', 1], [A, 'g4', 1], [A + .4, 'f5', 1], [A + .9, 'c6', 1], [A + 1.4, 'k7', 1], [t0 + 3.2, 'k7', 0]);
  blk.marks.push([A + .1, 'g4', '✓', C.green, t1], [A + .4, 'f5', '✓', C.green, t1], [A + .9, 'c6', '✓', C.green, t1], [A + 1.4, 'k7', '✓', C.green, t1]);
  curOn(A + .05, A + 1.7); cur(A, 'ts:input[name=email]', -90, 4); cur(A + .65, 'ts:#opt-91c4', 2, 3); cur(A + 1.15, 'ts:.btn-x82hd', 120, 4);
  click(A + .4, 'ts:input[name=email]', C.white); fill(A + .45, A + .7, 'ts:input[name=email]', 'ada@example.com');
  click(A + .9, 'ts:#opt-91c4', C.white); check(A + .9, 'ts:#opt-91c4');
  click(A + 1.4, 'ts:.btn-x82hd', C.white); press(A + 1.4, 'ts:.btn-x82hd');
  const SAVED = '✓ saved fix', end = t1 - .2;
  box({ at: 'ts:input[name=email]', col: C.green, t: [A + .4, end] });
  box({ at: 'ts:label.opt:nth-of-type(2)', col: C.green, t: [A + .9, end], pad: 6 });
  box({ at: 'ts:.btn-x82hd', col: C.green, t: [A + 1.4, end] });
  tag({ at: 'ts:input[name=email]', html: SAVED, col: C.green, t: [A + .5, end], solid: true, col0: 'ts:.f9a1' });
  tag({ at: 'ts:label.opt:nth-of-type(2)', html: SAVED, col: C.green, t: [A + 1.0, end], solid: true, col0: 'ts:.f9a1' });
  tag({ at: 'ts:.btn-x82hd', html: SAVED, col: C.green, t: [A + 1.5, end], solid: true, col0: 'ts:.f9a1' });
  if (keys.length !== 3) throw new Error('TS: the script has ' + keys.length + ' selectors');
  beat(A + 1.6, 'T2 the saved fixes, from TypeScript');
  focus(t0 + 3.2, 'run');
  beat(t0 + 4.4, 'T3 1 passed');
  return t1;
};

// ---------- popups nobody registered (dismiss=True): an offer is declined, a question is not
// answered. Two real runs of the same two lines on the redesign with a popup over the email
// field (D.P, capture_main.json); neither popup closes on Escape. The guide: a popup that
// reads as a promotion may be declined with "No thanks", and "Get my code" (get) is never
// clicked; a popup with a Keep button asks a question, and gets Escape and nothing more.
window.popupBeat = function popupBeat(t0) {
  const P = D.P, O = P.offer, Q = P.question, field = 'input[name=email]';
  const tq = t0 + 4.0, t1 = tq + 4.0;
  if (P.click !== field || Object.keys(P.click_kwargs).length || !P.open_kwargs.dismiss) throw new Error('popups: not the script shown');
  if (!O.ok || O.clicked.join() !== 'No thanks' || O.popup_up_after || !O.store_fresh) throw new Error('popups: the offer was not declined with No thanks');
  if (Q.ok || Q.clicked.length || !Q.popup_up_after || !Q.store_fresh) throw new Error('popups: the question was answered, or did not stay');
  montage(t0, t1);
  const blk = codeBlock({ t0, t1, rows: [...openRows('heal', P.open_kwargs), ...clickRows(P.click, P.click_kwargs, 2)] });
  url(t0, 'northwind.example/checkout');
  // the offer: Escape does nothing, so mendwright declines it; "Get my code" is never clicked
  frame({ id: 'po', html: P.pages.offer, t: [t0, tq], popup: '#offer', pop: [[t0 - 1, t0 + 2.3]] });
  focus(t0, 'code+page');
  cap(t0 + .2, t0 + 4.0, 'Offers: “No thanks”. Never “Get my code”.');
  const od = O.log.findIndex(l => /dismissed a popup/.test(l));
  if (od < 0) throw new Error('popups: no dismissed line for the offer');
  const kO = [[t0 + .8, 0], [t0 + 2.2, O.log_at[od]], [t0 + 2.6, O.seconds]];
  run({ t0, t1: tq, cmd: [t0 + .1, '$ python checkout.py'], keys: kO,
    stamp: { t: t0 + 2.6, kind: 'ok', html: `✓ Passed · ${quoteLabel('Get my code')} never clicked` },
    receipts: [{ t: t0 + 2.6, text: elide(msgOf(O.log[od]), 'dismissed a popup', 'with “No thanks”, after Escape did not close it') }] });
  const card = 'po:#offer > div';
  curOn(t0 + .75, t0 + 2.9); cur(t0 + .7, 'po:' + field, -90, 4); click(t0 + .8, 'po:' + field, C.red);
  keycap({ at: card, text: 'esc', t: [t0 + 1.0, t0 + 2.2] });
  box({ at: 'po:.pop-yes', col: C.red, dashed: true, t: [t0 + 1.4, t0 + 2.3], pad: 6 });
  tag({ at: 'po:.pop-yes', html: 'never', col: C.red, solid: true, t: [t0 + 1.4, t0 + 2.3], clear: card, leader: true, pad: 6 });
  cur(t0 + 1.7, 'po:.pop-no', -8, 4); haloOn(t0 + 1.8, t0 + 2.3);
  click(t0 + 2.2, 'po:.pop-no', C.purple); press(t0 + 2.2, 'po:.pop-no');
  cur(t0 + 2.35, 'po:' + field, -90, 4); click(t0 + 2.6, 'po:' + field, C.white);
  blk.hl.push([t0, 'c1', 0], [t0 + .8, 'c1', 1], [t0 + 2.6, 'c1', 0]);
  blk.marks.push([t0 + 2.6, 'c1', '✓', C.green, tq]);
  focus(t0 + 2.6, 'all');            // the result, with the dismiss=True line that made it
  beat(t0 + 1.8, 'O1 an offer: never Get my code');
  beat(t0 + 3.4, 'O2 declined with No thanks');
  // the question: Escape, and nothing more; the step stops with the browser's own error
  frame({ id: 'pq', html: P.pages.question, t: [tq, t1], popup: '#ask', pop: [[tq - 1, t1 + 1]] });
  reload(tq);
  focus(tq, 'code+page');
  cap(tq + .2, tq + 3.6, 'A question? Only Escape. You decide.');
  const qd = Q.log.findIndex(l => /popup/.test(l));
  if (qd < 0) throw new Error('popups: no log line for the question');
  const kQ = [[tq + .8, 0], [tq + 1.8, Q.log_at[qd]], [tq + 2.4, Q.seconds]];
  speedOf(kQ, tq + 1.8, tq + 2.4);
  run({ t0: tq, t1, cmd: [tq + .1, '$ python checkout.py'], keys: kQ, clockCol: [[tq + 2.4, C.white]],
    stamp: { t: tq + 2.6, kind: 'stop', html: '⊘ Stopped · nothing clicked' },
    receipts: [{ t: tq + 2.6, text: elide(msgOf(Q.log[qd]), 'could not dismiss a popup', 'mendwright pressed Escape, which did not close it') }] });
  curOn(tq + .75, tq + 2.0); cur(tq + .7, 'pq:' + field, -90, 4); click(tq + .8, 'pq:' + field, C.red);
  keycap({ at: 'pq:#ask > div', text: 'esc', t: [tq + 1.0, t1] });
  // its "No thanks" would answer the question (throw the cart away): never clicked here
  box({ at: 'pq:.ask-no', col: C.red, dashed: true, t: [tq + 1.4, t1], pad: 6 });
  tag({ at: 'pq:.ask-no', html: 'never', col: C.red, solid: true, t: [tq + 1.4, t1], clear: 'pq:#ask > div', leader: true, pad: 6 });
  blk.hl.push([tq + .8, 'c1', 1], [tq + 2.6, 'c1', 0]);
  blk.marks.push([tq + 2.6, 'c1', '⊘', C.amber, t1]);
  focus(tq + 2.6, 'all');
  beat(tq + 1.6, 'O3 a question: only Escape');
  beat(tq + 3.4, 'O4 stopped, nothing clicked');
  return t1;
};
