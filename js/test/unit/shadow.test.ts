// Open shadow roots, on the fake page (its CSS pierces open shadow roots, as Playwright's
// does): where a key's element is gets saved as the Python package saves it, a selector saved
// inside a shadow root runs only there, and a key last found inside one heals only inside it,
// entered strictly through its hosts, never on the page around its host, where a look-alike
// would be found. Each safety rule has a test here that fails without it. The CSS forms, the
// texts, the store rows and the journal line are compared with the Python package's.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { HealCoordinator } from '../../src/coordinator';
import { cssXpath, xpathLiteral } from '../../src/css';
import { PlaywrightDriver } from '../../src/driver';
import { AttentionRequired, InvalidSelector } from '../../src/errors';
import { NOT_LIVE, liveReason } from '../../src/gate';
import { Journal } from '../../src/journal';
import { NullProvider } from '../../src/providers/base';
import { ShadowDriver, healPlace, shadowUnreachable } from '../../src/scope';
import { chainHosts, runTarget, savedHosts, splitText, textHosts } from '../../src/shadow';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, tempDir, world, type World } from '../support/env';
import { FakePage } from '../support/fakepage';
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

/** A Save button with an id, the same test id and text as the key's: a look-alike. */
const button = (id: string) => '<button id="' + id + '" data-testid="save" type="button">Save</button>';

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

/** A world on v1 where 'save' matched once through selector, so its fingerprint and where it
 * is are saved. */
async function recorded(v1 = V1, selector = '#save'): Promise<World> {
  const w = world(v1);
  await w.open().locator('save', selector, { role: 'button' }).click();
  return w;
}

const last = (w: World) => w.page.clicked[w.page.clicked.length - 1];

/** The rows of key in the store file, with their place. */
function rows(w: World, key = 'save'): Record<string, unknown>[] {
  return JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors[key].candidates;
}

const healRows = (w: World) => rows(w).filter((c) => c.source === 'heal').map((c) => [c.xpath, c.shadowHosts, c.status]);
const declines = (w: World) => journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => [e.key, e.code]);

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

