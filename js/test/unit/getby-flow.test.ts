// heal.page.getBy*() with open(page, { healGetBy: true }), on the fake page, whose getBy* match the
// way Playwright's do: the call is looked up first, a saved heal answers only while it matches
// nothing, and a call that matches nothing is healed from its fingerprint only, never by a model.
// Every test that heals or declines a call has a model that would propose the right element, or
// one that throws if it is asked, to show it never is. Each refusal and each safety rule has a
// test here that fails without it.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import { errors } from '@playwright/test';

import { DISABLED_REASON } from '../../src/coordinator';
import { AttentionRequired, GetByNotFound, InterruptionLoop, InvalidSelector } from '../../src/errors';
import {
  NO_MODEL, getByKey, noFingerprintMessage, notHealedMessage, onObjectMessage, parse, reservedKeyMessage, roleMismatch,
  roleUnknownMessage, stateReason, tierOffMessage, twoSpellingsMessage, type GetByCall,
} from '../../src/getby';
import { getBySetting, stepOf, type OpenOptions } from '../../src/healer';
import type { HealProvider } from '../../src/providers/base';
import { FakeModel, cleanEnv, journalOf, world, type World } from '../support/env';
import { FakeLocator } from '../support/fakepage';

const KEY = 'get_by_role("button", name="Sign in")';
const PY_KEY = "get_by_role('button', name='Sign in')";
const HEAL = "//button[@id='login']";
const PAGE = "<html><body><main><form id='login-form'><label for='email'>Email</label>"
  + "<input id='email' name='email' type='email'>"
  + "<button id='login' type='submit' class='primary'>Sign in</button></form>"
  + "<a href='/help'>Help</a></main></body></html>";
const RENAMED = PAGE.replace('>Sign in<', '>Log in<');          // the name changed, the id stayed
const TWINS = PAGE.replace("<button id='login' type='submit' class='primary'>Sign in</button>",
  "<button type='submit' class='primary'>Log in</button><button type='submit' class='primary'>Log in</button>");
const TWO = PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in later</button>');
const URL = 'https://shop.test/login';

/** A model that throws if it is asked: a call is never healed by a model. */
class Refusing implements HealProvider {
  asked = 0;

  async propose(): Promise<null> {
    this.asked += 1;
    throw new Error('the model was asked to heal a getBy* call');
  }
}

beforeEach(cleanEnv);

function login(html = PAGE): World {
  return world(html, { url: URL });
}

function open(w: World, options: OpenOptions = {}) {
  return w.open({ healGetBy: true, provider: new FakeModel([HEAL, HEAL]), ...options });
}

function signIn(heal: ReturnType<typeof open>, options?: Record<string, unknown>): Promise<void> {
  return heal.page.getByRole('button', { name: 'Sign in' }).click(options);
}

/** A page whose Sign in button matched once, then was renamed and healed. */
async function healed(w: World, options: OpenOptions = {}) {
  const heal = open(w, options);
  await signIn(heal);
  w.page.goto(RENAMED, URL);
  await signIn(heal);
  return heal;
}

function saved(w: World): Record<string, any> {
  return fs.existsSync(w.storePath) ? JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors : {};
}

/** The selector of the fake page's own locator for heal.page.getByRole('button', { name }). */
function callSelector(w: World, name = 'Sign in'): string {
  return w.page.getByRole('button', { name }).selector;
}

async function rejection(promise: Promise<unknown>): Promise<any> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail('it did not throw');
}

