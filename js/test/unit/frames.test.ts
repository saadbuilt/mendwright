// Iframes one level down, on the fake page, whose iframes have documents of their own: a key
// whose own selector matches only inside an iframe is followed there, with no model call, and
// a key last found inside an iframe heals inside it, never on the page around it. Each refusal
// and each safety rule has a test here that fails without it. The rules, the texts, the store
// rows and the journal line are compared with the Python package's.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { HealCoordinator } from '../../src/coordinator';
import { cssString } from '../../src/css';
import { PlaywrightDriver } from '../../src/driver';
import { AttentionRequired } from '../../src/errors';
import { bareUrl, frameMessage, frameOptions, framePlan, frameSelector, type FrameMatch } from '../../src/frames';
import { open, type LocatorOptions, type OpenOptions } from '../../src/healer';
import { Journal } from '../../src/journal';
import { NullProvider } from '../../src/providers/base';
import { generated } from '../../src/providers/candidates';
import { pyRepr } from '../../src/py';
import { frameSpelling } from '../../src/selectors';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, tempDir, world, type World } from '../support/env';
import { FakePage, type FakeFrame } from '../support/fakepage';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

function page(body: string): string {
  return '<html><head><title>Sign up</title></head><body><main><h1>Sign up</h1>' + body + '</main></body></html>';
}

/** An iframe whose document is inner (as its srcdoc), with attrs. */
function iframe(inner: string, attrs = 'name="consent"'): string {
  return '<iframe ' + attrs + ' srcdoc="' + inner.replace(/&/g, '&amp;').replace(/"/g, '&quot;') + '"></iframe>';
}

const FRAME = 'iframe[name="consent"]';
const INNER = '<form id="agreement"><label><input type="checkbox" id="inner-terms" name="agree"> I agree</label></form>';
const AGREE = "//input[@name='agree']";
// On the page around the iframe: a checkbox with the same name and label.
const LOOKALIKE = '<label><input type="checkbox" id="outer-agree" name="agree"> I agree</label>';

/** The document of iframe n of w's page. */
function inner(w: World, n = 0): Document {
  return w.page.childFrames()[n].document;
}

function checked(doc: Document, id: string): boolean {
  return (doc.getElementById(id) as HTMLInputElement).checked;
}

/** The rows the store file holds for key, by their text. */
function saved(w: World, key: string): Record<string, Record<string, unknown>> {
  const data = JSON.parse(fs.readFileSync(w.storePath, 'utf-8'));
  return Object.fromEntries(data.selectors[key].candidates.map((c: Record<string, unknown>) => [c.xpath, c]));
}

const declines = (w: World) => journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => [e.key, e.code]);

/** Follow 'terms' into the iframe of w's page and confirm it. */
async function followed(w: World): Promise<void> {
  await w.open().locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
}

beforeEach(cleanEnv);

describe('a selector that matches only inside an iframe', () => {
  test('is looked up there from now on, with no model call, as a heal with source frame', async () => {
    const w = world(page(iframe(INNER)));
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(checked(inner(w), 'inner-terms'), true);
    assert.deepEqual(w.page.actions, [['check', 'css=#inner-terms']]);
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(heal.heals, [{ key: 'terms', old: ['#inner-terms'], new: 'css=#inner-terms', confirmed: null,
      source: 'frame' }]);
    // It waits for its canary like any heal: the fixture confirms it, and strict mode counts it.
    assert.deepEqual(heal.pending(), { terms: 'css=#inner-terms' });
    const row = saved(w, 'terms')['css=#inner-terms'];
    assert.deepEqual([row.source, row.status, row.frameXPath], ['heal', 'active', FRAME]);
    const events = journalOf(w.dir);
    assert.deepEqual(events.filter((e) => e.event === 'heal.applied').map(({ ts: _ts, ...e }) => e), [{
      event: 'heal.applied', key: 'terms', provider: 'frame', latency_ms: 0, xpath: 'css=#inner-terms',
      old: ['#inner-terms'], source: 'frame', frame: FRAME }]);
    assert.ok(!events.some((e) => e.event === 'heal.requested' || e.event === 'heal.declined'));
    assert.ok(w.lines.includes("warning: found 'terms' inside the iframe " + FRAME + ': #inner-terms matches nothing '
      + 'on the page, so it is looked up in that iframe from now on'));
    // The next run finds it there at once.
    const next = w.open({ provider: model });
    await next.locator('terms', '#inner-terms', { role: 'checkbox' }).uncheck();
    assert.equal(checked(inner(w), 'inner-terms'), false);
    assert.deepEqual(next.heals, []);
  });

  test('is confirmed or quarantined by its canary, which never reports it to the provider', async () => {
    const w = world(page(iframe(INNER)));
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    assert.equal(heal.heals[0].confirmed, true);
    const other = world(page(iframe(INNER)));
    const failed = other.open({ provider: model });
    await failed.locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => false });
    assert.equal(saved(other, 'terms')['css=#inner-terms'].status, 'quarantined');
    assert.deepEqual(model.outcomes, []);
  });

  test('a later selector of the key is followed when the first cannot be', async () => {
    const w = world(page(iframe(INNER + '<input type="checkbox" id="other">')));
    await w.open().locator('terms', ["//input[@type='checkbox']", '#inner-terms'], { role: 'checkbox' }).check();
    assert.deepEqual(w.page.actions, [['check', 'css=#inner-terms']]);
    assert.equal(checked(inner(w), 'inner-terms'), true);
  });

  test('an iframe whose attributes changed is followed to its new selector', async () => {
    const w = world(page(iframe(INNER)));
    await followed(w);
    w.page.goto(page(iframe(INNER, 'title="Consent"')));
    const heal = w.open();
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(saved(w, 'terms')['css=#inner-terms'].frameXPath, 'iframe[title="Consent"]');
    assert.deepEqual(heal.heals.map((h) => h.source), ['frame']);
  });

  test('the iframe is saved by its id, name, title or src, never by a generated id or a query', async () => {
    const doc = '<html><body>' + INNER + '</body></html>';
    const cases: [string, string][] = [
      [iframe(INNER, 'id="consent-frame"'), 'iframe[id="consent-frame"]'],
      [iframe(INNER, 'id="mui-generated-8842" name="pay"'), 'iframe[name="pay"]'],
      [iframe(INNER, 'title="Your &quot;consent&quot;"'), 'iframe[title="Your \\"consent\\""]'],
      ['<iframe src="/embed/consent?token=abc123"></iframe>', 'iframe[src^="/embed/consent?"]'],
      ['<iframe src="/embed/consent"></iframe>', 'iframe[src="/embed/consent"]'],
    ];
    for (const [html, frame] of cases) {
      const w = world(page(html));
      w.page.site['https://shop.test/embed/consent?token=abc123'] = doc;
      w.page.site['https://shop.test/embed/consent'] = doc;
      await w.open().locator('terms', '#inner-terms', { role: 'checkbox' }).check();
      assert.equal(saved(w, 'terms')['css=#inner-terms'].frameXPath, frame, html);
      assert.ok(!fs.readFileSync(w.storePath, 'utf-8').includes('token'), html);
    }
  });
});