describe('a key last found inside a shadow root', () => {
  test('heals inside that shadow root from its fingerprint, never onto the look-alike on the page around its host',
    async () => {
      const w = await recorded();
      w.page.goto(V2);
      const model = new FakeModel(["//button[@data-testid='save']"]);   // on the page, the look-alike
      const heal = w.open({ provider: model });
      await heal.locator('save', '#save', { role: 'button' }).click();
      assert.equal(last(w).id, 'save-2');
      assert.ok(inShadowRoot(last(w)));
      assert.deepEqual(heal.heals.map((h) => [h.source, h.old, h.new]), [['fingerprint', ['#save'], CHAIN]]);
      assert.equal(model.bundles.length, 0);
      assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'active']]);
      assert.ok(w.lines.some((l) => /^warning: healed 'save' inside the shadow root of my-app\[id="root"\]: #save -> my-app\[id="root"\] >> \[data-testid="save"\] \(fingerprint, \d+\.\ds\)$/.test(l)),
        w.lines.join('\n'));
      const applied = journalOf(w.dir).filter((e) => e.event === 'heal.applied');
      assert.deepEqual(applied.map((e) => [e.key, e.xpath, e.provider, e.source, e.shadow]),
        [['save', CHAIN, 'fingerprint', 'fingerprint', [HOST]]]);
      // The next run finds it there at once.
      const again = w.open();
      await again.locator('save', '#save', { role: 'button' }).click();
      assert.equal(last(w).id, 'save-2');
      assert.deepEqual(again.heals, []);
    });

  test('never acts outside its shadow root after a re-render that came after the heal', async () => {
    const w = await recorded();
    w.page.goto(V2);
    const box = await w.open().locator('save', '#save', { role: 'button' }).resolve();   // healed inside it
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'active']]);
    // The component re-renders: its button leaves the shadow root, and the page slots one with
    // the same test id into the host's light DOM, where Playwright's CSS reaches it.
    w.page.goto(page(shadow('<p>loading</p><slot></slot>', undefined, 'open', LOOKALIKE)));
    await assert.rejects(box.click(), /'save' was found inside the shadow root of my-app\[id="root"\], but when it came to click\(\)/);
    assert.equal(w.page.clicked.length, 1);
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'active']]);
    // While the button is inside the shadow root, the action runs.
    w.page.goto(V2);
    await box.click();
    assert.equal(last(w).id, 'save-2');
  });

  test('never heals onto a look-alike in the host\'s light DOM, which the page\'s HTML holds', async () => {
    const w = await recorded();
    // The look-alike in the light DOM scores as the renamed button does, and its own CSS form
    // also matches it: the chain pierces into the light DOM of the host.
    w.page.goto(page(shadow(SAVE_V2, undefined, 'open', button('light'))));
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(last(w).id, 'save-2');
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', HOST + ' >> #save-2']]);
  });

  test('never heals onto a look-alike in a shadow root inside it or in another host with the same tag', async () => {
    const w = await recorded();
    w.page.goto(page(shadow(SAVE_V2 + shadow(button('nested'), '<inner-box>'))
      + shadow(button('sibling'), '<my-app id="other">')));
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(last(w).id, 'save-2');
    // [data-testid="save"] also matches the button inside inner-box, so its id is used.
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', HOST + ' >> #save-2']]);
  });

  test('heals inside it with a model, which gets that shadow root\'s HTML and elements and nothing of the page',
    async () => {
      const w = await recorded();
      w.page.goto(V2);
      const model = new FakeModel(["//button[@id='save-2']"]);
      const heal = w.open({ provider: model, fingerprint: false });
      await heal.locator('save', '#save', { role: 'button' }).click();
      const [bundle] = model.bundles;
      assert.ok(bundle.html.includes('save-2'), bundle.html);
      assert.equal(bundle.html.split('data-testid="save"').length, 2, bundle.html);   // no look-alike from the page
      assert.deepEqual((bundle.inventory as { id: string }[]).map((r) => r.id), ['save-2']);
      assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['model', CHAIN]]);
      assert.equal(last(w).id, 'save-2');
      assert.ok(inShadowRoot(last(w)));
    });

  test('never heals onto a look-alike a model proposes outside the shadow root', async () => {
    const w = await recorded();
    w.page.goto(page(button('outer') + shadow(SAVE_V2 + shadow(button('nested'), '<inner-box>'), undefined, 'open',
      button('light')) + shadow(button('sibling'), '<my-app id="other">')));
    const lookalikes = ["//button[@id='outer']", "//button[@id='light']", "//button[@id='nested']",
      "//button[@id='sibling']"];
    const model = new FakeModel([{ xpath: lookalikes[0], fallbacks: lookalikes.slice(1), reasoning: 'look-alikes' }]);
    const heal = w.open({ provider: model, fingerprint: false });
    await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
      /no selector the model proposed for 'save' passed mendwright's local gate \(last reason: no match\)|the model returned no usable selector/);
    assert.equal(model.bundles.length, 2);
    assert.deepEqual([w.page.clicked.length, heal.heals.length, healRows(w)], [1, 0, []]);
  });

  test('heals inside the inner one of nested shadow roots', async () => {
    // A look-alike in the outer shadow root, and the button in the inner one.
    const nested = (inner: string) => page(LOOKALIKE + shadow(LOOKALIKE + shadow(inner, '<settings-panel>')));
    const w = await recorded(nested('<button id="save" data-testid="save" type="button">Save</button>'));
    assert.deepEqual(loadStore(w.storePath).shadowHosts('save'), [HOST, 'settings-panel']);
    w.page.goto(nested(button('save-2')));
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(last(w).id, 'save-2');
    assert.deepEqual(heal.heals.map((h) => h.new), [HOST + ' >> settings-panel >> [data-testid="save"]']);
    assert.deepEqual(healRows(w), [[HOST + ' >> settings-panel >> [data-testid="save"]', [HOST, 'settings-panel'], 'active']]);
  });

  test('reads whether an element is visible through a CSS form that counts one there, so a test id a nested shadow '
    + 'root reuses does not hide it', async () => {
    // A component inside the shadow root has a button with the same test id in its own shadow
    // root: [data-testid="save"] matches 2 through the chain, and says nothing of whether the
    // renamed button is visible; #save-2 does. The other Save button must not take its place.
    const app = (id: string) => page(shadow('<button type="button" id="' + id + '" data-testid="save">Save</button>'
      + '<button type="button" id="footer-save" class="link">Save</button>'
      + shadow('<button type="button" id="bar-save" data-testid="save">Save</button>', '<save-bar>')));
    const w = await recorded(app('save'));
    w.page.goto(app('save-2'));
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(last(w).id, 'save-2');
    assert.deepEqual(heal.heals.map((h) => h.new), [HOST + ' >> #save-2']);
  });

  for (const [name, v2] of [
    ['its host is gone', page(LOOKALIKE)],
    ['two hosts match', page(LOOKALIKE + shadow(SAVE_V2) + shadow(SAVE_V2))],
    ['its shadow root is closed now', page(LOOKALIKE + shadow(SAVE_V2, undefined, 'closed'))],
  ] as const) {
    test('is refused when ' + name + ', and never healed on the page outside it', async () => {
      const w = await recorded();
      w.page.goto(v2);
      const model = new FakeModel(["//button[@data-testid='save']"]);   // would find the look-alike
      const heal = w.open({ provider: model });
      await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(), (error: unknown) => {
        const message = (error as Error).message;
        return error instanceof AttentionRequired && message.includes('\n  -> ' + shadowUnreachable('save', [HOST]))
          && message.includes("'save' was last found inside the shadow root of " + HOST)
          && message.includes('never on the page outside it');
      });
      assert.deepEqual([model.bundles.length, heal.heals.length, w.page.clicked.length], [0, 0, 1]);
      assert.deepEqual(healRows(w), []);
      assert.deepEqual(declines(w), [['save', 'shadow']]);
    });
  }

  test('is refused on purpose, in dry run and under MENDWRIGHT_DISABLE=1, when its shadow root cannot be entered',
    async () => {
      const codes = async (options: { dryRun?: boolean }, env: Record<string, string> = {}) => {
        const w = await recorded();
        w.page.goto(page(LOOKALIKE));
        Object.assign(process.env, env);
        await assert.rejects(w.open(options).locator('save', '#save', { role: 'button' }).click(),
          /mendwright cannot enter that shadow root now/);
        cleanEnv();
        return declines(w);
      };
      assert.deepEqual(await codes({}), [['save', 'shadow']]);
      assert.deepEqual(await codes({ dryRun: true }), [['save', 'dry_run']]);
      assert.deepEqual(await codes({}, { MENDWRIGHT_DISABLE: '1' }), [['save', 'disabled']]);
    });

  test('behind a host nothing tells apart is refused, and not healed on the page either', async () => {
    const two = (inner: string) => page(LOOKALIKE + shadow(inner, '<div>') + '<div></div>');
    const w = await recorded(two(SAVE_V1));
    assert.deepEqual(loadStore(w.storePath).shadowHosts('save'), [null]);
    w.page.goto(two(SAVE_V2));
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
      /'save' was last found inside a shadow root whose host nothing tells apart from the elements around it \(it has no test id, id or name that no other element of its tag there has, and is not the only element of its custom tag\), so mendwright cannot find that shadow root again/);
    assert.deepEqual([model.bundles.length, heal.heals.length, w.page.clicked.length], [0, 0, 1]);
    assert.deepEqual(declines(w), [['save', 'shadow']]);
  });

  test('inside a shadow root in an iframe is refused, and not healed in a shadow root of the page with that host',
    async () => {
      // The page has a my-app[id="root"] of its own, with a look-alike in its shadow root.
      const w = world(page(shadow(LOOKALIKE)));
      const store = loadStore(w.storePath);
      store.setCodeCandidates('save', ['#save']);
      store.setMeta('save', { role: 'button', shadowHosts: [HOST] });
      store.addCandidate('save', "xpath=//button[@id='gone']", { frameXPath: 'iframe[id="f"]' });
      store.recordSuccess('save', "xpath=//button[@id='gone']");
      const model = new FakeModel(["//button[@data-testid='save']"]);
      const heal = w.open({ provider: model });
      await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
        /'save' was last found inside a shadow root in the iframe iframe\[id="f"\], and mendwright heals a key inside a shadow root only in the page or frame it was opened on, so it did not heal it, there or anywhere else\. Open mendwright on that frame to heal it there\./);
      assert.deepEqual([model.bundles.length, heal.heals.length, w.page.clicked.length], [0, 0, 0]);
      assert.deepEqual(declines(w), [['save', 'shadow']]);
    });

  test('an element only its text tells apart has no CSS form there, and is refused', async () => {
    const two = (cls: string) => page(shadow('<button type="button"' + cls + '>Save</button><button type="button">Cancel</button>'));
    const w = await recorded(two(' class="primary"'), '.primary');
    w.page.goto(two(''));
    await assert.rejects(w.open().locator('save', '.primary', { role: 'button' }).click(),
      /no CSS selector tells its element apart inside the shadow root \(mendwright builds one from a test id, an id, other attributes or class names, and CSS cannot match text\), and only CSS runs inside a shadow root/);
    assert.equal(w.page.clicked.length, 1);
    assert.deepEqual(healRows(w), []);
  });

  test('checks a heal on the live page inside the shadow root: a hidden element there is refused', async () => {
    const w = await recorded();
    w.page.goto(page(LOOKALIKE + shadow('<button id="save-2" data-testid="save" type="button" data-fake-hidden>Save</button>')));
    const model = new FakeModel(["//button[@id='save-2']", "//button[@id='save-2']"]);
    await assert.rejects(w.open({ provider: model, fingerprint: false }).locator('save', '#save', { role: 'button' }).click(),
      /last reason: passed static validation but not uniquely present on the live page/);
    assert.equal(w.page.clicked.length, 1);
    assert.deepEqual(healRows(w), []);
  });

  test('in dry run, the fingerprint match inside it is checked there and not applied', async () => {
    const w = await recorded();
    w.page.goto(V2);
    await assert.rejects(w.open({ dryRun: true }).locator('save', '#save', { role: 'button' }).click(),
      /dry-run mode is on, so the fingerprint match for 'save' was checked but not applied/);
    assert.ok(w.lines.some((l) => /^info: \[heal dry-run\] key=save proposed=\S+ \(fingerprint\) valid=true \(not applied\)$/.test(l)),
      w.lines.join('\n'));
    assert.deepEqual([w.page.clicked.length, healRows(w)], [1, []]);
    assert.deepEqual(declines(w), [['save', 'dry_run']]);
  });

  test('a key marked destructive is declined as destructive first', async () => {
    const w = await recorded();
    w.page.goto(V2);
    await assert.rejects(w.open().locator('save', '#save', { role: 'button', destructive: true }).click(),
      /marked as a destructive control/);
    assert.deepEqual(declines(w), [['save', 'destructive']]);
  });

  test('is never healed by a coordinator that has not entered its shadow root, in any mode', async () => {
    for (const [options, code] of [[{}, 'shadow'], [{ dryRun: true }, 'dry_run'], [{ disabled: true }, 'disabled']] as const) {
      const w = await recorded();
      w.page.goto(V2);
      const store = loadStore(w.storePath);
      const model = new FakeModel(["//button[@data-testid='save']"]);   // the look-alike on the page
      const coordinator = new HealCoordinator(store, {
        provider: model, driver: new PlaywrightDriver(w.page as never), journal: new Journal(path.join(w.dir, 'journal.jsonl')),
        fingerprint: true, log: () => undefined, ...options,
      });
      const applied = await coordinator.tryHeal('save', store.metaOf('save'), ['#save'],
        { url: w.page.url(), page: '/', lastPage: null, pages: [] });
      assert.equal(applied, false);
      assert.equal(coordinator.explain('save'), "'save' was last found inside the shadow root of " + HOST + ', and this heal '
        + 'would look for it on the page outside that shadow root, where an element that looks the same can be, so '
        + 'mendwright did not heal it. mendwright.open() heals it inside that shadow root; or fix the selector in your code.');
      assert.deepEqual([model.bundles.length, coordinator.heals.length, w.page.clicked.length], [0, 0, 1]);
      assert.deepEqual(declines(w), [['save', code]]);
    }
  });
});

