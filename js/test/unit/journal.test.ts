// The journal as the Python package 0.10.0 reads it: every line has the run id, a heal that
// did not apply is a heal.declined with the Python package's code, and a step that could not
// go on a flow.stop. So the Python package's mendwright notify sums up a run of this package
// as it sums up its own.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { DECLINE_CODES } from '../../src/coordinator';
import { Journal, runId, validRunId } from '../../src/journal';
import { FakeModel, cleanEnv, journalOf, tempDir, world } from '../support/env';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

const V1 = `<html><body><main><form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <button id="create" type="submit">Create account</button>
</form></main></body></html>`;

const V2 = `<html><body><main><form id="signup">
  <label for="user-email">Email</label><input id="user-email" name="email" type="email">
  <button id="create-account" type="submit">Create account</button>
</form></main></body></html>`;

beforeEach(cleanEnv);

const ENVS: Record<string, string>[] = [
  {},
  { MENDWRIGHT_RUN_ID: 'nightly-42' },
  { MENDWRIGHT_RUN_ID: 'ci:7/shop.a_b' },
  { MENDWRIGHT_RUN_ID: 'nightly\n' },
  { MENDWRIGHT_RUN_ID: '-starts-with-a-dash' },
  { MENDWRIGHT_RUN_ID: 'x'.repeat(101) },
  { MENDWRIGHT_RUN_ID: 'x'.repeat(100) },
  { GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '0011893412207', GITHUB_RUN_ATTEMPT: '2' },
  { GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '11893412207' },
  { GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '11893412207', GITHUB_RUN_ATTEMPT: 'two' },
  { GITHUB_ACTIONS: 'false', GITHUB_RUN_ID: '11893412207' },
  { GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '1189x' },
  { MENDWRIGHT_RUN_ID: 'bad id', GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '5' },
  { MENDWRIGHT_RUN_ID: 'mine', GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '5' },
];

describe('the run id', () => {
  test('follows the Python package\'s rules', () => {
    assert.deepEqual(runId({}), { id: null, source: null, bad: null });
    assert.deepEqual(runId({ MENDWRIGHT_RUN_ID: 'nightly-42' }), { id: 'nightly-42', source: 'MENDWRIGHT_RUN_ID', bad: null });
    assert.deepEqual(runId({ MENDWRIGHT_RUN_ID: 'nightly\n' }), { id: null, source: null, bad: 'nightly\n' });
    assert.deepEqual(runId({ GITHUB_ACTIONS: 'true', GITHUB_RUN_ID: '0011893412207', GITHUB_RUN_ATTEMPT: '2' }),
      { id: 'gh-11893412207.2', source: 'github', bad: null });
    assert.equal(validRunId('a'.repeat(100)), true);
    assert.equal(validRunId('a'.repeat(101)), false);
  });

  test('is the one the Python package gives for the same environment', { skip: needsPython() }, () => {
    const python = runPython<[string | null, string | null, string | null][]>(`
import json, sys
from mendwright.journal import run_id
print(json.dumps([list(run_id(env)) for env in json.load(sys.stdin)]))`, ENVS);
    assert.deepEqual(ENVS.map((env) => { const r = runId(env); return [r.id, r.source, r.bad]; }), python);
  });

  test('frames every line right after event, and a field of the same name never overwrites it', () => {
    const file = path.join(tempDir(), 'journal.jsonl');
    new Journal(file, { run: 'nightly-42', clock: () => '2026-10-17T00:00:00+00:00' })
      .event('heal.declined', { key: 'pay', run: 'forged', code: 'destructive', reason: 'r' });
    const line = fs.readFileSync(file, 'utf-8');
    assert.equal(line, '{"ts":"2026-10-17T00:00:00+00:00","event":"heal.declined","run":"nightly-42","key":"pay",'
      + '"code":"destructive","reason":"r"}\n');
    process.env.MENDWRIGHT_RUN_ID = 'from-env';
    assert.equal(new Journal(file).run, 'from-env');
    assert.equal(new Journal(file, { run: null }).run, null);
    assert.throws(() => new Journal(file, { run: 'two words' }), TypeError);
  });

  test('a MENDWRIGHT_RUN_ID that is not one is ignored, said once per open(), and fails nothing', async () => {
    process.env.MENDWRIGHT_RUN_ID = 'nightly\n';
    const w = world(V1);
    await w.open().locator('email', '#email').fill('ada@example.com');
    assert.equal(w.lines.filter((l) => l.includes('MENDWRIGHT_RUN_ID must be')).length, 1);
    assert.ok(journalOf(w.dir).every((e) => !('run' in e)));
  });
});