describe('turning it on', () => {
  test('off by default, a getBy* call is the real locator, and nothing is saved', async () => {
    const w = login();
    const heal = w.open({ provider: new FakeModel([HEAL]) });
    const button = heal.page.getByRole('button', { name: 'Sign in' });
    assert.ok(button instanceof FakeLocator);
    assert.equal(stepOf(button), null);
    await button.click();
    assert.deepEqual(w.page.actions.at(-1), ['click', callSelector(w)]);
    assert.deepEqual(saved(w), {});
    const guarded = w.open({ dismiss: true, healGetBy: false }).page.getByRole('button', { name: 'Sign in' });
    assert.equal(stepOf(guarded), "page.getByRole('button', { name: 'Sign in' })");
  });

  test('MENDWRIGHT_HEAL_GET_BY turns it on, and the option wins', () => {
    const w = login();
    process.env.MENDWRIGHT_HEAL_GET_BY = '1';
    assert.equal(stepOf(w.open().page.getByRole('button', { name: 'Sign in' })),
      "page.getByRole('button', { name: 'Sign in' })");
    assert.equal(stepOf(w.open({ healGetBy: null }).page.getByTestId('go')), "page.getByTestId('go')");
    assert.equal(stepOf(w.open({ healGetBy: false }).page.getByRole('button', { name: 'Sign in' })), null);
    process.env.MENDWRIGHT_HEAL_GET_BY = 'off';
    assert.equal(stepOf(w.open().page.getByRole('button', { name: 'Sign in' })), null);
    assert.equal(getBySetting(), false);
    assert.equal(getBySetting(true), true);
  });

  test("MENDWRIGHT_HEAL_GET_BY=model, the Python package's, reads as 1 here, with one warning", () => {
    const w = login();
    process.env.MENDWRIGHT_HEAL_GET_BY = ' Model ';
    assert.equal(stepOf(w.open().page.getByRole('button', { name: 'Sign in' })),
      "page.getByRole('button', { name: 'Sign in' })");
    w.open();
    assert.equal(getBySetting(), true);
    assert.deepEqual(w.lines.filter((l) => l.includes('MENDWRIGHT_HEAL_GET_BY')), ["warning: MENDWRIGHT_HEAL_GET_BY is "
      + "model, which lets the Python package's model provider heal heal.page's get_by_* calls. This package never asks "
      + 'a model to heal a getBy* call, so it reads it as 1: a call heals from its fingerprint only.']);
  });

  test('a healGetBy that is not a boolean, or a variable that is not a flag, throws before anything touches the page', () => {
    const w = login();
    assert.throws(() => w.open({ healGetBy: 'yes' as never }), (error: Error) => error instanceof TypeError
      && error.message === "mendwright.open() takes healGetBy: true, healGetBy: false or healGetBy: null, got healGetBy: 'yes'.");
    process.env.MENDWRIGHT_HEAL_GET_BY = 'maybe';
    assert.throws(() => w.open(), /MENDWRIGHT_HEAL_GET_BY must be 1 or 0/);
    assert.deepEqual(w.page.calls, []);
  });
});

describe('a call that matches', () => {
  test('acts on the element the call finds, and records the call as its key', async () => {
    const w = login();
    const model = new FakeModel([HEAL]);
    const heal = open(w, { provider: model });
    const button = heal.page.getByRole('button', { name: 'Sign in' });
    assert.equal(stepOf(button), "page.getByRole('button', { name: 'Sign in' })");
    assert.deepEqual(w.page.calls, []);                     // building it touches nothing
    await button.click();
    assert.deepEqual(w.page.actions.at(-1), ['click', callSelector(w)]);
    const entry = saved(w)[KEY];
    assert.equal(entry.expectedRole, 'button');
    assert.equal(entry.lastPage, '/login');
    assert.equal(entry.lastUrl, URL);
    assert.equal(entry.fingerprint.attrs.id, 'login');
    assert.deepEqual(entry.candidates.map((c: any) => [c.xpath, c.source]), [[KEY, 'code']]);
    assert.ok(entry.candidates[0].lastSucceededAt);
    assert.deepEqual([heal.heals, model.bundles], [[], []]);
    // The page passed to open() is the real one.
    const before = w.page.actions.length;
    await (heal.page as any).native.getByRole('button', { name: 'Sign in' }).click();
    assert.equal(w.page.actions.length, before + 1);
  });
});

describe('a call that matches nothing', () => {
  test('is healed from its fingerprint, with the journal of any fingerprint heal', async () => {
    const w = login();
    const model = new FakeModel([HEAL]);
    const heal = await healed(w, { provider: model });
    assert.deepEqual(w.page.actions.at(-1), ['click', 'xpath=' + HEAL]);
    assert.deepEqual(heal.heals.map((h) => [h.key, h.old, h.new, h.source]), [[KEY, [KEY], HEAL, 'fingerprint']]);
    const events = journalOf(w.dir);
    assert.deepEqual(events.map((e) => [e.event, e.source ?? null]), [['heal.requested', null],
      ['heal.proposed', 'fingerprint'], ['heal.applied', 'fingerprint']]);
    assert.deepEqual(events[2].old, [KEY]);
    assert.ok(w.lines.some((l) => l.startsWith("warning: healed '" + KEY + "': " + KEY + ' -> ' + HEAL + ' (fingerprint, ')),
      w.lines.join('\n'));
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(new Set(saved(w)[KEY].candidates.map((c: any) => c.xpath)), new Set([HEAL, KEY]));
  });

  test('a heal saved earlier answers at once while the call matches nothing', async () => {
    const w = login();
    await healed(w);
    const heal = open(w, { timeoutMs: 10_000 });
    const started = Date.now();
    await signIn(heal);
    assert.ok(Date.now() - started < 2000);
    assert.deepEqual(w.page.actions.at(-1), ['click', 'xpath=' + HEAL]);
    assert.deepEqual(heal.heals, []);
  });

  test('the call wins over a saved heal when it matches again', async () => {
    const w = login();
    const heal = await healed(w);
    w.page.goto(PAGE, URL);
    await signIn(heal);
    assert.deepEqual(w.page.actions.at(-1), ['click', callSelector(w)]);
    assert.equal(heal.store.lastSuccess(KEY)?.xpath, KEY);
  });
});

