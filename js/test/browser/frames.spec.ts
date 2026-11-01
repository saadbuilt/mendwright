// Iframes one level down in a real browser: a key whose own selector matches only inside an
// iframe is followed there with no model call, a key last found inside an iframe heals inside
// it (from its fingerprint, or by a model that reads the iframe's HTML) and never onto a
// look-alike on the page around it, a follow into two iframes or a nested one is refused, and
// mendwright opened on a Frame heals inside that frame. The iframes' documents are served by
// page.route(), as the pages are.

import { AttentionRequired, open } from 'mendwright';
import { expect, test } from 'mendwright/test';
import type { Page } from '@playwright/test';

import { FakeModel, journal, readStore, serve, tempStore } from './helpers';

const FRAME = 'iframe[name="consent"]';

function consent(id = 'inner-terms', name = 'agree'): string {
  return `<!doctype html><html><head><title>Consent</title></head><body><form id="agreement">
  <label><input type="checkbox" id="${id}" name="${name}"> I agree</label></form></body></html>`;
}

/** The sign-up page with the iframe consent (served at /consent), and before it, extra. */
function signup(extra = '', frame = '<iframe name="consent" src="/consent" width="600" height="160"></iframe>'): string {
  return `<!doctype html><html><head><title>Sign up</title></head><body><main><h1>Sign up</h1>
  ${extra}${frame}</main></body></html>`;
}

// On the page around the iframe: a checkbox with the same name and label.
const LOOKALIKE = '<label><input type="checkbox" id="outer-agree" name="agree"> I agree</label>';

/** Serve the sign-up page with consent as the iframe's document, go there, and wait for the
 * iframe's checkbox: a miss looks into the frames the page has then. */
async function load(page: Page, inner: string, extra = '', id = 'inner-terms'): Promise<void> {
  await serve(page, { '/signup': signup(extra), '/consent': inner }, '/signup');
  await page.frameLocator(FRAME).locator('#' + id).waitFor({ state: 'attached' });
}

function row(store: string, key: string, xpath: string): any {
  return readStore(store).selectors[key].candidates.find((c: any) => c.xpath === xpath);
}

test('a selector that matches only inside an iframe is followed there, with no model call', async ({ page }) => {
  await load(page, consent());
  const store = tempStore('frame-follow');
  const model = new FakeModel([]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 500 });
  await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
  await expect(page.frameLocator(FRAME).locator('#inner-terms')).toBeChecked();
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([{ key: 'terms', old: ['#inner-terms'], new: 'css=#inner-terms', confirmed: null,
    source: 'frame' }]);
  expect(row(store, 'terms', 'css=#inner-terms').frameXPath).toBe(FRAME);
  expect(journal(store).filter((e) => e.event === 'heal.applied')).toMatchObject([{ key: 'terms', provider: 'frame',
    latency_ms: 0, xpath: 'css=#inner-terms', old: ['#inner-terms'], source: 'frame', frame: FRAME }]);
  // The next run finds it there at once.
  const again = open(page, { provider: model, storePath: store, timeoutMs: 500 });
  await again.locator('terms', '#inner-terms', { role: 'checkbox' }).uncheck();
  await expect(page.frameLocator(FRAME).locator('#inner-terms')).not.toBeChecked();
  expect(again.heals).toEqual([]);
});

test('an iframe nothing but its src tells apart is saved by its src, without the query', async ({ page }) => {
  await serve(page, { '/signup': signup('', '<iframe src="/consent?token=abc123" width="600" height="160"></iframe>'),
    '/consent': consent() }, '/signup');
  await page.frameLocator('iframe').locator('#inner-terms').waitFor({ state: 'attached' });
  const store = tempStore('frame-src');
  const heal = open(page, { provider: new FakeModel([]), storePath: store, timeoutMs: 500 });
  await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
  expect(row(store, 'terms', 'css=#inner-terms').frameXPath).toBe('iframe[src^="/consent?"]');
  expect(JSON.stringify(readStore(store))).not.toContain('token');
});