describe('the driver of a heal inside a shadow root', () => {
  test('reads what the shadow root holds and nothing of the page around it, and runs CSS only', async () => {
    const w = world(page(LOOKALIKE + shadow(SAVE_V2 + shadow(button('nested'), '<inner-box>'), undefined, 'open', button('light'))));
    const inside = new ShadowDriver(new PlaywrightDriver(w.page as never), [HOST]);
    assert.equal(await inside.content(), '<html><head></head><body>' + SAVE_V2 + '<inner-box></inner-box></body></html>');
    assert.deepEqual((await inside.inventory()).map((r) => r.id), ['save-2']);
    await assert.rejects(inside.lib('collect', 'button'), TypeError);
    await assert.rejects(inside.lib('inventory', 200), TypeError);
    await assert.rejects(inside.count("//button[@id='save-2']"), InvalidSelector);
    await assert.rejects(inside.count('#save-2', 'iframe[id="f"]'), InvalidSelector);
    assert.equal(await inside.count('#save-2'), 1);
    assert.equal(await new ShadowDriver(new PlaywrightDriver(w.page as never), ['my-app[id="gone"]']).content(), '');
  });

  test('counts only an element inside the shadow root: one in its light DOM, a shadow root inside it or another host '
    + 'counts none, so the live check refuses it', async () => {
    const w = world(page(LOOKALIKE + shadow('<p>empty</p>' + shadow(button('nested'), '<inner-box>'), undefined, 'open',
      button('light')) + shadow(button('sibling'), '<my-app id="other">')));
    const inside = new ShadowDriver(new PlaywrightDriver(w.page as never), [HOST]);
    for (const css of ['#light', '#nested', '#sibling', 'button', '[data-testid="save"]']) {
      assert.equal(await inside.count(css), 0, css);
      assert.equal(await inside.isVisible(css), false, css);
      assert.equal(await liveReason(inside, css, { expectedRole: 'button' }), NOT_LIVE, css);
    }
    w.page.goto(page(shadow(SAVE_V2, undefined, 'open', button('light'))));
    // One inside and one in the light DOM: two, so not one.
    assert.equal(await inside.count('[data-testid="save"]'), 2);
    assert.equal(await inside.count('#save-2'), 1);
    assert.equal(await liveReason(inside, '#save-2', { expectedRole: 'button' }), '');
  });
});

