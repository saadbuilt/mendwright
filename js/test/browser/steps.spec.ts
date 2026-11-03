// heal.step() and heal.runSteps() in real browsers (the Python package's
// tests/browser/test_browser_steps.py). The checkout is built here: a shipping, a payment and a
// review page at /checkout/step-a, step-b, ... in the order a test asks for, so the address
// says where a step is in the flow, not which step it is. Each page's Continue button goes to
// the next one and carries what was filled in along in the query, and the last page shows it,
// so a test can tell what the steps did. The steps are the user's script, written once. No
// model is called: the FakeModel proposes.

import { AttentionRequired, DISMISS_KEY, WAITING_STEP, open, type Healer, type OpenOptions } from 'mendwright';
import { expect, test, type Page } from '@playwright/test';

import { FakeModel, journal, serve, tempStore } from './helpers';

// How long runSteps() waits for the next page in a flow that goes on, and in one that is
// expected to stop; how often it looks; and the healer's own timeout for a lookup.
const WAIT_MS = 5000;
const STOP_MS = 1200;
const POLL_MS = 50;
const LOOKUP_MS = 1000;

const CARD = '4242 4242 4242 4242';
const DECLINED = '4000 0000 0000 0002';

const ORIGIN = 'https://shop.test';

// go(url, [[param, field id], ...]) opens url with the fields' values added to what the query
// carries already, DELAY ms after the click (see checkout()). The payment page sends a declined
// card back to itself.
const SCRIPT = `<script>
var DELAY = 0;
function go(url, fields) {
  var q = location.search.slice(1).split('&').filter(function (p) {
    return p && p.indexOf('declined=') !== 0; });
  (fields || []).forEach(function (f) {
    q.push(f[0] + '=' + encodeURIComponent(document.getElementById(f[1]).value)); });
  var to = url + (q.length ? '?' + q.join('&') : '');
  if (DELAY) { setTimeout(function () { location.href = to; }, DELAY); }
  else { location.href = to; }
}
</script>`;

const html = (body: string, title: string) => '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>'
  + title + '</title></head><body>' + body + '</body></html>';

function go(path: string, ...fields: [string, string][]): string {
  return "go('" + ORIGIN + path + "', [" + fields.map(([p, i]) => "['" + p + "', '" + i + "']").join(', ') + '])';
}

type Builder = (next: string, me: string, field: string) => string;

const PAGES: Record<string, Builder> = {
  shipping: (next, _me, field) => html('<h1>Shipping address</h1><form><label for="' + field + '">Full name</label>'
    + '<input id="' + field + '" name="' + field + '"><button type="button" id="next" onclick="' + go(next, ['ship', field])
    + '">Continue</button></form>' + SCRIPT, 'shipping'),
  payment: (next, me) => {
    const pay = "var c = document.getElementById('card-number').value; if (c === '" + DECLINED + "') { location.href = '"
      + ORIGIN + me + "?declined=1'; } else { " + go(next, ['card', 'card-number']) + '; }';
    return html('<h1>Payment</h1><p id="error"></p><form><label for="card-number">Card number</label>'
      + '<input id="card-number" name="card-number"><button type="button" id="next" onclick="' + pay + '">Continue</button>'
      + '</form>' + SCRIPT + "<script>if (location.search.indexOf('declined=') >= 0) "
      + "document.getElementById('error').textContent = 'Your card was declined.';</script>", 'payment');
  },
  // A hidden copy of the button comes first, as for a mobile layout: the landmark matches both,
  // and the visible one shows the step.
  review: (next) => html('<h1>Review your order</h1><button type="button" hidden>Place order</button>'
    + '<button type="button" id="place" onclick="' + go(next) + '">Place order</button>' + SCRIPT, 'review'),
  upsell: (next) => html('<h1>Protect your order</h1><p>Add insurance for $4.99.</p><button type="button" '
    + 'id="skip-insurance" onclick="' + go(next) + '">Continue without insurance</button>' + SCRIPT, 'upsell'),
  gift: (next) => html('<h1>Gift options</h1><label><input type="checkbox" id="wrap"> Gift wrap</label>'
    + '<button type="button" id="gift-next" onclick="' + go(next) + '">Continue</button>' + SCRIPT, 'gift'),
};

const DONE = html(`<h1>Thank you</h1><p>Ship to <span id="ship"></span>, card <span id="card"></span>.</p>
<script>
var got = new URLSearchParams(location.search);
document.getElementById('ship').textContent = got.get('ship') || '';
document.getElementById('card').textContent = got.get('card') || '';
</script>`, 'done');

const pathOf = (i: number) => '/checkout/step-' + 'abcdefg'[i];

/** The pages of a checkout whose pages come in this order, by path. With delayMs, each
 * Continue goes to the next page that long after the click. */
