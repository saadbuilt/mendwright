// Following a key's own XPath into an open shadow root, on the fake page, whose CSS pierces
// open shadow roots as Playwright's does and whose XPath does not: when none of a key's
// selectors matches anything on the page, and one of them matches one element in exactly one
// open shadow root, the key is looked up there from now on through a CSS form of that element,
// with no model call. Each refusal has a test here that fails without it, and the rules, the
// texts and the journal line are compared with the Python package's.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { HealCoordinator } from '../../src/coordinator';
import { PlaywrightDriver } from '../../src/driver';
import { AttentionRequired } from '../../src/errors';
import { Journal } from '../../src/journal';
import { NullProvider } from '../../src/providers/base';
import { followAllowed } from '../../src/scope';
import {
  SHADOW_HTML_BUDGET, SHADOW_ROOTS_LIMIT, rootsOf, shadowArg, shadowFound, shadowMessage, shadowPlan, type ShadowFound,
} from '../../src/shadow';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, tempDir, world, type World } from '../support/env';
import { FakePage } from '../support/fakepage';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

const HOST = 'my-app[id="root"]';
const CHAIN = HOST + ' >> [data-testid="save"]';
const XPATH = "//button[@id='save']";

function shadow(inner: string, host = '<my-app id="root">', mode = 'open', light = ''): string {
  const tag = host.slice(1).split(/[ >]/)[0];
  return host + '<template shadowrootmode="' + mode + '">' + inner + '</template>' + light + '</' + tag + '>';
}

function page(body: string): string {
  return '<html><head><title>t</title></head><body>' + body + '</body></html>';
}

const LOOKALIKE = '<button data-testid="save" type="button">Save</button>';
const SAVE_V1 = '<div class="panel"><button id="save" data-testid="save" type="button">Save</button></div>';
const V1 = page(LOOKALIKE + shadow(SAVE_V1));

function inShadowRoot(el: Element | undefined): boolean {
  const root = el?.getRootNode() as ShadowRoot | undefined;
  return !!root && root.nodeType === 11 && !!root.host;
}

const last = (w: World) => w.page.clicked[w.page.clicked.length - 1];

function rows(w: World, key = 'save'): Record<string, unknown>[] {
  return JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors[key].candidates;
}

const healRows = (w: World) => rows(w).filter((c) => c.source === 'heal').map((c) => [c.xpath, c.shadowHosts, c.status]);
const declines = (w: World) => journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => [e.key, e.code]);

beforeEach(cleanEnv);

