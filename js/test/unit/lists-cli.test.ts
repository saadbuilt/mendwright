// A list heal this package proposes, checked and accepted with the Python package's command
// line, run from this repository: mendwright lists shows it with the id this package gave it,
// mendwright lists accept takes it, and then this package's lookup throws that it waits for
// mendwright fix, which sees it in a TypeScript file; mendwright lists reject is read by the
// healer that lived across it. The same page gives the same proposal in both packages, and a
// proposal the Python package made and accepted is kept and named here too.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired } from '../../src/errors';
import type { Healer } from '../../src/healer';
import { whenText } from '../../src/lists';
import { loadStore, type Candidate } from '../../src/store';
import { cleanEnv, journalOf, tempDir, world, type World } from '../support/env';
import { NoModel, cart } from '../support/listpages';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

beforeEach(cleanEnv);

const NO_MATCH = 'No matching element for: cart (tried 1 candidate(s)).\n  -> ';
const ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]";
const PY_HEAD = 'import json, sys\n';

/** python -m mendwright args, from cwd: [exit status, stdout, stderr]. */
function cli(args: string[], cwd = REPO): [number, string, string] {
  const command = pythonCommand();
  if (!command) throw new Error('no Python that imports mendwright from ' + REPO);
  const result = spawnSync(command, ['-m', 'mendwright', ...args], {
    cwd, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  return [result.status ?? -1, result.stdout, result.stderr];
}

async function raised(heal: Healer): Promise<string> {
  try {
    await heal.locatorAll('cart', '.cart-item', { nonempty: true });
  } catch (error) {
    assert.ok(error instanceof AttentionRequired, String(error));
    return (error as Error).message;
  }
  throw new Error('the lookup did not throw');
}

function rowOf(w: World, xpath: string): Candidate {
  const entry = JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors.cart;
  return entry.candidates.find((c: Candidate) => c.xpath === xpath);
}

/** A cart whose list was recorded with nonempty: true, then renamed, and the proposal made. */
async function proposal() {
  const w = world(cart(true), { url: 'https://shop.test/cart', rects: true });
  const model = new NoModel();
  const heal = w.open({ provider: model });
  await heal.locatorAll('cart', '.cart-item', { nonempty: true });
  w.page.goto(cart(true, { row: 'cart-row' }));
  await raised(heal);
  const pid = (rowOf(w, ROWS).proposal as { id: string }).id;
  return { w, heal, model, pid, flag: ' --store "' + heal.store.overridePath + '"' };
}

describe('the Python package\'s command line and a proposal this package made', { skip: needsPython() }, () => {
  test('mendwright lists shows it with its id, mendwright lists accept takes it, and the lookup then waits for fix',
    async () => {
      const { w, heal, model, pid, flag } = await proposal();
      const [status, out, err] = cli(['lists', '--store', w.storePath]);
      assert.equal(status, 0, err);
      const lines = out.split(/\r?\n/);
      assert.ok(lines.includes('1 list heal waits for you to check it.'), out);
      assert.ok(lines.includes('cart  (div elements)  id ' + pid), out);
      assert.ok(lines.includes('  was      .cart-item: 3 elements under \'Shopping cart\', in <section class="cart">'), out);
      assert.ok(lines.includes('  now      ' + ROWS), out);
      assert.ok(lines.includes('           as CSS: div.cart-row'), out);
      assert.ok(lines.includes('           they hold 3 of the 3 items recorded'), out);
      assert.ok(lines.includes('  accept   mendwright lists accept cart ' + pid + flag), out);
      // As JSON, the evidence is the row's as this package saved it.
      const [, json] = cli(['lists', '--json', '--store', w.storePath]);
      const listing = JSON.parse(json);
      assert.deepEqual(listing.proposed.map((p: { key: string; id: string; xpath: string; css: string }) =>
        [p.key, p.id, p.xpath, p.css]), [['cart', pid, ROWS, 'div.cart-row']]);
      assert.deepEqual(listing.proposed[0].proposal, rowOf(w, ROWS).proposal);

      const [accepted, said, refused] = cli(['lists', 'accept', 'cart', pid, '--store', w.storePath]);
      assert.equal(accepted, 0, refused);
      assert.deepEqual(said.trim().split(/\r?\n/), ["Accepted the list heal of 'cart' (" + pid + '): ' + ROWS
        + " (3 div elements under 'Shopping cart').", "mendwright doesn't use it until it is in your code. Write it there "
        + 'with: mendwright fix --apply' + flag]);
      const row = rowOf(w, ROWS);
      assert.equal(row.status, 'accepted');
      assert.ok(typeof row.acceptedAt === 'string' && row.acceptedAt);
      assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.accepted').map((e) => [e.key, e.xpath, e.id, e.list]),
        [['cart', ROWS, pid, true]]);

      // This package's lookup still throws, until the selector is in the code: in a fresh run,
      // and in the healer that lived across the accept.
      const waits = NO_MATCH + "'cart' matched 3 elements when its list fingerprint was recorded, and none of its "
        + 'selectors matches now. You accepted a list heal for it on ' + whenText(row.acceptedAt) + ', ' + ROWS
        + ', which matches 3 elements now, but mendwright takes a list only from the selectors in your code. Write it '
        + 'there with: mendwright fix --apply' + flag + '. If the list can be empty here, remove nonempty: true.';
      const looked = () => journalOf(w.dir).filter((e) => e.event === 'heal.requested' || e.event === 'heal.held').length;
      const before = looked();
      assert.equal(await raised(w.open({ provider: model })), waits);
      assert.equal(await raised(heal), waits);
      assert.equal(looked(), before);   // nothing looked for again, by the fresh healer or the one across the accept
      assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.declined').slice(-2).map((e) => [e.code, e.reason]),
        [['list_waits_fix', waits.slice(NO_MATCH.length)], ['list_waits_fix', waits.slice(NO_MATCH.length)]]);
      assert.equal(rowOf(w, ROWS).status, 'accepted');   // this package's writes kept the acceptance
      assert.deepEqual(model.calls, []);

      // mendwright fix sees it, in a TypeScript file that holds the old selector.
      const project = tempDir();
      fs.writeFileSync(path.join(project, 'cart.spec.ts'), "test('cart', async () => {\n"
        + "  const rows = await heal.locatorAll('cart', '.cart-item', { nonempty: true });\n});\n");
      const [fixed, diff, fixErr] = cli(['fix', '--store', w.storePath, '--path', project, '--glob', '*.ts']);
      assert.equal(fixed, 0, fixErr);
      assert.ok(diff.includes("-  const rows = await heal.locatorAll('cart', '.cart-item', { nonempty: true });"), diff);
      assert.ok(diff.includes("+  const rows = await heal.locatorAll('cart', 'div.cart-row', { nonempty: true });"), diff);
      // The selector in the code: the list is found as it is, and the list heal is no longer waiting.
      const fixedPage = await w.open({ provider: model }).locatorAll('cart', '.cart-row', { nonempty: true });
      assert.equal(fixedPage.length, 3);
    });

  test('the same page gives the same proposal in both packages: XPath, CSS, evidence and id', async () => {
    const { w, pid } = await proposal();
    const row = rowOf(w, ROWS);
    const html = await w.page.content();
    const py = runPython<{ xpath: string; css: string; evidence: Record<string, unknown> }>(PY_HEAD + `
from mendwright import lists
from mendwright.store import load_store
d = json.load(sys.stdin)
lfp = load_store(None, d['file']).meta('cart')['listFingerprint']
match = lists.find(lfp, d['html'], 'div')
evidence = lists.proposal_evidence('cart', lfp, match, match.xpath, match.css, ['.cart-item'], '/cart', d['at'])
print(json.dumps({'xpath': match.xpath, 'css': match.css, 'evidence': evidence}))`,
    { file: w.storePath, html, at: (row.proposal as { at: string }).at });
    assert.equal(py.xpath, ROWS);
    assert.equal(py.css, row.css);
    assert.deepEqual(py.evidence, row.proposal);
    assert.equal(py.evidence.id, pid);
  });
});