describe('never by a model', () => {
  for (const make of [() => new FakeModel([HEAL, HEAL]), () => new Refusing()]) {
    test('a fingerprint match that is not confident throws, and the model is not asked: ' + make().constructor.name,
      async () => {
        const w = login();
        const model = make();
        const heal = open(w, { provider: model });
        await signIn(heal);
        w.page.goto(TWINS, URL);
        const error = await rejection(signIn(heal));
        assert.ok(error instanceof GetByNotFound);
        assert.ok(error instanceof errors.TimeoutError && error instanceof AttentionRequired);
        assert.ok(error.message.startsWith('No matching element for: ' + KEY + ' (tried 1 candidate(s)).'), error.message);
        assert.ok(error.message.includes("the fingerprint match for '" + KEY + "' was not confident"), error.message);
        assert.ok(error.message.endsWith(NO_MODEL));
        assert.deepEqual(model instanceof FakeModel ? model.bundles : (model as Refusing).asked, model instanceof FakeModel ? [] : 0);
        assert.deepEqual(heal.heals, []);
        assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => e.code), ['get_by']);
      });
  }

  test('a call with no fingerprint is not healed', async () => {
    for (const name of ['Sing in', 'Log in']) {
      const w = login();
      const model = new Refusing();
      const heal = open(w, { provider: model });
      const error = await rejection(heal.page.getByRole('button', { name }).click());
      assert.ok(error instanceof GetByNotFound);
      assert.ok(error.message.endsWith(noFingerprintMessage(getByKey('getByRole', 'button', { name }))), error.message);
      assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.requested'), []);
      assert.ok((await rejection(heal.page.getByRole('btn' as never, { name: 'Sign in' }).click())) instanceof GetByNotFound);
      assert.equal(model.asked, 0);
    }
  });

  test('a call that matched only with the fingerprint tier off is not healed', async () => {
    const w = login();
    await signIn(open(w, { fingerprint: false }));
    w.page.goto(RENAMED, URL);
    const model = new Refusing();
    const error = await rejection(signIn(open(w, { provider: model })));
    assert.ok(error.message.endsWith(noFingerprintMessage(KEY)), error.message);
    assert.equal(model.asked, 0);
  });

  test('the fingerprint tier off, and MENDWRIGHT_DISABLE=1, say so, and heal nothing', async () => {
    const w = login();
    await signIn(open(w));
    w.page.goto(RENAMED, URL);
    const model = new Refusing();
    let error = await rejection(signIn(open(w, { provider: model, fingerprint: false })));
    assert.ok(error instanceof GetByNotFound && error.message.endsWith(tierOffMessage(KEY)), error.message);
    process.env.MENDWRIGHT_DISABLE = '1';
    const disabled = open(w, { provider: model });
    error = await rejection(signIn(disabled));
    assert.ok(error instanceof GetByNotFound && error.message.endsWith(DISABLED_REASON), error.message);
    assert.deepEqual([model.asked, disabled.heals], [0, []]);
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.declined').map((e) => e.code), ['get_by', 'disabled']);
    // The calls are still looked up: one that matches acts.
    w.page.goto(PAGE, URL);
    await signIn(disabled);
    assert.deepEqual(w.page.actions.at(-1), ['click', callSelector(w)]);
  });

  test('a key whose role could not be read is declined with the call\'s sentence', async () => {
    const w = login();
    const heal = open(w);
    heal.driver.describe = async () => null;               // the read fails
    await signIn(heal);
    assert.equal(saved(w)[KEY].expectedRole ?? 'unknown', 'unknown');
    w.page.goto(RENAMED, URL);
    const model = new Refusing();
    const error = await rejection(signIn(open(w, { provider: model })));
    assert.ok(error.message.endsWith(roleUnknownMessage(KEY)), error.message);
    assert.equal(model.asked, 0);
  });
});

