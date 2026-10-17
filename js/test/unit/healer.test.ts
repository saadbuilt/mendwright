// The healer on a fake page: lookups, the fingerprint tier, the model tier and the gate,
// what is never healed, the canary, and heal.page. Each safety rule has a test here that
// fails without it.

import { strict as assert } from 'node:assert';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired, InvalidSelector } from '../../src/errors';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, world } from '../support/env';

const V1 = `<html><body><main><form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <label><input type="checkbox" id="terms"> I accept the terms</label>
  <button id="create" type="submit">Create account</button>
</form><a id="help" href="/help">Help</a></main></body></html>`;

const V2 = `<html><body><main><form id="signup">
  <label for="user-email">Email</label><input id="user-email" name="email" type="email">
  <label><input type="checkbox" id="accept-terms" data-testid="terms"> I accept the terms</label>
  <button id="create-account" type="submit">Create account</button>
</form><a id="help-link" href="/help">Help</a></main></body></html>`;

beforeEach(cleanEnv);

describe('lookups', () => {
  test('a selector that matches is used as it is; its role, page and fingerprint are saved', async () => {
    const w = world(V1);
    const heal = w.open();
    await heal.locator('email', '#email').fill('ada@example.com');
    assert.deepEqual(w.page.actions, [['fill', 'css=#email']]);
    const meta = loadStore(w.storePath).metaOf('email');
    assert.equal(meta.expectedRole, 'input');
    assert.equal(meta.lastPage, '/signup');
    assert.equal(meta.fingerprint?.attrs?.name, 'email');
    assert.equal(meta.fingerprint?.label, 'Email');
    assert.equal(loadStore(w.storePath).lastUrl('email'), 'https://shop.test/signup');
    assert.deepEqual(heal.heals, []);
  });

  test('arguments that can never work raise at once, before the page is touched', () => {
    const heal = world(V1).open();
    assert.throws(() => heal.locator('x', ''), InvalidSelector);
    assert.throws(() => heal.locator('x', 'css='), InvalidSelector);
    assert.throws(() => heal.locator('x', 42 as never), TypeError);
    assert.throws(() => heal.locator('x', '#x', { role: 'tab' }), /unknown role/);
    assert.throws(() => heal.locator('get_by_role("button", name="Go")', '#go'), /get_by_/);
    assert.throws(() => heal.locator('x', 'get_by_role("button")'), InvalidSelector);
    assert.throws(() => heal.locator('x', null, { intent: 'the save button' }), /needs role/);
  });

  test('a selector the browser cannot parse raises InvalidSelector at once, and no model is asked', async () => {
    const w = world(V1);
    const model = new FakeModel(["//input[@id='email']"]);
    const started = Date.now();
    await assert.rejects(w.open({ provider: model, timeoutMs: 3000 }).locator('email', '//input[', { role: 'input' }).fill('x'),
      (error: unknown) => error instanceof InvalidSelector && /browser rejected a selector/.test((error as Error).message));
    assert.ok(Date.now() - started < 2000, 'it waited for the timeout');
    assert.equal(model.bundles.length, 0);
  });

  test('a key with no selector and nothing stored raises AttentionRequired', async () => {
    const heal = world(V1).open();
    await assert.rejects(heal.locator('email').click(), AttentionRequired);
  });

  test('an optional step that is not there is skipped and does nothing', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel(['//button']) });
    const promo = await heal.locator('promo', '#promo-close', { optional: true, role: 'button' }).resolve();
    assert.equal(promo.present, false);
    assert.equal(await promo.click(), undefined);
    assert.deepEqual(w.page.actions, []);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'flow.skip' && e.reason === 'optional'));
  });
});

