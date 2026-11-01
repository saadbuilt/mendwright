// The store file: what this package reads and writes, and one store shared with the Python
// package, which these tests run from the repository: it reads a store the Python package
// wrote, writes one the Python package reads, and merges with what the Python package wrote
// in between.

import { strict as assert } from 'node:assert';
import { spawn, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, describe, test } from 'node:test';

import { StoreReadError } from '../../src/errors';
import { setLogSink } from '../../src/log';
import { SelectorStore, loadStore, mergeStatus } from '../../src/store';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

let dir: string;
let file: string;
let lines: string[];

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mw-store-'));
  file = path.join(dir, '.mendwright', 'overrides.json');
  lines = [];
  setLogSink((level, message) => lines.push(level + ': ' + message));
});

afterEach(() => {
  setLogSink(null);
  fs.rmSync(dir, { recursive: true, force: true });
});

function read(): any {
  return JSON.parse(fs.readFileSync(file, 'utf-8'));
}

describe('the store file', () => {
  test('a missing file is an empty store, created on the first write, in the Python format', () => {
    const store = loadStore(file);
    assert.deepEqual(store.candidates('email'), []);
    store.setCodeCandidates('email', ['#email']);
    const data = read();
    assert.equal(data.schema, 2);
    assert.equal(data.selectors.email.candidates[0].xpath, '#email');
    assert.equal(data.selectors.email.candidates[0].source, 'code');
    assert.match(data.selectors.email.candidates[0].addedAt, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$/);
    // Compact, with every character outside ASCII escaped, as json.dumps writes it.
    store.setMeta('email', { text: 'E-Mail-Adresse \u00fc' });
    const text = fs.readFileSync(file, 'utf-8');
    assert.ok(!text.includes(': ') && !text.includes(', '));
    assert.ok(text.includes('\\u00fc'));
    assert.ok(!fs.existsSync(file + '.lock'));
  });

  test('a file that is not valid JSON raises and is never replaced', () => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, '{"schema": 2, "selectors": {<<<<<<< HEAD');
    assert.throws(() => loadStore(file), StoreReadError);
    fs.writeFileSync(file, '{"something": "else"}');
    assert.throws(() => loadStore(file), /not a selector file/);
    assert.equal(fs.readFileSync(file, 'utf-8'), '{"something": "else"}');
  });

  test('a file that became unreadable is left alone, with one warning', () => {
    const store = loadStore(file);
    store.setCodeCandidates('email', ['#email']);
    fs.writeFileSync(file, 'not json');
    store.setCodeCandidates('email', ['#email-v2']);
    assert.equal(fs.readFileSync(file, 'utf-8'), 'not json');
    assert.equal(lines.filter((l) => l.startsWith('warning: not saving')).length, 1);
    // It keeps working in memory.
    assert.deepEqual(store.candidates('email').map((c) => c.xpath), ['#email-v2']);
  });

  test('code selectors come first, old ones are superseded, heals stay as fallbacks', () => {
    const store = loadStore(file);
    store.setCodeCandidates('save', ['#save']);
    store.addCandidate('save', "//button[@id='save-v2']");
    store.recordSuccess('save', "//button[@id='save-v2']");
    store.setCodeCandidates('save', ['#save-new', '#save-old']);
    assert.deepEqual(store.candidates('save').map((c) => c.xpath), ['#save-new', '#save-old', "//button[@id='save-v2']"]);
    assert.equal(store.status('save', '#save'), 'superseded');
    const again = loadStore(file);
    assert.equal(again.status('save', '#save'), 'superseded');
    assert.equal(again.status('save', "//button[@id='save-v2']"), 'active');
  });

  test('a quarantined candidate is never tried again', () => {
    const store = loadStore(file);
    store.setCodeCandidates('terms', ['#terms']);
    store.addCandidate('terms', "//input[@id='t2']");
    store.quarantine('terms', "//input[@id='t2']");
    assert.deepEqual(store.candidates('terms').map((c) => c.xpath), ['#terms']);
    assert.deepEqual(loadStore(file).quarantined('terms'), ["//input[@id='t2']"]);
  });

  test('a write takes in what another process wrote: its quarantine wins over a stale view', () => {
    const one = loadStore(file);
    one.setCodeCandidates('k', ['#a']);
    one.addCandidate('k', '//b');
    const two = loadStore(file);
    one.quarantine('k', '//b');
    two.recordSuccess('k', '#a');      // a write from a store that still sees //b active
    assert.equal(loadStore(file).status('k', '//b'), 'quarantined');
    assert.equal(two.status('k', '//b'), 'quarantined');
  });

  test('three-way status merge', () => {
    assert.equal(mergeStatus('active', 'quarantined', 'active'), 'quarantined');
    assert.equal(mergeStatus('quarantined', 'active', 'active'), 'quarantined');
    assert.equal(mergeStatus('superseded', 'quarantined', 'active'), 'quarantined');
    assert.equal(mergeStatus('active', 'accepted', 'proposed'), 'accepted');
    assert.equal(mergeStatus('active', 'superseded', 'superseded'), 'active');
    assert.equal(mergeStatus('active', 'superseded', 'active'), 'superseded');   // only the file changed
  });

  test('a lock held by another process makes the write wait, then give up with a warning', () => {
    const store = loadStore(file);
    store.setCodeCandidates('k', ['#a']);
    fs.writeFileSync(file + '.lock', '123');
    const before = SelectorStore.lockTimeoutMs;
    SelectorStore.lockTimeoutMs = 200;
    try {
      store.setCodeCandidates('k', ['#b']);
    } finally {
      SelectorStore.lockTimeoutMs = before;
    }
    assert.equal(read().selectors.k.candidates[0].xpath, '#a');
    assert.ok(lines.some((l) => l.includes('could not save selector changes')));
  });

  test('a stale lock is removed', () => {
    const store = loadStore(file);
    store.setCodeCandidates('k', ['#a']);
    fs.writeFileSync(file + '.lock', '123');
    const old = new Date(Date.now() - 60_000);
    fs.utimesSync(file + '.lock', old, old);
    store.setCodeCandidates('k', ['#b']);
    assert.equal(read().selectors.k.candidates.find((c: any) => c.status === 'active').xpath, '#b');
    assert.ok(lines.some((l) => l.includes('removed a stale lock file')));
  });

  test('the destructive flag of destructiveKeys cannot be cleared', () => {
    const store = loadStore(file, { destructiveKeys: ['pay'] });
    store.setCodeCandidates('pay', ['#pay']);
    store.setMeta('pay', { destructive: false });
    assert.equal(store.metaOf('pay').destructive, true);
    store.setMeta('other', { destructive: true });
    store.setMeta('other', { destructive: false });
    assert.equal(loadStore(file).metaOf('other').destructive, false);
  });
});

