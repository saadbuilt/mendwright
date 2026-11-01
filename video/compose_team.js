// The later beats of the main cut: two popups, and the pull request. Every line on screen
// comes from D.P (capture_main.json: the popup scripts as they ran, with mendwright 0.10.1)
// and D.T (capture_team.json: the pytest run and mendwright fix), checked in render.py and
// with verbatim() here.
'use strict';
const TM = D.T;
const MONO = '"Cascadia Code","Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace';
// a page compose.js builds: the shop pages' font, and the same Inter as the stage
const builtPage = (css, body) => `<!doctype html><html><head><meta charset="utf-8"><style>*{box-sizing:border-box}html,body{overflow:hidden}
body{margin:0;font-family:Inter,"Inter Variable","Segoe UI",system-ui,sans-serif}${css}</style>${D.fontHead || ''}</head><body>${body}</body></html>`;
const plainOf = html => html.replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

// A line of the Python a popup beat ran, as an editor row, its words coloured as the main
// script's are. fold replaces one string literal of it by … (the page shows what it is).
function pyRow(id, n, line, fold) {
  if (fold && !line.includes(fold)) throw new Error('nothing to fold in ' + line);
  const text = fold ? line.replace(fold, '…') : line;
  let html = '', m;
  const re = /("(?:[^"\\]|\\.)*")|(\s+)|(\w+)(?=\()|(\w+)|([^\w\s"]+)/g;
  while ((m = re.exec(text))) {
    const [, str, ws, fn, word, punct] = m;
    if (str) html += Str(str);
    else if (ws) html += ws;
    else if (fn) html += F_(fn);
    else if (word) html += word === 'mendwright' ? MW(word) : word === 'def' || word === 'True' ? KW(word) : O_(word);
    else html += P_(punct);
  }
  if (plainOf(html) !== text) throw new Error('row is not its line: ' + text);
  return { id, n, html };
}
// the rows of a script for its 1-based lines, each [line, fold]
const scriptRowsOf = (script, picks) => {
  const lines = script.split('\n');
  return picks.map(([ln, fold]) => pyRow('s' + ln, ln, lines[ln - 1], fold));
};

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

window.pullRequestBeat = function pullRequestBeat(t0) {
  const t1 = t0 + 5.6;
  if (TM.n !== D.D_heals.length) throw new Error('PR: the pull request does not write back every repair');
  frame({ id: 'pr', html: prPage(), t: [t0, t1], cam: { x: 0, y: 0 }, fit: '.card, .dl' });
  reload(t0); chrome(t0, 'Pull request'); chrome(t1, null);
  focus(t0, 'page');
  run({ t0, t1, cmd: [t0 + .1, '$ ' + TM.fixCmd] });
  // the card comes in whole, title and diff, as the page loads; then the caption names it
  reveal({ at: 'pr:.card', t: t0, what: TM.title + ', and the diff of ' + TM.diffFile, result: true });
  cap(t0 + 1.2, t0 + 5.4, 'Confirmed repairs come back as a pull request.');
  beat(t0 + 1.2, 'P1 the pull request');
  beat(t0 + 3.6, 'P2 three selectors written back');
  return t1;
};


// ---------- a popup nobody registered, with dismiss=True: a promotion over the email field.
// mendwright pressed Escape, which did not close it, then its decline button, No thanks (the
// log and the journal say so); Subscribe is never touched.
window.offerBeat = function offerBeat(t0) {
  const O = D.P.offer, t1 = t0 + 6.8, field = 'pa:input[name=email]', no = 'pa:.pop-no';
  if (!O.ok || O.page.clicked.join() !== 'No thanks' || O.popup_up_after || O.heals.length) throw new Error('offer: not declined with No thanks');
  const J = O.journal.find(j => j.event === 'flow.dismiss');
  if (!J || J.how !== 'decline' || J.label !== 'No thanks' || !J.tried.some(x => x.how === 'escape')) throw new Error('offer: not declined after Escape');
  const od = O.log.findIndex(l => /dismissed a popup in the way of '#email'/.test(l));
  if (od < 0) throw new Error('offer: no log line');
  const blk = codeBlock({ t0, t1, rows: scriptRowsOf(O.script, [[1], [2], [3]]) });
  // the popup is up as the page shows; it closes as No thanks comes back up
  frame({ id: 'pa', html: D.P.pages.offer, t: [t0, t1], popup: '#offer', pop: [[t0 - 1, t0 + 3.25]] });
  reload(t0); url(t0, 'northwind.example/checkout');
  focus(t0, 'code+page');
  cap(t0 + .4, t0 + 5.0, 'A promotion in the way is declined, never accepted.');
  run({ t0, t1, cmd: [t0 + .1, '$ python checkout.py'],
    stamp: { t: t0 + 4.3, kind: 'ok', html: '✓ Passed · promotion declined' },
    receipts: [{ t: t0 + 4.3, text: elide(msgOf(O.log[od]), 'dismissed a popup', 'with “No thanks”, after Escape did not close it') }] });
  // the step's field is under the popup; the cursor goes to No thanks, coming into its row from
  // the left (under Subscribe, never over it), rests, and presses it: a grey pressed fill, then
  // the dialog closes; then the click on the field goes on
  curOn(t0 + 1.0, t0 + 4.4); cur(t0 + .6, field, -90, 4);
  cur(t0 + 1.9, no, -60, 40, 'left'); cur(t0 + 2.15, no, -8, 4);
  click(t0 + 2.95, no, C.white); press(t0 + 2.95, no, .35);
  cur(t0 + 3.55, field, -90, 4); click(t0 + 4.0, field, C.white);
  blk.hl.push([t0, 's3', 0], [t0 + 1.0, 's3', 1], [t0 + 4.3, 's3', 0]);
  blk.marks.push([t0 + 4.0, 's3', '✓', C.green, t1]);
  focus(t0 + 4.3, 'all');
  beat(t0 + 1.6, 'C1 a promotion in the way');
  beat(t0 + 3.1, 'C2 No thanks pressed');
  beat(t0 + 4.8, 'C3 declined, and the click goes on');
  return t1;
};