describe('what a heal of a call must pass', () => {
  const CASES: [string, string, string, unknown[]][] = [
    ['no longer a tab', "<button role='tab' id='settings'>Settings</button>", "<button id='settings'>Preferences</button>",
      ['tab', { name: 'Settings' }]],
    ['another level', "<h2 id='cart'>Cart</h2>", "<h2 id='cart' aria-level='3'>Cart</h2>", ['heading', { name: 'Cart', level: 2 }]],
    ['a link without href', "<a id='help' href='/help'>Help</a>", "<a id='help'>Help</a>", ['link', { name: 'Help' }]],
  ];
  for (const [name, before, after, args] of CASES) {
    test('a heal of a getByRole() call must have the call\'s ARIA role: ' + name, async () => {
      const w = login('<html><body><main>' + before + '</main></body></html>');
      const model = new Refusing();
      const heal = open(w, { provider: model });
      const call = () => (heal.page.getByRole as (...a: unknown[]) => any)(...args).click();
      await call();
      w.page.goto('<html><body><main>' + after + '</main></body></html>', URL);
      const error = await rejection(call());
      const mismatch = roleMismatch(parse(getByKey('getByRole', ...(args as [never]))) as GetByCall);
      assert.ok(error instanceof GetByNotFound && error.message.includes(mismatch + ')'), error.message);
      assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.rejected').map((e) => e.reason), [mismatch]);
      assert.deepEqual([model.asked, heal.heals], [0, []]);
    });
  }

  for (const answer of ['null', 'missing']) {
    test('a driver that cannot check the ARIA role refuses the heal: ' + answer, async () => {
      const w = login();
      const heal = open(w);
      await signIn(heal);
      w.page.goto(RENAMED, URL);
      (heal.driver as any).roleMatch = answer === 'missing' ? undefined : async () => null;
      const error = await rejection(signIn(heal));
      assert.ok(error.message.includes("could not check the element's ARIA role on the live page"), error.message);
      assert.deepEqual(heal.store.candidates(KEY).map((c) => c.xpath), [KEY]);
    });
  }

  test('a heal saved before the key was marked destructive is quarantined, and is not a TimeoutError', async () => {
    const w = login();
    await healed(w);
    const heal = open(w, { destructiveKeys: [KEY] });
    const error = await rejection(signIn(heal));
    assert.ok(error instanceof AttentionRequired && !(error instanceof errors.TimeoutError));
    assert.ok(error.message.includes('a destructive control never acts through a healed selector'));
    assert.equal(heal.store.status(KEY, HEAL), 'quarantined');
  });

  test('a key marked destructive, in any spelling of the call, is not healed', async () => {
    for (const key of [getByKey('getByRole', 'button', { name: 'Sign in' }), PY_KEY]) {
      const w = login();
      const model = new Refusing();
      const heal = open(w, { provider: model, destructiveKeys: [key] });
      await signIn(heal);
      w.page.goto(RENAMED, URL);
      const error = await rejection(signIn(heal));
      assert.ok(error instanceof GetByNotFound && /is marked as a destructive control/.test(error.message), key);
      assert.deepEqual([model.asked, heal.heals], [0, []]);
      assert.notDeepEqual(w.page.actions.at(-1), ['click', 'xpath=' + HEAL]);
    }
  });

  test('the page guard applies to call keys', async () => {
    const w = login();
    const heal = open(w);
    await signIn(heal);
    w.page.goto(RENAMED, 'https://shop.test/checkout');
    const error = await rejection(signIn(heal));
    assert.ok(error instanceof GetByNotFound && /was last found on \/login, and this page is \/checkout/.test(error.message));
    assert.deepEqual(journalOf(w.dir).map((e) => e.event), ['flow.page_guard', 'heal.declined']);
    assert.equal(journalOf(w.dir).at(-1)?.code, 'page_guard');
  });

  test('dry run checks and applies nothing', async () => {
    const w = login();
    await signIn(open(w));
    w.page.goto(RENAMED, URL);
    const model = new Refusing();
    const error = await rejection(signIn(open(w, { provider: model, dryRun: true })));
    assert.ok(error instanceof GetByNotFound && /dry-run mode is on/.test(error.message));
    assert.deepEqual(journalOf(w.dir).map((e) => e.event), ['heal.requested', 'heal.proposed', 'heal.declined']);
    assert.equal(journalOf(w.dir).at(-1)?.code, 'dry_run');
    assert.deepEqual(saved(w)[KEY].candidates.map((c: any) => c.xpath), [KEY]);
    assert.equal(model.asked, 0);
  });

  test('an action that fails on a heal quarantines it, and the next lookup avoids it', async () => {
    const w = login();
    const heal = await healed(w);
    w.page.failNext.set('xpath=' + HEAL, 'locator.click: Element is not attached to the DOM');
    await assert.rejects(signIn(heal), /not attached/);
    assert.equal(heal.store.status(KEY, HEAL), 'quarantined');
    const error = await rejection(signIn(heal));
    assert.ok(error instanceof GetByNotFound && /previously quarantined/.test(error.message), error.message);
  });

  test("an action on the call's own element reports nothing to the heal canary", async () => {
    const w = login();
    const heal = await healed(w);
    assert.deepEqual(heal.pending(), { [KEY]: HEAL });
    w.page.goto(PAGE, URL);
    w.page.failNext.set(callSelector(w), 'locator.click: Element is not attached to the DOM');
    await assert.rejects(signIn(heal), /not attached/);
    assert.deepEqual(heal.pending(), { [KEY]: HEAL });
    assert.equal(heal.store.status(KEY, HEAL), 'active');
  });
});