describe('an XPath that matches only inside one open shadow root', () => {
  test('is looked up there from now on, through a CSS form of its element, with no model call', async () => {
    const w = world(V1);
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    await heal.locator('save', XPATH, { role: 'button' }).click();
    assert.equal(last(w).id, 'save');
    assert.ok(inShadowRoot(last(w)));
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(heal.heals, [{ key: 'save', old: [XPATH], new: CHAIN, confirmed: null, source: 'shadow' }]);
    // It waits for its canary like any heal: the fixture confirms it, and strict mode counts it.
    assert.deepEqual(heal.pending(), { save: CHAIN });
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'active']]);
    const events = journalOf(w.dir);
    assert.deepEqual(events.filter((e) => e.event === 'heal.applied').map(({ ts: _ts, ...e }) => e), [{
      event: 'heal.applied', key: 'save', provider: 'shadow', latency_ms: 0, xpath: CHAIN, old: [XPATH],
      source: 'shadow', shadow: [HOST] }]);
    assert.ok(!events.some((e) => e.event === 'heal.requested' || e.event === 'heal.declined'));
    assert.ok(w.lines.includes("warning: found 'save' inside the shadow root of " + HOST + ': ' + XPATH + ' matches '
      + 'nothing on the page, so it is looked up there as ' + CHAIN + ' from now on'), w.lines.join('\n'));
    // The next run finds it there at once, and its fingerprint and place are saved.
    heal.confirmAll(true);
    const next = w.open({ provider: model });
    await next.locator('save', XPATH, { role: 'button' }).click();
    assert.equal(last(w).id, 'save');
    assert.deepEqual(next.heals, []);
    assert.deepEqual(loadStore(w.storePath).shadowHosts('save'), [HOST]);
  });

  test('never acts outside its shadow root after a re-render that came after the follow', async () => {
    const w = world(V1);
    const box = await w.open().locator('save', XPATH, { role: 'button' }).resolve();   // followed into it
    assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'active']]);
    // The component re-renders: its button leaves the shadow root, and the page slots one with
    // the same test id into the host's light DOM, where Playwright's CSS reaches it.
    w.page.goto(page(shadow('<p>loading</p><slot></slot>', undefined, 'open', LOOKALIKE)));
    await assert.rejects(box.click(), /'save' was found inside the shadow root of my-app\[id="root"\], but when it came to click\(\)/);
    assert.deepEqual([w.page.clicked, healRows(w)], [[], [[CHAIN, [HOST], 'active']]]);
    // While the button is inside the shadow root, the action runs.
    w.page.goto(V1);
    await box.click();
    assert.equal(last(w).id, 'save');
  });

  test('is confirmed or quarantined by its canary, which never reports it to the provider', async () => {
    const model = new FakeModel([]);
    const w = world(V1);
    const heal = w.open({ provider: model });
    await heal.locator('save', XPATH, { role: 'button' }).click({ expect: () => true });
    assert.equal(heal.heals[0].confirmed, true);
    const other = world(V1);
    await other.open({ provider: model }).locator('save', XPATH, { role: 'button' }).click({ expect: () => false });
    assert.deepEqual(healRows(other), [[CHAIN, [HOST], 'quarantined']]);
    assert.deepEqual(model.outcomes, []);
  });

  test('after its first CSS form was quarantined there, is followed through the next one, as the Python package does',
    async () => {
      const w = world(V1);
      await w.open().locator('save', XPATH, { role: 'button' }).click({ expect: () => false });
      assert.deepEqual(healRows(w), [[CHAIN, [HOST], 'quarantined']]);
      const heal = w.open();
      await heal.locator('save', XPATH, { role: 'button' }).click();
      assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['shadow', HOST + ' >> #save']]);
      assert.equal(last(w).id, 'save');
      assert.deepEqual(healRows(w).sort(), [[HOST + ' >> #save', [HOST], 'active'], [CHAIN, [HOST], 'quarantined']]);
    });

  test('stays in the inner one of nested shadow roots, through all their hosts', async () => {
    const w = world(page(LOOKALIKE + shadow(LOOKALIKE + shadow(SAVE_V1, '<settings-panel>'))));
    const heal = w.open();
    await heal.locator('save', XPATH, { role: 'button' }).click();
    assert.equal(last(w).id, 'save');
    assert.deepEqual(heal.heals.map((h) => h.new), [HOST + ' >> settings-panel >> [data-testid="save"]']);
    assert.deepEqual(healRows(w), [[HOST + ' >> settings-panel >> [data-testid="save"]', [HOST, 'settings-panel'], 'active']]);
  });

  test('a later selector of the key is followed when the first cannot be', async () => {
    const w = world(page(shadow('<button type="button">Save</button><button type="button">Cancel</button>'
      + '<button id="save" data-testid="save" type="button">Keep</button>')));
    const heal = w.open();
    await heal.locator('save', ["//button[normalize-space()='Save']", XPATH], { role: 'button' }).click();
    assert.equal(last(w).id, 'save');
    assert.deepEqual(heal.heals.map((h) => [h.old, h.new]), [[[XPATH], CHAIN]]);
  });
});

