// The team beats of the main cut: the repairs become a pull request, mendwright check before
// the nightly run, and the TypeScript package with the repairs the Python run saved. Every line
// on screen comes from D.T (capture_team.json: real runs of mendwright 0.10.0 and of the
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
  const css = `body{background:#f6f8fa;color:#1f2328}
.card{position:absolute;left:22px;top:22px;width:790px;background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:20px 22px 22px}
h1{font-size:25px;line-height:32px;margin:0;font-weight:700;letter-spacing:-.01em}
.ok{margin-top:10px;font-size:17px;line-height:24px;font-weight:600;color:#1a7f37}
.diff{margin-top:18px;border:1px solid #d0d7de;border-radius:10px;overflow:hidden}
.fn{background:#f6f8fa;border-bottom:1px solid #d0d7de;padding:8px 14px;font:600 16px/22px ${MONO}}
.dl{display:flex;font:16.5px/28px ${MONO};white-space:pre;overflow:hidden}
.no{width:26px;flex:none;text-align:right;color:#8c959f;font-size:13px;padding-right:4px}
.sg{width:18px;flex:none;text-align:center}
.del{background:#ffebe9;color:#82071e} .add{background:#dafbe1;color:#116329} .ctx{color:#57606a}`;
  return builtPage(css, `<div class="card"><div class="head"><h1>${esc(TM.title)}</h1>
<div class="ok">✓ ${TM.n} repairs, each confirmed by a passing test</div></div>
<div class="diff"><div class="fn">${esc(TM.diffFile)}</div>${rows.join('')}</div></div>`);
}

window.pullRequestBeat = function pullRequestBeat(t0, blk) {
  const t1 = t0 + 6;
  if (TM.n !== D.D_heals.length) throw new Error('PR: the pull request does not write back every repair');
  frame({ id: 'pr', html: prPage(), t: [t0, t1], cam: { x: 0, y: 0 }, fit: '.card, .dl' });
  reload(t0); chrome(t0, 'Pull request'); chrome(t1, null);
  blk.t1 = t1;                      // checkout.py stays in the editor, veiled
  focus(t0, 'page');
  run({ t0, t1, cmd: [t0 + .1, '$ ' + TM.fixCmd] });
  cap(t0 + .2, t0 + 4.8, 'Confirmed fixes come back as a <u>pull request</u>.');
  reveal({ at: 'pr:.head', t: t0 + 1.4, what: TM.title });
  reveal({ at: 'pr:.diff', t: t0 + 2.6, what: 'the diff of ' + TM.diffFile, result: true });
  beat(t0 + 2.1, 'P1 a pull request');
  beat(t0 + 4.2, 'P2 three selectors written back');
  return t1;
};

// ---------- mendwright check, before the nightly run ----------
function checkPage() {
  const out = TM.checkOut, L = out.split(/\r?\n/);
  const RES = /^ {2}(would heal|needs model|ok \(heal\)|ok|wrong role|refused|not checked) +\S+ +/;
  const row = l => {                 // the result and the key, verbatim; the reason is cut
    const m = l.match(RES);
    return { kind: m[1], text: verbatim(l, m[0].replace(/ +$/, '')) };
  };
  const rows = L.filter(l => RES.test(l)).map(row);
  const wh = rows.map((r, i) => r.kind === 'would heal' ? i : -1).filter(i => i >= 0);
  if (!wh.length || wh[wh.length - 1] - wh[0] !== wh.length - 1) throw new Error('check: the would-heal rows are not together');
  const pad = Math.max(...rows.map(r => r.text.length)) + 2;
  const line = r => `<div><span class="${r.kind === 'would heal' ? 'wh-r' : 'nm-r'}">${esc(r.text.slice(0, 2 + r.kind.length))}</span>${esc(r.text.slice(2 + r.kind.length).padEnd(pad - 2 - r.kind.length))}<span class="dim">…</span></div>`;
  const body = rows.map((r, i) => (i === wh[0] ? '<div class="wh">' : '') + line(r) + (i === wh[wh.length - 1] ? '</div>' : '')).join('');
  const head = wholeLine(out, L.find(l => l.startsWith('mendwright check: ')));
  const page = wholeLine(out, L.find(l => /^https?:\/\//.test(l)));
  const sum = wholeLine(out, L.find(l => / would heal from memory/.test(l))), exit = wholeLine(out, L.find(l => /^Exit \d/.test(l)));
  const css = `body{background:#0b0f17;color:#cfd6df}
.card{position:absolute;left:0;top:0;width:834px;padding:22px 28px;font:17px/28px ${MONO};white-space:pre}
.card div{overflow:hidden} .p{color:#7d8590} .c{color:#eef2f7} .dim{color:#7d8590}
.wh{display:inline-block;vertical-align:top} .wh div{overflow:visible} .wh-r{color:#9ccbff} .nm-r{color:#d4b2ff} .x{color:#ffb0b0}`;
  // .card keeps white space (pre): no white space between its lines
  return builtPage(css, `<div class="card"><div><span class="p">$ </span><span class="c">${esc(TM.checkCmd)}</span></div>`
    + `<div class="out"><div class="dim">${esc(head)}</div><div> </div><div>${esc(page)}</div>${body}<div> </div><div>${esc(sum)}</div><div class="x">${esc(exit)}</div></div></div>`);
}

window.checkBeat = function checkBeat(t0) {
  const t1 = t0 + 5;
  frame({ id: 'ck', html: checkPage(), t: [t0, t1], cam: { x: 0, y: 0 }, fit: '.card, .card > div, .out > div' });
  chrome(t0, 'Terminal'); chrome(t1, null);
  focus(t0, 'page');
  cap(t0 + .4, t0 + 4.6, 'Before the nightly run, <b>check</b> what broke.');
  reveal({ at: 'ck:.out', t: t0 + 1.6, what: 'mendwright check: what broke' });
  box({ at: 'ck:.wh', col: C.blue, t: [t0 + 2.8, t1 - .1], pad: 3 });
  callout({ at: 'ck:.wh', place: 'right', t: [t0 + 2.8, t1 - .1], sets: [
    { t: t0 + 2.8, ic: '◆', col: C.blue, h: 'A run repairs these from memory', s: 'the check itself saves nothing', result: true }] });
  beat(t0 + 2.2, 'K1 check: three keys broke');
  beat(t0 + 3.8, 'K2 two repair from memory');
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
  const t1 = t0 + 6;
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
  const res = verbatim(tl, tl.slice(tl.indexOf('checkout.spec.ts:')));
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