describe('heal.declined and flow.stop', () => {
  test('each decline has the Python package\'s code', async () => {
    const declined = (dir: string) => journalOf(dir).filter((e) => e.event === 'heal.declined')
      .map((e) => [e.key, e.code]);
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    await w.open().locator('create', '#create').click();
    w.page.goto(V2);
    await assert.rejects(w.open().locator('create', '#create', { destructive: true }).click());
    await assert.rejects(w.open({ fingerprint: false }).locator('email', '#email').fill('x'));
    await assert.rejects(w.open({ provider: new FakeModel([]), fingerprint: false }).locator('email', '#email').fill('x'));
    await assert.rejects(w.open({ provider: new FakeModel(["//input[@id='user-email']"]), dryRun: true, fingerprint: false })
      .locator('email', '#email').fill('x'));
    await assert.rejects(w.open({ guard: () => false }).locator('email', '#email').fill('x'));
    await assert.rejects(w.open({ provider: new FakeModel(["//main", "//form"]), fingerprint: false })
      .locator('email', '#email').fill('x'));
    assert.deepEqual(declined(w.dir), [['create', 'destructive'], ['email', 'no_provider'], ['email', 'no_proposal'],
      ['email', 'dry_run'], ['email', 'guard'], ['email', 'gate']]);
    for (const e of journalOf(w.dir).filter((x) => x.event === 'heal.declined')) assert.ok(String(e.reason));
  });

  test('a page the key was never found on, and MENDWRIGHT_DISABLE=1', async () => {
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    w.page.goto(V2, 'https://shop.test/checkout');
    await assert.rejects(w.open().locator('email', '#email').fill('x'), /a change in the flow/);
    process.env.MENDWRIGHT_DISABLE = '1';
    w.page.goto(V2, 'https://shop.test/signup');
    await assert.rejects(w.open().locator('email', '#email').fill('x'));
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => e.code),
      ['page_guard', 'disabled']);
  });

  test('every code is one the Python package has, with words of its own in mendwright notify',
    { skip: needsPython() }, () => {
      const python = runPython<{ codes: string[]; phrased: string[] }>(`
import json
from mendwright.coordinator import DECLINE_CODES
from mendwright.runsummary import PHRASES
print(json.dumps({'codes': list(DECLINE_CODES), 'phrased': list(PHRASES)}))`);
      for (const code of DECLINE_CODES) {
        assert.ok(python.codes.includes(code), code);
        assert.ok(['dry_run', 'disabled'].includes(code) || python.phrased.includes(code), code);
      }
    });

  test('an interruption that never goes away writes flow.stop before the step stops', { timeout: 20_000 }, async () => {
    const w = world(V1.replace('<main>', '<main><div id="nag"><button id="nag-x">x</button></div>')
      .replace('<form id="signup">', '<form id="signup" data-fake-hidden>'));
    const heal = w.open();
    heal.interrupt('nag', '#nag-x');
    await assert.rejects(heal.locator('email', '#email').fill('x'), /3 interruptions were handled/);
    const stops = journalOf(w.dir).filter((e) => e.event === 'flow.stop');
    assert.deepEqual(stops.map((e) => [e.key, e.code, e.before]), [['nag', 'interruption_loop', 'email']]);
  });
});

describe('mendwright notify on a run of this package', { skip: needsPython() }, () => {
  test('sums up its heals and what needs a human, by its run id', async () => {
    process.env.MENDWRIGHT_RUN_ID = 'ts-run-1';
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    await w.open().locator('create', '#create').click();
    w.page.goto(V2);
    const heal = w.open();
    await heal.locator('email', '#email').fill('ada@example.com');          // healed from its fingerprint
    await assert.rejects(heal.locator('create', '#create', { destructive: true }).click());
    heal.confirmAll(true);
    const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', w.storePath,
      '--run', 'ts-run-1', '--format', 'json', '--dry-run'], {
      cwd: REPO, encoding: 'utf-8',
      env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1', MENDWRIGHT_RUN_ID: 'ts-run-1' },
    });
    assert.equal(result.status, 0, result.stderr);
    const data = JSON.parse(result.stdout);
    assert.equal(data.run, 'ts-run-1');
    assert.equal(data.repaired.total, 1);
    assert.equal(data.repaired.fingerprint, 1);
    assert.equal(data.repaired.confirmed, 1);
    assert.deepEqual(data.needs_human, [{ key: 'create', code: 'destructive', about: 'selector',
      text: 'is marked destructive, so mendwright did not repair it' }]);
    assert.ok(!result.stdout.includes('user-email'), 'a selector reached the alert');
  });
});