describe('a key not followed into a shadow root, with why', () => {
  /** The AttentionRequired message of key's lookup on w's page, which must not be followed:
   * nothing is healed, saved inside a shadow root or acted on, and no model is asked. */
  async function refused(w: World, options: Parameters<World['open']>[0] = {}, selector = XPATH, role = 'button',
                         key = 'save', fresh = true): Promise<string> {
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model, ...options });
    const error = await heal.locator(key, selector, { role }).click().catch((e) => e);
    assert.ok(error instanceof AttentionRequired, String(error));
    assert.deepEqual(heal.heals, []);
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(w.page.clicked, []);
    if (fresh) assert.ok(!fs.existsSync(w.storePath) || !fs.readFileSync(w.storePath, 'utf-8').includes('shadowHosts'));
    // The reason journaled with the decline is the one explain() gives and the error ends with.
    const reasons = journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => e.reason as string);
    assert.equal(reasons.length, 1);
    assert.equal(reasons[0], heal.coordinator.explain(key));
    assert.ok(error.message.endsWith('\n  -> ' + reasons[0]), error.message);
    return error.message;
  }

  const because = (why: string, where = 'the shadow root of ' + HOST, selector = XPATH, key = 'save') => 'No matching '
    + 'element for: ' + key + ' (tried 1 candidate(s)).\n  -> \'' + key + "' is inside a shadow root: " + selector
    + ' matches nothing on the page, but matches inside ' + where + '. mendwright did not look for it there, because '
    + why + ', and did not ask a model to heal it. Fix the selector in your code.';

  test('in dry run', async () => {
    const w = world(V1);
    assert.equal(await refused(w, { dryRun: true }), because('dry-run mode is on'));
    assert.deepEqual(declines(w), [['save', 'dry_run']]);
  });

  test('under MENDWRIGHT_DISABLE=1', async () => {
    process.env.MENDWRIGHT_DISABLE = '1';
    const w = world(V1);
    assert.equal(await refused(w), because('healing is turned off by MENDWRIGHT_DISABLE=1'));
    assert.deepEqual(declines(w), [['save', 'disabled']]);
  });

  test('while a guard blocks heals', async () => {
    const w = world(V1);
    assert.equal(await refused(w, { guard: () => false }), because('a transaction guard is currently blocking heals'));
    assert.deepEqual(declines(w), [['save', 'shadow']]);
  });

  test('marked destructive', async () => {
    const w = world(V1);
    assert.equal(await refused(w, { destructiveKeys: ['save'] }), because("'save' is marked as a destructive control"));
    assert.deepEqual(declines(w), [['save', 'shadow']]);
  });

  test('when it matches in more than one shadow root', async () => {
    const w = world(page(shadow(SAVE_V1) + shadow(SAVE_V1, '<other-app>')));
    assert.equal(await refused(w), because('it matches in more than one shadow root',
      'the shadow root of ' + HOST + ' (and 1 more)'));
    assert.deepEqual(declines(w), [['save', 'shadow']]);
  });

  test('when it matches several elements there', async () => {
    const w = world(page(shadow(SAVE_V1 + SAVE_V1)));
    assert.equal(await refused(w), because('it matches 2 elements there'));
  });

  test('when nothing tells a host on the way apart', async () => {
    const w = world(page(shadow(SAVE_V1, '<div>') + '<div></div>'));
    assert.equal(await refused(w), because('nothing tells one of the shadow hosts on the way apart from the elements '
      + 'around it (no test id, id or name of its own there, and not the only element of its custom tag)',
    'the shadow root of an element nothing tells apart'));
  });

  test('when no CSS selector tells its element apart', async () => {
    const w = world(page(shadow('<button type="button">Save</button><button type="button">Cancel</button>')));
    const xpath = "//button[normalize-space()='Save']";
    assert.equal(await refused(w, {}, xpath), because('no CSS selector tells its element apart there, and only CSS '
      + 'runs inside a shadow root', undefined, xpath));
  });

  test('when that lookup was quarantined before', async () => {
    // A link that only its href tells apart there has one CSS form.
    const links = page(shadow('<a href="/help">Help</a><a href="/about">About</a>'));
    const xpath = "//a[normalize-space()='Help']";
    const w = world(links);
    await w.open().locator('help', xpath, { role: 'link' }).click({ expect: () => false });
    assert.deepEqual(rows(w, 'help').filter((c) => c.source === 'heal').map((c) => [c.xpath, c.status]),
      [[HOST + ' >> a[href="/help"]', 'quarantined']]);
    fs.rmSync(path.join(w.dir, 'journal.jsonl'));
    w.page.clicked.length = 0;
    assert.equal(await refused(w, {}, xpath, 'link', 'help', false), because('it was looked up in that shadow root before and '
      + 'an action on it failed there, so that lookup is quarantined', undefined, xpath, 'help'));
  });

  test('when that lookup was superseded before', async () => {
    const w = world(V1);
    fs.writeFileSync(w.storePath, JSON.stringify({ schema: 2, selectors: { save: { expectedRole: 'button', candidates: [
      { xpath: XPATH, source: 'code', addedAt: null, lastSucceededAt: null, status: 'active' },
      ...['[data-testid="save"]', '#save', 'button[data-testid="save"]'].map((css) => ({ xpath: HOST + ' >> ' + css,
        source: 'heal', addedAt: null, lastSucceededAt: null, status: 'superseded', shadowHosts: [HOST] })),
    ] } } }));
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    const error = await heal.locator('save', XPATH, { role: 'button' }).click().catch((e) => e);
    assert.equal(error.message, because('it was looked up in that shadow root before and that lookup was superseded'));
    assert.deepEqual([heal.heals, model.bundles.length, w.page.clicked], [[], 0, []]);
  });

  test('when the page has more shadow roots than mendwright reads', async () => {
    const many = Array.from({ length: SHADOW_ROOTS_LIMIT }, (_, i) => shadow('<p>' + i + '</p>', '<x-box id="b' + i + '">'));
    const w = world(page(shadow(SAVE_V1) + many.join('')));
    assert.equal(await refused(w), because('the page has more shadow roots than mendwright reads, so it cannot tell that '
      + 'the selector matches in that one only'));
  });

  test('on a page the key was never found on (the page guard)', async () => {
    const w = world(V1);
    loadStore(w.storePath).setLastPage('save', '/checkout');
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    const error = await heal.locator('save', XPATH, { role: 'button' }).click().catch((e) => e);
    assert.ok(error instanceof AttentionRequired);
    assert.match(error.message, /^No matching element for: save .*\n {2}-> 'save' was last found on \/checkout, and this page is \/signup\. That looks like a change in the flow/s);
    assert.deepEqual([heal.heals, model.bundles.length, w.page.clicked], [[], 0, []]);
    assert.deepEqual(declines(w), [['save', 'page_guard']]);
    assert.ok(!fs.readFileSync(w.storePath, 'utf-8').includes('shadowHosts'));
  });

  test('when its element there fails the live check', async () => {
    const hidden = world(page(shadow('<button id="save" data-testid="save" type="button" data-fake-hidden>Save</button>')));
    assert.equal(await refused(hidden), because("its element there failed mendwright's checks (passed static validation "
      + 'but not uniquely present on the live page)'));
    const role = world(V1);
    assert.equal(await refused(role, {}, XPATH, 'link'), because("its element there failed mendwright's checks (live "
      + 'element role mismatch: got button want link)'));
  });
});