describe('the fingerprint tier', () => {
  test('heals a renamed element without a model, saves it, and the next run uses it at once', async () => {
    const w = world(V1);
    let heal = w.open();
    await heal.locator('email', '#email').fill('x');
    await heal.locator('terms', '#terms').check();
    w.page.goto(V2);
    heal = w.open();
    await heal.locator('email', '#email').fill('ada@example.com');
    await heal.locator('terms', '#terms').check();
    assert.deepEqual(heal.heals.map((h) => [h.key, h.new, h.source]), [
      ['email', "//input[@id='user-email']", 'fingerprint'],
      ['terms', "//input[@data-testid='terms']", 'fingerprint'],
    ]);
    assert.ok(w.lines.some((l) => l.startsWith("warning: healed 'email': #email -> //input[@id='user-email'] (fingerprint,")));
    const events = journalOf(w.dir).filter((e) => e.key === 'email').map((e) => e.event);
    assert.deepEqual(events.slice(-3), ['heal.requested', 'heal.proposed', 'heal.applied']);
    // The next run: the code's selector is tried, then the saved heal, in one pass.
    const next = w.open();
    await next.locator('email', '#email').fill('again');
    assert.deepEqual(next.heals, []);
  });

  test('does not heal when two elements score alike, and says why', async () => {
    const page = '<html><body><form id="f"><button class="add">Add</button></form></body></html>';
    const w = world(page);
    await w.open().locator('add', 'button.add').click();
    w.page.goto('<html><body><form id="f"><button class="plus">Add</button><button class="plus">Add</button></form></body></html>');
    await assert.rejects(w.open().locator('add', 'button.add').click(),
      /two elements score alike .* and no model provider is configured to try instead/s);
  });

  test('does not heal an element that shares no id, name, label or text with the recorded one', async () => {
    const w = world('<html><body><form id="f"><button id="save" class="primary">Save</button></form></body></html>');
    await w.open().locator('save', '#save').click();
    w.page.goto('<html><body><form id="f"><button id="go" class="primary">Continue</button></form></body></html>');
    await assert.rejects(w.open().locator('save', '#save').click(), /shares no id, test id, name, link/);
  });

  test('does not heal an element outside the recorded container that is still on the page', async () => {
    const w = world('<html><body><form id="profile"><button>Save</button></form><div id="other"></div></body></html>');
    await w.open().locator('save', '//form[@id="profile"]/button').click();
    w.page.goto('<html><body><form id="profile"><p>moved</p></form><div id="other"><button>Save</button></div></body></html>');
    await assert.rejects(w.open().locator('save', '//form[@id="profile"]/button').click(), /outside the element with id 'profile'/);
  });

  test('does not heal an element that scores below the threshold, strong signal or not', async () => {
    const w = world('<html><body><div class="card"><form id="f"><label for="e">Email</label>'
      + '<input id="e" name="email" type="email" placeholder="you@example.com" class="field big"></form></div></body></html>');
    await w.open().locator('email', '#e').fill('x');
    w.page.goto('<html><body><section><label for="z">Contact</label><input id="z" name="email" type="text"></section></body></html>');
    await assert.rejects(w.open().locator('email', '#e').fill('x'), /the best score was 0\.\d\d, below the 0\.40 needed/);
  });

  test('a copy hidden on the live page is neither the answer nor a rival', async () => {
    const w = world('<html><body><form id="f"><button class="buy">Buy now</button></form></body></html>');
    await w.open().locator('buy', 'button.buy').click();
    // A copy hidden by a stylesheet, which only the live page knows is hidden: it scores
    // the same as the button, and would make the two alike.
    w.page.goto('<html><body><form id="f"><button class="cta" id="main-buy">Buy now</button>'
      + '<button class="cta" id="sticky-buy" data-fake-hidden>Buy now</button></form></body></html>');
    const heal = w.open();
    await heal.locator('buy', 'button.buy').click();
    assert.equal(heal.heals[0].new, "//button[@id='main-buy']");
  });

  test('is off with fingerprint: false and with MENDWRIGHT_FINGERPRINT=0', async () => {
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    w.page.goto(V2);
    await assert.rejects(w.open({ fingerprint: false }).locator('email', '#email').fill('x'), /no model provider/);
    process.env.MENDWRIGHT_FINGERPRINT = '0';
    await assert.rejects(w.open().locator('email', '#email').fill('x'), /no model provider/);
  });

  test('records no fingerprint for a key marked destructive', async () => {
    const w = world(V1);
    await w.open().locator('create', '#create', { destructive: true }).click();
    assert.equal(loadStore(w.storePath).metaOf('create').fingerprint, null);
  });
});