describe('a list heal this package retired', { skip: needsPython() }, () => {
  test("is retired for the Python package's command line too", async () => {
    const { w, pid } = await proposal();
    const [accepted, , err] = cli(['lists', 'accept', 'cart', pid, '--store', w.storePath]);
    assert.equal(accepted, 0, err);
    w.page.goto(cart(true));   // the old selector works again: the accepted list heal is quarantined
    assert.equal((await w.open().locatorAll('cart', '.cart-item')).length, 3);
    assert.equal(rowOf(w, ROWS).status, 'quarantined');
    const [status, out] = cli(['lists', '--store', w.storePath]);
    assert.equal(status, 0);
    assert.ok(out.includes('No list heal waits for you to check it.') && !out.includes('accepted list heal'), out);
    const [again, , refused] = cli(['lists', 'accept', 'cart', pid, '--store', w.storePath]);
    assert.equal(again, 1);
    assert.ok(refused.includes("mendwright lists: 'cart' has no list heal to check."), refused);
  });
});

describe('a list heal rejected with the Python package\'s command line', { skip: needsPython() }, () => {
  test('is refused by the healer that lived across the reject, and never proposed again', async () => {
    const { w, heal, model, pid } = await proposal();
    const held = journalOf(w.dir).filter((e) => e.event === 'heal.held').length;
    const [rejected, , err] = cli(['lists', 'reject', 'cart', pid, '--store', w.storePath]);
    assert.equal(rejected, 0, err);
    assert.equal(rowOf(w, ROWS).status, 'quarantined');
    const file = fs.readFileSync(w.storePath, 'utf-8');
    const written = fs.statSync(w.storePath).mtimeMs;
    // The healer that proposed it still holds the row as proposed: it reads the reject before
    // it looks again, so the list gate refuses the group, and the row stays rejected.
    const message = await raised(heal);
    assert.ok(message.includes('the group it found, ' + ROWS + ", failed mendwright's list checks (it is a list heal "
      + 'you rejected, or one mendwright quarantined'), message);
    assert.equal(rowOf(w, ROWS).status, 'quarantined');
    assert.equal(journalOf(w.dir).filter((e) => e.event === 'heal.held').length, held);
    // Never proposed again: the store is as the reject left it, and not written.
    assert.equal(fs.readFileSync(w.storePath, 'utf-8'), file);
    assert.equal(fs.statSync(w.storePath).mtimeMs, written);
    const declined = journalOf(w.dir).filter((e) => e.event === 'heal.declined');
    assert.equal(declined[declined.length - 1].code, 'list_not_found');
    const [, out] = cli(['lists', '--store', w.storePath]);
    assert.ok(out.includes('No list heal waits for you to check it.'), out);
    assert.deepEqual(model.calls, []);
  });
});