describe('quarantines inside a shadow root', () => {
  test('a heal quarantined inside the shadow root blocks its element there under any CSS form', async () => {
    const w = await recorded();
    w.page.goto(V2);
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).resolve();
    heal.confirmAll(false);
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'quarantined']]);
    await assert.rejects(w.open().locator('save', '#save', { role: 'button' }).click(),
      /previously quarantined \(the same element as a quarantined heal; choose a different element\)/);
    // Not saved as HOST >> #save-2 or HOST >> button[...] either.
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'quarantined']]);
    assert.equal(w.page.clicked.length, 1);
  });

  test('a heal quarantined in another shadow root does not block this one', async () => {
    const w = await recorded();
    const other = 'my-app[id="other"] >> [data-testid="save"]';
    const store = loadStore(w.storePath);
    store.addCandidate('save', other, { source: 'heal', shadowHosts: ['my-app[id="other"]'] });
    store.quarantine('save', other);
    w.page.goto(V2);
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.deepEqual(heal.heals.map((h) => h.new), [CHAIN]);
    assert.equal(last(w).id, 'save-2');
  });

  test('a quarantine on the page does not block the shadow root', async () => {
    const w = await recorded();
    const store = loadStore(w.storePath);
    store.addCandidate('save', "//button[@data-testid='save']", { source: 'heal' });
    store.quarantine('save', "//button[@data-testid='save']");
    w.page.goto(V2);
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.deepEqual(heal.heals.map((h) => h.new), [CHAIN]);
    assert.equal(last(w).id, 'save-2');
  });

  test('a quarantine inside a shadow root does not block the page', async () => {
    const onPage = (id: string) => page('<div class="panel"><button id="' + id + '" data-testid="save" type="button">Save</button></div>');
    const w = await recorded(onPage('save'));
    const store = loadStore(w.storePath);
    store.addCandidate('save', CHAIN, { source: 'heal', shadowHosts: [HOST] });
    store.quarantine('save', CHAIN);
    w.page.goto(onPage('save-2'));
    const heal = w.open();
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', "//button[@data-testid='save']"]]);
    assert.equal(last(w).id, 'save-2');
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
  ] as const) {
    test('matches nothing when ' + name + ', and the heal inside its shadow root never reaches it', async () => {
      const w = world(html);
      pythonHealed(w.storePath);
      const model = new FakeModel(["//button[@data-testid='save']", "//button[@data-testid='save']"]);
      const heal = w.open({ provider: model });
      await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(), AttentionRequired);
      assert.deepEqual([w.page.clicked, heal.heals, healRows(w)], [[], [], [[CHAIN, [HOST], 'active']]]);
      // The model was asked inside the shadow root, and got nothing from outside it.
      assert.equal(model.bundles.length, 2);
      for (const bundle of model.bundles) {
        assert.ok(!bundle.html.includes('data-testid'), bundle.html);
        assert.deepEqual(bundle.inventory, []);
      }
    });
  }

  for (const [name, html] of [
    ['two hosts match', page(LOOKALIKE + shadow(SAVE_V2) + shadow(SAVE_V2))],
    ['the host is gone', page(LOOKALIKE)],
    ['its shadow root is closed now', page(LOOKALIKE + shadow(SAVE_V2, undefined, 'closed'))],
  ] as const) {
    test('matches nothing when ' + name + ', and the key is refused, not healed on the page', async () => {
      const w = world(html);
      pythonHealed(w.storePath);
      const model = new FakeModel(["//button[@data-testid='save']"]);
      const heal = w.open({ provider: model });
      await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
        (error: unknown) => error instanceof AttentionRequired
          && (error as Error).message.includes(shadowUnreachable('save', [HOST])));
      assert.deepEqual([w.page.clicked, heal.heals, model.bundles.length], [[], [], 0]);
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

  test('answers isVisible() and isHidden() at once when its element went away, as Playwright does', async () => {
    // A close button whose click removes its dialog: the element goes away on purpose.
    const closes = '<div id="dlg"><button id="save-2" data-testid="save" type="button" data-click-removes="#dlg">'
      + 'Save</button></div>';
    const w = world(page(LOOKALIKE + shadow(closes)));
    pythonHealed(w.storePath);
    const box = await w.open({ timeoutMs: 5000 }).locator('save', '#save', { role: 'button' }).resolve();
    const other = await w.open().locator('save', '#save', { role: 'button' }).resolve();   // timeoutMs: 100
    let hidden: unknown;
    await box.click({ expect: async () => (hidden = await box.isHidden()) });
    assert.equal(w.page.clicked[0].id, 'save-2');
    assert.equal(hidden, true);
    const started = Date.now();
    assert.equal(await box.isHidden(), true);
    assert.equal(await box.isVisible(), false);
    assert.ok(Date.now() - started < 1500, 'answered at once');
    assert.deepEqual(loadStore(w.storePath).candidates('save').filter((c) => c.xpath === CHAIN).map((c) => c.status),
      ['active']);
    // An action still waits for the element, and throws when it never comes back.
    await assert.rejects(other.click(), (error: unknown) => error instanceof AttentionRequired
      && /when it came to click\(\)/.test((error as Error).message));
    assert.equal(w.page.clicked.length, 1);
  });

  for (const [name, html] of [
    ['matches outside its shadow root', page(shadow('<p>loading</p><slot></slot>', undefined, 'open', LOOKALIKE))],
    ['matches several elements in it', page(shadow(SAVE_V2 + SAVE_V2))],
  ] as const) {
    test('still throws from isVisible() and isHidden() when its Locator ' + name, async () => {
      const w = world(V2);
      pythonHealed(w.storePath);
      const box = await w.open().locator('save', '#save', { role: 'button' }).resolve();
      w.page.goto(html);
      await assert.rejects(box.isHidden(), /when it came to isHidden\(\)/);
      await assert.rejects(box.isVisible(), /when it came to isVisible\(\)/);
    });
  }

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
    // The look-alike's XPath matches nothing inside the shadow root; the renamed button does.
    const model = new FakeModel(["//button[@data-testid='save']", "//button[@data-testid='save-btn']"]);
    const heal = w.open({ provider: model });
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(w.page.clicked[0].id, 'save-3');
    assert.ok(inShadowRoot(w.page.clicked[0]));
    assert.deepEqual(heal.heals.map((h) => h.new), [HOST + ' >> [data-testid="save-btn"]']);
    assert.ok(model.bundles.every((b) => !b.html.includes('data-testid="save"')));
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

  test('a chain names its hosts only in a heal saved inside a shadow root', () => {
    assert.deepEqual(chainHosts({ xpath: CHAIN, source: 'heal' }), [HOST]);
    assert.deepEqual(chainHosts({ xpath: HOST + ' >> settings-panel >> .panel > button', source: 'heal' }),
      [HOST, 'settings-panel']);
    for (const c of [
      { xpath: HOST + ' >> #save', source: 'code' },                      // your code passes it
      { xpath: HOST + ' >> #save', source: 'heal', shadowHosts: [] },     // on the page now
      { xpath: HOST + ' >> #save', source: 'heal', frameXPath: 'iframe[id="f"]' },
      { xpath: 'div >> #save', source: 'heal' },                          // not a host it builds
      { xpath: HOST + ' >> //button', source: 'heal' },
      { xpath: HOST + ' >> text=Save', source: 'heal' },
      { xpath: "//button[@id='save']", source: 'heal' },
    ]) assert.deepEqual(chainHosts(c), [], JSON.stringify(c));
  });
});