describe('a key that is not followed, and heals where it was last found', () => {
  // The page's own submit button, and a newsletter component whose shadow root holds another.
  const SEND = '<form><input name="email"><button type="submit" id="send">Send</button></form>';
  const NEWSLETTER = shadow('<button type="submit" id="subscribe">Subscribe</button>', '<news-letter>');

  test('a CSS selector is never followed: Playwright\'s CSS reaches into the shadow root by itself', async () => {
    // With the component, the selector matches 2 elements, one on the page: 'send' heals on the
    // page, where it was found.
    const w = world(page(SEND));
    await w.open().locator('send', 'button[type="submit"]', { role: 'button' }).click();
    w.page.goto(page(SEND + NEWSLETTER));
    const heal = w.open();
    await heal.locator('send', 'button[type="submit"]', { role: 'button' }).click();
    assert.equal(last(w).id, 'send');
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', "//button[@id='send']"]]);
  });

  test('a key one of whose selectors matches something on the page is not followed', async () => {
    // //button[@data-testid='save'] matches two look-alikes on the page, outside the host and in
    // its light DOM, and one element in the shadow root: the key's element may be either of those two.
    const w = world(page(LOOKALIKE + shadow(SAVE_V1, undefined, 'open', LOOKALIKE)));
    const heal = w.open();
    const error = await heal.locator('save', "//button[@data-testid='save']", { role: 'button' }).click().catch((e) => e);
    assert.ok(error instanceof AttentionRequired);
    assert.ok(!error.message.includes('inside a shadow root'), error.message);
    assert.deepEqual([heal.heals, w.page.clicked], [[], []]);
  });

  test('a key last found on the page is not followed into a shadow root', async () => {
    // The page's button is no submit button now, and the component's is the only one: the
    // XPath matches nothing on the page and one element in its shadow root.
    const xpath = "//button[@type='submit']";
    const w = world(page(SEND));
    await w.open().locator('send', xpath, { role: 'button' }).click();
    w.page.goto(page(SEND.replace('type="submit"', 'type="button"') + NEWSLETTER));
    const heal = w.open();
    await heal.locator('send', xpath, { role: 'button' }).click();
    assert.equal(last(w).id, 'send');
    assert.deepEqual(heal.heals.map((h) => h.source), ['fingerprint']);
  });

  for (const [name, elsewhere] of [
    ['another host with the same tag', shadow('<div class="panel"><button id="save" data-testid="save" type="button">'
      + 'Save</button></div>', '<my-app id="other">')],
    ['a shadow root inside it', ''],
  ] as const) {
    test('a key last found in one shadow root is not followed into ' + name + ', and heals in its own', async () => {
      const w = world(V1);
      await w.open().locator('save', XPATH, { role: 'button' }).click({ expect: () => true });
      await w.open().locator('save', XPATH, { role: 'button' }).click();   // its fingerprint
      // The button is renamed in its shadow root, and a look-alike with its old id is elsewhere.
      const renamed = '<div class="panel"><button id="save-2" data-testid="save-btn" type="button">Save</button></div>';
      const nested = elsewhere ? '' : shadow(SAVE_V1, '<inner-box>');
      w.page.goto(page(LOOKALIKE + shadow(renamed + nested) + elsewhere));
      const heal = w.open();
      await heal.locator('save', XPATH, { role: 'button' }).click();
      assert.equal(last(w).id, 'save-2');
      assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', HOST + ' >> [data-testid="save-btn"]']]);
    });
  }

  test('a key last found in one shadow root is refused when only another one has it', async () => {
    const w = world(V1);
    await w.open().locator('save', XPATH, { role: 'button' }).click({ expect: () => true });
    w.page.goto(page(shadow(SAVE_V1, '<other-app>')));
    const heal = w.open();
    await assert.rejects(heal.locator('save', XPATH, { role: 'button' }).click(), /cannot enter that shadow root now/);
    assert.deepEqual([heal.heals, w.page.clicked.length], [[], 1]);
  });
});

