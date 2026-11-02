// Open shadow roots in a real browser, whose CSS pierces them: where a key's element is gets
// saved as the Python package saves it, a selector saved inside a shadow root runs only there,
// and a key last found inside one heals only inside it, never onto a look-alike on the page
// around its host, in its light DOM, in a shadow root inside it or in another host. The shadow
// roots are made by the page's own script with attachShadow(), as a web component makes them.

import * as fs from 'node:fs';
import * as path from 'node:path';

import type { Page } from '@playwright/test';
import { AttentionRequired, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, journal, readStore, serve, tempStore } from './helpers';

const HOST = 'my-app[id="root"]';
const CHAIN = HOST + ' >> [data-testid="save"]';

/** A Save button with the test id 'save' that sets window.clicked to place when clicked. */
const button = (id: string, place = id) => '<button id="' + id + '" data-testid="save" type="button" '
  + 'onclick="window.clicked = \'' + place + '\'">Save</button>';

/** On the page, a look-alike with the same test id and text, and no id. */
const LOOKALIKE = '<button data-testid="save" type="button" onclick="window.clicked = \'look-alike\'">Save</button>';

/** A page with a look-alike Save button on the page, and the real one in the shadow root of
 * <my-app id="root">, with its id; light is put in the host's light DOM, shown by a slot. */
function shop(inner: string, options: { light?: string; mode?: string; hosts?: number } = {}): string {
  const host = '<my-app id="root">' + (options.light ?? '') + '</my-app>';
  return `<!doctype html><html><head><title>Shop</title></head><body>
  ${LOOKALIKE}
  ${host.repeat(options.hosts ?? 1)}
  <script>
  for (const el of document.querySelectorAll('my-app')) {
    const root = el.attachShadow({ mode: ${JSON.stringify(options.mode ?? 'open')} });
    root.innerHTML = ${JSON.stringify(inner)} + '<slot></slot>';
  }
  </script></body></html>`;
}

/** A page whose script gives hosts their shadow roots: roots are [host CSS, html, mode], each
 * host looked for in the shadow root made before it (the page for the first), or in the page
 * when its CSS starts with '/'. */
