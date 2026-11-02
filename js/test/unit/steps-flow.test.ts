// heal.step() and heal.runSteps() on a fake page: a checkout whose steps the site shows in
// another order, on one page, or not at all (the Python package's tests/test_steps.py, for its
// async healer). Each page is HTML at https://shop.test/checkout/<path>, and a click on an
// element with data-click-goes goes to another page (see support/fakepage.ts). Each safety
// rule has a test here that fails without it.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { beforeEach, describe, test } from 'node:test';

import type { Page } from '@playwright/test';

import { AttentionRequired, InterruptionLoop, InvalidSelector } from '../../src/errors';
import { severalMessage } from '../../src/flow';
import type { Healer, OpenOptions } from '../../src/healer';
import {
  WAITING_STEP, againMessage, capMessage, changedMessage, droppedMessage, plainMessage, unknownMessage, type Step,
  type StepFunction,
} from '../../src/steps';
import { loadStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, world, type World } from '../support/env';
import type { FakePageOptions } from '../support/fakepage';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

beforeEach(cleanEnv);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const SITE = 'https://shop.test/checkout/';
const SHIPPING = "//h1[.='Shipping']";
const PAYMENT = "//h1[.='Payment']";
const REVIEW = "//button[.='Place order']";

const goes = (to: string) => " data-click-goes='" + SITE + to + "'";

function shippingPage(go: string, field = 'ship-name'): string {
  return "<h1>Shipping</h1><label for='" + field + "'>Full name</label><input id='" + field + "' name='" + field
    + "'><button id='next' type='button'" + goes(go) + '>Next</button>';
}

function paymentPage(go: string): string {
  return "<h1>Payment</h1><label for='card-number'>Card number</label><input id='card-number' name='card'>"
    + "<button id='next' type='button'" + goes(go) + '>Next</button>';
}

function reviewPage(go = 'done'): string {
  return "<h1>Review</h1><button id='place' type='button'" + goes(go) + '>Place order</button>';
}

const DONE = '<h1>Thank you</h1><p>Your order is placed.</p>';
const NEXT_X = "<button id='next' type='button'" + goes('x') + '>Next</button>';
const UPSELL = "<h1>Add insurance?</h1><button id='no-thanks' type='button' data-click-goes='{go}'>No thanks</button>";

const html = (body: string) => '<html><body>' + body + '</body></html>';

/** A checkout: its pages by path, served at SITE + path, opening at start. */
function shop(pages: Record<string, string>, start: string, options: FakePageOptions = {}): World {
  const w = world(html(pages[start]), { url: SITE + start, ...options });
  w.page.site = Object.fromEntries(Object.entries(pages).map(([p, body]) => [SITE + p, html(body)]));
  return w;
}

/** The pages of a checkout whose steps come in the order of names, at /checkout/a,
 * /checkout/b, ... (the addresses say where a step is in the flow, not which step it is).
 * declined: payment's Next sends the card back to its own page. */
function inOrder(names: string[], extra: { field?: string; declined?: boolean; pages?: Record<string, string> } = {}):
    Record<string, string> {
  const paths = [...names.map((_, i) => String.fromCharCode(97 + i)), 'done'];
  const out: Record<string, string> = { done: DONE };
  names.forEach((name, i) => {
    const go = paths[i + 1];
    if (name === 'shipping') out[paths[i]] = shippingPage(go, extra.field);
    else if (name === 'payment') out[paths[i]] = paymentPage(extra.declined ? paths[i] : go);
    else if (name === 'review') out[paths[i]] = reviewPage(go);
    else out[paths[i]] = (extra.pages ?? {})[name].split('{go}').join(SITE + go);
  });
  return out;
}

/** w, with the store and the journal of first: the next run of a flow. */
function sameStore(first: World, w: World): World {
  return {
    ...w, dir: first.dir, storePath: first.storePath,
    open: (options: OpenOptions = {}) => w.open({ storePath: first.storePath, ...options }),
  };
}

/** A healer as the Python package's tests open one: no fingerprint tier, and a model that
 * proposes nothing unless it is given one. */
function open(w: World, options: OpenOptions = {}): Healer {
  return w.open({ provider: new FakeModel(), fingerprint: false, ...options });
}

type Call = [string, ...unknown[]];

/** A step function that makes these calls on the page it gets, such as ['fill', '#ship-name',
 * 'Ada'], each awaited. */
function does(...calls: Call[]): StepFunction {
  return async (page: Page) => {
    for (const [verb, ...args] of calls) {
      await (page as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[verb](...args);
    }
  };
}

/** The approved example's three steps, and an optional upsell. */
function checkout(heal: Healer, options: { reviewLast?: boolean; upsell?: boolean } = {}): void {
  heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada'], ['click', '#next']));
  heal.step('payment', { when: PAYMENT }, does(['fill', '#card-number', '4242 4242 4242 4242'], ['click', '#next']));
  if (options.upsell) {
    heal.step('upsell', { when: "//h1[.='Add insurance?']", optional: true }, does(['click', '#no-thanks']));
  }
  heal.step('review', { when: REVIEW, last: options.reviewLast ?? true }, does(['click', '#place']));
}

const run = (heal: Healer, options: { timeoutMs?: number } = {}) => heal.runSteps({ pollMs: 5, ...options });
const book = (heal: Healer) => (heal as unknown as { stepBook: Map<string, Step> }).stepBook;
const visits = (w: World) => w.page.visits.map((url) => url.slice(SITE.length));
const done = (w: World) => w.page.actions.map(([verb, selector]) => [selector, verb]);
const filled = (w: World) => Object.fromEntries([...w.page.filled].map(([el, value]) => [el.id, value]));
const warnings = (w: World) => w.lines.filter((l) => l.startsWith('warning: ')).map((l) => l.slice('warning: '.length));
const infos = (w: World) => w.lines.filter((l) => l.startsWith('info: ')).map((l) => l.slice('info: '.length));

/** The flow.step events, without ts, event and seconds. */
const stepEvents = (w: World) => journalOf(w.dir).filter((e) => e.event === 'flow.step')
  .map(({ ts: _ts, event: _event, seconds: _seconds, ...rest }) => rest);

/** The flow.stop events, without ts and event. Each is the last line before the throw. */
function stops(w: World): Record<string, unknown>[] {
  const events = journalOf(w.dir);
  const out = events.filter((e) => e.event === 'flow.stop').map(({ ts: _ts, event: _event, ...rest }) => rest);
  if (out.length) assert.equal(events[events.length - 1].event, 'flow.stop');
  return out;
}

/** What a promise rejects with. */
async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  assert.fail('it did not reject');
}