// --- parity with the Python package -------------------------------------------------------------

/** The page library in a jsdom window, whose DOMParser and XPath the helpers use. */
function lib() {
  const dom = new JSDOM('<html><body></body></html>', { runScripts: 'outside-only' });
  return dom.window.eval('(' + mendwrightLib.toString() + ')()') as ReturnType<typeof mendwrightLib>;
}

const skip = needsPython();

describe('parity with the Python package', { skip }, () => {
  test('when a key is followed into a shadow root, and why not', () => {
    const one = (extra: Partial<ShadowFound> = {}): ShadowFound => ({ hosts: [HOST], count: 1,
      forms: ['[data-testid="save"]', '#save'], ...extra });
    type Case = { key: string; selector: string; found: ShadowFound[]; cut: boolean; disabled: boolean; dryRun: boolean;
      destructive: boolean; statuses: Record<string, string> };
    const base: Case = { key: 'save', selector: XPATH, found: [one()], cut: false, disabled: false, dryRun: false,
      destructive: false, statuses: {} };
    const cases: Case[] = [
      base, { ...base, disabled: true }, { ...base, dryRun: true }, { ...base, destructive: true },
      { ...base, disabled: true, dryRun: true, destructive: true }, { ...base, found: [one(), one({ hosts: ['other-app'] })] },
      { ...base, cut: true }, { ...base, cut: true, selector: '#save' }, { ...base, found: [one({ count: 3 })] },
      { ...base, found: [one({ count: 0 })] }, { ...base, found: [one({ hosts: [HOST, null] })] },
      { ...base, found: [one({ hosts: [null], count: 2 })] }, { ...base, found: [one({ forms: [] })] },
      { ...base, statuses: { [CHAIN]: 'quarantined' } },
      { ...base, statuses: { [CHAIN]: 'quarantined', [HOST + ' >> #save']: 'quarantined' } },
      { ...base, statuses: { [CHAIN]: 'quarantined', [HOST + ' >> #save']: 'superseded' } },
      { ...base, statuses: { [CHAIN]: 'superseded', [HOST + ' >> #save']: 'superseded' } },
      { ...base, statuses: { [CHAIN]: 'active', [HOST + ' >> #save']: 'quarantined' } },
      { ...base, statuses: { [CHAIN]: 'proposed', [HOST + ' >> #save']: 'quarantined' } },
      { ...base, found: [one({ hosts: [HOST, 'settings-panel'], forms: ['button.primary'] })],
        statuses: { [HOST + ' >> settings-panel >> button.primary']: 'quarantined' } },
      { ...base, key: "o'k", destructive: true }, { ...base, found: [one({ count: 2, forms: [] }), one()] },
    ];
    const python = runPython<[ShadowFound | null, string | null][]>(`
import json, sys
from types import SimpleNamespace
from mendwright.facade import Healer

class Store:
    def __init__(self, destructive, statuses):
        self.destructive, self.statuses = destructive, statuses
    def meta(self, key):
        return {'destructive': self.destructive}
    def status(self, key, xpath):
        return self.statuses.get(xpath)

out = []
for c in json.load(sys.stdin):
    me = SimpleNamespace(coordinator=SimpleNamespace(disabled=c['disabled'], dry_run=c['dryRun']),
                         store=Store(c['destructive'], c['statuses']))
    out.append(list(Healer._shadow_plan(me, c['key'], c['selector'], c['found'], c['cut'])))
print(json.dumps(out))`, cases);
    const ours = cases.map((c) => shadowPlan(c.key, c.selector, c.found, c.cut, { disabled: c.disabled, dryRun: c.dryRun,
      destructive: c.destructive, status: (s) => c.statuses[s] ?? null }));
    assert.deepEqual(ours, python);
    assert.deepEqual(python[0], [one(), null]);
  });

  test('why a key is not followed', () => {
    const found = (hosts: (string | null)[][]) => hosts.map((h) => ({ hosts: h, count: 1, forms: [] }));
    const cases: { key: string; selector: string; found: ShadowFound[]; why: string }[] = [
      { key: 'save', selector: XPATH, found: found([[HOST]]), why: 'dry-run mode is on' },
      { key: "o'k", selector: '#save', found: found([[HOST, 'settings-panel'], ['other-app'], ['x-y']]),
        why: 'it matches in more than one shadow root' },
      { key: 'zustimmung "ja"', selector: "//input[@aria-label='Ja – ü']", found: found([[null, 'inner-box']]),
        why: 'it matches 2 elements there' },
      { key: 'k', selector: 'xpath=//a', found: found([[null]]), why: '' },
    ];
    const python = runPython<string[]>(`
import json, sys
from mendwright.facade import _shadow_message
print(json.dumps([_shadow_message(c['key'], c['selector'], c['found'], c['why']) for c in json.load(sys.stdin)]))`,
    cases);
    assert.deepEqual(cases.map((c) => shadowMessage(c.key, c.selector, c.found, c.why)), python);
  });

  test('where a key may be followed, as facade.follow_allowed() says', () => {
    const at = (t: string) => '2026-10-0' + t + 'T00:00:00+00:00';
    const row = (xpath: string, source: string, when: string | null, extra: Record<string, unknown> = {}) => ({
      xpath, source, addedAt: at('1'), lastSucceededAt: when, status: 'active', ...extra });
    const entries: Record<string, unknown>[] = [
      { candidates: [row(XPATH, 'code', null)] },
      { candidates: [row(XPATH, 'code', at('1'))] },
      { shadowHosts: [HOST], candidates: [row(XPATH, 'code', null)] },
      { candidates: [row(XPATH, 'code', at('1')), row(CHAIN, 'heal', at('2'), { shadowHosts: [HOST] })] },
      { candidates: [row(XPATH, 'code', at('1')), row(CHAIN, 'heal', at('2'))] },
      { shadowHosts: [null], candidates: [row(XPATH, 'code', null)] },
      { candidates: [row(XPATH, 'code', at('1')), row('xpath=//b', 'heal', at('2'), { frameXPath: 'iframe[id="f"]' })] },
      { shadowHosts: [HOST, 'settings-panel'], candidates: [row(XPATH, 'code', at('1'))] },
    ];
    const founds: { hosts: (string | null)[] }[][] = [[{ hosts: [HOST] }], [{ hosts: ['other-app'] }],
      [{ hosts: [HOST, 'settings-panel'] }], [{ hosts: [HOST] }, { hosts: [HOST] }], [{ hosts: [null] }], []];
    const python = runPython<boolean[][]>(`
import json, os, sys, tempfile
from mendwright.facade import follow_allowed
from mendwright.store import load_store
data = json.load(sys.stdin)
out = []
for entry in data['entries']:
    path = os.path.join(tempfile.mkdtemp(), 'o.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'schema': 2, 'selectors': {'save': dict(entry, expectedRole='button')}}, f)
    store = load_store(None, path)
    out.append([follow_allowed(store, 'save', found) for found in data['founds']])
print(json.dumps(out))`, { entries, founds });
    const ours = entries.map((entry) => {
      const file = path.join(tempDir(), 'o.json');
      fs.writeFileSync(file, JSON.stringify({ schema: 2, selectors: { save: { ...entry, expectedRole: 'button' } } }));
      const store = loadStore(file);
      return founds.map((found) => followAllowed(store, 'save', found));
    });
    assert.deepEqual(ours, python);
    assert.ok(python.flat().includes(true) && python.flat().includes(false));
  });

  test('the shadow roots a selector matches in, and the CSS forms of its element there', async () => {
    const roots = [
      { hosts: [HOST], count: 2, html: SAVE_V1 + '<a href="/help">Help</a>' },
      { hosts: [HOST, 'settings-panel'], count: null, html: '<button id="save" type="button">Save</button><button type="button">Save</button>' },
      { hosts: ['other-app'], count: 0, html: SAVE_V1 + SAVE_V1 },
      { hosts: [null], count: 1, html: null },
      { hosts: ['x-y'], count: null, html: '<form id="f"><input name="q" placeholder="Search"><button>Go</button></form>' },
    ];
    // No (//button)[2]: jsdom's XPath reads it as //button[2], a browser's does not.
    const selectors = [XPATH, "//button[@type='button']", "//a[@href='/help']", "//input", '#save', 'css=.panel',
      "//button[normalize-space()='Go']", '//*[', 'text=Save', "xpath=//button[@data-testid='save']", '//div[2]/button'];
    const python = runPython<ShadowFound[][]>(`
import json, sys
from mendwright.facade import shadow_found
data = json.load(sys.stdin)
print(json.dumps([shadow_found(s, data['roots']) for s in data['selectors']]))`, { roots, selectors });
    const page = lib();
    const read = async (docs: string[], xpath: string) => JSON.parse(JSON.stringify(page.cssFormsEach({ htmls: docs, xpath })));
    const ours: ShadowFound[][] = [];
    for (const selector of selectors) ours.push(await shadowFound(selector, roots, read));
    assert.deepEqual(ours, python);
    assert.deepEqual(python[0].map((f) => [f.hosts, f.count, f.forms.length]),
      [[[HOST], 1, 3], [[HOST, 'settings-panel'], 1, 3], [['other-app'], 2, 0]]);
  });

  test('the page script\'s argument and result for the shadow roots of the page', () => {
    const selectors = [XPATH, '#save', 'css=#save', 'xpath=//a', 'text=Save', "role=button[name='Save']", '(//a)[1]',
      'my-app >> #save'];
    const facts = { tag: 'my-app', attrs: { id: 'root' }, counts: { tag: 2, id: 1 } };
    const raws: unknown[] = [
      { roots: [{ hosts: [facts], count: 2 }, { hosts: [facts, { tag: 'inner-box', attrs: {}, counts: { tag: 1 } }],
        html: '<b></b>' }], cut: false },
      { roots: [{ hosts: [facts], count: true, html: 7 }], cut: true },
      { roots: [{ hosts: [{ tag: 'div', attrs: {}, counts: { tag: 2 } }], count: 1.5 }], cut: 'yes' },
      { roots: [{ hosts: 'x' }] }, { roots: 'x' }, null, [], { roots: [] },
    ];
    const python = runPython<{ args: unknown[]; roots: unknown[]; selectorRaws: unknown[] }>(`
import json, sys
from mendwright.drivers.base import shadow_arg
from mendwright.shadow import host_selectors, roots_of
data = json.load(sys.stdin)
def as_selectors(raw):
    # The same read with each host as the selector this package's page script gives for it.
    if not isinstance(raw, dict) or not isinstance(raw.get('roots'), list):
        return raw
    out = []
    for item in raw['roots']:
        if isinstance(item, dict) and isinstance(item.get('hosts'), list):
            item = dict(item, hosts=host_selectors(item['hosts']))
        out.append(item)
    return dict(raw, roots=out)
roots = []
for raw in data['raws']:
    found, cut = roots_of(raw)
    roots.append([found, cut])
print(json.dumps({'args': [shadow_arg(s) for s in data['selectors']], 'roots': roots,
                  'selectorRaws': [as_selectors(raw) for raw in data['raws']]}))`, { selectors, raws });
    assert.deepEqual(selectors.map(shadowArg), python.args);
    assert.deepEqual(python.selectorRaws.map((raw) => rootsOf(raw)), python.roots);
    assert.deepEqual([SHADOW_ROOTS_LIMIT, SHADOW_HTML_BUDGET], [200, 2_000_000]);
  });
});