const PY_WRITE = `
import json, sys
import lxml.html
from mendwright.store import load_store
from mendwright.fingerprint import of_element
from mendwright.flow import page_url
args = json.load(sys.stdin)
store = load_store(None, args['file'])
root = lxml.html.fromstring('<form id="signup"><label for="email">Email</label><input id="email" name="email"></form>')
fp = of_element(root.xpath('//input')[0])
store.set_code_candidates('email', ['#email'])
store.record_success('email', '#email')
store.set_meta('email', role='input', text=['Email'], intent='the email field of the signup form', fingerprint=fp)
store.set_last_page('email', '/signup')
store.set_last_url('email', page_url('https://shop.test/signup'))
store.set_code_candidates('terms', ['#terms'])
store.add_candidate('terms', "//input[@id='terms-v2']", source='heal')
store.add_candidate('terms', "//input[@id='wrong']", source='heal')
store.quarantine('terms', "//input[@id='wrong']")
store.set_meta('terms', role='checkbox')
store.set_code_candidates('pay', ['#pay'])
store.set_meta('pay', role='button', destructive=True)
store.add_candidate('card', "xpath=//input[@id='card']", source='heal', frame_xpath="iframe[name='pay']")
store.set_code_candidates('cart', ['.cart-item'])
store.set_meta('cart', role='li', list_fingerprint={'v': 1, 'count': 2, 'items': [fp, fp]})
store.propose_list_heal('cart', "//li[@data-testid='line']", css='li.line',
                        proposal={'id': 'p1', 'at': '2026-10-01T00:00:00+00:00', 'count': 3, 'was': {'selectors': ['.cart-item']}})
print(json.dumps({'fp': fp}))
`;