describe('the model tier and the gate', () => {
  function broken() {
    const w = world(V2);
    return w;
  }

  test('a model heals a selector that never matched, with the role given; the bundle is redacted', async () => {
    const w = world(V2.replace('<input id="user-email"', '<input value="typed@secret.test" id="user-email"'));
    const model = new FakeModel(["//input[@id='user-email']"]);
    const heal = w.open({ provider: model });
    await heal.locator('email', '#email', { role: 'textbox' }).fill('ada@example.com');
    assert.deepEqual(heal.heals.map((h) => [h.new, h.source]), [["//input[@id='user-email']", 'model']]);
    const bundle = model.bundles[0];
    assert.equal(bundle.key, 'email');
    assert.equal(bundle.expectedRole, 'input');
    assert.deepEqual(bundle.candidates, ['#email']);
    assert.ok(!bundle.html.includes('typed@secret.test'), 'a value attribute reached the model');
    assert.ok(!('fingerprint' in bundle), 'a hosted model got the fingerprint');
    assert.equal(bundle.page, '/signup');
  });

  const REFUSED: [string, string, RegExp][] = [
    ['an index-only path', '/html[1]/body[1]/main[1]/form[1]/input[1]', /brittle absolute-index path/],
    ['a selector that matches several elements', '//input', /not unique \(2\)/],
    ['a selector that matches nothing', "//input[@id='nope']", /no match/],
    ['an element of another role', "//button[@id='create-account']", /role mismatch: got button want input/],
    ['a CSS selector', '#user-email', /xpath error|not an XPath/],
  ];
  for (const [what, proposal, reason] of REFUSED) {
    test('the gate refuses ' + what, async () => {
      const w = broken();
      const model = new FakeModel([proposal, proposal]);
      await assert.rejects(w.open({ provider: model }).locator('email', '#email', { role: 'input' }).fill('x'), reason);
      assert.equal(loadStore(w.storePath).candidates('email').length, 1, 'something was saved');
      // The second ask carries the reason of the first refusal.
      assert.match(String(model.bundles[1].rejectReason), reason);
    });
  }

  test('the gate refuses an element that is hidden on the live page', async () => {
    const w = world(V2.replace('<input id="user-email"', '<input data-fake-hidden id="user-email"'));
    await assert.rejects(
      w.open({ provider: new FakeModel(["//input[@id='user-email']", "//input[@id='user-email']"]) })
        .locator('email', '#email', { role: 'input' }).fill('x'),
      /not uniquely present on the live page/);
  });

  test('the gate takes a hidden checkbox: a styled checkbox hides its native input', async () => {
    const w = world('<html><body><label><input type="checkbox" id="t2" data-fake-hidden> Terms</label></body></html>');
    const heal = w.open({ provider: new FakeModel(["//input[@id='t2']"]) });
    await heal.locator('terms', '#terms', { role: 'checkbox' }).check();
    assert.equal(heal.heals.length, 1);
  });

  test('a second proposal that passes is used after a refused one', async () => {
    const w = broken();
    const model = new FakeModel(['//input', "//input[@name='email']"]);
    const heal = w.open({ provider: model });
    await heal.locator('email', '#email', { role: 'input' }).fill('x');
    assert.equal(heal.heals[0].new, "//input[@name='email']");
    assert.equal(model.bundles[1].rejectedXpath, '//input');
  });

  test('a quarantined selector, and any other selector of its element, is refused', async () => {
    const w = broken();
    const store = loadStore(w.storePath);
    store.setCodeCandidates('email', ['#email']);
    store.setMeta('email', { role: 'input' });
    store.addCandidate('email', "//input[@id='user-email']");
    store.quarantine('email', "//input[@id='user-email']");
    const model = new FakeModel(["//input[@id='user-email']", "//input[@name='email']"]);
    await assert.rejects(w.open({ provider: model }).locator('email', '#email').fill('x'), /previously quarantined \(the same element/);
    assert.equal(model.bundles[1].rejectReason, 'previously quarantined');
  });

  test('a superseded selector is refused', async () => {
    const w = broken();
    const store = loadStore(w.storePath);
    store.setCodeCandidates('email', ["//input[@id='user-email']"]);
    store.setCodeCandidates('email', ['#email']);
    store.setMeta('email', { role: 'input' });
    await assert.rejects(w.open({ provider: new FakeModel(["//input[@id='user-email']", "//input[@id='user-email']"]) })
      .locator('email', '#email').fill('x'), /previously superseded/);
  });

  test('a position is refused when the page was cut to fit the prompt', async () => {
    const filler = '<p>' + 'lorem ipsum '.repeat(6000) + '</p>';
    const w = world(V2.replace('</main>', filler + '</main>'));
    await assert.rejects(
      w.open({ provider: new FakeModel(['(//input)[1]', '(//input)[1]']) }).locator('email', '#email', { role: 'input' }).fill('x'),
      /uses a position/);
  });

  test('the provider\'s reason is given when it returns no proposal', async () => {
    const w = broken();
    const model = new FakeModel([null], 'Anthropic returned HTTP 401 (authentication_error): invalid x-api-key.');
    await assert.rejects(w.open({ provider: model }).locator('email', '#email', { role: 'input' }).fill('x'),
      /FakeModel returned no selector for 'email': Anthropic returned HTTP 401/);
  });
});

  test('with intent and role and no selector, a model finds the element on first use', async () => {
    const w = world(V2);
    const model = new FakeModel(["//button[@id='create-account']"]);
    const heal = w.open({ provider: model });
    await heal.locator('create', null, { intent: 'the button that creates the account', role: 'button' }).click();
    assert.deepEqual(heal.heals.map((h) => [h.new, h.source]), [["//button[@id='create-account']", 'intent']]);
    assert.equal(model.bundles[0].intent, 'the button that creates the account');
    assert.deepEqual(model.bundles[0].candidates, []);
    assert.equal(loadStore(w.storePath).metaOf('create').intent, 'the button that creates the account');
  });

  test('a list of selectors is tried in order before anything is healed', async () => {
    const w = world(V2);
    const model = new FakeModel(['//button']);
    const heal = w.open({ provider: model });
    await heal.locator('create', ['#create', '#create-account'], { role: 'button' }).click();
    assert.deepEqual(heal.heals, []);
    assert.deepEqual(w.page.actions, [['click', 'css=#create-account']]);
  });

describe('what is never healed', () => {
  test('a key marked destructive, before any fingerprint or model', async () => {
    const w = world(V1);
    await w.open().locator('create', '#create').click();
    w.page.goto(V2);
    const model = new FakeModel(["//button[@id='create-account']"]);
    await assert.rejects(w.open({ provider: model }).locator('create', '#create', { destructive: true }).click(),
      /'create' is marked as a destructive control, and mendwright never auto-heals those/);
    assert.equal(model.bundles.length, 0);
    assert.deepEqual(w.page.actions.filter(([a]) => a === 'click').length, 1);
    // The mark is saved: a later run without destructive still refuses.
    await assert.rejects(w.open({ provider: model }).locator('create', '#create').click(), /destructive/);
    // destructiveKeys marks a key for good.
    const v = world(V2);
    await assert.rejects(v.open({ provider: model, destructiveKeys: ['pay'] }).locator('pay', '#pay', { role: 'button' }).click(),
      /destructive/);
    assert.equal(model.bundles.length, 0);
  });

  test('a key marked destructive after a heal was saved for it: the heal is quarantined, nothing acts', async () => {
    const w = world(V2);
    const store = loadStore(w.storePath);
    store.setCodeCandidates('create', ['#create']);
    store.addCandidate('create', "//button[@id='create-account']");
    await assert.rejects(w.open().locator('create', '#create', { destructive: true }).click(), /never acts through a healed selector/);
    assert.deepEqual(w.page.actions, []);
    assert.equal(loadStore(w.storePath).status('create', "//button[@id='create-account']"), 'quarantined');
  });

  test('a key on a page where it was never found (the page guard)', async () => {
    const w = world(V1);
    await w.open().locator('create', '#create').click();
    w.page.goto(V2, 'https://shop.test/checkout/review');
    const model = new FakeModel(["//button[@id='create-account']"]);
    await assert.rejects(w.open({ provider: model }).locator('create', '#create').click(),
      /'create' was last found on \/signup, and this page is \/checkout\/review/);
    assert.equal(model.bundles.length, 0);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'flow.page_guard' && e.page === '/checkout/review'));
    // healAcrossPages lets it heal there.
    const heal = w.open({ provider: model, healAcrossPages: true });
    await heal.locator('create', '#create').click();
    assert.equal(heal.heals[0].source, 'fingerprint');
  });

  test('a key last found inside an iframe, which this package does not heal in', async () => {
    const w = world(V2);
    const store = loadStore(w.storePath);
    store.setCodeCandidates('card', ['#card']);
    store.setMeta('card', { role: 'input' });
    store.addCandidate('card', "xpath=//input[@id='card-number']", { frameXPath: "iframe[name='pay']" });
    store.recordSuccess('card', "xpath=//input[@id='card-number']");
    const model = new FakeModel(["//input[@name='email']"]);
    await assert.rejects(w.open({ provider: model }).locator('card', '#card').fill('4242'),
      /'card' was last found inside the iframe iframe\[name='pay'\], and mendwright for TypeScript does not heal inside an iframe/);
    assert.equal(model.bundles.length, 0);
  });

  test('a key whose role is unknown', async () => {
    const w = world(V2);
    const model = new FakeModel(["//input[@id='accept-terms']"]);
    await assert.rejects(w.open({ provider: model }).locator('terms', '#terms').check(), /does not know what kind of element 'terms' is/);
    assert.equal(model.bundles.length, 0);
  });

  test('while the guard blocks heals', async () => {
    const w = world(V2);
    const model = new FakeModel(["//input[@name='email']"]);
    await assert.rejects(w.open({ provider: model, guard: () => false }).locator('email', '#email', { role: 'input' }).fill('x'),
      /transaction guard/);
    assert.equal(model.bundles.length, 0);
  });

  test('past the hourly cap of model heals (fail closed)', async () => {
    const w = world(V2);
    const model = new FakeModel(['//nope', '//nope', '//nope']);
    const heal = w.open({ provider: model, ratePerKeyPerHour: 1 });
    await assert.rejects(heal.locator('email', '#email', { role: 'input' }).fill('x'), /no match/);
    const asked = model.bundles.length;
    await assert.rejects(heal.locator('email', '#email', { role: 'input' }).fill('x'), /rate cap/);
    assert.equal(model.bundles.length, asked);
  });

  test('in dry run: checked, not applied', async () => {
    const w = world(V2);
    const model = new FakeModel(["//input[@name='email']"]);
    await assert.rejects(w.open({ provider: model, dryRun: true }).locator('email', '#email', { role: 'input' }).fill('x'),
      /dry-run mode is on/);
    assert.equal(loadStore(w.storePath).candidates('email').length, 1);
    assert.deepEqual(w.page.actions, []);
  });

  test('with MENDWRIGHT_DISABLE=1, not even a provider passed in is asked', async () => {
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    w.page.goto(V2);
    process.env.MENDWRIGHT_DISABLE = '1';
    const model = new FakeModel(["//input[@name='email']"]);
    await assert.rejects(w.open({ provider: model }).locator('email', '#email').fill('x'), /MENDWRIGHT_DISABLE=1/);
    assert.equal(model.bundles.length, 0);
  });
});

