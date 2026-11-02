// Where the Playwright Test fixtures keep heals. The fixtures themselves run in the browser
// tests (test/browser/fixture.spec.ts).

import { strict as assert } from 'node:assert';
import * as os from 'node:os';
import * as path from 'node:path';
import { beforeEach, test } from 'node:test';

import type { TestInfo } from '@playwright/test';

import { storePathFor } from '../../src/test';
import { cleanEnv } from '../support/env';

const CONFIG = path.resolve('project', 'playwright.config.ts');
const info = { config: { configFile: CONFIG } } as unknown as TestInfo;

beforeEach(cleanEnv);

test('the store is next to the config file, else where each option says', () => {
  assert.equal(storePathFor(info, undefined), path.join(path.dirname(CONFIG), '.mendwright', 'overrides.json'));
  assert.equal(storePathFor(info, 'heals/o.json'), path.join(path.dirname(CONFIG), 'heals', 'o.json'));
  process.env.MENDWRIGHT_STORE = 'ci/o.json';
  assert.equal(storePathFor(info, undefined), path.resolve('ci', 'o.json'));
  assert.equal(storePathFor(info, 'heals/o.json'), path.join(path.dirname(CONFIG), 'heals', 'o.json'));
  assert.equal(storePathFor(info, 'heals/o.json', 'mine/o.json'), path.resolve('mine', 'o.json'));
});

test('a leading ~ is the home directory, in MENDWRIGHT_STORE, mendwrightStore and storePath', () => {
  const home = os.homedir();
  process.env.MENDWRIGHT_STORE = '~/.cache/mendwright/overrides.json';
  assert.equal(storePathFor(info, undefined), path.join(home, '.cache', 'mendwright', 'overrides.json'));
  assert.equal(storePathFor(info, '~/heals/o.json'), path.join(home, 'heals', 'o.json'));
  assert.equal(storePathFor(info, undefined, '~/mw/o.json'), path.join(home, 'mw', 'o.json'));
});

test('a store URL, which the Python package 0.11 reads, is refused by name, never quoted', () => {
  const url = 'https://user:s3cret@stores.example/team/overrides.json';
  const refused = (fn: () => unknown, where: string, scheme: string) => assert.throws(fn, (error: Error) => {
    assert.ok(error instanceof TypeError);
    assert.ok(error.message.startsWith('mendwright: ' + where + ' is a ' + scheme + ': store URL'), error.message);
    for (const secret of ['s3cret', 'stores.example', 'bucket', 'team']) assert.ok(!error.message.includes(secret), secret);
    return true;
  });
  refused(() => storePathFor(info, undefined, 's3://bucket/team/'), 'storePath', 's3');
  refused(() => storePathFor(info, url), 'mendwrightStore', 'https');
  process.env.MENDWRIGHT_STORE = url;
  refused(() => storePathFor(info, undefined), 'MENDWRIGHT_STORE', 'https');
  // An option that comes first still wins, and a Windows path or one with ~ is a path.
  assert.equal(storePathFor(info, 'heals/o.json'), path.join(path.dirname(CONFIG), 'heals', 'o.json'));
  process.env.MENDWRIGHT_STORE = 'C:\ci\overrides.json';
  assert.doesNotThrow(() => storePathFor(info, undefined));
  process.env.MENDWRIGHT_STORE = 'memory:';
  refused(() => storePathFor(info, undefined), 'MENDWRIGHT_STORE', 'memory');
});