// --- parity with the Python package -------------------------------------------------------------

/** The page library in a jsdom window, whose DOMParser and XPath the helpers use. */
function lib() {
  const dom = new JSDOM('<html><body></body></html>', { runScripts: 'outside-only' });
  return dom.window.eval('(' + mendwrightLib.toString() + ')()') as ReturnType<typeof mendwrightLib>;
}

const asDoc = (inner: string) => '<html><head></head><body>' + inner + '</body></html>';

// Whole documents with no tables (the browser's parser adds a tbody, lxml's does not), and no
// form feed in an attribute (lxml's parser cuts the value there): the pages of the Python
// package's tests of css.py, and shadow roots as a heal reads them.
const CSS_PAGES = [
  `<html><body>
<form id="checkout"><label>Code <input name="coupon" id="coupon-code"></label>
  <button type="submit" class="btn primary">Apply</button></form>
<div class="row"><button class="btn">Share</button></div>
<div class="row"><button class="btn">Like</button></div>
<button data-testid="save-btn">Save</button>
<button id="a.b">Dotted</button>
<svg><g id="icon"></g></svg>
</body></html>`,
  `<html><body><div id="shop"><ul class="items">
<li><button aria-label='Say "hi"'>A</button></li><li><button title="a\\b">B</button></li></ul>
<section class="panel"><button>Buy</button></section><section class="other"><button>Buy</button></section>
<label for="line&#10;break">L</label><input placeholder="x&#13;y"><input name="f&#9;g">
<div data-qa="q1"><span>One</span></div><div data-qa="q2"><span>One</span></div></div></body></html>`,
  "<html><body><div><a href='/cart?session=abc'>Cart</a><a href='/help'>Help</a></div></body></html>",
  '<html><body><div><button id="save">Delete</button><button id="save\n">Save</button></div></body></html>',
  asDoc(SAVE_V2 + '<button type="button">Cancel</button>'),
  asDoc('<form id="signup"><label for="email">Email</label><input id="email" name="email" type="email" '
    + 'placeholder="you@example.com" autocomplete="email"><input type="checkbox" id="terms" name="terms">'
    + '<button type="submit" class="btn btn-primary">Create</button><button type="submit" class="btn">Later</button></form>'),
  asDoc('<div id="mui-generated-8842" class="css-1x2y3 card"><button id=":r1:" class="sc-bdVaJa primary-action">Go</button>'
    + '<a href="javascript:void(0)" class="link">Open</a><a href="/docs#top" title="Docs">Docs</a>'
    + '<a href="/docs" class="link nav-link">Docs</a></div>'),
  asDoc('<section id="billing"><button class="btn">Edit</button></section><section id="shipping"><button class="btn">'
    + 'Edit</button></section><nav aria-label="Main"><a href="/a">A</a></nav><nav aria-label="Footer"><a href="/a">A</a></nav>'
    + '<div name="left"><button type="button">X</button></div><div name="right"><button type="button">X</button></div>'),
  asDoc('<button type="button" name="go" title="Go">A</button><button type="submit" name="go" title="Go">B</button>'
    + '<button type="button" name="stop" title="Go">C</button><button type="button" name="go" title="Stop">D</button>'),
  asDoc('<div id="outer"><div><div><div><div><div><div><span><button class="x">Deep</button></span></div></div></div></div>'
    + '</div></div></div><div><button class="x">Other</button></div>'),
  asDoc('<settings-panel data-qa="settings"><toggle-switch role="switch" aria-label="Dark mode"></toggle-switch>'
    + '</settings-panel><img alt="Logo" src="/logo.png"><input formcontrolname="city"><input formcontrolname="city" type="text">'),
  asDoc('<BUTTON DATA-TESTID="Größe &quot;L&quot;">Größe</BUTTON><input name=\'it&#39;s\' aria-label=\'both &quot; and &#39;\'>'
    + '<button title="emoji \u{1F600} ünïcödé">E</button>'),
  asDoc('<a href="[URL]">x</a><input placeholder="[redacted email]"><button aria-label="' + 'x'.repeat(130) + '">L</button>'
    + '<button aria-label="' + 'y'.repeat(120) + '">M</button><input name="user_42"><input name="a1b2c3d4">'),
  asDoc('<div class="p-4 flex active card-body"><button class="kXhNjP btn2 Header_link__a1B2c ab primary">P</button>'
    + '<button class="btn2">Q</button></div><div class="card-body"><button class="btn2">R</button></div>'),
  asDoc('<div data-testid="row-1"><button>Open</button></div><div data-testid="row-2"><button>Open</button></div>'
    + '<div data-cy="x" data-test="y"><button data-e2e="go" data-qa="ignored">Go</button></div>'
    + '<label for="t1">T</label><input id="t1" type="checkbox"><input id="1abc"><input id="-x" name="-x">'),
];