function checkoutPages(order: string[], options: { field?: string; delayMs?: number } = {}): Record<string, string> {
  const paths = [...order.map((_, i) => pathOf(i)), '/checkout/done'];
  const routes: Record<string, string> = { '/checkout/done': DONE };
  order.forEach((name, i) => {
    routes[paths[i]] = PAGES[name](paths[i + 1], paths[i], options.field ?? 'ship-name')
      .replace('var DELAY = 0;', 'var DELAY = ' + (options.delayMs ?? 0) + ';');
  });
  return routes;
}

/** Serve a checkout whose pages come in this order, and open its first page. */
async function checkout(page: Page, order: string[], options: { field?: string; delayMs?: number } = {}): Promise<void> {
  await serve(page, checkoutPages(order, options), pathOf(0));
}

/** The approved example: shipping, payment and review, and an optional upsell. */
function steps(heal: Healer, options: { card?: string; upsell?: boolean } = {}): void {
  heal.step('shipping', { when: "//h1[normalize-space()='Shipping address']" }, async (p) => {
    await p.fill('#ship-name', 'Ada Lovelace');
    await p.click('#next');
  });
  heal.step('payment', { when: ["//h1[normalize-space()='Payment']", '#card-number'] }, async (p) => {
    await p.fill('#card-number', options.card ?? CARD);
    await p.click('#next');
  });
  if (options.upsell) {
    heal.step('upsell', { when: "//h1[normalize-space()='Protect your order']", optional: true },
      (p) => p.click('#skip-insurance'));
  }
  heal.step('review', { when: 'text=Place order', last: true }, (p) => p.click('#place'));
}

function openOn(page: Page, store: string, options: OpenOptions = {}): Healer {
  return open(page, { storePath: store, timeoutMs: LOOKUP_MS, provider: new FakeModel([]), ...options });
}

const run = (heal: Healer, timeoutMs = WAIT_MS) => heal.runSteps({ timeoutMs, pollMs: POLL_MS });

/** The flow.step events: key, position, registered and outcome. */
const stepEvents = (store: string) => journal(store).filter((e) => e.event === 'flow.step')
  .map((e) => [e.key, e.position, e.registered, e.outcome]);

/** The flow.stop events: key, code and page. */
const stops = (store: string) => journal(store).filter((e) => e.event === 'flow.stop').map((e) => [e.key, e.code, e.page]);

async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('it did not reject');
}

test('the same script runs the checkout in either order', async ({ page }) => {
  const store = tempStore('steps-order');
  const model = new FakeModel([]);
  const warnings: string[] = [];
  for (const order of [['shipping', 'payment', 'review'], ['payment', 'shipping', 'review']]) {
    await checkout(page, order);
    const heal = openOn(page, store, { provider: model, log: (level, m) => level === 'warning' && warnings.push(m) });
    steps(heal);

    expect(await run(heal)).toEqual(order);

    await expect(page.locator('#ship')).toHaveText('Ada Lovelace');
    await expect(page.locator('#card')).toHaveText(CARD);
    expect(heal.heals).toEqual([]);
  }
  expect(warnings).toEqual(["ran 'payment' before 'shipping': the site changed the order of the steps"]);
  expect(stepEvents(store).slice(3)).toEqual([['payment', 1, 2, 'ran'], ['shipping', 2, 1, 'ran'], ['review', 3, 3, 'ran']]);
  expect(model.bundles).toEqual([]);
});

test('a one-page checkout runs both forms, then places the order', async ({ page }) => {
  const one = html('<h1>Shipping address</h1><label for="ship-name">Full name</label><input id="ship-name">'
    + '<h1>Payment</h1><label for="card-number">Card number</label><input id="card-number">'
    + '<button type="button" id="place" onclick="' + go('/checkout/done', ['ship', 'ship-name'], ['card', 'card-number'])
    + '">Place order</button>' + SCRIPT, 'one page');
  await serve(page, { '/checkout': one, '/checkout/done': DONE });
  const heal = openOn(page, tempStore('steps-one-page'));
  heal.step('review', { when: 'text=Place order', last: true }, (p) => p.click('#place'));   // registered first, runs last
  heal.step('shipping', { when: "//h1[normalize-space()='Shipping address']" }, (p) => p.fill('#ship-name', 'Ada Lovelace'));
  heal.step('payment', { when: '#card-number' }, (p) => p.fill('#card-number', CARD));

  expect(await run(heal)).toEqual(['shipping', 'payment', 'review']);
  await expect(page.locator('#ship')).toHaveText('Ada Lovelace');
  await expect(page.locator('#card')).toHaveText(CARD);
});