describe('the journal shared with the Python package', { skip }, () => {
  test('a key followed into a shadow root writes the Python package\'s heal.applied line and log line, byte for byte', () => {
    const lines = [['save', XPATH, CHAIN, [HOST]],
      ['zustimmung "ja"', "//input[@aria-label='Zustimmung – ja']", HOST + ' >> settings-panel >> [aria-label="Zustimmung – ja"]',
        [HOST, 'settings-panel']]] as const;
    const dir = tempDir();
    const file = path.join(dir, 'journal.jsonl');
    const logged: string[] = [];
    const coordinator = new HealCoordinator(loadStore(path.join(dir, 'overrides.json')), {
      provider: new NullProvider('none'), driver: new PlaywrightDriver(new FakePage('<html></html>') as never),
      journal: new Journal(file, { run: 'ts-run-1', clock: () => '2026-11-02T00:00:00+00:00' }),
      log: (_level, message) => logged.push(message),
    });
    for (const [key, old, alt, hosts] of lines) coordinator.recordShadow(key, old, alt, [...hosts]);
    const python = runPython<{ journal: string; log: string[] }>(`
import json, os, sys, tempfile
from mendwright.coordinator import HealCoordinator
from mendwright.journal import Journal
path = os.path.join(tempfile.mkdtemp(), 'journal.jsonl')
journal = Journal(path, clock=lambda: '2026-11-02T00:00:00+00:00', run='ts-run-1')
logged = []
coordinator = HealCoordinator(None, provider=object(), html_provider=lambda: '', inventory_provider=lambda: [],
                              guard=lambda: True, journal=journal, log=logged.append)
for key, old, new, hosts in json.load(sys.stdin):
    coordinator.record_shadow(key, old, new, hosts)
print(json.dumps({'journal': open(path, encoding='utf-8').read(), 'log': logged}))`, lines);
    assert.equal(fs.readFileSync(file, 'utf-8'), python.journal);
    assert.deepEqual(logged, python.log);
  });

  test('mendwright notify counts a key followed into a shadow root as found inside a shadow root', async () => {
    process.env.MENDWRIGHT_RUN_ID = 'ts-shadow-1';
    const w = world(V1);
    await w.open().locator('save', XPATH, { role: 'button' }).click({ expect: () => true });
    const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', w.storePath,
      '--run', 'ts-shadow-1', '--format', 'json', '--dry-run'], {
      cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' },
    });
    assert.equal(result.status, 0, result.stderr);
    const data = JSON.parse(result.stdout);
    assert.deepEqual([data.repaired.total, data.repaired.shadow, data.repaired.model, data.repaired.confirmed], [1, 1, 0, 1]);
    assert.deepEqual(data.needs_human, []);
  });
});