// ---------- a popup that needs an answer: the script registered a function for it once, and
// mendwright called it before the step it covered. The function fills the field and confirms;
// mendwright fills nothing itself.
window.confirmBeat = function confirmBeat(t0) {
  const Q = D.P.confirm, t1 = t0 + 9.4, f = 'pc:';
  const pg = Q.page;
  if (!Q.ok || pg.confirmed !== pg.email || !pg.continued || !pg.terms || Q.popup_up_after || Q.heals.length) throw new Error('confirm: not answered');
  const qi = Q.log.findIndex(l => /handled interruption 'confirm_email' before '#continue' with confirm_email\(\)/.test(l));
  if (qi < 0) throw new Error('confirm: no log line');
  const lines = Q.script.split('\n');
  if (!/^def confirm_email\(page\):$/.test(lines[2]) || !/^heal\.interrupt\("confirm_email",$/.test(lines[6])) throw new Error('confirm: not the script shown');
  const blk = codeBlock({ t0, t1, rows: scriptRowsOf(Q.script, [[3], [4, '"ada@example.com"'], [5], [7], [8], [9]]) });
  frame({ id: 'pc', html: D.P.pages.confirm, t: [t0, t1], popup: '#confirm-email-dialog', popDisplay: 'flex', pop: [[t0 + 2.2, t0 + 3.9]] });
  reload(t0); url(t0, 'northwind.example/checkout');
  focus(t0, 'code+page');
  cap(t0 + .4, t0 + 4.6, 'Some popups need an answer from your script.');
  // the test's own steps, through the saved repairs; accepting the terms opens the dialog
  curOn(t0 + .85, t0 + 5.6);
  cur(t0 + .8, f + 'input[name=email]', -90, 4); click(t0 + 1.2, f + 'input[name=email]', C.white);
  fill(t0 + 1.25, t0 + 1.5, f + 'input[name=email]', pg.email);
  cur(t0 + 1.5, f + '#opt-91c4', 2, 3); click(t0 + 1.8, f + '#opt-91c4', C.white); check(t0 + 1.8, f + '#opt-91c4');
  // before Continue, mendwright calls confirm_email(): it fills the field and presses Confirm
  blk.hl.push([t0, 's3', 0], [t0 + 2.6, 's3', 1], [t0 + 2.9, 's4', 1], [t0 + 3.4, 's5', 1], [t0 + 4.0, 's5', 0]);
  cur(t0 + 2.6, f + '#confirm-email', -90, 4);
  fill(t0 + 3.0, t0 + 3.3, f + '#confirm-email', pg.confirmed);
  cur(t0 + 3.3, f + '#confirm-email-ok', 0, 4); click(t0 + 3.6, f + '#confirm-email-ok', C.white); press(t0 + 3.6, f + '#confirm-email-ok');
  // then the step it covered goes on
  cur(t0 + 4.4, f + '.btn-x82hd', 120, 4); click(t0 + 5.2, f + '.btn-x82hd', C.white); press(t0 + 5.2, f + '.btn-x82hd');
  cap(t0 + 4.6, t0 + 8.8, 'Register it once, and your function answers it.');
  run({ t0, t1, cmd: [t0 + .1, '$ python checkout.py'],
    stamp: { t: t0 + 5.8, kind: 'ok', html: '✓ Passed · email confirmed' },
    receipts: [{ t: t0 + 5.8, text: verbatim(Q.log[qi], msgOf(Q.log[qi]).replace(/^mendwright: /, '')) }] });
  blk.marks.push([t0 + 3.6, 's5', '✓', C.green, t1]);
  focus(t0 + 5.8, 'all');
  beat(t0 + 2.75, 'D1 a dialog asks to confirm the email');
  beat(t0 + 3.5, 'D2 your function answers it');
  beat(t0 + 6.6, 'D3 the step goes on');
  return t1;
};