test('a declined card stops the flow instead of paying again', async ({ page }) => {
  await checkout(page, ['shipping', 'payment', 'review']);
  const store = tempStore('steps-declined');
  const heal = openOn(page, store);
  steps(heal, { card: DECLINED });

  const error = await rejection(run(heal, STOP_MS));

  expect(error).toBeInstanceOf(AttentionRequired);
  expect(error.message.startsWith("mendwright: 'payment' is on the page again after it ran, and no step that has not run "
    + 'showed on /')).toBe(true);
  expect(error.message.split(' within ')[0]).toContain('step-b');
  expect(error.message).toContain('its submit probably failed');
  expect(error.message).toContain("Steps that ran: 'shipping', 'payment'.");
  await expect(page.locator('#error')).toHaveText('Your card was declined.');
  expect(stepEvents(store).map((e) => e[0])).toEqual(['shipping', 'payment']);
  expect(stops(store)).toEqual([['payment', 'step_again', '/checkout/step-b']]);
});

test('a step nobody registered stops the flow and names the page', async ({ page }) => {
  await checkout(page, ['shipping', 'gift', 'payment', 'review']);
  const store = tempStore('steps-gift');
  const heal = openOn(page, store);
  steps(heal);

  const error = await rejection(run(heal, STOP_MS));

  expect(error).toBeInstanceOf(AttentionRequired);
  expect(error.message.startsWith('mendwright: no step showed on /checkout/step-b within 1200 ms.')).toBe(true);
  expect(error.message).toContain("mendwright looked for 'payment' (//h1[normalize-space()='Payment'], #card-number) and "
    + "'review' (text=Place order)");
  expect(error.message).toContain("Steps that ran: 'shipping'.");
  expect(error.message).toContain('add it with heal.step()');
  expect(await page.evaluate(() => (document.getElementById('wrap') as HTMLInputElement).checked)).toBe(false);   // nothing guessed
  expect(stops(store)).toEqual([[WAITING_STEP, 'no_step', '/checkout/step-b']]);
});

test('an optional upsell that shows only sometimes', async ({ page }) => {
  const store = tempStore('steps-upsell');
  await checkout(page, ['shipping', 'payment', 'upsell', 'review']);
  const first = openOn(page, store);
  steps(first, { upsell: true });
  expect(await run(first)).toEqual(['shipping', 'payment', 'upsell', 'review']);

  await checkout(page, ['shipping', 'payment', 'review']);
  const lines: string[] = [];
  const heal = openOn(page, store, { log: (level, m) => lines.push(level + ': ' + m) });
  steps(heal, { upsell: true });
  expect(await run(heal)).toEqual(['shipping', 'payment', 'review']);
  await expect(page.locator('#card')).toHaveText(CARD);
  expect(lines.filter((l) => l.startsWith('warning: '))).toEqual([]);
  expect(lines).toContain("info: step 'upsell' did not show, and it is optional (optional: true)");
});

test('a step the site dropped is named once the last step ran', async ({ page }) => {
  await checkout(page, ['payment', 'review']);
  const store = tempStore('steps-dropped');
  const warnings: string[] = [];
  const heal = openOn(page, store, { log: (level, m) => level === 'warning' && warnings.push(m) });
  steps(heal);

  expect(await run(heal)).toEqual(['payment', 'review']);

  await expect(page.locator('#card')).toHaveText(CARD);
  await expect(page.locator('#ship')).toHaveText('');
  expect(warnings).toEqual(["'shipping' never showed before the flow ended with 'review': the site may have dropped it. If "
    + "it did, delete its heal.step(). If it shows only sometimes, pass optional: true to heal.step('shipping', ...)."]);
  expect(stepEvents(store)).toEqual([['payment', 1, 2, 'ran'], ['review', 2, 3, 'ran'], ['shipping', undefined, 1, 'missing']]);
});

test('a renamed field in a reordered step heals on its new page', async ({ page }) => {
  const store = tempStore('steps-renamed');
  await checkout(page, ['shipping', 'payment', 'review']);
  const first = openOn(page, store, { fingerprint: false });
  steps(first);
  expect(await run(first)).toEqual(['shipping', 'payment', 'review']);

  // The site now asks for the card first, and its shipping form's field has a new id, on the
  // page where the payment form used to be.
  await checkout(page, ['payment', 'shipping', 'review'], { field: 'full-name' });
  const model = new FakeModel(["//input[@id='full-name']"]);

  // Outside runSteps() the page guard declines the lookup there, as before.
  await page.goto(ORIGIN + pathOf(1));
  const outside = openOn(page, store, { provider: model, fingerprint: false });
  await expect(outside.page.fill('#ship-name', 'Ada Lovelace')).rejects.toThrow('was last found on');
  expect(model.bundles).toEqual([]);

  await page.goto(ORIGIN + pathOf(0));
  const lines: string[] = [];
  const heal = openOn(page, store, { provider: model, fingerprint: false, log: (level, m) => lines.push(level + ': ' + m) });
  steps(heal);
  expect(await run(heal)).toEqual(['payment', 'shipping', 'review']);

  await expect(page.locator('#ship')).toHaveText('Ada Lovelace');
  await expect(page.locator('#card')).toHaveText(CARD);
  expect(heal.heals.map((h) => [h.key, h.new])).toEqual([['#ship-name', "//input[@id='full-name']"]]);
  expect(model.bundles.length).toBe(1);
  expect(lines.filter((l) => l.includes('the page guard lets'))).toEqual(["info: the page guard lets '#ship-name' heal on "
    + "/checkout/step-b: it was found during step 'shipping' before (last on /checkout/step-a), and this is the page "
    + "'shipping' showed on"]);
});