describe('the flow', () => {
  test('steps in the registered order', async () => {
    const w = shop(inOrder(['shipping', 'payment', 'review']), 'a');
    const heal = open(w);
    checkout(heal);

    assert.deepEqual(await run(heal), ['shipping', 'payment', 'review']);

    assert.deepEqual(visits(w), ['a', 'b', 'c', 'done']);
    assert.deepEqual(done(w), [['css=#ship-name', 'fill'], ['css=#next', 'click'], ['css=#card-number', 'fill'],
      ['css=#next', 'click'], ['css=#place', 'click']]);
    assert.deepEqual(stepEvents(w), [
      { key: 'shipping', position: 1, registered: 1, page: '/checkout/a', outcome: 'ran' },
      { key: 'payment', position: 2, registered: 2, page: '/checkout/b', outcome: 'ran' },
      { key: 'review', position: 3, registered: 3, page: '/checkout/c', outcome: 'ran' }]);
    assert.ok(journalOf(w.dir).filter((e) => e.event === 'flow.step').every((e) => typeof e.seconds === 'number'));
    assert.deepEqual(infos(w).filter((l) => l.startsWith('ran step')).map((l) => l.split(' (')[0]), [
      "ran step 'shipping' on /checkout/a", "ran step 'payment' on /checkout/b", "ran step 'review' on /checkout/c"]);
    assert.deepEqual(warnings(w), []);
  });

  test('the site asks for the card before the address: one WARNING names the order', async () => {
    const w = shop(inOrder(['payment', 'shipping', 'review']), 'a');
    const heal = open(w);
    checkout(heal);

    assert.deepEqual(await run(heal), ['payment', 'shipping', 'review']);

    assert.deepEqual(done(w).filter(([, verb]) => verb === 'fill'), [['css=#card-number', 'fill'], ['css=#ship-name', 'fill']]);
    assert.deepEqual(filled(w), { 'card-number': '4242 4242 4242 4242', 'ship-name': 'Ada' });
    assert.deepEqual(warnings(w), ["ran 'payment' before 'shipping': the site changed the order of the steps"]);
    assert.deepEqual(stepEvents(w).map((e) => [e.key, e.position, e.registered]),
      [['payment', 1, 2], ['shipping', 2, 1], ['review', 3, 3]]);
  });

  test('a one-page checkout runs the steps in the order registered, and the last step last', async () => {
    const one = shippingPage('x').replace(NEXT_X, '') + paymentPage('x').replace(NEXT_X, '') + reviewPage();
    const w = shop({ one, done: DONE }, 'one');
    const heal = open(w);
    // The last step is registered first: it still waits until no other step shows.
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));
    heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada']));
    heal.step('payment', { when: PAYMENT }, does(['fill', '#card-number', '4242']));

    assert.deepEqual(await run(heal), ['shipping', 'payment', 'review']);

    assert.deepEqual(done(w).map(([, verb]) => verb), ['fill', 'fill', 'click']);
    assert.deepEqual(visits(w), ['one', 'done']);
    assert.deepEqual(warnings(w), []);   // the last step's place is not an order
  });

  test('two steps on one page, then the rest', async () => {
    const both = shippingPage('x').replace(NEXT_X, '') + paymentPage('review');
    const w = shop({ both, review: reviewPage(), done: DONE }, 'both');
    const heal = open(w);
    heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada']));
    heal.step('payment', { when: PAYMENT }, does(['fill', '#card-number', '4242'], ['click', '#next']));
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    assert.deepEqual(await run(heal), ['shipping', 'payment', 'review']);
    assert.deepEqual(visits(w), ['both', 'review', 'done']);
  });

  test('steps that showed together are looked at again after each', async () => {
    // The upsell shows with shipping, and shipping's Next goes to the review page: the upsell
    // is gone by then, so its function must not run there.
    const both = shippingPage('review') + UPSELL.split('{go}').join(SITE + 'review');
    const w = shop({ both, review: reviewPage(), done: DONE }, 'both');
    const heal = open(w);
    heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada'], ['click', '#next']));
    heal.step('upsell', { when: "//h1[.='Add insurance?']", optional: true }, does(['click', '#no-thanks']));
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    assert.deepEqual(await run(heal), ['shipping', 'review']);
    assert.deepEqual(visits(w), ['both', 'review', 'done']);
  });

  test('a step that shows a moment later is waited for', async () => {
    // Each page loads 30 ms after its click: until then the step that ran is still the only
    // one on the page, which is not yet a step that came back.
    const pages = Object.fromEntries(Object.entries(inOrder(['shipping', 'payment', 'review']))
      .map(([p, body]) => [p, body.split('data-click-goes=').join("data-click-goes-after='30' data-click-goes=")]));
    const w = shop(pages, 'a');
    const heal = open(w);
    checkout(heal);

    assert.deepEqual(await run(heal, { timeoutMs: 2000 }), ['shipping', 'payment', 'review']);
    assert.deepEqual(done(w).map(([, verb]) => verb), ['fill', 'click', 'fill', 'click', 'click']);
    assert.deepEqual(filled(w), { 'ship-name': 'Ada', 'card-number': '4242 4242 4242 4242' });
    assert.deepEqual(stepEvents(w).map((e) => e.page), ['/checkout/a', '/checkout/b', '/checkout/c']);
  });

  test('a page that loads between its address and its landmarks is looked at again', async () => {
    // The next page loads only when its landmarks are looked at, not when its address is read,
    // 30 ms after the click: the page changes between the two, as when a navigation commits
    // right after runSteps() read the address.
    const pages = Object.fromEntries(Object.entries(inOrder(['shipping', 'payment', 'review']))
      .map(([p, body]) => [p, body.split('data-click-goes=').join('data-late-goes=')]));
    const w = shop(pages, 'a');
    let pending: { url: string; at: number } | null = null;
    const clickOn = w.page.clickOn.bind(w.page);
    w.page.clickOn = (el: Element) => {
      const to = el.getAttribute('data-late-goes');
      if (to) pending = { url: to, at: performance.now() + 30 };
      clickOn(el);
    };
    const query = w.page.query.bind(w.page);
    w.page.query = (selector: string, scope: Element | null = null) => {
      if (pending && performance.now() >= pending.at) {
        const { url } = pending;
        pending = null;
        w.page.navigate(url);
      }
      return query(selector, scope);
    };
    const heal = open(w);
    checkout(heal);

    assert.deepEqual(await run(heal, { timeoutMs: 2000 }), ['shipping', 'payment', 'review']);
    // Each step is given the page it showed on, not the one before it.
    assert.deepEqual(stepEvents(w).map((e) => e.page), ['/checkout/a', '/checkout/b', '/checkout/c']);
  });

  test("runSteps() waits as long as the healer's timeoutMs by default", async () => {
    const w = shop({ a: '<h1>Nothing</h1>' }, 'a');
    const heal = open(w, { timeoutMs: 70 });
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));
    await assert.rejects(run(heal), /within 70 ms/);
  });
});

