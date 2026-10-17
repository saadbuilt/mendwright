// Open shadow roots in a real browser, whose CSS pierces them: where a key's element is gets
// saved as the Python package saves it, a heal the Python package saved inside a shadow root
// runs only there, and a key last found inside one is never healed onto the look-alike on the
// page around its host.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { AttentionRequired, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, readStore, serve, tempStore } from './helpers';

const HOST = 'my-app[id="root"]';
const CHAIN = HOST + ' >> [data-testid="save"]';

/** A page with a look-alike Save button on the page, and the real one in the shadow root of
 * <my-app id="root">, with its id; light is put in the host's light DOM, shown by a slot. */
function shop(inner: string, options: { light?: string; mode?: string; hosts?: number } = {}): string {
  const host = '<my-app id="root">' + (options.light ?? '') + '</my-app>';
  return `<!doctype html><html><head><title>Shop</title></head><body>
  <button data-testid="save" type="button" onclick="window.clicked = 'look-alike'">Save</button>
  ${host.repeat(options.hosts ?? 1)}
  <script>
  for (const el of document.querySelectorAll('my-app')) {
    const root = el.attachShadow({ mode: ${JSON.stringify(options.mode ?? 'open')} });
    root.innerHTML = ${JSON.stringify(inner)} + '<slot></slot>';
  }
  </script></body></html>`;
}

const inside = (id: string) => '<div class="panel"><button id="' + id + '" data-testid="save" type="button" '
  + 'onclick="window.clicked = \'' + id + '\'">Save</button></div>';

test('a match inside a shadow root saves where it is, and a renamed element is not healed onto the look-alike', async ({ page }) => {
  await serve(page, { '/shop': shop(inside('save')) });
  const store = tempStore('shadow-record');
  await open(page, { provider: null, storePath: store, timeoutMs: 300 }).locator('save', '#save', { role: 'button' }).click();
  expect(await page.evaluate(() => (window as any).clicked)).toBe('save');
  expect(readStore(store).selectors.save.shadowHosts).toEqual([HOST]);

  await serve(page, { '/shop': shop(inside('save-2')) });
  const model = new FakeModel(["//button[@data-testid='save']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(String(error.message)).toContain("'save' was last found inside the shadow root of " + HOST);
  expect(await page.evaluate(() => (window as any).clicked)).toBeUndefined();
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
});

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
  expect(await page.evaluate(() => (window as any).clicked)).toBe('save-2');
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
  expect(await page.evaluate(() => (window as any).clicked)).toBeUndefined();
});

for (const [name, html] of [
  ['only the light DOM of the host has it', shop('<p>Nothing to save</p>', {
    light: '<button data-testid="save" type="button" onclick="window.clicked = \'light\'">Save</button>' })],
  ['two hosts match', shop(inside('save-2'), { hosts: 2 })],
  ['its shadow root is closed now', shop(inside('save-2'), { mode: 'closed' })],
] as const) {
  test('a heal saved inside a shadow root matches nothing when ' + name, async ({ page }) => {
    await serve(page, { '/shop': html });
    const store = tempStore('shadow-strict');
    pythonHealed(store);
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
    const error = await heal.locator('save', '#save', { role: 'button' }).click().catch((e) => e);
    expect(error).toBeInstanceOf(AttentionRequired);
    expect(await page.evaluate(() => (window as any).clicked)).toBeUndefined();
    expect(model.bundles).toEqual([]);
  });
}