const PY_READ = `
import json, sys
from mendwright.store import load_store
from mendwright.fingerprint import valid
args = json.load(sys.stdin)
store = load_store(None, args['file'])
out = {}
for key in args['keys']:
    meta = store.meta(key)
    out[key] = {
        'candidates': [[c['xpath'], c.get('source'), c.get('frameXPath')] for c in store.candidates(key)],
        'quarantined': store.quarantined(key),
        'role': meta['expectedRole'], 'text': meta['expectedText'], 'destructive': meta['destructive'],
        'intent': meta['intent'], 'fingerprint': valid(meta['fingerprint']), 'lastPage': meta['lastPage'],
        'pages': meta['pages'], 'lastUrl': store.last_url(key), 'lists': [[r['xpath'], r['status'], r.get('proposal', {}).get('id')]
                                                                           for r in store.list_heals(key)],
        'listFingerprint': meta['listFingerprint'] is not None,
    }
store.set_code_candidates('python_after', ['#again'])
print(json.dumps(out))
`;

describe('one store shared with the Python package', { skip: needsPython() }, () => {
  test('reads a store the Python package wrote', () => {
    const { fp } = runPython<{ fp: unknown }>(PY_WRITE, { file });
    const store = loadStore(file);
    assert.deepEqual(store.candidates('email').map((c) => c.xpath), ['#email']);
    const email = store.metaOf('email');
    assert.equal(email.expectedRole, 'input');
    assert.deepEqual(email.expectedText, ['Email']);
    assert.equal(email.intent, 'the email field of the signup form');
    assert.deepEqual(email.fingerprint, fp);
    assert.equal(email.lastPage, '/signup');
    assert.deepEqual(email.pages, ['/signup']);
    assert.equal(store.lastUrl('email'), 'https://shop.test/signup');
    assert.deepEqual(store.candidates('terms').map((c) => c.xpath), ['#terms', "//input[@id='terms-v2']"]);
    assert.deepEqual(store.quarantined('terms'), ["//input[@id='wrong']"]);
    assert.equal(store.metaOf('pay').destructive, true);
    assert.deepEqual(store.candidates('card').map((c) => [c.xpath, c.frameXPath]), [["xpath=//input[@id='card']", "iframe[name='pay']"]]);
    // A list heal waits for a person: it is never a candidate here.
    assert.deepEqual(store.candidates('cart').map((c) => c.xpath), ['.cart-item']);
    assert.equal(store.status('cart', "//li[@data-testid='line']"), 'proposed');
  });

  test('writes a store the Python package reads, keeping what only the Python package uses', () => {
    runPython(PY_WRITE, { file });
    const store = loadStore(file);
    store.setCodeCandidates('email', ['#email-v2']);
    store.addCandidate('email', "//input[@name='email']");
    store.recordSuccess('email', "//input[@name='email']");
    store.setMeta('email', { text: ['E-mail'] });
    store.quarantine('terms', "//input[@id='terms-v2']");
    store.setCodeCandidates('search', ['#q']);
    store.setMeta('search', { role: 'input' });
    store.setLastPage('search', '/results');
    store.setMeta('cart', { role: 'li' });     // a write that touches the list key
    const py = runPython<Record<string, any>>(PY_READ, { file, keys: ['email', 'terms', 'pay', 'card', 'cart', 'search'] });
    // A new process lists them by recency until its code passes its selectors again.
    assert.deepEqual(py.email.candidates, [["//input[@name='email']", 'heal', null], ['#email-v2', 'code', null]]);
    assert.deepEqual(py.email.text, ['E-mail']);
    assert.equal(py.email.fingerprint, true);
    assert.equal(py.email.intent, 'the email field of the signup form');
    assert.equal(py.email.lastUrl, 'https://shop.test/signup');
    assert.deepEqual(py.terms.candidates, [['#terms', 'code', null]]);
    assert.deepEqual(py.terms.quarantined.sort(), ["//input[@id='terms-v2']", "//input[@id='wrong']"]);
    assert.equal(py.pay.destructive, true);
    assert.deepEqual(py.card.candidates, [["xpath=//input[@id='card']", 'heal', "iframe[name='pay']"]]);
    assert.deepEqual(py.cart.lists, [["//li[@data-testid='line']", 'proposed', 'p1']]);
    assert.equal(py.cart.listFingerprint, true);
    assert.deepEqual(py.search.candidates, [['#q', 'code', null]]);
    assert.equal(py.search.role, 'input');
    assert.deepEqual(py.search.pages, ['/results']);
    // And the Python package wrote again on top of it.
    assert.deepEqual(loadStore(file).candidates('python_after').map((c) => c.xpath), ['#again']);
    // Its report lists what this package saved.
    const report = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'report', '--store', file],
      { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' }, encoding: 'utf-8' });
    assert.equal(report.status, 0, report.stderr);
    assert.match(report.stdout, /search\s+\(input\)/);
    assert.match(report.stdout, /active\s+code\s+#q/);
  });

  test('merges with what the Python package wrote since this store read the file', () => {
    runPython(PY_WRITE, { file });
    const store = loadStore(file);
    runPython(`
import json, sys
from mendwright.store import load_store
store = load_store(None, json.load(sys.stdin))
store.quarantine('terms', "//input[@id='terms-v2']")
store.set_last_page('email', '/account')
print('null')`, file);
    // This store still sees //input[@id='terms-v2'] active; its next write must not undo the quarantine.
    store.setCodeCandidates('email', ['#email', '#email-alt']);
    const py = runPython<Record<string, any>>(PY_READ, { file, keys: ['terms', 'email'] });
    assert.ok(py.terms.quarantined.includes("//input[@id='terms-v2']"));
    assert.deepEqual(py.email.candidates.map((c: string[]) => c[0]), ['#email', '#email-alt']);
    assert.deepEqual(py.email.pages, ['/account', '/signup']);
    assert.equal(store.status('terms', "//input[@id='terms-v2']"), 'quarantined');
  });

  test('a lock file the Python package holds is honoured', async () => {
    runPython(PY_WRITE, { file });
    const store = loadStore(file);
    const marker = path.join(dir, 'locked');
    const child = spawn(pythonCommand() as string, ['-c', `
import sys, time
from mendwright.store import _locked
with _locked(sys.argv[1], timeout=5, stale_after=30):
    open(sys.argv[2], 'w').write('locked')
    time.sleep(1.5)
`, file, marker], { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO } });
    const exited = new Promise((resolve) => child.on('exit', resolve));
    for (let i = 0; i < 200 && !fs.existsSync(marker); i++) await new Promise((r) => setTimeout(r, 25));
    assert.ok(fs.existsSync(marker), 'the Python process did not take the lock');
    const before = SelectorStore.lockTimeoutMs;
    SelectorStore.lockTimeoutMs = 300;
    try {
      store.setCodeCandidates('during', ['#during']);   // waits 300 ms for the lock, then gives up
    } finally {
      SelectorStore.lockTimeoutMs = before;
    }
    assert.ok(lines.some((l) => l.includes('could not save selector changes')));
    assert.equal(read().selectors.during, undefined);
    await exited;
    // The Python process let the lock go: this store writes again, with what it kept in memory.
    store.setCodeCandidates('later', ['#later']);
    const py = runPython<Record<string, any>>(PY_READ, { file, keys: ['later', 'during'] });
    assert.deepEqual(py.later.candidates, [['#later', 'code', null]]);
    assert.deepEqual(py.during.candidates, [['#during', 'code', null]]);
  });
});