describe('a call that is not broken goes to Playwright as written', () => {
  test('several matches: Playwright\'s strict mode error, at once, and nothing is saved', async () => {
    const w = login(TWO);
    const heal = open(w, { timeoutMs: 10_000 });
    const started = Date.now();
    const error = await rejection(signIn(heal));
    assert.ok(/strict mode violation/.test(error.message) && !(error instanceof AttentionRequired), error.message);
    assert.ok(Date.now() - started < 2000);
    assert.ok(!fs.existsSync(w.storePath));
    assert.deepEqual(journalOf(w.dir), []);
    assert.ok(w.lines.includes('debug: ' + KEY + ' matches 2 elements; Playwright runs it as written.'), w.lines.join('\n'));
  });

  test('one hidden match: Playwright\'s own wait, and nothing is saved', async () => {
    const w = login(PAGE.replace("class='primary'", "class='primary' data-fake-hidden"));
    const error = await rejection(signIn(open(w, { timeoutMs: 10_000 })));
    assert.ok(/element is not visible/.test(error.message) && !(error instanceof AttentionRequired), error.message);
    assert.ok(!fs.existsSync(w.storePath));
  });

  test('a hidden checkbox is looked up, not passed through', async () => {
    const terms = "<html><body><label><input type='checkbox' id='terms'> I accept the terms</label></body></html>";
    const w = login(terms);
    await open(w).page.getByRole('checkbox', { name: 'I accept the terms' }).check();
    w.page.goto(terms.replace("id='terms'", "id='terms' data-fake-hidden"), URL);
    const again = open(w);
    await again.page.getByRole('checkbox', { name: 'I accept the terms', includeHidden: false }).check();
    assert.equal(again.store.lastSuccess('get_by_role("checkbox", name="I accept the terms")')?.xpath,
      'get_by_role("checkbox", name="I accept the terms")');
  });

  test('a call the browser rejects: Playwright\'s error, at once', async () => {
    const w = login();
    const heal = open(w, { timeoutMs: 10_000 });
    const started = Date.now();
    const error = await rejection(heal.page.getByRole('button', { name: 'Sign in', level: 2 }).click());
    assert.ok(/"level" attribute is only supported/.test(error.message), error.message);
    assert.ok(!(error instanceof AttentionRequired) && !(error instanceof InvalidSelector));
    assert.ok(Date.now() - started < 2000);
    assert.ok(!fs.existsSync(w.storePath));
  });

  test('a saved heal never answers a call that matches something', async () => {
    const w = login();
    const heal = await healed(w);
    const acted = w.page.actions.length;
    w.page.goto(RENAMED.replace('</form>', '<button>Sign in</button><button>Sign in</button></form>'), URL);
    await assert.rejects(signIn(heal), /strict mode violation/);
    assert.equal(heal.store.status(KEY, HEAL), 'active');
    assert.ok(w.page.actions.slice(acted).every(([, selector]) => selector !== 'xpath=' + HEAL));
  });

  test('a call that starts to match several while it waits goes to Playwright at once', async () => {
    const w = login(RENAMED);
    const heal = open(w, { timeoutMs: 10_000 });
    setTimeout(() => w.page.goto(TWO.replace('>Sign in later<', '>Sign in<'), URL), 150).unref();
    const started = Date.now();
    await assert.rejects(signIn(heal), /strict mode violation/);
    assert.ok(Date.now() - started < 3000);
    assert.ok(w.lines.some((l) => l.endsWith('started to match something other than one visible element; Playwright runs '
      + 'it as written.')), w.lines.join('\n'));
  });
});