describe('the canary', () => {
  async function healed(provider = new FakeModel(["//input[@name='email']"])) {
    const w = world(V2);
    const heal = w.open({ provider });
    return { w, heal, provider };
  }

  test('an action that fails in the browser quarantines the heal, and it is not used again', async () => {
    const { w, heal } = await healed();
    w.page.failNext.set("xpath=//input[@name='email']", 'locator.fill: Error: element is detached');
    await assert.rejects(heal.locator('email', '#email', { role: 'input' }).fill('x'), /detached/);
    assert.equal(loadStore(w.storePath).status('email', "//input[@name='email']"), 'quarantined');
    assert.equal(heal.heals[0].confirmed, false);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'heal.quarantine'));
    await assert.rejects(w.open({ provider: new FakeModel(["//input[@name='email']", "//input[@name='email']"]) })
      .locator('email', '#email', { role: 'input' }).fill('x'), /previously quarantined/);
  });

  test('a wrong argument quarantines nothing', async () => {
    const { w, heal } = await healed();
    await assert.rejects(heal.locator('email', '#email', { role: 'input' }).fill(42 as never), /expected string/);
    assert.equal(loadStore(w.storePath).status('email', "//input[@name='email']"), 'active');
  });

  test('an action that went through confirms nothing; expect does', async () => {
    const { w, heal, provider } = await healed();
    await heal.locator('email', '#email', { role: 'input' }).fill('x');
    assert.deepEqual(heal.pending(), { email: "//input[@name='email']" });
    assert.equal(heal.heals[0].confirmed, null);
    await heal.locator('email', '#email', { role: 'input' }).fill('y', { expect: () => true });
    assert.equal(heal.heals[0].confirmed, true);
    assert.deepEqual(heal.pending(), {});
    assert.deepEqual(provider.outcomes, [['email', "//input[@name='email']", true]]);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'heal.canary' && e.success === true && e.by === 'expect'));
  });

  test('expect that is false quarantines the heal', async () => {
    const { w, heal } = await healed();
    await heal.locator('email', '#email', { role: 'input' }).fill('x', { expect: () => false });
    assert.equal(loadStore(w.storePath).status('email', "//input[@name='email']"), 'quarantined');
  });

  test('confirmAll reports every pending heal', async () => {
    const { w, heal } = await healed();
    await heal.locator('email', '#email', { role: 'input' }).fill('x');
    assert.equal(heal.confirmAll(false), 1);
    assert.equal(loadStore(w.storePath).status('email', "//input[@name='email']"), 'quarantined');
  });

  test('the fingerprint of a heal made in this run is not recorded until it is confirmed', async () => {
    const { w, heal } = await healed();
    await heal.locator('email', '#email', { role: 'input' }).fill('x');
    assert.equal(loadStore(w.storePath).metaOf('email').fingerprint, null);
  });

  test('a fingerprint heal is not reported to the provider, which did not propose it', async () => {
    const w = world(V1);
    await w.open().locator('email', '#email').fill('x');
    w.page.goto(V2);
    const model = new FakeModel([]);
    const heal = w.open({ provider: model });
    await heal.locator('email', '#email').fill('x', { expect: () => true });
    assert.deepEqual(model.outcomes, []);
    assert.equal(model.bundles.length, 0);
  });
});