describe('a key not followed into an iframe, with why', () => {
  /** The AttentionRequired message of key's lookup on w's page, which must not be followed:
   * nothing is healed, saved in an iframe or acted on, and no model is asked. */
  async function refused(w: World, options: OpenOptions = {}, locator: LocatorOptions = {},
                         selector: string | string[] = '#inner-terms', key = 'terms'): Promise<string> {
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model, ...options });
    const error = await heal.locator(key, selector, { role: 'checkbox', ...locator }).check().catch((e) => e);
    assert.ok(error instanceof AttentionRequired, String(error));
    assert.deepEqual(heal.heals, []);
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(w.page.actions, []);
    assert.ok(!fs.existsSync(w.storePath) || !fs.readFileSync(w.storePath, 'utf-8').includes('frameXPath'));
    // The reason journaled with the decline is the one explain() gives and the error ends with.
    const reasons = journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => e.reason as string);
    assert.equal(reasons.length, 1);
    assert.equal(reasons[0], heal.coordinator.explain(key));
    assert.ok(error.message.endsWith('\n  -> ' + reasons[0]), error.message);
    return error.message;
  }

  const because = (why: string) => "No matching element for: terms (tried 1 candidate(s)).\n  -> 'terms' is inside an "
    + 'iframe: #inner-terms matches nothing in the page mendwright was opened on, but matches inside the iframe named '
    + "'consent'. mendwright did not look for it there, because " + why + ', and did not ask a model to heal it. Open '
    + "mendwright on that Frame instead: heal = open(page.frame({ name: 'consent' })).";

  test('in dry run', async () => {
    const w = world(page(iframe(INNER)));
    assert.equal(await refused(w, { dryRun: true }), because('dry-run mode is on'));
    assert.deepEqual(declines(w), [['terms', 'dry_run']]);
  });

  test('under MENDWRIGHT_DISABLE=1', async () => {
    process.env.MENDWRIGHT_DISABLE = '1';
    const w = world(page(iframe(INNER)));
    assert.equal(await refused(w), because('healing is turned off by MENDWRIGHT_DISABLE=1'));
    assert.deepEqual(declines(w), [['terms', 'disabled']]);
  });

  test('while a guard blocks heals', async () => {
    const w = world(page(iframe(INNER)));
    assert.equal(await refused(w, { guard: () => false }), because('a transaction guard is currently blocking heals'));
    assert.deepEqual(declines(w), [['terms', 'frame']]);
  });

  test('marked destructive', async () => {
    const w = world(page(iframe(INNER)));
    assert.equal(await refused(w, {}, { destructive: true }), because("'terms' is marked as a destructive control"));
    assert.deepEqual(declines(w), [['terms', 'frame']]);
  });

  test('when it matches in two iframes', async () => {
    const w = world(page(iframe(INNER) + iframe(INNER, 'name="other"')));
    const message = await refused(w);
    assert.ok(message.includes("matches inside the iframe named 'consent' (and 1 more)."), message);
    assert.ok(message.includes('because it matches in more than one iframe,'), message);
  });

  test('when it matches several elements there', async () => {
    const w = world(page(iframe(INNER + INNER.replace('inner-terms', 'second'))));
    const message = await refused(w, {}, {}, "//input[@type='checkbox']");
    assert.ok(message.includes('because it matches 2 elements there,'), message);
  });

  test('when its iframe is inside another iframe', async () => {
    const w = world(page(iframe('<html><body><p>Outer frame</p>' + iframe(INNER) + '</body></html>', 'name="outer"')));
    assert.equal(w.page.frames().length, 3);
    assert.equal(await refused(w), because('that iframe is inside another iframe'));
  });

  test('when nothing tells its iframe apart from the others', async () => {
    const cases: [string, string][] = [
      [iframe(INNER, ''), "the iframe at about:srcdoc. mendwright did not look for it there, because nothing tells that "
        + 'iframe apart from the others on the page, and did not ask a model to heal it. Open mendwright on that Frame '
        + "instead: heal = open(page.frame({ url: 'about:srcdoc' }))."],
      [iframe(INNER, 'id="mui-generated-8842"'), "the iframe named 'mui-generated-8842'. mendwright did not look for it "
        + 'there, because nothing tells that iframe apart'],
      // Its name is another iframe's too.
      [iframe(INNER) + iframe('<p>Other</p>'), "the iframe named 'consent'. mendwright did not look for it there, "
        + 'because nothing tells that iframe apart'],
    ];
    for (const [html, text] of cases) {
      const message = await refused(world(page(html)));
      assert.ok(message.includes(text), message);
    }
  });

  test('for a Playwright engine selector', async () => {
    const w = world(page(iframe(INNER)));
    const message = await refused(w, {}, {}, 'text=I agree');
    assert.ok(message.includes("'terms' is inside an iframe: text=I agree matches nothing in the page mendwright was "
      + "opened on, but matches inside the iframe named 'consent'."), message);
    assert.ok(message.includes('because a Playwright engine selector is not followed into an iframe,'), message);
  });

  test('when that lookup was quarantined before', async () => {
    const w = world(page(iframe(INNER)));
    await w.open().locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => false });
    assert.equal(saved(w, 'terms')['css=#inner-terms'].status, 'quarantined');
    fs.rmSync(path.join(w.dir, 'journal.jsonl'));
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model });
    const error = await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check().catch((e) => e);
    assert.equal(error.message, because('it was looked up in an iframe before and an action on it failed there, so '
      + 'that lookup is quarantined'));
    assert.deepEqual([heal.heals, model.bundles.length], [[], 0]);
  });

  test('when that lookup was superseded before', async () => {
    const w = world(page(iframe(INNER)));
    fs.writeFileSync(w.storePath, JSON.stringify({ schema: 2, selectors: { terms: { expectedRole: 'checkbox', candidates: [
      { xpath: '#inner-terms', source: 'code', addedAt: null, lastSucceededAt: null, status: 'active' },
      { xpath: 'css=#inner-terms', source: 'heal', addedAt: null, lastSucceededAt: null, status: 'superseded',
        frameXPath: FRAME }] } } }));
    const heal = w.open({ provider: new FakeModel([AGREE]) });
    const error = await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check().catch((e) => e);
    assert.equal(error.message, because('it was looked up in an iframe before and that lookup was superseded'));
    assert.deepEqual(heal.heals, []);
  });

  test('on a page the key was never found on (the page guard)', async () => {
    const w = world(page(iframe(INNER)));
    loadStore(w.storePath).setLastPage('terms', '/checkout');
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model });
    const error = await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check().catch((e) => e);
    assert.ok(error instanceof AttentionRequired);
    assert.match(error.message, /^No matching element for: terms .*\n {2}-> 'terms' was last found on \/checkout, and this page is \/signup\. That looks like a change in the flow/s);
    assert.deepEqual([heal.heals, model.bundles.length, w.page.actions], [[], 0, []]);
    assert.deepEqual(declines(w), [['terms', 'page_guard']]);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'flow.page_guard' && e.page === '/signup'));
    assert.ok(!fs.readFileSync(w.storePath, 'utf-8').includes('frameXPath'));
  });

  test('when its element there fails the live check', async () => {
    const hidden = world(page(iframe('<input id="inner-email" name="email" style="display: none">')));
    const message = await refused(hidden, {}, { role: 'input' }, '#inner-email', 'email');
    assert.ok(message.includes("because its element there failed mendwright's checks (passed static validation but not "
      + 'uniquely present on the live page),'), message);
    const role = world(page(iframe(INNER)));
    assert.equal(await refused(role, {}, { role: 'button' }), because("its element there failed mendwright's checks "
      + '(live element role mismatch: got checkbox want button)'));
    // An element inside an iframe that is hidden is hidden too.
    const inHidden = world(page(iframe('<input id="inner-email" name="email">', 'name="consent" style="display: none"')));
    assert.ok((await refused(inHidden, {}, { role: 'input' }, '#inner-email', 'email')).includes('not uniquely present'));
  });
});