function mounted(roots: [string, string, string?][], body: string): string {
  const script = ['let scope = document, root = null;'];
  for (const [host, html, mode] of roots) {
    const where = host.startsWith('/') ? 'document' : 'scope';
    script.push('root = ' + where + '.querySelector(' + JSON.stringify(host.replace(/^\//, '')) + ').attachShadow({ mode: '
      + JSON.stringify(mode ?? 'open') + ' }); root.innerHTML = ' + JSON.stringify(html) + '; scope = root;');
  }
  return '<!doctype html><html><head><title>Shop</title></head><body>' + body + '<script>(() => {' + script.join(' ')
    + '})();</script></body></html>';
}

const inside = (id: string) => '<div class="panel">' + button(id) + '</div>';
const clicked = (page: Page) => page.evaluate(() => (window as any).clicked);

/** Open mendwright on the page served at /shop and click 'save' through #save, so its
 * fingerprint and where it is are saved. */
async function recorded(page: Page, html: string, name: string): Promise<string> {
  await serve(page, { '/shop': html });
  const store = tempStore(name);
  await open(page, { provider: null, storePath: store, timeoutMs: 300 }).locator('save', '#save', { role: 'button' }).click();
  await page.evaluate(() => { delete (window as any).clicked; });
  return store;
}

test('a key last found inside a shadow root heals there by its fingerprint, never onto the look-alike on the page', async ({ page }) => {
  const store = await recorded(page, shop(inside('save')), 'shadow-heal');
  expect(readStore(store).selectors.save.shadowHosts).toEqual([HOST]);

  await serve(page, { '/shop': shop(inside('save-2')) });
  const model = new FakeModel(["//button[@data-testid='save']"]);   // on the page, the look-alike
  const lines: string[] = [];
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300, log: (_level, message) => lines.push(message) });
  await heal.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('save-2');
  expect(heal.heals.map((h) => [h.source, h.new])).toEqual([['fingerprint', CHAIN]]);
  expect(model.bundles).toEqual([]);
  expect(readStore(store).selectors.save.candidates.find((c: any) => c.xpath === CHAIN).shadowHosts).toEqual([HOST]);
  expect(lines.filter((l) => l.startsWith('healed '))).toEqual([expect.stringMatching(
    /^healed 'save' inside the shadow root of my-app\[id="root"\]: #save -> my-app\[id="root"\] >> \[data-testid="save"\] \(fingerprint, \d+\.\ds\)$/)]);
  expect(journal(store).filter((e) => e.event === 'heal.applied').map((e) => e.shadow)).toEqual([[HOST]]);

  // The next run finds it there at once.
  await page.evaluate(() => { delete (window as any).clicked; });
  const rerun = open(page, { provider: null, storePath: store, timeoutMs: 300 });
  await rerun.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('save-2');
  expect(rerun.heals).toEqual([]);
});

test('a key last found inside a shadow root heals there with a model, which reads that shadow root only', async ({ page }) => {
  const store = await recorded(page, shop(inside('save')), 'shadow-model');
  await serve(page, { '/shop': shop(inside('save-2')) });
  const model = new FakeModel(["//button[@id='save-2']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300, fingerprint: false });
  await heal.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('save-2');
  expect(heal.heals.map((h) => [h.source, h.new])).toEqual([['model', CHAIN]]);
  const [bundle] = model.bundles;
  expect(bundle.html).toContain('save-2');
  expect(bundle.html.split('data-testid="save"').length).toBe(2);          // no look-alike from the page
  expect((bundle.inventory as { id: string }[]).map((r) => r.id)).toEqual(['save-2']);
});

// After the redesign: the renamed button in the shadow root of my-app[id="root"], and look-alikes
// with its test id and text on the page, in the host's light DOM, in a shadow root inside it,
// and in another host with the same tag.
const LOOKALIKES = mounted([
  ['my-app', inside('save-2') + '<inner-box></inner-box><slot></slot>'],
  ['inner-box', button('nested')],
  ['/my-app[id="other"]', button('sibling')],
], button('outer') + '<my-app id="root">' + button('light') + '</my-app><my-app id="other"></my-app>');

test('a heal inside a shadow root never lands on a look-alike in its light DOM, a shadow root inside it or another host',
  async ({ page }) => {
    const store = await recorded(page, shop(inside('save')), 'shadow-lookalikes');
    await serve(page, { '/shop': LOOKALIKES });
    const heal = open(page, { provider: null, storePath: store, timeoutMs: 300 });
    await heal.locator('save', '#save', { role: 'button' }).click();
    expect(await clicked(page)).toBe('save-2');
    // [data-testid="save"] also reaches the light DOM and the shadow root inside, so its id is used.
    expect(heal.heals.map((h) => [h.source, h.new])).toEqual([['fingerprint', HOST + ' >> #save-2']]);
  });

test('a model\'s look-alike outside the shadow root is refused there, and nothing is clicked', async ({ page }) => {
  const store = await recorded(page, shop(inside('save')), 'shadow-model-lookalikes');
  await serve(page, { '/shop': LOOKALIKES });
  const model = new FakeModel([{ xpath: "//button[@id='outer']", reasoning: 'look-alikes',
    fallbacks: ["//button[@id='light']", "//button[@id='nested']", "//button[@id='sibling']"] }]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300, fingerprint: false });
  const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(await clicked(page)).toBeUndefined();
  expect(heal.heals).toEqual([]);
  expect(model.bundles.length).toBe(2);
  for (const bundle of model.bundles) {
    for (const id of ['outer', 'light', 'nested', 'sibling']) expect(bundle.html).not.toContain('id="' + id + '"');
  }
});

test('a heal inside nested shadow roots stays in the inner one', async ({ page }) => {
  const nested = (id: string) => mounted([
    ['my-app', button('outer-copy', 'outer') + '<settings-panel></settings-panel>'],
    ['settings-panel', button(id, 'inner')],
  ], LOOKALIKE + '<my-app id="root"></my-app>');
  const store = await recorded(page, nested('save'), 'shadow-nested');
  expect(readStore(store).selectors.save.shadowHosts).toEqual([HOST, 'settings-panel']);
  await serve(page, { '/shop': nested('save-2') });
  const heal = open(page, { provider: null, storePath: store, timeoutMs: 300 });
  await heal.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('inner');
  expect(heal.heals.map((h) => h.new)).toEqual([HOST + ' >> settings-panel >> [data-testid="save"]']);
});

test('a test id that a shadow root inside it reuses does not hide the element', async ({ page }) => {
  // [data-testid="save"] matches 2 through the chain and says nothing of whether the renamed
  // button is visible; #save-2 does. The other Save button must not take its place.
  const app = (id: string) => mounted([
    ['my-app', button(id, 'inside') + '<button type="button" id="footer-save" class="link" '
      + 'onclick="window.clicked = \'footer\'">Save</button><save-bar></save-bar>'],
    ['save-bar', button('bar-save', 'nested')],
  ], '<my-app id="root"></my-app>');
  const store = await recorded(page, app('save'), 'shadow-reused');
  await serve(page, { '/shop': app('save-2') });
  const heal = open(page, { provider: null, storePath: store, timeoutMs: 300 });
  await heal.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('inside');
  expect(heal.heals.map((h) => h.new)).toEqual([HOST + ' >> #save-2']);
});

for (const [name, html] of [
  ['its host is gone', shop('<p>gone</p>').replace('<my-app id="root"></my-app>', '')],
  ['two hosts match', shop(inside('save-2'), { hosts: 2 })],
  ['its shadow root is closed now', shop(inside('save-2'), { mode: 'closed' })],
] as const) {
  test('a key last found inside a shadow root is refused, never healed on the page, when ' + name, async ({ page }) => {
    const store = await recorded(page, shop(inside('save')), 'shadow-refused');
    await serve(page, { '/shop': html });
    const model = new FakeModel(["//button[@data-testid='save']"]);   // would find the look-alike
    const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
    const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
    expect(error).toBeInstanceOf(AttentionRequired);
    expect(String(error.message)).toContain("'save' was last found inside the shadow root of " + HOST + ', and mendwright '
      + 'cannot enter that shadow root now: one of its hosts (' + HOST + ') is not on the page, matches more than one '
      + 'element, or has a closed shadow root.');
    expect(await clicked(page)).toBeUndefined();
    expect(model.bundles).toEqual([]);
    expect(heal.heals).toEqual([]);
    expect(journal(store).filter((e) => e.event === 'heal.declined').map((e) => e.code)).toEqual(['shadow']);
  });
}

function pythonHealed(store: string): void {
  fs.mkdirSync(path.dirname(store), { recursive: true });
  fs.writeFileSync(store, JSON.stringify({ schema: 2, selectors: { save: {
    expectedRole: 'button', shadowHosts: [HOST], candidates: [
      { xpath: '#save', source: 'code', addedAt: '2026-10-01T00:00:00+00:00',
        lastSucceededAt: '2026-10-01T00:00:00+00:00', status: 'active' },
      { xpath: CHAIN, source: 'heal', addedAt: '2026-10-02T00:00:00+00:00',
        lastSucceededAt: '2026-10-02T00:00:00+00:00', status: 'active', shadowHosts: [HOST] },
    ] } } }));
}

test('a heal the Python package saved inside a shadow root runs there', async ({ page }) => {
  await serve(page, { '/shop': shop(inside('save-2')) });
  const store = tempStore('shadow-run');
  pythonHealed(store);
  const heal = open(page, { provider: null, storePath: store, timeoutMs: 300 });
  await heal.locator('save', '#save', { role: 'button' }).click();
  expect(await clicked(page)).toBe('save-2');
  expect(readStore(store).selectors.save.candidates.find((c: any) => c.xpath === CHAIN).shadowHosts).toEqual([HOST]);
});

test('a heal saved inside a shadow root never acts outside it after a re-render', async ({ page }) => {
  await serve(page, { '/shop': shop(inside('save-2')) });
  const store = tempStore('shadow-rerender');
  pythonHealed(store);
  const box = await open(page, { provider: null, storePath: store, timeoutMs: 300 })
    .locator('save', '#save', { role: 'button' }).resolve();
  // The component re-renders: its button leaves the shadow root, and the page slots one with the
  // same test id into the host's light DOM, which Playwright's CSS reaches through the chain.
  await page.evaluate(() => {
    const host = document.querySelector('my-app') as Element;
    (host.shadowRoot as ShadowRoot).innerHTML = '<p>loading</p><slot></slot>';
    host.innerHTML = '<button data-testid="save" type="button" onclick="window.clicked = \'light\'">Save</button>';
  });
  const error = await box.click().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(String(error.message)).toContain('when it came to click()');
  expect(await clicked(page)).toBeUndefined();
});

test('a heal saved inside a shadow root answers isHidden() at once after its click closed its dialog', async ({ page }) => {
  // A close button whose click removes its dialog: Playwright says hidden at once, and so does
  // the HealLocator, instead of waiting for the element inside the shadow root.
  await serve(page, { '/shop': shop('<div id="dlg"><button id="save-2" data-testid="save" type="button" '
    + 'onclick="this.parentNode.remove()">Save</button></div>') });
  const store = tempStore('shadow-closed');
  pythonHealed(store);
  const box = open(page, { provider: null, storePath: store, timeoutMs: 3000 }).locator('save', '#save', { role: 'button' });
  let hidden: unknown;
  await box.click({ expect: async () => (hidden = await box.isHidden()) });
  expect(hidden).toBe(true);
  const started = Date.now();
  expect(await box.isHidden()).toBe(true);
  expect(await box.isVisible()).toBe(false);
  expect(Date.now() - started).toBeLessThan(1500);
  expect(readStore(store).selectors.save.candidates.find((c: any) => c.xpath === CHAIN).status).toBe('active');
});

test('a heal saved inside a shadow root matches nothing when only the light DOM of the host has it, and the heal '
  + 'inside its shadow root never reaches it', async ({ page }) => {
  await serve(page, { '/shop': shop('<p>Nothing to save</p>', {
    light: '<button data-testid="save" type="button" onclick="window.clicked = \'light\'">Save</button>' }) });
  const store = tempStore('shadow-light');
  pythonHealed(store);
  const model = new FakeModel(["//button[@data-testid='save']", "//button[@data-testid='save']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(await clicked(page)).toBeUndefined();
  expect(heal.heals).toEqual([]);
  expect(model.bundles.length).toBe(2);
  for (const bundle of model.bundles) {
    expect(bundle.html).not.toContain('data-testid');
    expect(bundle.inventory).toEqual([]);
  }
});

for (const [name, html] of [
  ['two hosts match', shop(inside('save-2'), { hosts: 2 })],
  ['its shadow root is closed now', shop(inside('save-2'), { mode: 'closed' })],
] as const) {
  test('a heal saved inside a shadow root matches nothing when ' + name + ', and the key is refused', async ({ page }) => {
    await serve(page, { '/shop': html });
    const store = tempStore('shadow-strict');
    pythonHealed(store);
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
    const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
    expect(error).toBeInstanceOf(AttentionRequired);
    expect(String(error.message)).toContain('mendwright cannot enter that shadow root now');
    expect(await clicked(page)).toBeUndefined();
    expect(model.bundles).toEqual([]);
  });
}