describe('where runSteps() stops', () => {
  test('a step that comes back stops the flow: the declined card is never paid twice', async () => {
    const w = shop(inOrder(['shipping', 'payment', 'review'], { declined: true }), 'a');
    const heal = open(w);
    checkout(heal);

    const error = await rejection(run(heal, { timeoutMs: 40 }));

    assert.ok(error instanceof AttentionRequired);
    assert.equal(error.message, againMessage('payment', '/checkout/b', 40, ['shipping', 'payment']));
    assert.ok(error.message.startsWith("mendwright: 'payment' is on the page again after it ran"));
    assert.ok(error.message.includes('its submit probably failed'));
    assert.equal(done(w).filter(([, verb]) => verb === 'fill').length, 2);   // the card once, never twice
    assert.deepEqual(stepEvents(w).map((e) => e.key), ['shipping', 'payment']);
    assert.deepEqual(stops(w), [{ key: 'payment', code: 'step_again', page: '/checkout/b' }]);
  });

  test('a page with a step nobody registered stops the flow, naming the page and every landmark', async () => {
    const gift = "<h1>Gift wrap</h1><button id='wrap' type='button' data-click-goes='{go}'>Wrap it</button>";
    const w = shop(inOrder(['shipping', 'gift', 'payment', 'review'], { pages: { gift } }), 'a');
    const heal = open(w);
    checkout(heal);

    const error = await rejection(run(heal, { timeoutMs: 30 }));

    const waiting = [...book(heal).values()].slice(1);
    assert.equal(error.message, unknownMessage('/checkout/b', waiting, ['shipping'], 30));
    assert.ok(error.message.startsWith('mendwright: no step showed on /checkout/b within 30 ms.'));
    assert.ok(error.message.includes("looked for 'payment' (//h1[.='Payment']) and 'review' (//button[.='Place order'])"));
    assert.ok(error.message.includes("Steps that ran: 'shipping'."));
    assert.ok(error.message.includes('add it with heal.step()'));
    assert.deepEqual(stops(w), [{ key: WAITING_STEP, code: 'no_step', page: '/checkout/b' }]);
  });

  test('a new page that shows part of a step that ran is named as a new step, not a failed submit', async () => {
    // The site added gift options on a page that keeps the Shipping heading: shipping's submit
    // worked, so the error must not send the reader after a failed submit.
    const gift = "<h1>Shipping</h1><p>Ada, 1 Main St</p><h2>Gift options</h2>"
      + "<button id='wrap' type='button' data-click-goes='{go}'>Wrap it</button>";
    const w = shop(inOrder(['shipping', 'gift', 'payment', 'review'], { pages: { gift } }), 'a');
    const heal = open(w);
    checkout(heal);

    const error = await rejection(run(heal, { timeoutMs: 30 }));

    assert.equal(error.message, changedMessage('shipping', '/checkout/a', '/checkout/b', 30, ['shipping']));
    assert.ok(error.message.startsWith("mendwright: 'shipping' shows on /checkout/b, and it ran on /checkout/a, so the page "
      + 'changed after it ran'));
    assert.ok(error.message.includes('add it with heal.step()'));
    assert.ok(!error.message.includes('its submit probably failed'));
    assert.deepEqual(visits(w), ['a', 'b']);
    assert.deepEqual(stops(w), [{ key: 'shipping', code: 'step_moved', page: '/checkout/b' }]);
  });

  test('an optional step that shows only sometimes', async () => {
    const first = shop(inOrder(['shipping', 'payment', 'upsell', 'review'], { pages: { upsell: UPSELL } }), 'a');
    const heal = open(first);
    checkout(heal, { upsell: true });
    assert.deepEqual(await run(heal), ['shipping', 'payment', 'upsell', 'review']);

    const w = sameStore(first, shop(inOrder(['shipping', 'payment', 'review']), 'a'));
    const again = open(w);
    checkout(again, { upsell: true });
    assert.deepEqual(await run(again), ['shipping', 'payment', 'review']);
    assert.deepEqual(warnings(w), []);
    assert.ok(infos(w).includes("step 'upsell' did not show, and it is optional (optional: true)"));
  });

  test('a step the site dropped is named once the flow ended with the last step', async () => {
    const w = shop(inOrder(['payment', 'review']), 'a');
    const heal = open(w);
    checkout(heal);

    assert.deepEqual(await run(heal), ['payment', 'review']);

    assert.deepEqual(warnings(w), [droppedMessage('shipping', 'review')]);
    assert.ok(warnings(w)[0].includes('pass optional: true to heal.step('));
    assert.deepEqual(stepEvents(w).slice(-1), [{ key: 'shipping', registered: 1, outcome: 'missing' }]);
  });

  test('without a last step the flow ends once every step that is not optional ran', async () => {
    const w = shop(inOrder(['shipping', 'payment', 'upsell'], { pages: { upsell: UPSELL } }), 'a');
    const heal = open(w);
    heal.step('shipping', { when: SHIPPING }, does(['click', '#next']));
    heal.step('payment', { when: PAYMENT }, does(['click', '#next']));
    heal.step('upsell', { when: "//h1[.='Add insurance?']", optional: true }, does(['click', '#no-thanks']));

    assert.deepEqual(await run(heal), ['shipping', 'payment']);   // the upsell after them is not waited for
    assert.deepEqual(visits(w), ['a', 'b', 'c']);
  });

  test('without a last step a step the site dropped stops the flow, and the message says so', async () => {
    const w = shop(inOrder(['shipping', 'review']), 'a');
    const heal = open(w);
    checkout(heal, { reviewLast: false });

    const error = await rejection(run(heal, { timeoutMs: 30 }));

    assert.equal(error.message, unknownMessage('/checkout/done', [book(heal).get('payment') as Step],
      ['shipping', 'review'], 30, true));
    assert.ok(error.message.includes('No step has last: true, so runSteps() waits until every step that is not '
      + 'optional ran, and the site may have dropped one'));
    assert.deepEqual(visits(w), ['a', 'b', 'done']);
  });

  test('an error in a step is thrown as it is, after flow.step records the step as failed', async () => {
    const w = shop(inOrder(['shipping', 'payment', 'review']), 'a');
    const heal = open(w);
    const boom = new RangeError('the card form is broken');
    heal.step('shipping', { when: SHIPPING }, does(['click', '#next']));
    heal.step('payment', { when: PAYMENT }, async () => {
      throw boom;
    });
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    assert.equal(await rejection(run(heal)), boom);
    assert.deepEqual(stepEvents(w).slice(-1), [{ key: 'payment', position: 2, registered: 2, page: '/checkout/b',
      outcome: 'failed', error: 'RangeError' }]);
    assert.equal((heal as unknown as { stepNow(): unknown }).stepNow(), null);
  });

  test('a flow that keeps growing stops after 3 times as many steps as were registered', async () => {
    const w = shop({ a: '<h1>Anything</h1>' }, 'a');
    const heal = open(w);
    let count = 0;
    const grow = async () => {
      count += 1;
      if (count < 6) heal.step('step' + count, { when: '//h1' }, grow);   // so it would end, and fail, without the cap
    };
    heal.step('step0', { when: '//h1' }, grow);

    const error = await rejection(run(heal));

    assert.ok(error instanceof AttentionRequired);
    assert.equal(error.message, capMessage(3, 1));
    assert.equal(count, 3);
    assert.deepEqual(stops(w), [{ key: WAITING_STEP, code: 'step_cap', ran: 3 }]);
  });
});