// Since 0.10.0 the Python package saves where a key's element is when it is inside an open
// shadow root: the key's shadowHosts, and a heal made there as the chain of its hosts and a
// CSS selector, with the hosts as the candidate's shadowHosts (mendwright/shadow.py).
const HOST = 'my-app[id="root"]';
const CHAIN = HOST + ' >> [data-testid="save"]';

const PY_SHADOW = `
import json, sys
from mendwright.store import load_store
args = json.load(sys.stdin)
store = load_store(None, args['file'])
store.set_code_candidates('save', ['#save'])
store.set_meta('save', role='button', shadow_hosts=[args['host']])
store.add_candidate('save', args['chain'], source='heal', shadow_hosts=[args['host']])
store.record_success('save', args['chain'])
store.add_candidate('save', args['host'] + ' >> #wrong', source='heal', shadow_hosts=[args['host']])
store.quarantine('save', args['host'] + ' >> #wrong')
store.set_code_candidates('nowhere', ['#nowhere'])
store.set_meta('nowhere', role='button', shadow_hosts=[None])
print('null')
`;

const PY_SHADOW_READ = `
import json, sys
from mendwright.store import load_store
args = json.load(sys.stdin)
store = load_store(None, args['file'])
out = {}
for key in args['keys']:
    out[key] = {'hosts': store.shadow_hosts(key),
                'rows': [[c['xpath'], c.get('status', 'active'), c.get('shadowHosts')]
                         for c in store._data['selectors'].get(key, {}).get('candidates', [])],
                'page_quarantine': store.quarantined(key, ''),
                'shadow_quarantine': store.quarantined(key, '', [args['host']])}
print(json.dumps(out))
`;