describe('a proposal the Python package made and accepted', { skip: needsPython() }, () => {
  test('this package keeps its rows through its writes, and its lookup throws that it waits for fix', async () => {
    const w = world(cart(true), { url: 'https://shop.test/cart', rects: true });
    const heal = w.open({ provider: new NoModel() });
    await heal.locatorAll('cart', '.cart-item', { nonempty: true });
    const renamed = cart(true, { row: 'cart-row' });
    const py = runPython<{ id: string; row: Candidate }>(PY_HEAD + `
from mendwright import lists
from mendwright.store import edit_list_heal, load_store
d = json.load(sys.stdin)
store = load_store(None, d['file'])
lfp = store.meta('cart')['listFingerprint']
match = lists.find(lfp, d['html'], 'div')
evidence = lists.proposal_evidence('cart', lfp, match, match.xpath, match.css, ['.cart-item'], '/cart',
                                   '2026-11-02T10:14:03+00:00')
store.propose_list_heal('cart', match.xpath, css=match.css, proposal=evidence)
done = edit_list_heal(d['file'], 'cart', 'accept', evidence['id'], now='2026-11-02T10:20:00+00:00')
print(json.dumps({'id': evidence['id'], 'row': done.row}))`, { file: w.storePath, html: renamed });
    assert.equal(py.row.status, 'accepted');
    // This package writes the store (another key), and the rows the Python package wrote stay.
    const store = loadStore(w.storePath);
    store.setCodeCandidates('other', ['#other']);
    assert.deepEqual(rowOf(w, ROWS), py.row);
    assert.deepEqual(store.listHeals('cart').map((c) => [c.xpath, c.status]), [[ROWS, 'accepted']]);
    w.page.goto(renamed);
    const flag = ' --store "' + heal.store.overridePath + '"';
    assert.equal(await raised(w.open({ provider: new NoModel() })), NO_MATCH + "'cart' matched 3 elements when its list "
      + 'fingerprint was recorded, and none of its selectors matches now. You accepted a list heal for it on '
      + '2026-11-02 10:20:00 UTC, ' + ROWS + ', which matches 3 elements now, but mendwright takes a list only from the '
      + 'selectors in your code. Write it there with: mendwright fix --apply' + flag + '. If the list can be empty here, '
      + 'remove nonempty: true.');
    assert.deepEqual(rowOf(w, ROWS), py.row);
  });
});