describe('the step function', () => {
  test('gets heal.page', async () => {
    const w = shop(inOrder(['review']), 'a');
    const heal = open(w);
    const got: unknown[] = [];
    heal.step('review', { when: REVIEW, last: true }, async (page) => {
      got.push(page);
      await page.click('#place');
    });
    await run(heal);
    assert.deepEqual(got, [heal.page]);
  });

  test('may be a plain function that returns the promise', async () => {
    const w = shop(inOrder(['review']), 'a');
    const heal = open(w);
    heal.step('review', { when: REVIEW, last: true }, (page) => page.click('#place'));
    assert.deepEqual(await run(heal), ['review']);
    assert.deepEqual(visits(w), ['a', 'done']);
    assert.deepEqual(done(w), [['css=#place', 'click']]);
  });

  for (const name of ['review', 'shipping']) {
    test('one that returns no promise throws TypeError, and the step counts as failed (' + name + ')', async () => {
      // mendwright waits for what the function returns: without a promise it cannot know the
      // step is done, so the step must not count as having run, last or not.
      const w = shop(name === 'shipping' ? inOrder(['shipping', 'review']) : inOrder(['review']), 'a');
      const heal = open(w);
      const plain = (() => undefined) as unknown as StepFunction;
      heal.step('shipping', { when: SHIPPING }, plain);
      heal.step('review', { when: REVIEW, last: true }, plain);

      const error = await rejection(run(heal));

      assert.ok(error instanceof TypeError);
      assert.equal(error.message, plainMessage(name, undefined));
      assert.deepEqual(w.page.actions, []);
      assert.deepEqual(stepEvents(w), [{ key: name, position: 1, registered: name === 'shipping' ? 1 : 2,
        page: '/checkout/a', outcome: 'failed', error: 'TypeError' }]);
    });
  }
});

describe('interruptions while runSteps() waits', () => {
  const WALL = "<div id='wall'><p>We use cookies.</p><button id='accept' type='button' data-click-removes='#wall' "
    + "data-click-shows='#main'>Accept</button></div><div id='main' style='display: none'>" + reviewPage() + '</div>';

  test('are handled, as before the next step', async () => {
    const w = shop({ a: WALL, done: DONE }, 'a', { blocker: '#wall' });
    const heal = open(w);
    heal.interrupt('consent', '#accept');
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    assert.deepEqual(await run(heal), ['review']);

    assert.ok(warnings(w).includes("handled interruption 'consent' before '<next step>'"));
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'flow.interrupt').map((e) => [e.key, e.before]),
      [['consent', WAITING_STEP]]);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['consent', WAITING_STEP]]);
  });

  test('one whose action fails is tried once in a wait', async () => {
    // Something covers the promotion's own Close button, so clicking it fails.
    const w = shop({ a: "<div id='cover'></div><button id='promo-close' type='button'>Close</button>" }, 'a',
      { blocker: '#cover' });
    const heal = open(w);
    heal.interrupt('promo', '#promo-close');
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    const error = await rejection(run(heal, { timeoutMs: 60 }));

    assert.ok(error.message.startsWith('mendwright: no step showed on /checkout/a'));
    assert.equal(warnings(w).length, 1);
    assert.ok(warnings(w)[0].startsWith("could not handle interruption 'promo' before '<next step>'"));
    assert.deepEqual(done(w), [['css=#promo-close', 'click']]);
  });

  test('one whose action fails leaves the others to be handled', async () => {
    // The promotion's Close button is covered, so its click fails; a consent wall that hides
    // the review step loads a moment later and must still be answered.
    const w = shop({ a: "<div id='cover'></div><button id='promo-close' type='button'>Close</button>", wall: WALL,
      done: DONE }, 'a', { blocker: '#cover, #wall' });
    const query = w.page.query.bind(w.page);
    let looks = 0;
    w.page.query = (selector: string, scope: Element | null = null) => {
      if (selector.includes('Place order') && w.page.url().endsWith('/a')
          && w.page.actions.some(([verb, s]) => verb === 'click' && s === 'css=#promo-close')) {
        looks += 1;
        if (looks === 2) w.page.navigate(SITE + 'wall');
      }
      return query(selector, scope);
    };
    const heal = open(w);
    heal.interrupt('promo', '#promo-close');
    heal.interrupt('consent', '#accept');
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    assert.deepEqual(await run(heal, { timeoutMs: 1000 }), ['review']);

    assert.deepEqual(done(w), [['css=#promo-close', 'click'], ['css=#accept', 'click'], ['css=#place', 'click']]);
    assert.equal(warnings(w).length, 2);
    assert.ok(warnings(w)[0].startsWith("could not handle interruption 'promo' before '<next step>'"));
    assert.equal(warnings(w)[1], "handled interruption 'consent' before '<next step>'");
  });

  test('one that keeps coming back ends the wait after 3, as for one action', async () => {
    // The promotion's Close button takes it away only on the 5th click.
    const w = shop({ a: "<div id='promo'><button id='promo-close' type='button'>Close</button></div>"
      + "<div id='main' style='display: none'>" + reviewPage() + '</div>', done: DONE }, 'a');
    const clickOn = w.page.clickOn.bind(w.page);
    let closes = 0;
    w.page.clickOn = (el: Element) => {
      if (el.id !== 'promo-close') return clickOn(el);
      closes += 1;
      if (closes >= 5) {
        w.page.document.getElementById('promo')?.remove();
        w.page.document.getElementById('main')?.removeAttribute('style');
      }
    };
    const heal = open(w);
    heal.interrupt('promo', '#promo-close');
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));

    const error = await rejection(run(heal, { timeoutMs: 50 }));

    assert.ok(error instanceof InterruptionLoop);
    assert.equal(closes, 3);
    assert.deepEqual(stops(w), [{ key: 'promo', code: 'interruption_loop', before: WAITING_STEP }]);
  });
});