describe('parity with the Python package', { skip: needsPython() }, () => {
  test('the CSS forms of each element, as css.css_selectors() builds them', () => {
    const python = runPython<[string, string[]][][]>(`
import json, sys
import lxml.html
from mendwright.css import css_selectors
out = []
for html in json.load(sys.stdin):
    doc = lxml.html.fromstring(html)
    tree = doc.getroottree()
    out.append([[tree.getpath(el), css_selectors(el, doc, limit=20)] for el in doc.iter() if isinstance(el.tag, str)])
print(json.dumps(out))`, CSS_PAGES);
    const page = lib();
    const differ: string[] = [];
    let forms = 0;
    CSS_PAGES.forEach((html, i) => {
      for (const [where, expected] of python[i]) {
        const found = JSON.parse(JSON.stringify(page.cssForms({ html, xpath: where, limit: 20 })));
        const inSvg = where.includes('/svg');
        if (!inSvg && found?.count !== 1) differ.push(i + ' ' + where + ': ' + JSON.stringify(found));
        const ours = found && found.count === 1 ? found.forms : [];
        if (JSON.stringify(ours) !== JSON.stringify(expected)) {
          differ.push(i + ' ' + where + ': ' + JSON.stringify(ours) + ' != ' + JSON.stringify(expected));
        }
        forms += expected.length;
      }
    });
    assert.deepEqual(differ, []);
    assert.ok(forms > 150, String(forms));
    // The default is the three most robust, as a heal uses them.
    assert.deepEqual(JSON.parse(JSON.stringify(page.cssForms({ html: CSS_PAGES[0], xpath: "//input[@name='coupon']" }))),
      { count: 1, forms: python[0].find(([where]) => where.endsWith('/input'))?.[1].slice(0, 3) });
  });

  test('the XPath twin of a CSS form, as css.css_xpath() reads it', () => {
    const forms = runPython<string[]>(`
import json, sys
import lxml.html
from mendwright.css import css_selectors
out = []
for html in json.load(sys.stdin):
    doc = lxml.html.fromstring(html)
    for el in doc.iter():
        if isinstance(el.tag, str):
            out.extend(css_selectors(el, doc, limit=20))
print(json.dumps(out))`, CSS_PAGES);
    const texts: unknown[] = [...forms, '#save', '[data-testid="save"]', 'button[type="submit"]', '.primary', 'div.panel button',
      'form#checkout button[type="submit"]', '[aria-label="Say \\"hi\\""]', '[title="a\\\\b"]', '[for="line\\a break"]',
      'button.btn.primary', '#a.b', '[id="a.b"]', '', '   ', 'div > button', 'div  button', 'button:hover', '*', '-x', '_a',
      'a-', '[data-x=unquoted]', "[x='y']", '[x="\\110000 "]', '[x="\\0 "]', '[name="it\'s"]', '[name="both \\"and\' "]',
      ' #padded ', 'BUTTON#Save', '[a:b="c"]', '[x="\\41 \\42 "]', '[x="\\d800 "]', '#-', '.9a', 'a#b.c[d="e"] f.g',
      'button ', ' button', null, 7];
    const python = runPython<(string | null | { error: true })[]>(`
import json, sys
from mendwright.css import css_xpath
out = []
for text in json.load(sys.stdin):
    try:
        out.append(css_xpath(text))
    except Exception:
        out.append({'error': True})
print(json.dumps(out))`, texts);
    const ours = texts.map((t) => {
      try {
        return cssXpath(t);
      } catch {
        return { error: true };
      }
    });
    assert.deepEqual(ours, python);
    assert.ok(forms.length > 150);
    const literals = ['plain', "it's", 'say "hi"', 'both \' and "', '', "'", '"\'"', 'a\'b"c\'d'];
    assert.deepEqual(literals.map(xpathLiteral), runPython<string[]>(`
import json, sys
from mendwright.candidates import xpath_literal
print(json.dumps([xpath_literal(v) for v in json.load(sys.stdin)]))`, literals));
  });

  test('where a key heals, and why it may not heal anywhere, as facade.heal_place() says', () => {
    const at = (t: string) => '2026-10-0' + t + 'T00:00:00+00:00';
    const row = (xpath: string, source: string, when: string | null, extra: Record<string, unknown> = {}) => ({
      xpath, source, addedAt: at('1'), lastSucceededAt: when, status: 'active', ...extra });
    const cases: { key: string; entry: Record<string, unknown> }[] = [
      { key: 'save', entry: { shadowHosts: [HOST], candidates: [row('#save', 'code', at('1'))] } },
      { key: 'save', entry: { shadowHosts: [HOST], candidates: [row('#save', 'code', at('1')),
        row(HOST + ' >> settings-panel >> #s', 'heal', at('2'), { shadowHosts: [HOST, 'settings-panel'] })] } },
      { key: 'save', entry: { candidates: [row('#save', 'code', at('1')), row(CHAIN, 'heal', at('2'))] } },
      { key: 'save', entry: { candidates: [row(CHAIN, 'heal', at('1')), row('#save', 'code', at('2'))] } },
      { key: 'save', entry: { shadowHosts: [null], candidates: [row('#save', 'code', at('1'))] } },
      { key: 'save', entry: { shadowHosts: 'x', candidates: [row('#save', 'code', at('1'))] } },
      { key: 'save', entry: { shadowHosts: [HOST, null], candidates: [row('#save', 'code', null)] } },
      { key: 'save', entry: { shadowHosts: [HOST], candidates: [row('#save', 'code', at('1')),
        row('xpath=//button', 'heal', at('2'), { frameXPath: 'iframe[id="f"]' })] } },
      { key: 'save', entry: { candidates: [row('#save', 'code', at('1')),
        row('xpath=//button', 'heal', at('2'), { frameXPath: 'iframe[id="f"]' })] } },
      { key: 'save', entry: { shadowHosts: [null], candidates: [row('xpath=//button', 'heal', at('2'),
        { frameXPath: 'iframe[name="pay"]' })] } },
      { key: 'save', entry: { candidates: [row('#save', 'code', null)] } },
      { key: "o'k \"x\"", entry: { shadowHosts: [HOST], candidates: [row('#save', 'code', null)] } },
      { key: 'save', entry: { shadowHosts: [HOST], candidates: [row('#save', 'code', at('1')),
        row(CHAIN, 'heal', at('2'), { shadowHosts: [] })] } },
      { key: 'save', entry: { shadowHosts: ['other-app'], candidates: [row('#save', 'code', at('1')),
        row(CHAIN, 'heal', at('3'), { shadowHosts: [HOST], status: 'quarantined' })] } },
    ];
    const python = runPython<[string, (string | null)[], string][]>(`
import json, os, sys, tempfile
from mendwright.facade import heal_place
from mendwright.store import load_store
out = []
for case in json.load(sys.stdin):
    path = os.path.join(tempfile.mkdtemp(), 'o.json')
    entry = dict(case['entry'], expectedRole='button')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'schema': 2, 'selectors': {case['key']: entry}}, f)
    frame, hosts, why = heal_place(load_store(None, path), case['key'])
    out.append([frame, list(hosts), why])
print(json.dumps(out))`, cases);
    const ours = cases.map((c) => {
      const file = path.join(tempDir(), 'o.json');
      fs.writeFileSync(file, JSON.stringify({ schema: 2, selectors: { [c.key]: { ...c.entry, expectedRole: 'button' } } }));
      const { frame, hosts, why } = healPlace(loadStore(file), c.key);
      return [frame, hosts, why];
    });
    assert.deepEqual(ours, python);
    assert.ok(python.some(([, , why]) => why.includes('in the iframe')) && python.some(([, , why]) => why.includes('nothing tells')));
  });

  test('why a shadow root cannot be entered, as facade.shadow_unreachable() says', () => {
    const cases: [string, string[]][] = [['save', [HOST]], ['save', [HOST, 'settings-panel', 'div[data-testid="x"]']],
      ["o'k", ['buy-box']], ['zustimmung "ja"', ['my-app[name="a\\"b"]']]];
    const python = runPython<string[]>(`
import json, sys
from mendwright.facade import shadow_unreachable
print(json.dumps([shadow_unreachable(key, hosts, True, False) for key, hosts in json.load(sys.stdin)]))`, cases);
    assert.deepEqual(cases.map(([key, hosts]) => shadowUnreachable(key, hosts)), python);
  });
});