describe('a key last found inside an iframe', () => {
  test('heals inside it from the fingerprint recorded there, with no model call', async () => {
    const w = world(page(iframe(INNER)));
    const first = w.open();
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    // Once the move into the iframe is confirmed, the next match records the fingerprint there.
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).resolve();
    assert.equal(loadStore(w.storePath).metaOf('terms').fingerprint?.attrs?.id, 'inner-terms');
    assert.deepEqual(loadStore(w.storePath).shadowHosts('terms'), []);
    w.page.goto(page(iframe(INNER.replace('inner-terms', 'terms-v2'))));
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', "xpath=//input[@id='terms-v2']"]]);
    assert.equal(model.bundles.length, 0);
    assert.equal(checked(inner(w), 'terms-v2'), true);
    assert.equal(saved(w, 'terms')["xpath=//input[@id='terms-v2']"].frameXPath, FRAME);
    assert.ok(w.lines.some((l) => l.startsWith("warning: healed 'terms': #inner-terms (+1 more) -> "
      + "xpath=//input[@id='terms-v2'] (fingerprint, ")), w.lines.join('\n'));
  });

  test('heals inside it with a model, which reads the iframe\'s HTML and elements only', async () => {
    const w = world(page(iframe(INNER)));
    await followed(w);
    w.page.goto(page(iframe(INNER.replace('inner-terms', 'terms-v2'))));
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model, fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['model', 'xpath=' + AGREE]]);
    const [bundle] = model.bundles;
    assert.ok(bundle.html.includes('terms-v2'), bundle.html);
    assert.ok(!bundle.html.includes('Sign up'), bundle.html);
    assert.deepEqual(bundle.inventory.map((r) => (r as { id: string }).id), ['terms-v2']);
    assert.equal(bundle.page, '/signup');   // the page's, which the page guard compares
    // Saved with its prefix written out, so it never shares a row with the same XPath on the page.
    const rows = saved(w, 'terms');
    assert.equal(rows['xpath=' + AGREE].frameXPath, FRAME);
    assert.equal(rows[AGREE], undefined);
    assert.equal(checked(inner(w), 'terms-v2'), true);
  });

  test('never picks a look-alike on the page around it, by the model', async () => {
    const w = world(page(LOOKALIKE + iframe(INNER)));
    await followed(w);
    w.page.goto(page(LOOKALIKE + iframe(INNER.replace('inner-terms', 'terms-v2'))));
    const heal = w.open({ provider: new FakeModel([AGREE]), fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(checked(inner(w), 'terms-v2'), true);
    assert.equal(checked(w.page.document, 'outer-agree'), false);
    assert.equal(saved(w, 'terms')['xpath=' + AGREE].frameXPath, FRAME);
  });

  test('never picks a look-alike on the page around it, by the fingerprint', async () => {
    const w = world(page(LOOKALIKE + iframe(INNER)));
    const first = w.open();
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    await first.locator('terms', '#inner-terms', { role: 'checkbox' }).resolve();
    w.page.goto(page(LOOKALIKE + iframe(INNER.replace('inner-terms', 'terms-v2'))));
    const heal = w.open();
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['fingerprint', "xpath=//input[@id='terms-v2']"]]);
    assert.equal(checked(inner(w), 'terms-v2'), true);
    assert.equal(checked(w.page.document, 'outer-agree'), false);
  });

  test('checks a heal on the live page inside it, not on the page around it', async () => {
    // Inside the iframe the field the model names is hidden now; around it, one of that name shows.
    const around = '<input id="outer-email" name="mail">';
    const w = world(page(around + iframe('<form id="f"><input id="email" name="email"></form>')));
    await w.open().locator('email', '#email', { role: 'input' }).fill('a', { expect: () => true });
    w.page.goto(page(around + iframe('<form id="f"><input id="email-2" name="mail" style="display: none"></form>')));
    const model = new FakeModel(["//input[@name='mail']", "//input[@name='mail']"]);
    const heal = w.open({ provider: model, fingerprint: false });
    await assert.rejects(heal.locator('email', '#email', { role: 'input' }).fill('b'),
      /-> no selector the model proposed for 'email' passed mendwright's local gate \(last reason: passed static validation but not uniquely present on the live page\)/);
    assert.equal(model.bundles.length, 2);
    assert.equal(saved(w, 'email')["xpath=//input[@name='mail']"], undefined);
    assert.equal((w.page.document.getElementById('outer-email') as HTMLInputElement).value, '');
  });

  test('a quarantine made inside an iframe blocks that element there, and not the page', async () => {
    const quarantinedInFrame = async (w: World) => {
      await followed(w);
      w.page.goto(page(iframe(INNER.replace('inner-terms', 'v2'))));
      await w.open({ provider: new FakeModel([AGREE]), fingerprint: false })
        .locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => false });
      assert.equal(saved(w, 'terms')['xpath=' + AGREE].status, 'quarantined');
    };
    // The same element, spelled another way, is refused there.
    const there = world(page(iframe(INNER)));
    await quarantinedInFrame(there);
    const respelled = "(//input[@name='agree'])[1]";
    await assert.rejects(there.open({ provider: new FakeModel([respelled, respelled]), fingerprint: false })
      .locator('terms', '#inner-terms', { role: 'checkbox' }).check(), /previously quarantined \(the same element as a quarantined heal/);
    // The iframe is gone and the form is on the page: the same XPath is fine there.
    const moved = world(page(iframe(INNER)));
    await quarantinedInFrame(moved);
    moved.page.goto(page('<form><label><input type="checkbox" id="page-terms" name="agree"> I agree</label></form>'));
    const model = new FakeModel([AGREE]);
    const heal = moved.open({ provider: model, fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(checked(moved.page.document, 'page-terms'), true);
    const rows = saved(moved, 'terms');
    assert.deepEqual([rows[AGREE].status, rows[AGREE].frameXPath], ['active', undefined]);
    assert.equal(rows['xpath=' + AGREE].status, 'quarantined');
  });

  test('heals on the page when that iframe is not on the page now, as the Python package does', async () => {
    const w = world(page(iframe(INNER)));
    await followed(w);
    w.page.goto(page('<form><label><input type="checkbox" id="terms-v2" name="agree"> I agree</label></form>'));
    const model = new FakeModel([AGREE]);
    const heal = w.open({ provider: model, fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(checked(w.page.document, 'terms-v2'), true);
    assert.ok(model.bundles[0].html.includes('<h1>Sign up</h1>'));
    assert.equal(saved(w, 'terms')[AGREE].frameXPath, undefined);
  });

  /** A store with 'card' last found inside the iframe pay, as the Python package saves it. */
  function card(options: { destructive?: boolean; page?: string } = {}) {
    const w = world(page(iframe('<input id="card-v2" name="card">', 'name="pay"')));
    const store = loadStore(w.storePath);
    store.setCodeCandidates('card', ['#card']);
    store.setMeta('card', { role: 'input', destructive: options.destructive });
    store.addCandidate('card', "xpath=//input[@id='card-number']", { frameXPath: "iframe[name='pay']" });
    store.recordSuccess('card', "xpath=//input[@id='card-number']");
    if (options.page) store.setLastPage('card', options.page);
    return w;
  }

  test('marked destructive, or on a page it was never found on, is declined before it is looked for', async () => {
    const model = new FakeModel(["//input[@name='card']"]);
    const destructive = card({ destructive: true });
    await assert.rejects(destructive.open({ provider: model }).locator('card', '#card').fill('4242'),
      /'card' is marked as a destructive control/);
    assert.deepEqual(declines(destructive), [['card', 'destructive']]);
    const moved = card({ page: '/checkout/pay' });
    await assert.rejects(moved.open({ provider: model }).locator('card', '#card').fill('4242'),
      /'card' was last found on \/checkout\/pay, and this page is \/signup/);
    assert.deepEqual(declines(moved), [['card', 'page_guard']]);
    assert.equal(model.bundles.length, 0);
    // Otherwise it heals inside the iframe.
    const w = card();
    const heal = w.open({ provider: model, fingerprint: false });
    await heal.locator('card', '#card').fill('4242');
    assert.equal((inner(w).getElementById('card-v2') as HTMLInputElement).value, '4242');
    assert.equal(saved(w, 'card')["xpath=//input[@name='card']"].frameXPath, "iframe[name='pay']");
  });

  test('in dry run the heal inside it is checked there and not applied, and under MENDWRIGHT_DISABLE=1 not tried',
    async () => {
      const dry = card();
      await assert.rejects(dry.open({ provider: new FakeModel(["//input[@name='card']"]), dryRun: true, fingerprint: false })
        .locator('card', '#card').fill('4242'), /dry-run mode is on/);
      assert.ok(dry.lines.includes("info: [heal dry-run] key=card proposed=//input[@name='card'] valid=true (not applied)"),
        dry.lines.join('\n'));
      assert.deepEqual(declines(dry), [['card', 'dry_run']]);
      const off = card();
      process.env.MENDWRIGHT_DISABLE = '1';
      await assert.rejects(off.open().locator('card', '#card').fill('4242'), /MENDWRIGHT_DISABLE=1/);
      assert.deepEqual(declines(off), [['card', 'disabled']]);
    });

  test('inside a shadow root in that iframe is refused, and not healed in the iframe around it either', async () => {
    const HOST = 'my-app[id="root"]';
    const w = world(page(iframe('<button data-testid="save" type="button">Save</button>')));
    const store = loadStore(w.storePath);
    store.setCodeCandidates('save', ['#save']);
    store.setMeta('save', { role: 'button', shadowHosts: [HOST] });
    store.addCandidate('save', 'css=#save', { frameXPath: FRAME });
    store.recordSuccess('save', 'css=#save');
    const model = new FakeModel(["//button[@data-testid='save']"]);
    const heal = w.open({ provider: model });
    await assert.rejects(heal.locator('save', '#save', { role: 'button' }).click(),
      /'save' was last found inside a shadow root in the iframe iframe\[name="consent"\], and mendwright heals a key inside a shadow root only in the page or frame it was opened on, so it did not heal it, there or anywhere else\. Open mendwright on that frame to heal it there\./);
    assert.deepEqual([model.bundles.length, heal.heals, w.page.actions], [0, [], []]);
    assert.deepEqual(declines(w), [['save', 'shadow']]);
  });
});

describe('opened on a Frame, the way for an iframe deeper down', () => {
  test('heals inside that frame, which it reads as its page', async () => {
    const w = world(page(iframe('<html><body><p>Outer frame</p>' + iframe(INNER) + '</body></html>', 'name="outer"')));
    const frame = w.page.frames()[2] as FakeFrame;
    assert.equal(frame.name(), 'consent');
    const options = { storePath: w.storePath, timeoutMs: 100, log: () => undefined };
    await open(frame as never, { ...options, provider: new FakeModel([]) })
      .locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    assert.equal(checked(frame.document, 'inner-terms'), true);
    (frame.document.getElementById('inner-terms') as Element).id = 'terms-v2';
    const model = new FakeModel([AGREE]);
    const heal = open(frame as never, { ...options, provider: model, fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).uncheck();
    assert.equal(checked(frame.document, 'terms-v2'), false);
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['model', AGREE]]);
    assert.ok(model.bundles[0].html.includes('terms-v2') && !model.bundles[0].html.includes('Outer frame'));
    assert.equal(saved(w, 'terms')[AGREE].frameXPath, undefined);
  });
});

// --- the Python package -------------------------------------------------------------------------

const skip = needsPython();

describe('parity with the Python package', { skip }, () => {
  test('CSS strings, generated-looking values and the spelling of a lookup inside an iframe', () => {
    const strings = ['consent', 'a"b', 'back\\slash', 'line\nbreak', 'cr\rlf', 'form\ffeed', 'tab\there', 'ünïcödé',
      '日本語', 'emoji \u{1F600}', '', "it's", 'x'.repeat(250), '\u2028', 'nul\u0000', '"\\\n\r\f'];
    const values = [':r3:', ':r1a:', 'ember123', 'ember\u0663', 'mat-input-0', 'mat-x-\u0661', 'cdk-overlay-12',
      'react-select-3-input', 'radix-:r1:', 'headlessui-menu-button-1', 'f81d4fae-7dec-11d0-a765', '12345', '\u0661\u0662',
      'abc1234', 'css-1x2y3', 'sc-abcd', 'jsx-1234', 'consent', 'consent-frame', 'payment', 'stripe-frame-1', 'a1b2c3',
      'x1n2onr6', 'ab1cdef', 'lastName', 'user_42', 'step-3', 'frame\n', '12\n', '1abcd\n', 'abcd\u00b2', 'abcde\u00b2',
      '\u216bframe', '', null, 'mui-generated-8842', 'card_frame', 'pay-2024', 'iframe_ab12c', 'Consent Form',
      'ab1cd', 'a_1bcde', 'checkout-iframe', 'zahlung-\u0663\u0664\u0665'];
    const spellings = ['#inner', "//input[@id='a']", 'xpath=html/body', 'text=Save', 'css=#x', 'xpath=//a', 'css=/x',
      "xpath=id('a')", '(//a)[1]', 'button.primary', "role=button[name='Save']", '//a >> text=x', '  #padded  '];
    const py = runPython<{ css: string[]; generated: boolean[]; spelled: (string | null)[] }>(`
import json, sys
from mendwright.candidates import generated
from mendwright.css import css_string
from mendwright.selectors import frame_spelling
data = json.load(sys.stdin)
print(json.dumps({'css': [css_string(s) for s in data['strings']], 'generated': [generated(v) for v in data['values']],
                  'spelled': [frame_spelling(s) for s in data['spellings']]}))`, { strings, values, spellings });
    assert.deepEqual(strings.map(cssString), py.css);
    assert.deepEqual(values.map(generated), py.generated);
    assert.deepEqual(spellings.map(frameSpelling), py.spelled);
  });

  test('the selector of an iframe, and the selectors it tries', () => {
    type Case = { info: unknown; counts: Record<string, number>; fallback: number };
    const attrs = (extra: Record<string, string>) => ({ tag: 'iframe', id: '', name: '', title: '', src: '', ...extra });
    const cases: Case[] = [
      { info: attrs({ id: 'consent-frame' }), counts: {}, fallback: 1 },
      { info: attrs({ id: 'mui-generated-8842', name: 'pay', title: 'Payment', src: '/pay?x=1' }), counts: {}, fallback: 1 },
      { info: attrs({ id: ':r3:', title: 'Consent "form"' }), counts: {}, fallback: 1 },
      { info: attrs({ id: 'a\\b' }), counts: {}, fallback: 1 },
      { info: attrs({ title: 'line\nbreak\rand\fmore' }), counts: {}, fallback: 1 },
      { info: attrs({ id: 'payment\n' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '/embed?token=abc' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '/embed' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'https://pay.example.com/frame#card' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '?only=query' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'about:srcdoc' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'data:text/html,<p>hi</p>' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'javascript:void(0)' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'blob:https://shop.test/x' }), counts: {}, fallback: 1 },
      { info: attrs({ src: 'ABOUT:blank' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '' }), counts: {}, fallback: 1 },
      { info: attrs({ id: 'x'.repeat(200), name: 'y'.repeat(201) }), counts: {}, fallback: 1 },
      { info: attrs({ name: 'y'.repeat(201), title: 't' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '/' + 'p'.repeat(299) + '?q' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '/' + 'p'.repeat(300) + '?q' }), counts: {}, fallback: 1 },
      { info: attrs({ id: 'ünïcödé-frame' }), counts: {}, fallback: 1 },
      { info: attrs({ src: '/einwilligung/ü?x=1' }), counts: {}, fallback: 1 },
      { info: attrs({ id: '   ', name: '\u00a0', title: 'Consent' }), counts: {}, fallback: 1 },
      { info: { tag: 'frame', id: 'nav', name: '', title: '', src: '' }, counts: {}, fallback: 1 },
      { info: { tag: 'IFRAME', id: 'nav' }, counts: {}, fallback: 1 },
      { info: { id: 'nav' }, counts: {}, fallback: 1 },
      { info: attrs({ id: 'x', name: 'consent', title: 'Consent' }), counts: { 'iframe[id="x"]': 2 }, fallback: 1 },
      { info: attrs({ id: 'x', name: 'consent', title: 'Consent' }), counts: { 'iframe[name="consent"]': 0 }, fallback: 2 },
      { info: attrs({ id: 'x', name: 'consent', src: '/c' }), counts: {}, fallback: 2 },
      { info: null, counts: {}, fallback: 1 },
      { info: 'iframe', counts: {}, fallback: 1 },
      { info: [], counts: {}, fallback: 1 },
      { info: { tag: 'iframe', id: 7, name: null, title: 'T', src: 3 }, counts: {}, fallback: 1 },
    ];
    const py = runPython<{ selector: (string | null)[]; options: string[][] }>(`
import json, sys
from mendwright.drivers.base import frame_selector
from mendwright.drivers.playwright_async import _frame_options
out, options = [], []
for case in json.load(sys.stdin):
    counts, fallback = case['counts'], case['fallback']
    out.append(frame_selector(case['info'], lambda s: counts.get(s, fallback)))
    options.append(_frame_options(case['info']))
print(json.dumps({'selector': out, 'options': options}))`, cases);
    const ours = cases.map((c) => frameSelector(c.info, (s) => (Object.prototype.hasOwnProperty.call(c.counts, s)
      ? c.counts[s] : c.fallback)));
    assert.deepEqual(ours, py.selector);
    assert.deepEqual(cases.map((c) => frameOptions(c.info)), py.options);
    assert.equal(py.selector[6], 'iframe[src^="/embed?"]');
    assert.ok(py.selector.some((s) => s === null));
  });

  test('a frame\'s URL in a message', () => {
    const urls = ['about:srcdoc', 'about:blank', '', 'https://shop.test/frame?x=1#y', 'https://user:pw@Consent.Example:8443/c?token=t#x',
      'https://consent.example:443/c', 'http://[::1]:8080/x', 'data:text/html,<p>hi</p>', 'file:///C:/x.html', 'https://host',
      'https://host:abc/', 'javascript:void(0)', 'blob:https://shop.test/uuid', '//cdn.test/frame', '/relative/path?q',
      'HTTPS://SHOP.TEST/Frame', 'https://shop.test:0/x', 'https://ex.com:65536/x', 'mailto:a@b.c', 'https://пример.рф/путь?q',
      '  https://lead.test/space', 'https://tab\t.test/x', 'ftp:', 'https:x', 'weird://host/path#frag'];
    const py = runPython<string[]>(`
import json, sys
from mendwright.facade import _bare_url
print(json.dumps([_bare_url(u) for u in json.load(sys.stdin)]))`, urls);
    assert.deepEqual(urls.map(bareUrl), py);
    assert.equal(py[4], 'https://consent.example:8443/c');
  });

  test('when a key is followed into an iframe, and why not', () => {
    const one = (extra: Partial<FrameMatch> = {}): FrameMatch => ({ name: 'consent', url: 'about:srcdoc', count: 1,
      nested: false, selector: FRAME, ...extra });
    type Case = { key: string; selector: string; frames: FrameMatch[]; disabled: boolean; dryRun: boolean;
      destructive: boolean; statuses: Record<string, string> };
    const base: Case = { key: 'terms', selector: '#inner-terms', frames: [one()], disabled: false, dryRun: false,
      destructive: false, statuses: {} };
    const cases: Case[] = [
      base, { ...base, disabled: true }, { ...base, dryRun: true }, { ...base, destructive: true },
      { ...base, disabled: true, dryRun: true, destructive: true }, { ...base, frames: [one(), one({ name: 'b' })] },
      { ...base, frames: [one({ nested: true, selector: null })] }, { ...base, frames: [one({ count: 3 })] },
      { ...base, frames: [one({ nested: true, count: 2, selector: null })] }, { ...base, frames: [one({ selector: null })] },
      { ...base, selector: 'text=I agree' }, { ...base, selector: "role=checkbox[name='I agree']" },
      { ...base, selector: "//input[@id='inner-terms']" }, { ...base, selector: 'xpath=html/body' },
      { ...base, statuses: { 'css=#inner-terms': 'quarantined' } }, { ...base, statuses: { 'css=#inner-terms': 'superseded' } },
      { ...base, statuses: { 'css=#inner-terms': 'active' } }, { ...base, statuses: { 'css=#inner-terms': 'proposed' } },
      { ...base, statuses: { '#inner-terms': 'quarantined' } }, { ...base, key: "o'k", destructive: true },
      { ...base, frames: [one({ selector: null }), one()] },
    ];
    const py = runPython<[string | null, string | null][]>(`
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
    out.append(list(Healer._frame_plan(me, c['key'], c['selector'], c['frames'])))
print(json.dumps(out))`, cases);
    const ours = cases.map((c) => framePlan(c.key, c.selector, c.frames, { disabled: c.disabled, dryRun: c.dryRun,
      destructive: c.destructive, status: (s) => c.statuses[s] ?? null }));
    assert.deepEqual(ours, py);
    assert.deepEqual(py[0], [FRAME, 'css=#inner-terms']);
  });

  test('why a key is not followed, with the TypeScript way to open mendwright on that frame', () => {
    const frame = (name: string, url: string): FrameMatch => ({ name, url, count: 1, nested: false, selector: null });
    const cases: { key: string; selector: string; frames: FrameMatch[]; why: string | null }[] = [
      { key: 'terms', selector: '#inner-terms', frames: [frame('consent', 'about:srcdoc')], why: null },
      { key: 'terms', selector: '#inner-terms', frames: [frame('', 'https://user:pw@consent.example/c?token=t#x')],
        why: 'it matches 2 elements there' },
      { key: 'terms', selector: '#inner-terms', frames: [frame('', '')], why: 'dry-run mode is on' },
      { key: 'terms', selector: '#inner-terms', frames: [frame('', 'about:srcdoc')], why: '' },
      { key: 'terms', selector: '#inner-terms', frames: [frame("it's", '')], why: null },
      { key: 'terms', selector: '#inner-terms', frames: [frame('a\\b"c\'', '')], why: null },
      { key: "o'k", selector: "//input[@id='a']", frames: [frame('pay', ''), frame('other', '')],
        why: 'it matches in more than one iframe' },
      { key: 'k', selector: 'text=I agree', frames: [frame('', 'https://pay.test/x'), frame('', ''), frame('b', '')],
        why: 'a Playwright engine selector is not followed into an iframe' },
      { key: 'zustimmung', selector: '#z', frames: [frame('Einwilligung – ü', 'https://x.test/a')], why: null },
      { key: 'k', selector: '#k', frames: [frame('', 'https://host:abc/')], why: null },
    ];
    const py = runPython<{ messages: string[]; bare: string[] }>(`
import json, sys
from mendwright.facade import _bare_url, _frame_message
cases = json.load(sys.stdin)
print(json.dumps({
    'messages': [_frame_message(c['key'], c['selector'], c['frames'], 'mendwright.open', c['why']) for c in cases],
    'bare': [_bare_url(c['frames'][0]['url']) for c in cases]}))`, cases);
    cases.forEach((c, i) => {
      const { name } = c.frames[0];
      const url = py.bare[i];
      const pyHow = name ? 'page.frame(name=' + pyRepr(name) + ')' : url ? 'page.frame(url=' + pyRepr(url) + ')'
        : 'the Frame from page.frames';
      const tsHow = name ? 'page.frame({ name: ' + pyRepr(name) + ' })' : url ? 'page.frame({ url: ' + pyRepr(url) + ' })'
        : 'the Frame from page.frames()';
      const advice = 'heal = mendwright.open(' + pyHow + ').';
      assert.ok(py.messages[i].endsWith(advice), py.messages[i]);
      assert.equal(frameMessage(c.key, c.selector, c.frames, c.why),
        py.messages[i].slice(0, -advice.length) + 'heal = open(' + tsHow + ').');
    });
  });
});

// A page with one iframe (FRAME) as the Python package's tests drive one: the outer document
// and the iframe's, each an lxml document, through the driver methods (Python's TwoDocs).
const PY_FRAMED = `
import json, sys
import lxml.html
import mendwright
from mendwright.store import load_store

FRAME = 'iframe[name="consent"]'
OUTER = "<main><h1>Sign up</h1><iframe name='consent'></iframe></main>"

def _find(doc, selector):
    body = selector
    for prefix in ('xpath=', 'css='):
        if body.startswith(prefix):
            body = body[len(prefix):]
    if body.startswith('#'):
        return doc.xpath('//*[@id=$i]', i=body[1:])
    try:
        return [e for e in doc.xpath(body) if hasattr(e, 'tag')]
    except Exception:
        return []

class Doc:
    def __init__(self, html, inner=None):
        self.doc = lxml.html.fromstring(html)
        self.inner = inner
        self.actions = []
    def _doc(self, frame_chain):
        if tuple(frame_chain) == (FRAME,):
            return self.inner.doc if self.inner else None
        return None if frame_chain else self.doc
    def count(self, selector, frame_chain=()):
        doc = self._doc(frame_chain)
        return 0 if doc is None else len(_find(doc, selector))
    def is_visible(self, selector, frame_chain=()):
        return self.count(selector, frame_chain) == 1
    def scroll_into_view(self, selector, frame_chain=()):
        pass
    def handle(self, selector, frame_chain=()):
        return (selector, tuple(frame_chain))
    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        self.actions.append([verb, selector, list(frame_chain)])
    def act_on(self, native, verb, *args, **kwargs):
        self.actions.append([verb, native[0], list(native[1])])
    def url(self):
        return 'https://shop.test/signup'
    def outer_html(self, scope=None):
        return lxml.html.tostring(self.doc, encoding='unicode')
    def describe(self, selector, frame_chain=()):
        doc = self._doc(frame_chain)
        found = [] if doc is None else _find(doc, selector)
        if len(found) != 1:
            return None
        el = found[0]
        role = 'checkbox' if el.get('type') == 'checkbox' else el.tag
        return {'tag': el.tag, 'role': role, 'text': el.text_content().strip(),
                'outer_html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}
    def inventory(self, limit=200):
        return []

class Framed(Doc):
    def __init__(self, inner_html):
        super().__init__(OUTER, inner=Doc(inner_html))
    def frame_matches(self, selector):
        n = self.inner.count(selector)
        return [{'name': 'consent', 'url': 'about:srcdoc', 'count': n, 'nested': False, 'selector': FRAME}] if n else []
    def framed(self, frame_selector):
        return self.inner if frame_selector == FRAME else None

class Stub:
    def __init__(self, xpath=None):
        self.xpath, self.bundles, self.outcomes = xpath, [], []
    def propose(self, bundle):
        self.bundles.append(bundle)
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': 'stub'} if self.xpath else None
    def report_outcome(self, key, xpath, success):
        self.outcomes.append([key, xpath, bool(success)])

args = json.load(sys.stdin)
`;

// What the Python package makes of the store: the key's rows, where it heals the key
// (heal_place), and a lookup of the key on the page with the iframe whose document is inner.
const PY_RESOLVE = PY_FRAMED + `
from mendwright.facade import heal_place
from mendwright.resolver import resolve
store = load_store(None, args['store'])
rows = [[c['xpath'], c.get('source'), c.get('frameXPath') or '', c.get('status')] for c in store.candidates('terms')]
place = heal_place(store, 'terms')
handle = resolve(store, Framed(args['inner']), 'terms', timeout_ms=0, poll_ms=0)
page = Framed(args['inner'])
provider = Stub("//input")
heal = mendwright.open(page, provider=provider, store_path=args['store'])
heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
print(json.dumps({'rows': rows, 'place': [place[0], list(place[1]), place[2]], 'handle': [handle[0], list(handle[1])],
                  'heals': len(heal.heals), 'asked': len(provider.bundles), 'actions': page.actions}))
`;

describe('one store shared with the Python package', { skip }, () => {
  const INNER_PY = "<label><input type='checkbox' id='inner-terms' name='agree'> I agree</label>";

  test('a key this package followed into an iframe is looked up there by the Python package', () => {
    const w = world(page(iframe(INNER)));
    return w.open().locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true }).then(() => {
      const py = runPython<Record<string, any>>(PY_RESOLVE, { store: w.storePath, inner: INNER_PY });
      // By recency, as a new process lists them until its code passes its selectors.
      assert.deepEqual(py.rows, [['css=#inner-terms', 'heal', FRAME, 'active'], ['#inner-terms', 'code', '', 'active']]);
      assert.deepEqual(py.place, [FRAME, [], '']);
      assert.deepEqual(py.handle, ['css=#inner-terms', [FRAME]]);
      assert.deepEqual([py.heals, py.asked, py.actions], [0, 0, [['check', 'css=#inner-terms', [FRAME]]]]);
    });
  });

  test('a heal this package made inside an iframe is looked up there by the Python package', async () => {
    const w = world(page(iframe(INNER)));
    await followed(w);
    w.page.goto(page(iframe(INNER.replace('inner-terms', 'terms-v2'))));
    await w.open({ provider: new FakeModel([AGREE]), fingerprint: false })
      .locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    const py = runPython<Record<string, any>>(PY_RESOLVE, { store: w.storePath,
      inner: INNER_PY.replace('inner-terms', 'terms-v2') });
    assert.deepEqual(py.rows.find((r: string[]) => r[0] === 'xpath=' + AGREE), ['xpath=' + AGREE, 'heal', FRAME, 'active']);
    assert.deepEqual(py.place, [FRAME, [], '']);
    assert.deepEqual(py.handle, ['xpath=' + AGREE, [FRAME]]);
    assert.deepEqual([py.heals, py.asked], [0, 0]);
  });

  test('a key the Python package followed and healed inside an iframe heals inside it here', async () => {
    const w = world(page(iframe(INNER)));
    const py = runPython<Record<string, any>>(PY_FRAMED + `
first = Framed(args['inner'])
heal = mendwright.open(first, provider=Stub(), store_path=args['store'])
heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
heal.coordinator.confirm_pending('terms', True)
second = Framed(args['inner'].replace('inner-terms', 'terms-v2'))
again = mendwright.open(second, provider=Stub("//input[@name='agree']"), store_path=args['store'], fingerprint=False)
again.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
again.coordinator.confirm_pending('terms', True)
print(json.dumps({'heals': [[h.source, list(h.old), h.new] for h in heal.heals + again.heals],
                  'actions': second.actions}))`, { store: w.storePath, inner: INNER_PY });
    assert.deepEqual(py.heals, [['frame', ['#inner-terms'], 'css=#inner-terms'], ['model', ['#inner-terms',
      'css=#inner-terms'], 'xpath=' + AGREE]]);
    assert.deepEqual(py.actions, [['check', 'xpath=' + AGREE, [FRAME]]]);
    // Here, after another redesign inside the iframe.
    w.page.goto(page(iframe(INNER.replace('inner-terms', 'terms-v3').replace('name="agree"', 'name="consent-agree"'))));
    const model = new FakeModel(["//input[@name='consent-agree']"]);
    const heal = w.open({ provider: model, fingerprint: false });
    await heal.locator('terms', '#inner-terms', { role: 'checkbox' }).check();
    assert.equal(checked(inner(w), 'terms-v3'), true);
    assert.deepEqual(heal.heals.map((h) => [h.source, h.new]), [['model', "xpath=//input[@name='consent-agree']"]]);
    assert.ok(model.bundles[0].html.includes('terms-v3') && !model.bundles[0].html.includes('Sign up'));
    assert.equal(saved(w, 'terms')["xpath=//input[@name='consent-agree']"].frameXPath, FRAME);
  });
});