describe('url patterns and landmarks', () => {
  test('a step found by its url, which is normalized as the page guard normalizes a path', async () => {
    const w = shop({ 'orders/987/pay': paymentPage('orders/987/review'), 'orders/987/review': reviewPage(),
      done: DONE }, 'orders/987/pay');
    const heal = open(w);
    heal.step('payment', { url: '/Checkout/Orders/123/PAY/' }, does(['click', '#next']));
    heal.step('review', { when: REVIEW, url: '*/review', last: true }, does(['click', '#place']));
    assert.deepEqual(await run(heal), ['payment', 'review']);
  });

  test('a landmark on the wrong url does not show the step, and the message names the url', async () => {
    const w = shop(inOrder(['payment', 'review']), 'a');
    const heal = open(w);
    heal.step('payment', { when: PAYMENT, url: '/checkout/pay' }, does(['click', '#next']));
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));
    const error = await rejection(run(heal, { timeoutMs: 0 }));
    assert.ok(error.message.includes("'payment' (//h1[.='Payment'], url /checkout/pay)"), error.message);
  });

  test('a step with a url and a landmark shows only with both', async () => {
    // url scopes every step to the checkout and the landmarks tell them apart: payment's url
    // matches the review page, but its landmark is not there.
    const w = shop(inOrder(['shipping', 'review']), 'a');
    const heal = open(w);
    heal.step('shipping', { when: SHIPPING, url: '/checkout/*' }, does(['fill', '#ship-name', 'Ada'], ['click', '#next']));
    heal.step('payment', { when: PAYMENT, url: '/checkout/*' }, does(['fill', '#card-number', '4242'], ['click', '#next']));
    heal.step('review', { when: REVIEW, url: '/checkout/*', last: true }, does(['click', '#place']));
    assert.deepEqual(await run(heal), ['shipping', 'review']);
  });

  test('a later selector of a landmark list shows the step', async () => {
    const w = shop(inOrder(['payment', 'review']), 'a');
    const heal = open(w);
    heal.step('payment', { when: ['#gone', PAYMENT] }, does(['click', '#next']));
    heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));
    assert.deepEqual(await run(heal), ['payment', 'review']);
  });

  test('any visible match of a landmark shows the step, an XPath or a CSS selector', async () => {
    const page = "<h1 style='display: none'>Payment</h1><h1>Payment</h1>"
      + "<h2 class='step-title' style='display: none'>Shipping</h2><h2 class='step-title'>Payment</h2>"
      + "<button id='next' type='button'" + goes('done') + '>Next</button>';
    for (const when of [PAYMENT, '.step-title']) {
      const w = shop({ a: page, done: DONE }, 'a');
      const heal = open(w);
      heal.step('payment', { when, last: true }, does(['click', '#next']));
      assert.deepEqual(await run(heal), ['payment'], when);
    }
  });

  test('a landmark that matches more elements than mendwright looks at is named once per page', async () => {
    const page = "<h1 style='display: none'>Payment</h1>".repeat(5) + "<h1 id='pay-title'>Payment</h1>"
      + "<button id='next' type='button'" + goes('done') + '>Next</button>';
    const w = shop({ a: page, done: DONE }, 'a');
    const heal = open(w);
    heal.step('payment', { when: PAYMENT, last: true }, does(['click', '#next']));

    await assert.rejects(run(heal), /no step showed on \/checkout\/a/);   // it looks every 20 ms for 100 ms

    const message = severalMessage('payment', PAYMENT, 6, 5, 'landmark');
    assert.equal(message, "step 'payment' did not show: its landmark \"//h1[.='Payment']\" matches 6 elements, and "
      + 'mendwright looks at the first 5 of them only, and none of those is visible. Use a landmark that matches one '
      + 'element: its id or test id, its role and name, as in role=button[name="Accept all"], its whole text, as in '
      + "text=\"Accept all\", or an XPath of its tag and text, as in //button[normalize-space()='Accept all'].");
    assert.deepEqual(warnings(w).filter((l) => l.includes('did not show')), [message]);
    heal.step('payment', { when: '#pay-title', last: true }, does(['click', '#next']));
    assert.deepEqual(await run(heal), ['payment']);
  });

  test('so is a CSS landmark, of which mendwright looks at the first 5 matches', async () => {
    const page = "<h2 class='step-title' hidden>Shipping</h2>".repeat(5) + "<h2 class='step-title'>Payment</h2>";
    const w = shop({ a: page }, 'a');
    const heal = open(w);
    heal.step('payment', { when: '.step-title', last: true }, does(['click', '#next']));
    await assert.rejects(run(heal, { timeoutMs: 40 }), /no step showed on \/checkout\/a/);
    assert.deepEqual(warnings(w), [severalMessage('payment', '.step-title', 6, 5, 'landmark')]);
  });

  test('a landmark that matches several is not named when a later one shows the step', async () => {
    const page = "<h1 style='display: none'>Payment</h1>".repeat(5) + "<h1 id='pay-title'>Payment</h1>"
      + "<button id='next' type='button'" + goes('done') + '>Next</button>';
    const w = shop({ a: page, done: DONE }, 'a');
    const heal = open(w);
    heal.step('payment', { when: [PAYMENT, '#pay-title'], last: true }, does(['click', '#next']));
    assert.deepEqual(await run(heal), ['payment']);
    assert.deepEqual(w.lines.filter((l) => l.includes('did not show')), []);
  });

  test('a landmark is never healed, and nothing is saved for it', async () => {
    // The site renamed the payment page's heading: the step does not show, and no model is
    // asked, though one would propose the new heading, and there is a fingerprint tier.
    const w = shop({ a: paymentPage('done').replace('<h1>Payment</h1>', '<h1>Pay now</h1>'), done: DONE }, 'a');
    const model = new FakeModel(["//h1[.='Pay now']"]);
    const heal = open(w, { provider: model, fingerprint: true });
    heal.step('payment', { when: PAYMENT, last: true }, does(['click', '#next']));

    await assert.rejects(run(heal, { timeoutMs: 30 }), /no step showed on \/checkout\/a/);

    assert.deepEqual(model.bundles, []);
    assert.deepEqual(journalOf(w.dir).filter((e) => String(e.event).startsWith('heal.')), []);
    assert.equal(loadStore(w.storePath).has(PAYMENT), false);
    // And a landmark that shows is not saved either: only the step's own keys are.
    const other = shop(inOrder(['shipping', 'payment', 'review']), 'a');
    const flow = open(other);
    checkout(flow);
    await run(flow);
    assert.deepEqual(loadStore(other.storePath).keys().sort(), ['#card-number', '#next', '#place', '#ship-name']);
  });

  test('a landmark the browser cannot parse throws InvalidSelector naming the step', async () => {
    // mendwright for TypeScript leaves XPath syntax to the browser, so this comes from
    // runSteps(), the first time the landmark is looked at.
    const w = shop(inOrder(['review']), 'a');
    const heal = open(w);
    heal.step('review', { when: '//button[', last: true }, does(['click', '#place']));
    const error = await rejection(run(heal));
    assert.ok(error instanceof InvalidSelector);
    assert.match(error.message, /^mendwright: the selector for 'review' is not usable: the browser rejected a selector/);
    assert.deepEqual(w.page.actions, []);
  });
});