describe('shadow roots in a store the Python package wrote', { skip: needsPython() }, () => {
  const args = () => ({ file, host: HOST, chain: CHAIN });

  test('reads where the element is, and the heals saved inside the shadow root', () => {
    runPython(PY_SHADOW, args());
    const store = loadStore(file);
    assert.deepEqual(store.shadowHosts('save'), [HOST]);
    assert.deepEqual(store.shadowHosts('nowhere'), [null]);
    const chain = store.candidates('save').find((c) => c.xpath === CHAIN);
    assert.deepEqual(chain?.shadowHosts, [HOST]);
    // A quarantine counts in its own place, as the Python package counts it.
    assert.deepEqual(store.quarantined('save', ''), []);
    assert.deepEqual(store.quarantined('save', '', [HOST]), [HOST + ' >> #wrong']);
    assert.deepEqual(store.quarantined('save'), [HOST + ' >> #wrong']);
  });

  test('keeps them when it writes, so the Python package still heals the key only inside the shadow root', () => {
    runPython(PY_SHADOW, args());
    const store = loadStore(file);
    store.setLastPage('save', '/cart');                 // writes the key
    store.recordSuccess('save', CHAIN);
    store.setCodeCandidates('other', ['#other']);
    store.setMeta('nowhere', { text: 'Nowhere' });
    const py = runPython<Record<string, any>>(PY_SHADOW_READ, { ...args(), keys: ['save', 'nowhere'] });
    assert.deepEqual(py.save.hosts, [HOST]);
    assert.deepEqual(py.nowhere.hosts, [null]);
    assert.deepEqual(py.save.rows.find((r: unknown[]) => r[0] === CHAIN), [CHAIN, 'active', [HOST]]);
    assert.deepEqual(py.save.rows.find((r: unknown[]) => r[0] === HOST + ' >> #wrong'),
      [HOST + ' >> #wrong', 'quarantined', [HOST]]);
    assert.deepEqual(py.save.page_quarantine, []);
    assert.deepEqual(py.save.shadow_quarantine, [HOST + ' >> #wrong']);
  });

  test('merges them with what the Python package wrote since it read the file', () => {
    runPython(PY_SHADOW, args());
    const store = loadStore(file);
    runPython(`
import json, sys
from mendwright.store import load_store
args = json.load(sys.stdin)
store = load_store(None, args['file'])
store.set_meta('save', shadow_hosts=[args['host'], 'inner-box'])
store.set_meta('nowhere', shadow_hosts=[])
print('null')`, args());
    store.setLastPage('save', '/cart');
    store.setLastPage('nowhere', '/cart');
    const py = runPython<Record<string, any>>(PY_SHADOW_READ, { ...args(), keys: ['save', 'nowhere'] });
    assert.deepEqual(py.save.hosts, [HOST, 'inner-box']);
    assert.deepEqual(py.nowhere.hosts, []);
    assert.deepEqual(store.shadowHosts('save'), [HOST, 'inner-box']);
    assert.deepEqual(store.shadowHosts('nowhere'), []);
  });

  test('a heal mendwright fix wrote into the code as its chain runs as the code passes it', () => {
    runPython(PY_SHADOW, args());
    const store = loadStore(file);
    store.setCodeCandidates('save', [CHAIN]);
    assert.deepEqual(store.candidates('save').find((c) => c.xpath === CHAIN)?.shadowHosts, []);
    const py = runPython<Record<string, any>>(PY_SHADOW_READ, { ...args(), keys: ['save'] });
    assert.deepEqual(py.save.rows.find((r: unknown[]) => r[0] === CHAIN), [CHAIN, 'active', null]);
  });
});