test('a lookup after a step went on to the next page is not healed there', async ({ page }) => {
  // '#ship-name' was found during shipping. This time shipping's function looks for it again
  // after its own Continue went to the payment page: a heal there would fill the card field, so
  // the page guard declines it as outside runSteps().
  const store = tempStore('steps-went-on');
  await checkout(page, ['shipping', 'payment', 'review']);
  const first = openOn(page, store, { fingerprint: false });
  steps(first);
  expect(await run(first)).toEqual(['shipping', 'payment', 'review']);

  await checkout(page, ['shipping', 'payment', 'review']);
  const model = new FakeModel(["//input[@id='card-number']"]);
  const heal = openOn(page, store, { provider: model, fingerprint: false });
  steps(heal);
  heal.step('shipping', { when: "//h1[normalize-space()='Shipping address']" }, async (p) => {
    await p.click('#next');
    // On the payment page: WebKit can still show the shipping page right after the click.
    await p.waitForURL(/\/checkout\/step-b(\?|$)/);
    await p.fill('#ship-name', 'Ada Lovelace');
  });

  await expect(run(heal)).rejects.toThrow('was last found on');
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
  await expect(page.locator('#card-number')).toHaveValue('');
});

test('a page that loads a moment after the click is waited for', async ({ page }) => {
  // Each Continue goes to the next page 300 ms after the click, as on a site that checks the
  // form with its server first: until then the step that just ran is the only one on the page,
  // and that is not a step that came back.
  await checkout(page, ['shipping', 'payment', 'review'], { delayMs: 300 });
  const heal = openOn(page, tempStore('steps-delay'));
  steps(heal);

  expect(await run(heal)).toEqual(['shipping', 'payment', 'review']);
  await expect(page.locator('#ship')).toHaveText('Ada Lovelace');
  await expect(page.locator('#card')).toHaveText(CARD);
});

const CONSENT_WALL = `<div id="consent-wall" role="dialog" aria-label="Cookie consent"
  style="position: fixed; inset: 0; z-index: 20; background: #fff; padding: 40px">
  <p>We use cookies.</p>
  <button type="button" id="cookie-reject" onclick="document.getElementById('consent-wall').remove();
    document.getElementById('shop').style.display = 'block'">Reject all</button>
</div>`;

const NEWSLETTER = `<div id="nl" role="dialog" aria-modal="true" aria-label="Newsletter"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">
  <div style="background: #fff; width: 320px; margin: 140px auto; padding: 16px">
    <p>Get 10% off your first order.</p>
    <button type="button" id="nl-subscribe" onclick="document.title = 'subscribed'">Subscribe</button>
    <button type="button" id="nl-no" onclick="document.getElementById('nl').remove()">No thanks</button>
  </div>
</div>`;

test('a consent wall and a popup nobody registered during the flow', async ({ page }) => {
  // The first page hides the checkout behind a consent wall until it is answered, and the
  // payment page opens a newsletter popup over the card field.
  const routes = checkoutPages(['payment', 'shipping', 'review']);
  routes[pathOf(0)] = routes[pathOf(0)].replace('<body>', '<body>' + CONSENT_WALL + '<div id="shop" style="display: none">')
    .replace('</body>', '</div>' + NEWSLETTER + '</body>');
  await serve(page, routes, pathOf(0));
  const heal = openOn(page, tempStore('steps-popups'), { dismiss: true });
  heal.interrupt('cookies', '#cookie-reject');
  steps(heal);

  expect(await run(heal)).toEqual(['payment', 'shipping', 'review']);

  await expect(page.locator('#ship')).toHaveText('Ada Lovelace');
  await expect(page.locator('#card')).toHaveText(CARD);
  const events = heal.handledInterruptions;
  expect([events[0].key, events[0].before]).toEqual(['cookies', WAITING_STEP]);
  const popups = events.filter((e) => e.key === DISMISS_KEY);
  expect(popups.map((e) => e.label)).toEqual(['No thanks']);
  expect(await page.title()).not.toBe('subscribed');
  expect(heal.heals).toEqual([]);
});