describe('the page guard inside a step', () => {
  /** The flow in the registered order, which saves where and during which step each key was
   * found. */
  async function firstRun(): Promise<World> {
    const w = shop(inOrder(['shipping', 'payment', 'review']), 'a');
    const heal = open(w);
    checkout(heal);
    await run(heal);
    return w;
  }

  test('a key saves the steps it was found during', async () => {
    const w = await firstRun();
    const store = loadStore(w.storePath);
    assert.deepEqual(store.metaOf('#ship-name').steps, ['shipping']);
    assert.deepEqual(store.metaOf('#ship-name').pages, ['/checkout/a']);
    assert.deepEqual(store.metaOf('#next').steps, ['payment', 'shipping']);   // used in both
    assert.deepEqual(store.metaOf('#card-number').steps, ['payment']);
  });

  test('a renamed field in a step that moved heals there, with one INFO line', async () => {
    const first = await firstRun();
    // The site now asks for the card first, and the shipping form's field has a new id.
    const w = sameStore(first, shop(inOrder(['payment', 'shipping', 'review'], { field: 'ship-full-name' }), 'a'));
    const model = new FakeModel(["//input[@id='ship-full-name']"]);
    const heal = open(w, { provider: model });
    checkout(heal);

    assert.deepEqual(await run(heal), ['payment', 'shipping', 'review']);

    assert.deepEqual(filled(w), { 'card-number': '4242 4242 4242 4242', 'ship-full-name': 'Ada' });
    assert.deepEqual(heal.heals.map((h) => [h.key, h.new]), [['#ship-name', "//input[@id='ship-full-name']"]]);
    assert.equal(model.bundles.length, 1);
    assert.equal(model.bundles[0].page, '/checkout/b');
    assert.equal(model.bundles[0].lastPage, '/checkout/a');
    assert.deepEqual(infos(w).filter((l) => l.startsWith('the page guard')), ["the page guard lets '#ship-name' heal on "
      + "/checkout/b: it was found during step 'shipping' before (last on /checkout/a), and this is the page "
      + "'shipping' showed on"]);
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'flow.page_guard'), []);
  });

  test('outside runSteps() the guard declines as before', async () => {
    const first = await firstRun();
    const w = sameStore(first, shop(inOrder(['payment', 'shipping', 'review'], { field: 'ship-full-name' }), 'b'));
    const model = new FakeModel(["//input[@id='ship-full-name']"]);
    const heal = open(w, { provider: model });

    await assert.rejects(heal.locator('#ship-name', '#ship-name', { timeoutMs: 0 }).resolve(),
      /'#ship-name' was last found on \/checkout\/a, and this page is \/checkout\/b/);
    assert.deepEqual(model.bundles, []);
  });

  test('a key of another step is declined on a new page', async () => {
    const first = await firstRun();
    // On the payment step's new page, the script looks for the shipping step's field.
    const w = sameStore(first, shop(inOrder(['review', 'payment']), 'b'));
    const model = new FakeModel(["//input[@id='card-number']"]);   // a field that would pass
    const heal = open(w, { provider: model });
    heal.step('payment', { when: PAYMENT }, does(['fill', '#ship-name', 'Ada']));

    await assert.rejects(run(heal), /'#ship-name' was last found on \/checkout\/a/);
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(filled(w), {});
  });

  test('a key missed after the step went to another page is declined', async () => {
    // '#ship-name' was found during shipping. This time shipping's function looks for it again
    // after its own Next went to the payment page, which is not shipping's page.
    const first = await firstRun();
    const w = sameStore(first, shop(inOrder(['shipping', 'payment', 'review']), 'a'));
    const model = new FakeModel(["//input[@id='card-number']"]);   // a field that would pass
    const heal = open(w, { provider: model });
    heal.step('shipping', { when: SHIPPING }, does(['click', '#next'], ['fill', '#ship-name', 'Ada']));

    await assert.rejects(run(heal), /'#ship-name' was last found on \/checkout\/a, and this page is \/checkout\/b/);
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(filled(w), {});
  });

  test('a step that no longer goes through a page does not heal onto the next step', async () => {
    // Shipping's Next went to an address check whose Use this address button went on to
    // payment. The site dropped the check: the button's lookup now misses on the payment page,
    // where a heal would press payment's Next with no card.
    const verify = "<h1>Check your address</h1><button id='use-address' type='button'" + goes('b')
      + '>Use this address</button>';
    const steps = (heal: Healer) => {
      heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada'], ['click', '#next'],
        ['click', '#use-address']));
      heal.step('payment', { when: PAYMENT }, does(['fill', '#card-number', '4242 4242 4242 4242'], ['click', '#next']));
      heal.step('review', { when: REVIEW, last: true }, does(['click', '#place']));
    };
    const first = shop({ a: shippingPage('verify'), verify, b: paymentPage('c'), c: reviewPage(), done: DONE }, 'a');
    const heal1 = open(first);
    steps(heal1);
    assert.deepEqual(await run(heal1), ['shipping', 'payment', 'review']);
    const meta = loadStore(first.storePath).metaOf('#use-address');
    assert.deepEqual(meta.pages, ['/checkout/verify']);
    assert.deepEqual(meta.steps, []);   // found after shipping left its page: not shipping's

    const w = sameStore(first, shop(inOrder(['shipping', 'payment', 'review']), 'a'));
    const model = new FakeModel(["//button[@id='next']"]);
    const heal = open(w, { provider: model });
    steps(heal);

    await assert.rejects(run(heal), /'#use-address' was last found on \/checkout\/verify, and this page is \/checkout\/b/);
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(visits(w), ['a', 'b']);
    assert.deepEqual(filled(w), { 'ship-name': 'Ada' });
  });

  test('a key found after the step went to another page is not saved with it', async () => {
    const w = shop(inOrder(['shipping', 'payment', 'review']), 'a');
    const heal = open(w);
    checkout(heal);
    // Shipping's function goes on to fill the card on the payment page.
    heal.step('shipping', { when: SHIPPING }, does(['fill', '#ship-name', 'Ada'], ['click', '#next'],
      ['fill', '#card-number', '4242']));
    assert.deepEqual(await run(heal), ['shipping', 'payment', 'review']);
    const store = loadStore(w.storePath);
    assert.deepEqual(store.metaOf('#card-number').steps, ['payment']);
    assert.deepEqual(store.metaOf('#ship-name').steps, ['shipping']);
  });

  test('the keys of a run function are not saved with the step it ran during', async () => {
    const relogin = "<div id='relogin' role='dialog'><p>Your session expired.</p><input id='user' name='user'>"
      + "<button id='signin' type='button' data-click-removes='#relogin'>Sign in</button></div>";
    const w = shop({ a: relogin + paymentPage('done'), done: DONE }, 'a', { blocker: '#relogin' });
    const heal = open(w);
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        calls.push(page);
        await page.fill('#user', 'ada');
        await page.click('#signin');
      },
    });
    heal.step('payment', { when: PAYMENT, last: true }, does(['fill', '#card-number', '4242'], ['click', '#next']));

    assert.deepEqual(await run(heal), ['payment']);

    // The popup was handled before the step's first action, and its keys are the function's,
    // which may run during any step.
    assert.equal(calls.length, 1);
    const store = loadStore(w.storePath);
    assert.deepEqual(store.metaOf('#card-number').steps, ['payment']);
    assert.deepEqual(store.metaOf('#user').steps, []);
    assert.deepEqual(store.metaOf('#signin').steps, []);
    assert.deepEqual(store.metaOf('#user').pages, ['/checkout/a']);
  });

  test('a lookup awaited next to runSteps() is not part of the step', async () => {
    const w = shop({ a: reviewPage() + "<input id='search' name='q'>", done: DONE }, 'a');
    const heal = open(w);
    let started!: () => void;
    const inStep = new Promise<void>((resolve) => (started = resolve));
    heal.step('review', { when: REVIEW, last: true }, async (page) => {
      started();
      await sleep(50);
      await page.click('#place');
    });
    const search = async () => {
      await inStep;   // while review runs, on its page
      await heal.locator('search', '#search', { timeoutMs: 0 }).resolve();
    };

    const [ran] = await Promise.all([run(heal), search()]);

    assert.deepEqual(ran, ['review']);
    const store = loadStore(w.storePath);
    assert.deepEqual(store.metaOf('#place').steps, ['review']);
    assert.deepEqual(store.metaOf('search').pages, ['/checkout/a']);
    assert.deepEqual(store.metaOf('search').steps, []);
  });

  test('a callback the step left behind is not part of it once the step ended', async () => {
    const w = shop({ a: reviewPage() + "<input id='search' name='q'>", done: DONE }, 'a');
    const heal = open(w);
    let later!: Promise<unknown>;
    heal.step('review', { when: REVIEW, last: true }, async () => {
      later = new Promise((resolve, reject) => {
        setTimeout(() => heal.locator('search', '#search', { timeoutMs: 0 }).resolve().then(resolve, reject), 30);
      });
    });

    assert.deepEqual(await run(heal), ['review']);
    await later;

    const store = loadStore(w.storePath);
    assert.deepEqual(store.metaOf('search').pages, ['/checkout/a']);
    assert.deepEqual(store.metaOf('search').steps, []);
  });
});