describe('shadowHosts in this package', () => {
  test('setMeta saves where the element is, and [] removes it', () => {
    const store = loadStore(file);
    store.setCodeCandidates('save', ['#save']);
    store.setMeta('save', { shadowHosts: [HOST, null] });
    assert.deepEqual(read().selectors.save.shadowHosts, [HOST, null]);
    assert.deepEqual(loadStore(file).shadowHosts('save'), [HOST, null]);
    store.setMeta('save', { shadowHosts: [] });
    assert.equal(read().selectors.save.shadowHosts, undefined);
    assert.deepEqual(loadStore(file).shadowHosts('save'), []);
    assert.throws(() => store.setMeta('save', { shadowHosts: 'my-app' as any }), TypeError);
  });

  test('a malformed value reads as a host nothing tells apart, never as the page', () => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify({ schema: 2, selectors: {
      a: { shadowHosts: 'my-app', candidates: [] },
      b: { shadowHosts: [HOST, 7, '  '], candidates: [] },
      c: { shadowHosts: [], candidates: [] } } }));
    const store = loadStore(file);
    assert.deepEqual(store.shadowHosts('a'), [null]);
    assert.deepEqual(store.shadowHosts('b'), [HOST, null, null]);
    assert.deepEqual(store.shadowHosts('c'), []);
  });
});

describe('list rows the Python package 0.10.0 wrote', { skip: needsPython() }, () => {
  test('stay as they are when this package writes, a list fingerprint taken inside a shadow root included', () => {
    runPython(`
import json, sys
import lxml.html
from mendwright.store import load_store
from mendwright.fingerprint import of_element
args = json.load(sys.stdin)
store = load_store(None, args['file'])
root = lxml.html.fromstring('<ul><li class="line">A</li><li class="line">B</li></ul>')
fp = of_element(root.xpath('//li')[0])
store.set_code_candidates('cart', ['.cart-item'])
store.set_meta('cart', role='li', list_fingerprint={'v': 1, 'count': 2, 'items': [fp, fp], 'shadow': True})
store.propose_list_heal('cart', "//li[@class='line']", css='li.line',
                        proposal={'id': 'p1', 'at': '2026-10-01T00:00:00+00:00', 'count': 2, 'was': {'selectors': ['.cart-item']}})
print('null')`, { file });
    const store = loadStore(file);
    store.setMeta('cart', { text: 'Cart' });
    store.recordSuccess('cart', '.cart-item');
    const py = runPython<Record<string, any>>(`
import json, sys
from mendwright.store import load_store
from mendwright.lists import in_shadow
store = load_store(None, json.load(sys.stdin))
meta = store.meta('cart')
print(json.dumps({'shadow': in_shadow(meta['listFingerprint']),
                  'lists': [[r['xpath'], r['status'], r.get('css'), r.get('proposal', {}).get('id')]
                            for r in store.list_heals('cart')]}))`, file);
    assert.equal(py.shadow, true);
    assert.deepEqual(py.lists, [["//li[@class='line']", 'proposed', 'li.line', 'p1']]);
    // A list heal waits for a person: never a candidate here.
    assert.deepEqual(store.candidates('cart').map((c) => c.xpath), ['.cart-item']);
  });
});