for (const fingerprint of [true, false]) {
  test('a key last found inside an iframe heals inside it ' + (fingerprint ? 'from its fingerprint' : 'by a model')
    + ', and never onto a look-alike on the page around it', async ({ page }) => {
    await load(page, consent(), LOOKALIKE);
    const store = tempStore('frame-heal');
    const first = open(page, { provider: new FakeModel([]), storePath: store, timeoutMs: 500, fingerprint });
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: async () => true });
    // Once the move into the iframe is confirmed, the next match records the fingerprint there.
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).resolve();
    if (fingerprint) expect(readStore(store).selectors.terms.fingerprint.attrs.id).toBe('inner-terms');

    await load(page, consent('terms-v2'), LOOKALIKE, 'terms-v2');   // the redesign, inside the iframe
    const model = new FakeModel(["//input[@name='agree']"]);
    const heal = open(page, { provider: model, storePath: store, timeoutMs: 500, fingerprint });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    await expect(page.frameLocator(FRAME).locator('#terms-v2')).toBeChecked();
    await expect(page.locator('#outer-agree')).not.toBeChecked();
    const [event] = heal.heals;
    expect(event.source).toBe(fingerprint ? 'fingerprint' : 'model');
    expect(event.new).toBe(fingerprint ? "xpath=//input[@id='terms-v2']" : "xpath=//input[@name='agree']");
    expect(row(store, 'terms', event.new).frameXPath).toBe(FRAME);
    if (fingerprint) {
      expect(model.bundles).toEqual([]);
    } else {
      expect(model.bundles[0].html).toContain('terms-v2');        // the iframe's HTML
      expect(model.bundles[0].html).not.toContain('Sign up');     // not the page's
      expect(model.bundles[0].html).not.toContain('outer-agree');
    }
  });
}

test('a selector that matches in two iframes is not followed, and says so', async ({ page }) => {
  await serve(page, { '/signup': signup('', '<iframe name="consent" src="/consent" width="600" height="160"></iframe>'
    + '<iframe name="other" src="/consent" width="600" height="160"></iframe>'), '/consent': consent() }, '/signup');
  for (const name of ['consent', 'other']) {
    await page.frameLocator('iframe[name="' + name + '"]').locator('#inner-terms').waitFor({ state: 'attached' });
  }
  const store = tempStore('frame-two');
  const model = new FakeModel(["//input[@name='agree']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 500 });
  const error = await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(String(error.message)).toContain("matches inside the iframe named 'consent' (and 1 more).");
  expect(String(error.message)).toContain('because it matches in more than one iframe,');
  expect(String(error.message)).toContain("heal = open(page.frame({ name: 'consent' })).");
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
  expect(journal(store).filter((e) => e.event === 'heal.declined').map((e) => e.code)).toEqual(['frame']);
});

/** The sign-up page with the iframe outer, whose document has the iframe consent. */
async function nested(page: Page): Promise<void> {
  await serve(page, {
    '/signup': signup('', '<iframe name="outer" src="/outer" width="700" height="260"></iframe>'),
    '/outer': '<!doctype html><html><body><p>Outer frame</p><iframe name="consent" src="/consent"></iframe></body></html>',
    '/consent': consent(),
  }, '/signup');
  await page.frameLocator('iframe[name="outer"]').frameLocator(FRAME).locator('#inner-terms')
    .waitFor({ state: 'attached' });
}

test('a selector that matches only in an iframe inside another iframe is not followed, and says so', async ({ page }) => {
  await nested(page);
  const model = new FakeModel(["//input[@name='agree']"]);
  const heal = open(page, { provider: model, storePath: tempStore('frame-nested'), timeoutMs: 500 });
  const error = await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check().catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(String(error.message)).toContain("matches inside the iframe named 'consent'. mendwright did not look for it "
    + 'there, because that iframe is inside another iframe,');
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
});

test('mendwright opened on a Frame heals inside that frame, an iframe deeper down too', async ({ page }) => {
  await nested(page);
  const frame = page.frame({ name: 'consent' });
  expect(frame).not.toBeNull();
  const store = tempStore('frame-open');
  await open(frame!, { provider: new FakeModel([]), storePath: store, timeoutMs: 500 })
    .locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: async () => true });
  await expect(frame!.locator('#inner-terms')).toBeChecked();

  // The redesign, inside that frame: the page stays, and the frame loads its new document.
  await page.route('https://shop.test/consent', (route) => route.fulfill({ contentType: 'text/html',
    body: consent('terms-v2') }));
  await frame!.goto('https://shop.test/consent');
  const model = new FakeModel(["//input[@name='agree']"]);
  const heal = open(frame!, { provider: model, storePath: store, timeoutMs: 500, fingerprint: false });
  await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
  await expect(frame!.locator('#terms-v2')).toBeChecked();
  expect(heal.heals.map((h) => [h.source, h.new])).toEqual([['model', "//input[@name='agree']"]]);
  expect(model.bundles[0].html).toContain('terms-v2');
  expect(model.bundles[0].html).not.toContain('Outer frame');
  expect(row(store, 'terms', "//input[@name='agree']").frameXPath).toBeUndefined();
});