describe('one store shared with the Python package', { skip: needsPython() }, () => {
  test("the steps the Python package saved for a key let it heal in that step's new place", async () => {
    // The Python package found '#ship-name' during shipping, on /checkout/a. Now shipping is
    // on /checkout/b, and its field has a new id.
    const w = shop(inOrder(['payment', 'shipping', 'review'], { field: 'ship-full-name' }), 'a');
    runPython(`
import json, sys
from mendwright.store import load_store
store = load_store(None, json.load(sys.stdin))
store.set_code_candidates('#ship-name', ['#ship-name'])
store.record_success('#ship-name', '#ship-name')
store.set_meta('#ship-name', role='input')
store.set_last_page('#ship-name', '/checkout/a')
store.set_last_step('#ship-name', 'shipping')
print('null')`, w.storePath);
    const model = new FakeModel(["//input[@id='ship-full-name']"]);
    const heal = open(w, { provider: model });
    checkout(heal);

    assert.deepEqual(await run(heal), ['payment', 'shipping', 'review']);

    assert.deepEqual(heal.heals.map((h) => [h.key, h.new]), [['#ship-name', "//input[@id='ship-full-name']"]]);
    assert.deepEqual(filled(w), { 'card-number': '4242 4242 4242 4242', 'ship-full-name': 'Ada' });
  });

  test('mendwright notify names a step the site dropped, one that came back and a page where none showed',
    async () => {
      process.env.MENDWRIGHT_RUN_ID = 'ts-run-steps';
      const first = shop(inOrder(['payment', 'review']), 'a');
      const heal = open(first);
      checkout(heal);
      assert.deepEqual(await run(heal), ['payment', 'review']);   // shipping never showed
      const declined = sameStore(first, shop(inOrder(['shipping', 'payment', 'review'], { declined: true }), 'a'));
      const again = open(declined);
      checkout(again);
      await assert.rejects(run(again, { timeoutMs: 40 }), /is on the page again after it ran/);
      const nothing = sameStore(first, shop({ a: '<h1>Gift wrap</h1>' }, 'a'));
      const none = open(nothing);
      checkout(none);
      await assert.rejects(run(none, { timeoutMs: 20 }), /no step showed/);
      const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', first.storePath,
        '--run', 'ts-run-steps', '--format', 'json', '--dry-run'], {
        cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' },
      });
      assert.equal(result.status, 0, result.stderr);
      const data = JSON.parse(result.stdout);
      assert.deepEqual(data.needs_human, [
        { key: 'shipping', code: 'step_missing', about: 'step', text: 'never showed, so the site may have dropped it' },
        { key: 'payment', code: 'step_again', about: 'step', text: 'showed again after it ran' },
        { code: 'no_step', about: 'run_steps', text: 'stopped: no registered step showed' }]);
    });

  test("the steps this package saved let the Python package heal the key in that step's new place", async () => {
    const w = await (async () => {
      const first = shop(inOrder(['shipping', 'payment', 'review']), 'a');
      const heal = open(first);
      checkout(heal);
      await run(heal);
      return first;
    })();
    // The Python package runs the flow on an lxml checkout with the shape of a sync Playwright
    // page, the one of its own tests/test_steps.py, cut to what this flow uses: it needs only
    // lxml and mendwright.
    const py = runPython<{ ran: string[]; heals: string[][]; filled: Record<string, string>; asked: number }>(`
import json, sys
import lxml.html
import mendwright
from mendwright.drivers.base import VISIBLE_MATCH_JS
from mendwright.fingerprint import FINGERPRINT_JS

SITE = 'https://shop.test/checkout/'
SHIPPING = "//h1[.='Shipping']"
PAYMENT = "//h1[.='Payment']"
REVIEW = "//button[.='Place order']"

def hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False

class Locator:
    def __init__(self, page, selector, visible=False):
        self.page, self.selector, self.visible = page, selector, visible
    def els(self):
        els = self.page.query(self.selector)
        return [e for e in els if not hidden(e)] if self.visible else els
    def locator(self, selector):
        assert selector == 'visible=true'
        return Locator(self.page, self.selector, True)
    def count(self):
        return len(self.els())
    def is_visible(self, **kwargs):
        els = self.els()
        if len(els) > 1:
            raise RuntimeError('strict mode violation: ' + self.selector)
        return len(els) == 1 and not hidden(els[0])
    def scroll_into_view_if_needed(self, **kwargs):
        pass
    def evaluate_all(self, js):
        els = self.els()
        html = lambda el: lxml.html.tostring(el, encoding='unicode', with_tail=False)
        if js == VISIBLE_MATCH_JS and els:
            return {'tag': els[0].tag, 'visible': min(len(els), 2), 'html': html(els[0])}
        if js == FINGERPRINT_JS or len(els) != 1:
            return None
        return {'tag': els[0].tag, 'html': html(els[0])}
    def get_attribute(self, name, **kwargs):
        els = self.els()
        return els[0].get(name) if els else None
    def __getattr__(self, verb):
        if verb not in ('click', 'fill'):
            raise AttributeError(verb)
        return lambda *args, **kwargs: self.page.act(self.selector, verb, args)

class Shop:
    def __init__(self, pages, start):
        self.pages, self.filled = pages, {}
        self.goto(start)
    def goto(self, path):
        self.url = SITE + path
        self.doc = lxml.html.document_fromstring('<html><body>' + self.pages[path] + '</body></html>')
    def query(self, selector):
        if selector.startswith('xpath='):
            try:
                found = self.doc.xpath(selector[len('xpath='):])
            except Exception:
                return []
            return [e for e in found if isinstance(getattr(e, 'tag', None), str)]
        body = selector[len('css='):] if selector.startswith('css=') else selector
        return self.doc.xpath('//*[@id=$i]', i=body[1:]) if body.startswith('#') else []
    def act(self, selector, verb, args):
        els = self.query(selector)
        if len(els) != 1:
            raise RuntimeError('Locator.' + verb + ': ' + selector + ' matched ' + str(len(els)))
        if verb == 'fill':
            els[0].set('value', args[0])
            self.filled[els[0].get('id')] = args[0]
        elif els[0].get('data-go'):
            self.goto(els[0].get('data-go'))
    def locator(self, selector):
        return Locator(self, selector)
    def frame_locator(self, selector):
        return self
    def content(self):
        return lxml.html.tostring(self.doc, encoding='unicode')
    def evaluate(self, js, arg=None):
        return []

def next_button(go):
    return "<button id='next' type='button' data-go='" + go + "'>Next</button>"

# The site now asks for the card first, and the shipping form's field has a new id.
pages = {
    'a': "<h1>Payment</h1><label for='card-number'>Card number</label><input id='card-number' name='card'>"
         + next_button('b'),
    'b': "<h1>Shipping</h1><label for='ship-full-name'>Full name</label><input id='ship-full-name' "
         "name='ship-full-name'>" + next_button('c'),
    'c': "<h1>Review</h1><button id='place' type='button' data-go='done'>Place order</button>",
    'done': '<h1>Thank you</h1><p>Your order is placed.</p>',
}

class Model:
    def __init__(self, xpath):
        self.xpath, self.bundles = xpath, []
    def propose(self, bundle):
        self.bundles.append(bundle)
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': 'stub'}
    def report_outcome(self, key, xpath, success):
        pass

store = json.load(sys.stdin)
shop = Shop(pages, 'a')
model = Model("//input[@id='ship-full-name']")
heal = mendwright.open(shop, provider=model, store_path=store, timeout_ms=60, fingerprint=False)
heal.coordinator.log = None
def shipping(page):
    page.fill('#ship-name', 'Ada')
    page.click('#next')
def payment(page):
    page.fill('#card-number', '4242')
    page.click('#next')
heal.step('shipping', SHIPPING, shipping)
heal.step('payment', PAYMENT, payment)
heal.step('review', REVIEW, lambda page: page.click('#place'), last=True)
ran = heal.run_steps(poll_ms=5)
print(json.dumps({'ran': ran, 'heals': [[h.key, h.new] for h in heal.heals], 'filled': shop.filled,
                  'asked': len(model.bundles)}))`, w.storePath);
    assert.deepEqual(py.ran, ['payment', 'shipping', 'review']);
    assert.deepEqual(py.heals, [['#ship-name', "//input[@id='ship-full-name']"]]);
    assert.deepEqual(py.filled, { 'card-number': '4242', 'ship-full-name': 'Ada' });
    assert.equal(py.asked, 1);
  });
});
