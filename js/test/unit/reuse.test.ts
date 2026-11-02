// A selector a heal or a seed file saved is checked again on the page where a lookup would use it
// (resolver.Reuse, gate.reuseReason): the element's role against the key's role when a control is
// involved, its text when the key is destructive and has expected text, and its ARIA role for the
// key of a getByRole() call. One that fails is left out of that lookup, logged once per key and
// page, journaled as heal.refused, and never quarantined. A selector the code passes is used as
// before. The cases are the Python package's tests/test_reuse_check.py: a test id that moved onto
// a "Delete my account" button, and //input[@name='terms'], saved on a checkbox, matching an
// <input type="submit" name="terms">. Both packages share overrides.json, so a heal either one
// saved is checked here as there.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired, GetByNotFound } from '../../src/errors';
import { getByKey, parse, roleMismatch } from '../../src/getby';
import { FakeModel, cleanEnv, journalOf, world } from '../support/env';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

beforeEach(cleanEnv);

const URL = 'https://shop.test/checkout';
const KEY = 'accept_terms';
const CODE = '#accept-terms';
const TESTID = "//*[@data-testid='tos-optin']";
const BY_NAME = "//input[@name='terms']";

function html(body: string): string {
  return "<html><body><main><h1>Checkout</h1><form id='checkout'>"
    + "<label for='email'>Email</label><input id='email' name='email' type='email'>"
    + body + "<button id='pay' type='submit'>Pay now</button></form></main></body></html>";
}

const CHECKBOX = html("<label><input type='checkbox' data-testid='tos-optin' name='terms'><span>I accept the terms</span></label>");
const DELETE = html("<button type='button' data-testid='tos-optin'>Delete my account</button>");
const SUBMIT = html("<input type='submit' name='terms' value='Read the terms'>");

function row(xpath: string, source = 'heal', extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { xpath, source, addedAt: '2026-10-01T00:00:00+00:00', lastSucceededAt: '2026-10-02T00:00:00+00:00',
    status: 'active', ...extra };
}

const CODE_ROW = row(CODE, 'code', { lastSucceededAt: '2026-10-01T00:00:00+00:00' });

function write(file: string, rows: Record<string, unknown>[], key = KEY, entry: Record<string, unknown> = {}): void {
  fs.writeFileSync(file, JSON.stringify({ schema: 2, selectors: { [key]: {
    expectedRole: 'checkbox', lastPage: '/checkout', pages: ['/checkout'], candidates: rows, ...entry } } }));
}

function refused(dir: string): Record<string, unknown>[] {
  return journalOf(dir).filter((e) => e.event === 'heal.refused');
}

function refusalLines(lines: string[]): string[] {
  return lines.filter((l) => l.includes('did not use'));
}

function diskStatus(file: string, key: string, xpath: string): string {
  const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
  return data.selectors[key].candidates.find((c: { xpath: string }) => c.xpath === xpath).status ?? 'active';
}