describe('what stays unhealed', () => {
  test('chained calls, positions and the page passed to open() are the real locators, and save nothing', async () => {
    const w = login();
    const heal = open(w);
    const call = heal.page.getByRole('button', { name: 'Sign in' }) as any;
    const form = heal.page.locator('#login-form') as any;
    const built = [call.first(), call.nth(0), call.last(), call.filter({ hasText: 'Sign' }),
      call.and(heal.page.getByRole('button')), call.or(heal.page.getByText('Help')), call.locator('xpath=.'),
      call.getByText('Sign in'), form.getByRole('button', { name: 'Sign in' }),
      (heal.page as any).frameLocator('#f').getByRole('button', { name: 'Sign in' }),
      (heal.page as any).native.getByRole('button', { name: 'Sign in' })];
    for (const locator of built) assert.ok(locator instanceof FakeLocator && stepOf(locator) === null, String(locator));
    await built[0].click();
    assert.ok(!fs.existsSync(w.storePath));
    const guarded = open(w, { dismiss: true }).page.getByRole('button', { name: 'Sign in' }) as any;
    assert.deepEqual([guarded.first(), guarded.nth(1), guarded.filter({ hasText: 'S' })].map(stepOf), [
      "page.getByRole('button', { name: 'Sign in' }).first()", "page.getByRole('button', { name: 'Sign in' }).nth(1)",
      "page.getByRole('button', { name: 'Sign in' }).filter({ hasText: 'S' })"]);
    assert.deepEqual((await guarded.all()).map(stepOf), ["page.getByRole('button', { name: 'Sign in' }).all()[0]"]);
  });

  test('a call that matches nothing on the page but matches in an iframe is not followed there', async () => {
    const pay = "<button id='pay'>Pay</button>";
    const w = login("<html><body><main><p>Pay below</p><iframe name='payment' srcdoc=\"" + pay + '"></iframe></main></body></html>');
    const key = 'get_by_role("button", name="Pay")';
    const heal = open(w, { provider: new Refusing() });
    heal.store.addCandidate(key, "//button[@id='pay']");      // a heal that matches only in there
    const error = await rejection(heal.page.getByRole('button', { name: 'Pay' }).click());
    assert.ok(error instanceof GetByNotFound);
    assert.ok(error.message.endsWith("-> '" + key + "' is inside an iframe: " + key + ' matches nothing in the page '
      + "mendwright was opened on, but matches inside the iframe named 'payment'. mendwright did not look for it there, "
      + 'because mendwright does not follow a getBy* call into an iframe, and did not ask a model to heal it. Open '
      + "mendwright on that Frame instead: heal = open(page.frame({ name: 'payment' }), { healGetBy: true })."), error.message);
    assert.ok(!heal.store.candidates(key).some((c) => c.frameXPath));
    assert.deepEqual(heal.heals, []);
  });

  test('a call that is never a key says so once, and goes to Playwright as written', async () => {
    const w = login("<html><body><label><input type='checkbox' checked> Terms</label></body></html>");
    const heal = open(w);
    for (let i = 0; i < 2; i++) {
      const box = heal.page.getByRole('checkbox', { name: 'Terms', checked: true });
      assert.ok(box instanceof FakeLocator);
      await box.isChecked();
    }
    assert.deepEqual(w.lines, ['info: ' + notHealedMessage("page.getByRole('checkbox', { name: 'Terms', checked: true })",
      stateReason('checked'))]);
    assert.ok(!fs.existsSync(w.storePath));
  });

  test("isVisible() of a call is Playwright's own, never answered from a heal", async () => {
    const w = login();
    const heal = await healed(w);
    const before = fs.readFileSync(w.storePath, 'utf-8');
    assert.equal(await heal.page.getByRole('button', { name: 'Sign in' }).isVisible(), false);
    assert.equal(await w.page.locator('xpath=' + HEAL).isVisible(), true);      // the renamed button shows
    assert.equal(fs.readFileSync(w.storePath, 'utf-8'), before);
  });

  test('a string written as a call is reserved: no key or selector of the other entry points', () => {
    const w = login();
    const heal = open(w);
    const call = 'get_by_role("button", name="Go")';
    for (const [entry, run] of [['heal.locator()', () => heal.locator(call, '#go')],
      ['heal.locatorAll()', () => heal.locatorAll(call, '#go')], ['heal.interrupt()', () => heal.interrupt(call, '#go')]] as
      [string, () => unknown][]) {
      assert.throws(run, (error: Error) => error instanceof TypeError && error.message === reservedKeyMessage(call, entry));
    }
    const refused = "mendwright: the selector for 'go' is not usable: '" + call + "' is a get_by_* call, not a selector. "
      + 'Call heal.page.getByRole(...) on open(page, { healGetBy: true }), or pass an XPath, CSS or Playwright selector.';
    for (const run of [() => heal.locator('go', call), () => heal.locatorAll('go', call), () => heal.interrupt('go', call),
      () => heal.step('go', { when: call }, async () => undefined)]) {
      assert.throws(run, (error: Error) => error instanceof InvalidSelector && error.message === refused);
    }
    assert.ok(heal.page.locator(call) instanceof FakeLocator);              // to the page as written
    assert.deepEqual(w.page.calls, []);
    assert.ok(!fs.existsSync(w.storePath));
  });
});