describe('interruptions and the cookies preset', () => {
  const BANNER = `<div id="onetrust-banner-sdk" class="otFlat">We use cookies to improve your experience.
    <button id="onetrust-accept-btn-handler" data-click-removes="#onetrust-banner-sdk">Accept all</button>
    <button id="onetrust-reject-all-handler" data-click-removes="#onetrust-banner-sdk">Reject all</button></div>`;

  test("cookies: 'reject' clicks the banner's reject button before the action, which then goes through", async () => {
    const w = world(V1.replace('<main>', '<main>' + BANNER), { blocker: '#onetrust-banner-sdk' });
    const heal = w.open({ cookies: 'reject' });
    await heal.locator('email', '#email').fill('ada@example.com');
    assert.deepEqual(w.page.clicked.map((el) => el.id), ['onetrust-reject-all-handler']);
    assert.deepEqual(heal.handledInterruptions, [{ key: 'cookie_banner', before: 'email' }]);
    assert.ok(journalOf(w.dir).some((e) => e.event === 'flow.interrupt' && e.preset === 'reject'));
  });

  test("cookies: 'accept' clicks accept", async () => {
    const w = world(V1.replace('<main>', '<main>' + BANNER), { blocker: '#onetrust-banner-sdk' });
    await w.open({ cookies: 'accept' }).locator('email', '#email').fill('x');
    assert.deepEqual(w.page.clicked.map((el) => el.id), ['onetrust-accept-btn-handler']);
  });

  test('the fallback clicks reject in a cookie banner, and nothing in a consent screen that is not about cookies', async () => {
    const generic = '<div class="cookie-notice">This site uses cookies. <button data-click-removes=".cookie-notice">Decline</button>'
      + '<button>Accept</button></div>';
    const w = world(V1.replace('<main>', '<main>' + generic), { blocker: '.cookie-notice' });
    await w.open({ cookies: 'reject' }).locator('email', '#email').fill('x');
    assert.deepEqual(w.page.clicked.map((el) => el.textContent), ['Decline']);

    const oauth = '<div class="consent-screen">Shop wants to read your profile. <button>Deny</button><button>Allow</button></div>';
    const v = world(V1.replace('<main>', '<main>' + oauth));
    await v.open({ cookies: 'accept' }).locator('email', '#email').fill('x');
    await v.open({ cookies: 'reject' }).locator('email', '#email').fill('x');
    assert.deepEqual(v.page.clicked, []);
  });

  test('a Close button counts for reject only when its whole label closes the banner', async () => {
    const page = (label: string) => V1.replace('<main>', '<main><div id="cookie-bar">We use cookies. <button data-click-removes="#cookie-bar">'
      + label + '</button></div>');
    const ok = world(page('Close banner'), { blocker: '#cookie-bar' });
    await ok.open({ cookies: 'reject' }).locator('email', '#email').fill('x');
    assert.equal(ok.page.clicked.length, 1);
    const account = world(page('Close account'));
    await account.open({ cookies: 'reject' }).locator('email', '#email').fill('x');
    assert.deepEqual(account.page.clicked, []);
  });

  test('an action that was intercepted is tried once more, only after an interruption was handled', async () => {
    const w = world(V1.replace('<main>', '<main><div id="promo">Sale! <button id="promo-x" data-click-removes="#promo">x</button></div>'),
      { blocker: '#promo' });
    const heal = w.open();
    // Not registered: the action fails with the browser's error, tried once.
    await assert.rejects(heal.locator('email', '#email').fill('x'), /intercepts pointer events/);
    assert.equal(w.page.actions.filter(([a]) => a === 'fill').length, 1);
    heal.interrupt('promo', '#promo-x');
    await heal.locator('email', '#email').fill('x');
    assert.deepEqual(heal.handledInterruptions.map((e) => e.key), ['promo']);
  });

  test('an interruption that never goes away stops the step after 3', { timeout: 20_000 }, async () => {
    // The form stays hidden behind a nag whose button does not close it.
    const w = world(V1.replace('<main>', '<main><div id="nag"><button id="nag-x">x</button></div>')
      .replace('<form id="signup">', '<form id="signup" data-fake-hidden>'));
    const heal = w.open();
    heal.interrupt('nag', '#nag-x');
    // Without the limit the step would click it forever: it goes away after the 5th click.
    const clicked = w.page.clicked;
    const push = clicked.push.bind(clicked);
    clicked.push = (...els: Element[]) => {
      const n = push(...els);
      if (n >= 5) w.page.document.querySelector('#nag')?.remove();
      return n;
    };
    await assert.rejects(heal.locator('email', '#email').fill('x'),
      /3 interruptions were handled for 'email' and 'nag' is still on the page/);
    assert.equal(w.page.clicked.length, 3);
  });

  test('an intercepted action is tried once more after one interruption, not again', async () => {
    const w = world(V1.replace('<main>', '<main><div id="nag"><button id="nag-x">x</button></div>'), { blocker: '#nag' });
    const heal = w.open();
    heal.interrupt('nag', '#nag-x');
    await assert.rejects(heal.locator('email', '#email').fill('x'), /intercepts pointer events/);
    assert.equal(w.page.actions.filter(([a]) => a === 'fill').length, 2);
  });

  test('an action whose call log says it was done is never tried again', async () => {
    const w = world(V1);
    const heal = w.open();
    heal.interrupt('nag', '#email');
    // Without the rule, the interception in the log would get the click run a second time.
    w.page.failNext.set('css=#create', ['locator.click: Timeout 5000ms exceeded.', 'Call log:',
      '  - <div> intercepts pointer events', '  - click action done',
      '  - waiting for scheduled navigations to finish'].join('\n'));
    await assert.rejects(heal.locator('create', '#create').click(), /scheduled navigations/);
    assert.equal(w.page.actions.filter(([a, s]) => a === 'click' && s === 'css=#create').length, 1);
  });

  test("a step of the interruption's own key is skipped when it is not on the page", async () => {
    const w = world(V1);
    const heal = w.open();
    heal.interrupt('cookies', '#cookie-accept');
    const step = await heal.locator('cookies', '#cookie-accept').resolve();
    assert.equal(step.present, false);
  });

  test('an interruption that hides the element is handled when the lookup misses', async () => {
    const wall = V1.replace('<form id="signup">', '<div id="wall"><button id="wall-ok" data-click-removes="#wall">OK</button></div>'
      + '<form id="signup" data-fake-hidden>');
    const w = world(wall);
    // The form shows once the wall is gone.
    const heal = w.open();
    heal.interrupt('wall', '#wall-ok');
    const original = w.page.query.bind(w.page);
    w.page.query = (selector: string) => {
      if (!w.page.document.querySelector('#wall')) w.page.document.querySelector('#signup')?.removeAttribute('data-fake-hidden');
      return original(selector);
    };
    await heal.locator('email', '#email').fill('x');
    assert.deepEqual(heal.handledInterruptions.map((e) => e.key), ['wall']);
    assert.deepEqual(heal.heals, []);
  });
});

describe('heal.page', () => {
  test('page methods that take a selector heal it, the rest is the real page', async () => {
    const w = world(V1);
    const page = w.open().page;
    await page.fill('#email', 'x');
    await page.locator('#terms').check();
    w.page.goto(V2);
    const heal = w.open();
    await heal.page.fill('#email', 'ada@example.com');
    await heal.page.locator('#terms').check();
    assert.deepEqual(heal.heals.map((h) => h.key), ['#email', '#terms']);
    assert.equal(heal.page.url(), 'https://shop.test/signup');
    assert.equal((heal.page as unknown as { native: unknown }).native, w.page);
  });

  test('a read of a selector that matches a hidden element goes to the page as written', async () => {
    const w = world('<html><body><span id="price" data-fake-hidden>9.99</span></body></html>');
    const page = w.open().page;
    assert.equal(await page.textContent('#price'), '9.99');
    assert.deepEqual(w.page.actions, []);
  });

  test('isVisible never waits or heals', async () => {
    const w = world(V1);
    const page = w.open().page;
    assert.equal(await page.isVisible('#email'), true);
    w.page.goto(V2);
    assert.equal(await w.open().page.isVisible('#email'), false);
  });
});