describe('a saved selector on another control', () => {
  for (const [name, healXpath, page] of [['a test id on a Delete my account button', TESTID, DELETE],
    ['an input of type submit', BY_NAME, SUBMIT]] as const) {
    for (const [how, role] of [['role passed', { role: 'checkbox' }], ['role stored', {}]] as const) {
      test(name + ', ' + how + ': not used, refused once, never quarantined', async () => {
        const w = world(page, { url: URL });
        write(w.storePath, [CODE_ROW, row(healXpath)]);
        const model = new FakeModel([null]);
        const heal = w.open({ provider: model, timeoutMs: 0 });
        const reason = 'live element role mismatch: got button want checkbox';
        await assert.rejects(heal.locator(KEY, CODE, role as never).check(), (error: unknown) =>
          error instanceof AttentionRequired
          && error.message.includes('failed the live check here, so it was not used: ' + reason));
        assert.deepEqual(w.page.actions, []);           // nothing was clicked or checked
        assert.equal(model.bundles.length, 1);          // it went on to the heal path
        const [event, ...more] = refused(w.dir);
        assert.deepEqual(more, []);
        assert.deepEqual([event.key, event.xpath, event.source, event.reason, event.page],
          [KEY, healXpath, 'heal', reason, '/checkout']);
        assert.deepEqual(refusalLines(w.lines), ['warning: did not use ' + healXpath + " for 'accept_terms' on /checkout: "
          + 'a heal saved that selector, and the element it matches here failed the live check (' + reason + '). '
          + 'mendwright left it in the store for the pages where it fits and went on without it.']);
        // Not quarantined: it may be right on other pages.
        assert.equal(heal.store.status(KEY, healXpath), 'active');
        assert.equal(diskStatus(w.storePath, KEY, healXpath), 'active');
        assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.quarantine'), []);
      });
    }
  }

  test('the same heal is used on the page it fits', async () => {
    const w = world(CHECKBOX, { url: URL });
    write(w.storePath, [CODE_ROW, row(BY_NAME)]);
    const model = new FakeModel();
    await w.open({ provider: model, timeoutMs: 0 }).locator(KEY, CODE).check();
    assert.deepEqual(w.page.actions, [['check', 'xpath=' + BY_NAME]]);
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(refused(w.dir), []);
  });

  test('a seed row the Python package saved is checked too', async () => {
    const w = world(SUBMIT, { url: URL });
    write(w.storePath, [row(BY_NAME, 'seed', { addedAt: null, lastSucceededAt: null })]);
    const heal = w.open({ timeoutMs: 0 });
    await assert.rejects(heal.locator(KEY).check(), /got button want checkbox/);
    assert.deepEqual(w.page.actions, []);
    const [event] = refused(w.dir);
    assert.deepEqual([event.source, event.xpath], ['seed', BY_NAME]);
    w.page.goto(CHECKBOX);
    await heal.locator(KEY).check();                    // the page it fits: used
    assert.deepEqual(w.page.actions, [['check', 'xpath=' + BY_NAME]]);
  });

  test('the selector in the code is used as before', async () => {
    const w = world(SUBMIT, { url: URL });
    await w.open({ timeoutMs: 0 }).locator(KEY, BY_NAME, { role: 'checkbox' }).click();
    assert.deepEqual(w.page.actions, [['click', 'xpath=' + BY_NAME]]);
    assert.deepEqual(refused(w.dir), []);
    assert.deepEqual(refusalLines(w.lines), []);
    // The warning a mismatched role always gave.
    assert.ok(w.lines.some((l) => l.includes("role 'checkbox' was given for 'accept_terms'")), w.lines.join('\n'));
  });

  test('one event per key, page and selector, and one warning per key and page', async () => {
    const w = world(DELETE, { url: URL });
    write(w.storePath, [CODE_ROW, row(TESTID)]);
    const heal = w.open({ timeoutMs: 0, healAcrossPages: true });
    for (let i = 0; i < 3; i++) await assert.rejects(heal.locator(KEY, CODE).check(), AttentionRequired);
    assert.equal(refusalLines(w.lines).length, 1);
    assert.equal(refused(w.dir).length, 1);
    w.page.goto(DELETE, 'https://shop.test/checkout/express');
    await assert.rejects(heal.locator(KEY, CODE).check(), AttentionRequired);
    assert.deepEqual(refusalLines(w.lines).map((l) => l.split(':')[1].split(' on ').pop()), ['/checkout', '/checkout/express']);
    assert.deepEqual(refused(w.dir).map((e) => e.page), ['/checkout', '/checkout/express']);
  });

  test('saved rows that are all refused give up at once, without waiting for the timeout', async () => {
    const w = world(DELETE, { url: URL });
    write(w.storePath, [row(TESTID)]);
    const started = Date.now();
    await assert.rejects(w.open({ timeoutMs: 5000 }).locator(KEY).check(),
      (error: unknown) => error instanceof AttentionRequired && /got button want checkbox/.test(error.message));
    assert.ok(Date.now() - started < 2500, 'it waited ' + (Date.now() - started) + ' ms');
  });
});