// The Python package on its own fakes of a page with shadow roots (tests/conftest.py), which
// run its real Playwright driver: ShadowPage keeps each declarative shadow root's content in
// a document of its own, as a browser does.
const PY_SHADOW = `
import json, sys
sys.path.insert(0, 'tests')
from conftest import ShadowPage
import mendwright
from mendwright.facade import heal_place
from mendwright.journal import NullJournal
from mendwright.resolver import resolve
from mendwright.store import load_store
args = json.load(sys.stdin)

def opened(target, **kwargs):
    kwargs.setdefault('provider', None)
    kwargs.setdefault('journal', NullJournal())
    return mendwright.open(target, store_path=args['store'], timeout_ms=0, **kwargs)

def where(page_, el):
    return 'page' if page_.dom.owner[el] is page_.dom.document else 'shadow'
`;

describe('one store shared with the Python package', { skip: needsPython() }, () => {
  test('a heal this package made inside a shadow root runs there in the Python package', async () => {
    const w = await recorded();
    w.page.goto(V2);
    await w.open().locator('save', '#save', { role: 'button' }).click({ expect: () => true });
    const py = runPython<Record<string, any>>(PY_SHADOW + `
store = load_store(None, args['store'])
place = heal_place(store, 'save')
rows = [[c['xpath'], c['source'], c.get('shadowHosts'), c['status']] for c in store.candidates('save')]
page_ = ShadowPage(args['html'])
handle = resolve(store, page_, 'save', timeout_ms=0, poll_ms=0)
heal = opened(page_)
heal.locator('save', '#save', role='button', poll_ms=0).click()
el = page_.actions[-1][0]
print(json.dumps({'place': [place[0], list(place[1]), place[2]], 'rows': rows, 'handle': [[str(s) for s in handle._steps], [[e.get('id'), where(page_, e)] for e in handle._elements()]],
                  'clicked': [el.get('id'), where(page_, el)], 'heals': len(heal.heals)}))`, { store: w.storePath, html: V2 });
    assert.deepEqual(py.place, ['', [HOST], '']);
    assert.deepEqual([...py.rows].sort(), [['#save', 'code', null, 'active'], [CHAIN, 'heal', [HOST], 'active']]);
    // Its Locator: the chain of the host and the CSS, which matches the button in the shadow root.
    assert.deepEqual(py.handle, [['css=' + HOST, 'css=[data-testid="save"]'], [['save-2', 'shadow']]]);
    assert.deepEqual([py.clicked, py.heals], [['save-2', 'shadow'], 0]);
  });

  test('a key the Python package healed inside a shadow root heals inside it again here', async () => {
    const w = world(page(LOOKALIKE + shadow('<button id="save-3" data-testid="save-btn" type="button">Save</button>')));
    const py = runPython<Record<string, any>>(PY_SHADOW + `
page_ = ShadowPage(args['v1'])
opened(page_).locator('save', '#save', role='button', poll_ms=0)
page_.set_html(args['v2'])
heal = opened(page_)
heal.locator('save', '#save', role='button', poll_ms=0).click()
heal.confirm_all(True)
opened(page_).locator('save', '#save', role='button', poll_ms=0)
el = page_.actions[-1][0]
print(json.dumps({'heals': [[h.source, h.new] for h in heal.heals], 'clicked': [el.get('id'), where(page_, el)]}))`,
    { store: w.storePath, v1: V1, v2: V2 });
    assert.deepEqual(py, { heals: [['fingerprint', CHAIN]], clicked: ['save-2', 'shadow'] });
    // Here, after another redesign inside the shadow root: the look-alike on the page keeps the test id.
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await heal.locator('save', '#save', { role: 'button' }).click();
    assert.equal(last(w).id, 'save-3');
    assert.ok(inShadowRoot(last(w)));
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', HOST + ' >> [data-testid="save-btn"]']]);
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(rows(w).find((c) => c.xpath === HOST + ' >> [data-testid="save-btn"]')?.shadowHosts, [HOST]);
  });
});