describe('heal.present()', () => {
  test('looks at the call first, and at its heals only while it matches nothing', async () => {
    const w = login();
    const heal = await healed(w);
    assert.equal(await heal.present(KEY), true);                 // the saved heal, while the call misses
    assert.equal(await heal.present(PY_KEY), true);              // the call's key in another spelling
    w.page.goto(PAGE, URL);
    assert.equal(await heal.present(KEY), true);
    w.page.goto(PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in</button>'), URL);
    assert.equal(await heal.present(KEY), false);                // several: no heal answers
    w.page.goto('<html><body></body></html>', URL);
    assert.equal(await heal.present(KEY), false);
  });
});

const BANNER = "<div id='banner' role='dialog'><p>We use cookies</p><button id='reject' data-click-removes='#banner' "
  + "data-click-shows='main'>Reject</button></div>";
const REJECT = "//button[@id='reject']";

describe('with interruptions and popups', () => {
  test('an interruption is handled before a call\'s action, and logged before its key', async () => {
    const w = login(PAGE.replace('<main>', BANNER + '<main>'));
    const heal = open(w);
    heal.interrupt('cookies', REJECT);
    await signIn(heal);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['cookies', KEY]]);
    assert.deepEqual(w.page.actions.map((a) => a[0]), ['click', 'click']);
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'flow.interrupt').map((e) => e.before), [KEY]);
  });

  test('a banner that hides the page is handled before the wait', async () => {
    const w = login(PAGE.replace('<main>', BANNER + "<main style='display: none'>"));
    const heal = open(w, { timeoutMs: 10_000 });
    heal.interrupt('cookies', REJECT);
    const started = Date.now();
    await signIn(heal);
    assert.ok(Date.now() - started < 2000);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['cookies', KEY]]);
    assert.deepEqual(w.page.actions.at(-1), ['click', callSelector(w)]);
  });

  test('what a run function throws, and an interruption that keeps coming, are thrown as they are', async () => {
    const w = login(PAGE.replace('<main>', BANNER + "<main style='display: none'>"));
    const heal = open(w);
    const thrown = new AttentionRequired('the sign-in form was not there');
    heal.interrupt('session', REJECT, { run: async () => { throw thrown; } });
    assert.equal(await rejection(signIn(heal)), thrown);
    // A banner whose Reject button closes nothing: handled 3 times, then the step stops.
    const stays = login(PAGE.replace('<main>', BANNER.replace(" data-click-removes='#banner' data-click-shows='main'", '')
      + "<main style='display: none'>"));
    const looped = open(stays);
    looped.interrupt('cookies', REJECT);
    const error = await rejection(signIn(looped));
    assert.ok(error instanceof InterruptionLoop && !(error instanceof errors.TimeoutError), String(error));
  });

  test('dismiss looks for a popup before the action of a call, and of one that goes to Playwright', async () => {
    const w = login();
    const heal = open(w, { dismiss: true });
    const looked: [string, string][] = [];
    (heal as any).dismissBefore = async (key: string, _native: unknown, verb: string) => {
      looked.push([key, verb]);
      return false;
    };
    await signIn(heal);
    w.page.goto(TWO.replace('>Sign in later<', '>Sign in<'), URL);
    await assert.rejects(signIn(heal), /strict mode violation/);
    assert.deepEqual(looked, [[KEY, 'click'], [KEY, 'click']]);
  });
});

function total(text: string, testid = 'total'): string {
  return "<html><body><main><span id='total' data-testid='" + testid + "' class='amount'>" + text + '</span></main></body></html>';
}