describe('what the check compares', () => {
  for (const [name, expectedText, used] of [['with expected text', ['Pay now'], false], ['without', null, true]] as const) {
    test('the text of a destructive key is checked when it has one: ' + name, async () => {
      const w = world(html('').replace('Pay now', 'Delete my account'), { url: URL });
      const entry: Record<string, unknown> = { expectedRole: 'button', destructive: true };
      if (expectedText) entry.expectedText = expectedText;
      write(w.storePath, [row("//button[@type='submit']", 'seed', { addedAt: null, lastSucceededAt: null })], 'pay', entry);
      const heal = w.open({ timeoutMs: 0 });
      if (used) {
        await heal.locator('pay').click();               // the guide's seed example: no text
        assert.deepEqual(w.page.actions, [['click', "xpath=//button[@type='submit']"]]);
        return;
      }
      await assert.rejects(heal.locator('pay').click(), /live action text mismatch: Delete my account/);
      assert.deepEqual(w.page.actions, []);
    });
  }

  test('a role that is only a guess is not compared', async () => {
    const w = world(html("<textarea id='note'></textarea>"), { url: URL });
    write(w.storePath, [row("//*[@id='note']", 'seed', { addedAt: null, lastSucceededAt: null })], 'note',
      { expectedRole: 'unknown' });
    const heal = w.open({ timeoutMs: 0 });
    await heal.locator('note', null, { role: 'textbox' }).fill('hello');     // textbox: input or textarea
    assert.deepEqual(w.page.actions, [['fill', "xpath=//*[@id='note']"]]);
    assert.deepEqual(refused(w.dir), []);
    assert.equal(heal.store.metaOf('note').expectedRole, 'textarea');
  });

  test('another tag on a key that is only read is used', async () => {
    const w = world(html("<div id='total'>12.00</div>"), { url: URL });
    write(w.storePath, [row("//*[@id='total']", 'seed', { addedAt: null, lastSucceededAt: null })], 'total',
      { expectedRole: 'span' });
    assert.equal(await w.open({ timeoutMs: 0 }).locator('total').innerText(), '12.00');
    assert.deepEqual(refused(w.dir), []);
  });

  test('present() and isVisible() leave a refused heal out, and log nothing', async () => {
    const w = world(DELETE, { url: URL });
    write(w.storePath, [CODE_ROW, row(TESTID)]);
    const heal = w.open({ timeoutMs: 0 });
    assert.equal(await heal.present(KEY), false);
    assert.equal(await heal.peekVisible(KEY), false);
    assert.deepEqual(refused(w.dir), []);
    assert.deepEqual(refusalLines(w.lines), []);
    w.page.goto(CHECKBOX);
    assert.equal(await heal.present(KEY), true);
  });

  test('a saved heal of a getByRole() call needs the call\'s ARIA role', async () => {
    const key = getByKey('getByRole', 'button', { name: 'Sign in' });
    const healXpath = "//button[@id='login']";
    const w = world(html("<button id='login' role='tab'>Log in</button>"), { url: URL });
    write(w.storePath, [row(key, 'code', { lastSucceededAt: '2026-10-01T00:00:00+00:00' }), row(healXpath)], key,
      { expectedRole: 'button' });
    const heal = w.open({ timeoutMs: 0, healGetBy: true });
    await assert.rejects(heal.page.getByRole('button', { name: 'Sign in' }).click(), GetByNotFound);
    const [event] = refused(w.dir);
    assert.equal(event.reason, roleMismatch(parse(key) as never));
    assert.deepEqual(w.page.actions, []);
    w.page.goto(html("<button id='login'>Log in</button>"));
    await heal.page.getByRole('button', { name: 'Sign in' }).click();
    assert.deepEqual(w.page.actions, [['click', 'xpath=' + healXpath]]);
  });
});

describe('the two packages', { skip: needsPython() }, () => {
  test('the heal.refused line is the one the Python package writes, and mendwright notify reads it', async () => {
    process.env.MENDWRIGHT_RUN_ID = 'ts-reuse-1';
    const w = world(DELETE, { url: URL });
    const rows = [CODE_ROW, row(TESTID)];
    write(w.storePath, rows);
    await assert.rejects(w.open({ timeoutMs: 0 }).locator(KEY, CODE).check(), AttentionRequired);
    const ours = fs.readFileSync(w.dir + '/journal.jsonl', 'utf-8').split('\n').filter((l) => l.includes('"heal.refused"'));
    const python = runPython<string[]>(`
import json, os, sys, tempfile
sys.path.insert(0, 'tests')
import mendwright
from mendwright import AttentionRequired
from conftest import FakePage
case = json.load(sys.stdin)
folder = tempfile.mkdtemp()
store = os.path.join(folder, 'overrides.json')
with open(store, 'w', encoding='utf-8') as f:
    json.dump({'schema': 2, 'selectors': {case['key']: {'expectedRole': 'checkbox', 'lastPage': '/checkout',
               'pages': ['/checkout'], 'candidates': case['rows']}}}, f)
heal = mendwright.open(FakePage(case['html'], url=case['url']), store_path=store, timeout_ms=0, provider=None)
try:
    heal.locator(case['key'], case['code']).check()
except AttentionRequired:
    pass
lines = open(os.path.join(folder, 'journal.jsonl'), encoding='utf-8').read().splitlines()
print(json.dumps([line for line in lines if '"heal.refused"' in line]))`,
    { key: KEY, code: CODE, rows, html: DELETE, url: URL });
    const plain = (line: string) => line.replace(/"ts":"[^"]*"/, '"ts":"-"').replace(/"run":(null|"[^"]*"),/, '');
    assert.equal(ours.length, 1);
    assert.deepEqual(ours.map(plain), python.map(plain));
    const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', w.storePath,
      '--run', 'ts-reuse-1', '--format', 'json', '--dry-run'], {
      cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout).repaired.total, 0);
  });
});