describe('the journal shared with the Python package', { skip: needsPython() }, () => {
  test('a heal inside a shadow root writes the Python package\'s heal.applied line and log line, byte for byte', () => {
    const cases = [
      { key: 'save', old: ['#save'], applied: CHAIN, provider: 'fingerprint', model: null, source: 'fingerprint',
        hosts: [HOST], ms: 1234 },
      { key: 'zustimmung "ja"', old: ["//button[@aria-label='Ja – ü']", '#ja'], applied: HOST + ' >> settings-panel >> #ja',
        provider: 'AnthropicProvider', model: 'claude-x', source: 'model', hosts: [HOST, 'settings-panel'], ms: 2500 },
      { key: "o'k", old: [], applied: 'buy-box >> [aria-label="Buy \\"now\\""]', provider: 'FakeModel', model: null,
        source: 'model', hosts: ['buy-box'], ms: 0 },
      { key: 'plain', old: ['#a'], applied: "//button[@id='a']", provider: 'fingerprint', model: null,
        source: 'fingerprint', hosts: [], ms: 40 },
    ];
    const dir = tempDir();
    const file = path.join(dir, 'journal.jsonl');
    const logged: string[] = [];
    let now = 0;
    const coordinator = new HealCoordinator(loadStore(path.join(dir, 'overrides.json')), {
      provider: new NullProvider('none'), driver: new PlaywrightDriver(new FakePage('<html></html>') as never),
      journal: new Journal(file, { run: 'ts-run-1', clock: () => '2026-11-02T00:00:00+00:00' }),
      log: (_level, message) => logged.push(message), clock: () => now,
    });
    const record = (coordinator as unknown as { recordApplied: (...args: unknown[]) => void }).recordApplied;
    for (const c of cases) {
      now = c.ms;
      record.call(coordinator, c.key, c.old, c.applied, 0, c.provider, c.model, c.source, c.hosts);
    }
    const python = runPython<{ journal: string; log: string[] }>(`
import json, os, sys, tempfile
from mendwright.coordinator import HealCoordinator
from mendwright.journal import Journal
path = os.path.join(tempfile.mkdtemp(), 'journal.jsonl')
journal = Journal(path, clock=lambda: '2026-11-02T00:00:00+00:00', run='ts-run-1')
logged, now = [], [0.0]
coordinator = HealCoordinator(None, provider=object(), html_provider=lambda: '', inventory_provider=lambda: [],
                              guard=lambda: True, journal=journal, log=logged.append, clock=lambda: now[0])
for c in json.load(sys.stdin):
    now[0] = c['ms'] / 1000
    coordinator.shadow_hosts = tuple(c['hosts']) or None
    coordinator._record_applied(c['key'], c['old'], c['applied'], 0.0, c['provider'], c['model'], c['source'])
print(json.dumps({'journal': open(path, encoding='utf-8').read(), 'log': logged}))`, cases);
    assert.equal(fs.readFileSync(file, 'utf-8'), python.journal);
    assert.deepEqual(logged, python.log);
    assert.ok(python.journal.includes('"shadow":["my-app[id=\\"root\\"]"]'), python.journal);
  });
});