// The Python package on its own fakes of a page with shadow roots (tests/conftest.py).
const PY_SHADOW = `
import json, sys
sys.path.insert(0, 'tests')
from conftest import ShadowPage
import mendwright
from mendwright.journal import NullJournal
from mendwright.resolver import resolve
from mendwright.store import load_store
args = json.load(sys.stdin)

def where(page_, el):
    return 'page' if page_.dom.owner[el] is page_.dom.document else 'shadow'
`;

describe('one store shared with the Python package', { skip }, () => {
  test('a key this package followed into a shadow root is looked up there by the Python package', async () => {
    const w = world(V1);
    await w.open().locator('save', XPATH, { role: 'button' }).click({ expect: () => true });
    const py = runPython<Record<string, any>>(PY_SHADOW + `
store = load_store(None, args['store'])
page_ = ShadowPage(args['html'])
handle = resolve(store, page_, 'save', timeout_ms=0, poll_ms=0)
heal = mendwright.open(page_, provider=None, store_path=args['store'], timeout_ms=0, journal=NullJournal())
heal.locator('save', args['xpath'], role='button', poll_ms=0).click()
el = page_.actions[-1][0]
print(json.dumps({'handle': [str(s) for s in handle._steps], 'clicked': [el.get('id'), where(page_, el)],
                  'heals': len(heal.heals)}))`, { store: w.storePath, html: V1, xpath: XPATH });
    assert.deepEqual(py, { handle: ['css=' + HOST, 'css=[data-testid="save"]'], clicked: ['save', 'shadow'], heals: 0 });
  });

  test('a key the Python package followed into a shadow root is looked up there here', async () => {
    const w = world(V1);
    const py = runPython<Record<string, any>>(PY_SHADOW + `
page_ = ShadowPage(args['html'])
heal = mendwright.open(page_, provider=None, store_path=args['store'], timeout_ms=0, journal=NullJournal())
heal.locator('save', args['xpath'], role='button', poll_ms=0).click()
heal.confirm_all(True)
print(json.dumps({'heals': [[h.source, h.new] for h in heal.heals]}))`, { store: w.storePath, html: V1, xpath: XPATH });
    assert.deepEqual(py.heals, [['shadow', CHAIN]]);
    const heal = w.open();
    await heal.locator('save', XPATH, { role: 'button' }).click();
    assert.equal(last(w).id, 'save');
    assert.ok(inShadowRoot(last(w)));
    assert.deepEqual(heal.heals, []);
  });
});
