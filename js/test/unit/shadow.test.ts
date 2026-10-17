// Open shadow roots, on the fake page (its CSS pierces open shadow roots, as Playwright's
// does): where a key's element is gets saved as the Python package saves it, a heal the
// Python package saved inside a shadow root runs only there, and a key last found inside
// one is never healed on the page around its host, where a look-alike would be found. Each
// safety rule has a test here that fails without it.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired, InvalidSelector } from '../../src/errors';
import { runTarget, savedHosts, splitText, textHosts } from '../../src/shadow';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, world } from '../support/env';
import { needsPython, runPython } from '../support/python';

const HOST = 'my-app[id="root"]';
const CHAIN = HOST + ' >> [data-testid="save"]';

function shadow(inner: string, host = '<my-app id="root">', mode = 'open', light = ''): string {
  const tag = host.slice(1).split(/[ >]/)[0];
  return host + '<template shadowrootmode="' + mode + '">' + inner + '</template>' + light + '</' + tag + '>';
}

function page(body: string): string {
  return '<html><head><title>t</title></head><body>' + body + '</body></html>';
}

// Outside the host, on the page, a look-alike with the same test id and the same text.
const LOOKALIKE = '<button data-testid="save" type="button">Save</button>';
const SAVE_V1 = '<div class="panel"><button id="save" data-testid="save" type="button">Save</button></div>';
const SAVE_V2 = '<div class="panel"><button id="save-2" data-testid="save" type="button">Save</button></div>';
const V1 = page(LOOKALIKE + shadow(SAVE_V1));
const V2 = page(LOOKALIKE + shadow(SAVE_V2));

/** The store the Python package 0.10.0 leaves after it healed 'save' inside the shadow root. */
function pythonHealed(storePath: string): void {
  fs.writeFileSync(storePath, JSON.stringify({ schema: 2, selectors: { save: {
    expectedRole: 'button', shadowHosts: [HOST], lastPage: '/signup', pages: ['/signup'],
    candidates: [
      { xpath: '#save', source: 'code', addedAt: '2026-10-01T00:00:00+00:00',
        lastSucceededAt: '2026-10-01T00:00:00+00:00', status: 'active' },
      { xpath: CHAIN, source: 'heal', addedAt: '2026-10-02T00:00:00+00:00',
        lastSucceededAt: '2026-10-02T00:00:00+00:00', status: 'active', shadowHosts: [HOST] },
    ] } } }));
}

function inShadowRoot(el: Element | undefined): boolean {
  const root = el?.getRootNode() as ShadowRoot | undefined;
  return !!root && root.nodeType === 11 && !!root.host;
}

beforeEach(cleanEnv);

describe('where the element is', () => {
  test('a match inside a shadow root saves its hosts as the key\'s shadowHosts', async () => {
    const w = world(V1);
    await w.open().locator('save', '#save', { role: 'button' }).click();
    assert.ok(inShadowRoot(w.page.clicked[0]));
    const store = loadStore(w.storePath);
    assert.deepEqual(store.shadowHosts('save'), [HOST]);
    assert.equal(store.metaOf('save').fingerprint?.attrs?.id, 'save');
    assert.equal(JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors.save.fingerprint.shadowHosts, undefined);
  });

  test('a match on the page removes them again', async () => {
    const w = world(V1);
    await w.open().locator('save', '#save', { role: 'button' }).click();
    w.page.goto(page('<button id="save" type="button">Save</button>'));
    await w.open().locator('save', '#save', { role: 'button' }).click();
    assert.deepEqual(loadStore(w.storePath).shadowHosts('save'), []);
    assert.equal(JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors.save.shadowHosts, undefined);
  });

  test('hosts are named as the Python package names them, outermost first', async () => {
    const cases: [string, (string | null)[]][] = [
      [page(shadow(shadow(SAVE_V1, '<settings-panel>'))), [HOST, 'settings-panel']],
      [page(shadow(SAVE_V1, '<div data-testid="box">')), ['div[data-testid="box"]']],
      [page(shadow(SAVE_V1, '<buy-box>')), ['buy-box']],
      // A generated id is skipped, and a bare tag counts only for a custom element alone there.
      [page(shadow(SAVE_V1, '<div id="mui-generated-8842">')), [null]],
      [page(shadow(SAVE_V1, '<buy-box>') + '<buy-box></buy-box>'), [null]],
      [page(shadow(SAVE_V1, '<div name="a&quot;b">')), ['div[name="a\\"b"]']],
    ];
    for (const [html, hosts] of cases) {
      const w = world(html);
      await w.open().locator('save', '#save', { role: 'button' }).click();
      assert.deepEqual(loadStore(w.storePath).shadowHosts('save'), hosts, html);
    }
  });
});