describe('the journal shared with the Python package', { skip }, () => {
  test('a key followed into an iframe writes the Python package\'s heal.applied line, byte for byte', () => {
    const lines = [['terms', '#inner-terms', 'css=#inner-terms', FRAME],
      ['zustimmung "ja"', "//input[@aria-label='Zustimmung – ja']", "xpath=//input[@aria-label='Zustimmung – ja']",
        'iframe[title="Einwilligung \\"ü\\""]']];
    const dir = tempDir();
    const file = path.join(dir, 'journal.jsonl');
    const coordinator = new HealCoordinator(loadStore(path.join(dir, 'overrides.json')), {
      provider: new NullProvider('none'), driver: new PlaywrightDriver(new FakePage('<html></html>') as never),
      journal: new Journal(file, { run: 'ts-run-1', clock: () => '2026-11-02T00:00:00+00:00' }), log: () => undefined,
    });
    for (const [key, old, alt, frame] of lines) coordinator.recordFrame(key, old, alt, frame);
    const python = runPython<string>(`
import json, os, sys, tempfile
from mendwright.coordinator import HealCoordinator
from mendwright.journal import Journal
path = os.path.join(tempfile.mkdtemp(), 'journal.jsonl')
journal = Journal(path, clock=lambda: '2026-11-02T00:00:00+00:00', run='ts-run-1')
coordinator = HealCoordinator(None, provider=object(), html_provider=lambda: '', inventory_provider=lambda: [],
                              guard=lambda: True, journal=journal, log=lambda message: None)
for key, old, new, frame in json.load(sys.stdin):
    coordinator.record_frame(key, old, new, frame)
print(json.dumps(open(path, encoding='utf-8').read()))`, lines);
    assert.equal(fs.readFileSync(file, 'utf-8'), python);
  });

  test('mendwright notify counts a key followed into an iframe as found inside an iframe', async () => {
    process.env.MENDWRIGHT_RUN_ID = 'ts-frames-1';
    const w = world(page(iframe(INNER)));
    await w.open().locator('terms', '#inner-terms', { role: 'checkbox' }).check({ expect: () => true });
    const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', w.storePath,
      '--run', 'ts-frames-1', '--format', 'json', '--dry-run'], {
      cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' },
    });
    assert.equal(result.status, 0, result.stderr);
    const data = JSON.parse(result.stdout);
    assert.deepEqual([data.repaired.total, data.repaired.frame, data.repaired.confirmed], [1, 1, 1]);
    assert.deepEqual(data.needs_human, []);
  });
});