describe('expectValues', () => {
  test('a read through a heal of a call is checked, under the call\'s key in any spelling', async () => {
    for (const key of [getByKey('getByTestId', 'total'), "get_by_test_id('total')"]) {
      const w = login(total('$12.00'));
      const heal = open(w, { expectValues: { [key]: 'money' } });
      assert.equal(await heal.page.getByTestId('total').innerText(), '$12.00');
      w.page.goto(total('$12.00', 'order-total'), URL);
      assert.equal(await heal.page.getByTestId('total').innerText(), '$12.00');
      assert.deepEqual(heal.heals.map((h) => h.confirmed), [true]);
      const other = login(total('$12.00'));
      const second = open(other, { expectValues: { [key]: 'money' } });
      await second.page.getByTestId('total').innerText();
      other.page.goto(total('3 items', 'order-total'), URL);
      const error = await rejection(second.page.getByTestId('total').innerText());
      assert.ok(error instanceof AttentionRequired && !(error instanceof errors.TimeoutError), String(error));
      assert.deepEqual(second.heals.map((h) => h.confirmed), [false]);
    }
  });

  test('two spellings of one call are refused, and a call written on an object is warned about', () => {
    const w = login();
    assert.throws(() => open(w, { expectValues: { [KEY]: 'money', [PY_KEY]: 'money' } }),
      (error: Error) => error instanceof TypeError && error.message === twoSpellingsMessage('expectValues', KEY, PY_KEY));
    const onPage = 'page.' + PY_KEY;
    const totalOnPage = "page.get_by_test_id('total')";
    open(w, { destructiveKeys: [onPage], expectValues: { [totalOnPage]: 'money' } });
    assert.deepEqual(w.lines, [
      'warning: ' + onObjectMessage('expectValues', totalOnPage, parse("get_by_test_id('total')") as GetByCall),
      'warning: ' + onObjectMessage('destructiveKeys', onPage, parse(KEY) as GetByCall)]);
  });

  test('a call written on an object in destructiveKeys is a key of its own: the call still heals', async () => {
    const w = login();
    const heal = open(w, { destructiveKeys: ['page.' + PY_KEY] });
    await signIn(heal);
    w.page.goto(RENAMED, URL);
    await signIn(heal);
    assert.deepEqual(heal.heals.map((h) => h.key), [KEY]);
    assert.ok(heal.store.isLockedDestructive('page.' + PY_KEY) && !heal.store.isLockedDestructive(KEY));
  });
});

describe('the error of a miss', () => {
  test('GetByNotFound is Playwright\'s TimeoutError and an AttentionRequired, and the others are as they were', () => {
    const cause = new AttentionRequired('missed');
    const error = new GetByNotFound('No matching element for: k (tried 1 candidate(s)).', { cause });
    assert.ok(error instanceof errors.TimeoutError && error instanceof AttentionRequired && error instanceof Error);
    assert.equal(error.name, 'TimeoutError');
    assert.equal(error.message, 'No matching element for: k (tried 1 candidate(s)).');
    assert.equal(error.cause, cause);
    assert.ok(!(error instanceof InterruptionLoop) && !(error instanceof InvalidSelector));
    const loop = new InterruptionLoop('3 handled');
    assert.ok(loop instanceof InterruptionLoop && loop instanceof AttentionRequired && !(loop instanceof GetByNotFound));
    assert.ok(!(loop instanceof errors.TimeoutError));
    assert.ok(!(new AttentionRequired('x') instanceof GetByNotFound) && !(new AttentionRequired('x') instanceof InterruptionLoop));
    assert.ok(!(new errors.TimeoutError('t') instanceof AttentionRequired));
    for (const value of [{}, null, 'x', new Error('x')] as unknown[]) assert.ok(!(value instanceof AttentionRequired));
  });

  test('a step that may not be there, in try/catch of TimeoutError, still works; timeout is the lookup\'s', async () => {
    const w = login();
    const heal = open(w, { timeoutMs: 10_000 });
    const started = Date.now();
    let skipped = false;
    try {
      await heal.page.getByRole('button', { name: 'Maybe later' }).click({ timeout: 300 });
    } catch (error) {
      if (!(error instanceof errors.TimeoutError)) throw error;
      skipped = true;
    }
    assert.ok(skipped && Date.now() - started < 3000);
    // timeout: 0 is no limit to Playwright, and the healer's own timeout for the lookup.
    const quick = open(w, { timeoutMs: 200 });
    const before = Date.now();
    await assert.rejects(quick.page.getByRole('button', { name: 'Maybe later' }).click({ timeout: 0 }), GetByNotFound);
    assert.ok(Date.now() - before < 3000);
  });
});