describe('a heal the Python package saved inside a shadow root', () => {
  test('runs there, through its hosts, and not on the look-alike', async () => {
    const w = world(V2);
    pythonHealed(w.storePath);
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(w.page.clicked[0].id, 'save-2');
    assert.ok(inShadowRoot(w.page.clicked[0]));
    assert.deepEqual(heal.heals, []);
    assert.equal(model.bundles.length, 0);
  });

  for (const [name, html] of [
    ['only the light DOM of the host has it', page(LOOKALIKE + shadow('<p>Nothing to save</p>', undefined, 'open',
      '<button data-testid="save" type="button">Save</button>'))],
    ['it is in a shadow root inside that one', page(LOOKALIKE + shadow(shadow(SAVE_V2, '<inner-box>')))],
    ['two hosts match', page(LOOKALIKE + shadow(SAVE_V2) + shadow(SAVE_V2))],
    ['the host is gone', page(LOOKALIKE)],
    ['its shadow root is closed now', page(LOOKALIKE + shadow(SAVE_V2, undefined, 'closed'))],
  ] as const) {
    test('matches nothing when ' + name + ', and the key is not healed on the page either', async () => {
      const w = world(html);
      pythonHealed(w.storePath);
      const model = new FakeModel(["//button[@data-testid='save']"]);
      const heal = w.open({ provider: model });
      await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
        (error: unknown) => error instanceof AttentionRequired
          && (error as Error).message.includes("'save' was last found inside the shadow root of " + HOST)
          && (error as Error).message.includes('does not heal inside a shadow root'));
      assert.deepEqual(w.page.clicked, []);
      assert.equal(model.bundles.length, 0);
      assert.deepEqual(heal.heals, []);
    });
  }

  test('never acts outside its shadow root after a re-render that came after the lookup', async () => {
    const w = world(V2);
    pythonHealed(w.storePath);
    const box = await w.open().locator('save', '#save', { role: 'button' }).resolve();
    // The component re-renders: its button leaves the shadow root, and the page slots one with
    // the same test id into the host's light DOM, where Playwright's CSS reaches it.
    w.page.goto(page(shadow('<p>loading</p><slot></slot>', undefined, 'open', LOOKALIKE)));
    await assert.rejects(box.click(), /'save' was found inside the shadow root of my-app\[id="root"\], but when it came to click\(\)/);
    await assert.rejects(box.innerText(), /when it came to innerText\(\)/);
    assert.equal(w.page.clicked.length, 0);
    w.page.goto(V2);
    await box.click();
    assert.equal(w.page.clicked[0].id, 'save-2');
  });

  test('keeps to its shadow root after the Python package 0.9.0 wrote the store without its shadowHosts', async () => {
    // 0.9.0 drops every shadowHosts, the key's and the heal's. The button is renamed again,
    // and the look-alike outside the host keeps the old test id.
    const w = world(page(LOOKALIKE + shadow('<button id="save-3" data-testid="save-btn" type="button">Save</button>')));
    pythonHealed(w.storePath);
    const data = JSON.parse(fs.readFileSync(w.storePath, 'utf-8'));
    delete data.selectors.save.shadowHosts;
    for (const row of data.selectors.save.candidates) delete row.shadowHosts;
    fs.writeFileSync(w.storePath, JSON.stringify(data));
    assert.deepEqual(loadStore(w.storePath).candidates('save').map((c) => c.shadowHosts), [[HOST], undefined]);
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
      /'save' was last found inside the shadow root of my-app\[id="root"\]/);
    assert.deepEqual(w.page.clicked, []);
    assert.equal(model.bundles.length, 0);
  });

  test('reads the hosts back from a heal\'s text as the Python package does', { skip: needsPython() }, () => {
    const texts = [CHAIN, HOST + ' >> settings-panel >> #save', 'div[data-testid="a\\"b"] >> button.primary',
      'div >> #save', 'my-app[class="x"] >> #save', HOST + ' >> //button', HOST + ' >> xpath=//button',
      'text=Save >> nth=0', 'css=my-app >> #save', '#save', "//button[@id='save']", 'Foo-bar >> #x'];
    const python = runPython<string[][]>('import json, sys\nfrom mendwright.shadow import text_hosts\n'
      + 'print(json.dumps([list(text_hosts(t)) for t in json.load(sys.stdin)]))', texts);
    assert.deepEqual(texts.map(textHosts), python);
    assert.deepEqual(python.slice(0, 3), [[HOST], [HOST, 'settings-panel'], ['div[data-testid="a\\"b"]']]);
  });

  test('one behind a host nothing tells apart, or inside an iframe, cannot run', () => {
    const base = { xpath: CHAIN, source: 'heal', status: 'active' };
    assert.throws(() => runTarget({ ...base, shadowHosts: [null] }), InvalidSelector);
    assert.throws(() => runTarget({ ...base, shadowHosts: [HOST], frameXPath: 'iframe[name="pay"]' }), InvalidSelector);
    assert.throws(() => runTarget({ ...base, xpath: 'other-app >> #save', shadowHosts: [HOST] }), /not the chain/);
    assert.throws(() => runTarget({ ...base, xpath: HOST + " >> xpath=//button", shadowHosts: [HOST] }), /not a CSS/);
    assert.deepEqual(runTarget({ ...base, shadowHosts: [HOST] }), { selector: '[data-testid="save"]', frame: null, hosts: [HOST] });
    assert.deepEqual(runTarget({ xpath: '#save' }), { selector: '#save', frame: null, hosts: [] });
    assert.deepEqual(splitText('a[x=" >> "] >> b'), ['a[x=" >> "]', 'b']);
    assert.deepEqual(savedHosts('x'), [null]);
  });
});

describe('a key last found inside a shadow root', () => {
  test('is never healed on the page around its host, by the fingerprint or a model', async () => {
    const w = world(V1);
    await w.open().locator('save', '#save', { role: 'button' }).click();
    w.page.goto(V2);
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
      /'save' was last found inside the shadow root of my-app\[id="root"\], and mendwright for TypeScript does not heal inside a shadow root yet/);
    assert.equal(w.page.clicked.length, 1);           // the first run's click, nothing since
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(heal.heals, []);
    assert.ok(!journalOf(w.dir).some((e) => e.event === 'heal.applied' || e.event === 'heal.requested'));
  });

  test('a key marked destructive is declined as destructive first', async () => {
    const w = world(V1);
    await w.open().locator('save', '#save', { role: 'button' }).click();
    w.page.goto(V2);
    await assert.rejects(w.open().locator('save', '#save', { role: 'button', destructive: true }).click(),
      /marked as a destructive control/);
  });
});
