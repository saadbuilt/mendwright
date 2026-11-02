// heal.page.getBy*() with open(page, { healGetBy: true }) in real browsers. A redesign is a second
// page served at the same address. The model would be asked nothing: every test that heals or
// declines a call checks that it was not, as mendwright heals a call from its fingerprint only.

import { errors, type Locator, type Page } from '@playwright/test';
import { AttentionRequired, GetByNotFound, getByKey, open, type GetByMethod } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { build, parse, type GetByCall } from '../../src/getby';
import { FakeModel, ONETRUST, journal, readStore, serve, tempStore } from './helpers';

const DONE = '<p>Done: <span id="done"></span></p><script>function done(what) { '
  + "document.getElementById('done').textContent += what + ' '; }</script>";
// A one-pixel image, so an <img> has a size without the network.
const PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const KEY = 'get_by_role("button", name="Sign in")';

const html = (body: string, title = 'getBy') => '<!doctype html><html><head><title>' + title + '</title></head><body><main>'
  + body + '</main>' + DONE + '</body></html>';

/** Serve body at https://shop.test/login and go there; the returned function serves another
 * body there and loads it, as a redesign. */
async function site(page: Page, body: string): Promise<(next: string) => Promise<void>> {
  const routes: Record<string, string> = { '/login': html(body) };
  await serve(page, routes, '/login');
  return async (next: string) => {
    routes['/login'] = html(next);
    await page.goto('https://shop.test/login');
  };
}

const done = async (page: Page) => (await page.locator('#done').textContent()) ?? '';

function opened(page: Page, store: string, options: Parameters<typeof open>[1] = {}) {
  const model = new FakeModel([]);
  const heal = open(page, { healGetBy: true, provider: model, storePath: store, timeoutMs: 1000, ...options });
  return { heal, model };
}

/** heal.page.<method>(...args). */
function call(heal: ReturnType<typeof open>, method: string, args: unknown[]): Locator {
  return (heal.page as unknown as Record<string, (...a: unknown[]) => Locator>)[method](...args);
}

// Each method, before and after a redesign that changed the words and kept an id or a test id:
// [the call's arguments, the page before, the page after, the action, what the action did].
const REDESIGNS: Record<string, [unknown[], string, string, 'click' | 'fill', (page: Page) => Promise<boolean>]> = {
  getByRole: [['button', { name: 'Sign in' }],
    '<button id="login" type="button" onclick="done(\'login\')">Sign in</button><button id="help" type="button">Help</button>',
    '<button id="login" type="button" onclick="done(\'login\')">Log in</button><button id="help" type="button">Help</button>',
    'click', async (page) => (await done(page)) === 'login '],
  getByText: [['Free shipping'],
    '<span id="ship" onclick="done(\'ship\')">Free shipping</span><p>On orders over $50</p>',
    '<span id="ship" onclick="done(\'ship\')">Shipping included</span><p>On orders over $50</p>',
    'click', async (page) => (await done(page)) === 'ship '],
  getByLabel: [['Email'],
    '<label for="email">Email</label><input id="email" name="email" type="email">',
    '<label for="email">Where we send receipts</label><input id="email" name="email" type="email">',
    'fill', async (page) => (await page.locator('#email').inputValue()) === 'ada@example.com'],
  getByPlaceholder: [['you@example.com'],
    '<input id="email" name="email" type="email" placeholder="you@example.com">',
    '<input id="email" name="email" type="email" placeholder="name@company.com">',
    'fill', async (page) => (await page.locator('#email').inputValue()) === 'ada@example.com'],
  getByAltText: [['Logo'],
    '<img id="logo" alt="Logo" src="' + PIXEL + '" width="40" height="40" onclick="done(\'logo\')">',
    '<img id="logo" alt="Acme home" src="' + PIXEL + '" width="40" height="40" onclick="done(\'logo\')">',
    'click', async (page) => (await done(page)) === 'logo '],
  getByTitle: [['Close'],
    '<button id="close" type="button" title="Close" onclick="done(\'close\')">x</button>',
    '<button id="close" type="button" title="Dismiss" onclick="done(\'close\')">x</button>',
    'click', async (page) => (await done(page)) === 'close '],
  getByTestId: [['login'],
    '<button id="login" data-testid="login" type="button" onclick="done(\'login\')">Sign in</button>',
    '<button id="login" data-testid="sign-in" type="button" onclick="done(\'login\')">Sign in</button>',
    'click', async (page) => (await done(page)) === 'login '],
};

for (const [method, [args, before, after, action, did]] of Object.entries(REDESIGNS)) {
  test(method + ' heals after a redesign, from its fingerprint', async ({ page }) => {
    const key = getByKey(method as GetByMethod, ...(args as [never]));
    const store = tempStore('getby-' + method);
    const redesign = await site(page, before);
    const act = (locator: Locator) => (action === 'fill' ? locator.fill('ada@example.com') : locator.click());
    await act(call(opened(page, store).heal, method, args));
    expect(readStore(store).selectors[key].fingerprint).toBeTruthy();
    await redesign(after);
    const { heal, model } = opened(page, store);
    await act(call(heal, method, args));
    expect(await did(page)).toBe(true);
    expect(heal.heals.map((h) => [h.key, h.source])).toEqual([[key, 'fingerprint']]);
    expect(model.bundles).toEqual([]);
  });
}

test('a call rebuilt from its key selects as the call does', async ({ page }) => {
  await site(page, '<nav><a href="#a">A</a></nav><h2>Cart</h2><button>Sign in</button><button hidden>Hidden</button>'
    + '<p>Free shipping</p><input aria-label="Email"><input placeholder="you@example.com"><img alt="Logo" src="' + PIXEL
    + '"><span title="Close">x</span><b data-testid="login">in</b>');
  const TABLE: [string, unknown[]][] = [
    ['getByRole', ['button', { name: 'Sign in' }]], ['getByRole', ['button', { name: 'Sign in', exact: false }]],
    ['getByRole', ['button', { name: 'Sign in', exact: true }]], ['getByRole', ['heading', { name: 'Cart', level: 2 }]],
    ['getByRole', ['button', { name: /sign\s?in/i }]], ['getByRole', ['navigation']], ['getByRole', ['button', { exact: true }]],
    ['getByText', ['Free shipping']], ['getByText', ['Say "hi"']], ['getByLabel', ['Email', { exact: true }]],
    ['getByPlaceholder', ['you@example.com']], ['getByAltText', ['Logo']], ['getByTitle', ['Close']],
    ['getByTestId', ['login']], ['getByText', [/ship/is, { exact: true }]], ['getByTestId', [/^log/]],
  ];
  for (const [method, args] of TABLE) {
    const key = getByKey(method as GetByMethod, ...(args as [never]));
    const rebuilt = build(page, parse(key) as GetByCall);
    const original = (page as unknown as Record<string, (...a: unknown[]) => Locator>)[method](...args);
    expect((rebuilt as unknown as { _selector: string })._selector, key)
      .toBe((original as unknown as { _selector: string })._selector);
    expect(await rebuilt.count(), key).toBe(await original.count());
  }
  expect(getByKey('getByRole', 'button', { includeHidden: false })).toBe('get_by_role("button")');
  expect(await page.getByRole('button', { includeHidden: false }).count()).toBe(await page.getByRole('button').count());
});

test("a heal of a getByRole() call must have the call's ARIA role in the browser", async ({ page }) => {
  const store = tempStore('getby-role');
  const redesign = await site(page, '<button id="login" type="button">Sign in</button>'
    + '<button id="cancel" type="button">Cancel</button>');
  await opened(page, store).heal.page.getByRole('button', { name: 'Sign in' }).click();
  // The button is gone; a tab that keeps its id and its text is not a button to Playwright.
  await redesign('<div role="tablist"><button role="tab" id="login" type="button">Sign in</button></div>'
    + '<button id="cancel" type="button">Cancel</button>');
  const { heal, model } = opened(page, store);
  const error = await heal.page.getByRole('button', { name: 'Sign in' }).click().catch((e: unknown) => e);
  const mismatch = "live element role mismatch: Playwright does not give it the ARIA role 'button'";
  expect(error).toBeInstanceOf(GetByNotFound);
  expect(String((error as Error).message)).toContain(mismatch);
  expect(journal(store).filter((e) => e.event === 'heal.rejected').map((e) => e.reason)).toEqual([mismatch]);
  expect(readStore(store).selectors[KEY].candidates.map((c: { xpath: string }) => c.xpath)).toEqual([KEY]);
  expect(model.bundles).toEqual([]);
});

test("level on a button is Playwright's own error, at once", async ({ page }) => {
  const store = tempStore('getby-level');
  await site(page, '<button id="login" type="button">Sign in</button>');
  const { heal } = opened(page, store, { timeoutMs: 10_000 });
  const started = Date.now();
  const error = await heal.page.getByRole('button', { name: 'Sign in', level: 2 }).click({ timeout: 4000 })
    .catch((e: unknown) => e);
  expect(String((error as Error).message)).toMatch(/"level" attribute is only supported/);
  expect(error).not.toBeInstanceOf(AttentionRequired);
  expect(Date.now() - started).toBeLessThan(3000);
  expect(journal(store)).toEqual([]);
});

test('a name that matches part of two buttons is a strict mode violation, run as written', async ({ page }) => {
  const store = tempStore('getby-strict');
  await site(page, '<button type="button">Save</button><button type="button">Save draft</button>');
  const { heal, model } = opened(page, store, { timeoutMs: 10_000 });
  const started = Date.now();
  await expect(heal.page.getByRole('button', { name: 'Save' }).click({ timeout: 4000 })).rejects.toThrow(/strict mode violation/);
  expect(Date.now() - started).toBeLessThan(3000);
  expect(journal(store)).toEqual([]);
  await heal.page.getByRole('button', { name: 'Save', exact: true }).click();
  expect(heal.store.candidates('get_by_role("button", name="Save", exact=True)').length).toBeGreaterThan(0);
  expect(model.bundles).toEqual([]);
});

test('a miss is GetByNotFound, which a catch of TimeoutError takes', async ({ page }) => {
  await site(page, '<button id="login" type="button">Sign in</button>');
  const { heal } = opened(page, tempStore('getby-timeout'), { timeoutMs: 10_000 });
  const started = Date.now();
  let caught: unknown = null;
  try {
    await heal.page.getByRole('button', { name: 'Maybe later' }).click({ timeout: 1000 });
  } catch (error) {
    if (!(error instanceof errors.TimeoutError)) throw error;
    caught = error;
  }
  expect(caught).toBeInstanceOf(GetByNotFound);
  expect(caught instanceof AttentionRequired).toBe(true);
  expect((caught as Error).name).toBe('TimeoutError');
  expect(Date.now() - started).toBeLessThan(5000);
});

const MODAL = '<div id="consent" role="dialog" aria-modal="true" style="position: fixed; inset: 0; z-index: 10; '
  + 'background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p><button id="accept" type="button" '
  + "onclick=\"document.getElementById('consent').remove(); document.getElementById('app').removeAttribute('aria-hidden')\">"
  + 'Accept</button></div>';

test('a banner that hides the page from the accessibility tree is handled before the wait', async ({ page }) => {
  await site(page, '<div id="app" aria-hidden="true"><button id="login" type="button" onclick="done(\'login\')">Sign in'
    + '</button></div>' + MODAL);
  const { heal } = opened(page, tempStore('getby-banner'), { timeoutMs: 10_000 });
  heal.interrupt('cookies', '#accept');
  const started = Date.now();
  await heal.page.getByRole('button', { name: 'Sign in' }).click();
  expect(Date.now() - started).toBeLessThan(4000);          // it did not wait the 10 s out first
  expect(await done(page)).toBe('login ');
  expect(heal.handledInterruptions.map((e) => [e.key, e.before])).toEqual([['cookies', KEY]]);
});

test("the cookies preset closes a consent banner once, before a call's action, under the call's key", async ({ page }) => {
  await site(page, ONETRUST + '<button id="go" type="button" onclick="done(\'go\')">Continue</button>');
  const { heal } = opened(page, tempStore('getby-cookies'), { cookies: 'reject' });
  await heal.page.getByRole('button', { name: 'Continue' }).click();
  expect(await done(page)).toBe('go ');
  expect(await page.evaluate(() => (window as unknown as { consent?: string }).consent)).toBe('reject');
  expect(heal.handledInterruptions.map((e) => [e.key, e.before]))
    .toEqual([['cookie_banner', 'get_by_role("button", name="Continue")']]);
});

test("a popup over a call's element is dismissed, and the call is recorded", async ({ page }) => {
  const popup = '<div id="nl" role="dialog" aria-modal="true" aria-label="Newsletter" style="position: fixed; inset: 0; '
    + 'z-index: 10; background: rgba(0, 0, 0, 0.5)"><div style="background: #fff; width: 320px; margin: 140px auto">'
    + '<p>Get 10% off your first order.</p><button type="button" id="nl-signup">Sign up</button> '
    + "<button type=\"button\" id=\"nl-no\" onclick=\"document.getElementById('nl').remove()\">No thanks</button></div></div>";
  await site(page, '<button id="go" type="button" onclick="done(\'go\')">Go</button>' + popup);
  const { heal } = opened(page, tempStore('getby-dismiss'), { dismiss: true });
  await heal.page.getByRole('button', { name: 'Go' }).click({ timeout: 5000 });
  expect(await done(page)).toBe('go ');
  expect(heal.handledInterruptions.map((e) => [e.key, e.before, e.how])).toEqual([['<popup>',
    'get_by_role("button", name="Go")', expect.any(String)]]);
  expect(heal.store.candidates('get_by_role("button", name="Go")').length).toBeGreaterThan(0);
});

// In a function: a second top-level const would throw when the page is loaded again.
const SHADOW = `<buy-box id="box"></buy-box><script>
(() => {
  const root = document.getElementById('box').attachShadow({ mode: 'open' });
  root.innerHTML = '<button id="buy" type="button" onclick="done(\\'buy\\')">LABEL</button>';
})();
</script>`;

test('a renamed element in an open shadow root heals only inside it', async ({ page }) => {
  const store = tempStore('getby-shadow');
  const key = 'get_by_role("button", name="Buy now")';
  const redesign = await site(page, SHADOW.replace('LABEL', 'Buy now'));
  await opened(page, store).heal.page.getByRole('button', { name: 'Buy now' }).click();
  const recorded = opened(page, store).heal;
  expect(recorded.store.shadowHosts(key)).toEqual(['buy-box[id="box"]']);
  await redesign(SHADOW.replace('LABEL', 'Purchase'));
  const { heal, model } = opened(page, store);
  await heal.page.getByRole('button', { name: 'Buy now' }).click();
  expect(heal.heals.map((h) => [h.source, h.new])).toEqual([['fingerprint', 'buy-box[id="box"] >> #buy']]);
  expect(await done(page)).toBe('buy ');
  expect(model.bundles).toEqual([]);
});

test('a call that matches only inside an iframe is not followed, and heals on that frame', async ({ page }) => {
  const inner = '<!doctype html><html><head><title>Pay</title></head><body><button id="pay" type="button" '
    + "onclick=\"this.textContent += '!'\">Pay</button></body></html>";
  await serve(page, { '/checkout': html('<p>Pay below.</p><iframe name="payment" src="/pay"></iframe>', 'Checkout'),
    '/pay': inner }, '/checkout');
  await page.frameLocator('iframe[name="payment"]').locator('#pay').waitFor({ state: 'attached' });
  const store = tempStore('getby-frame');
  const { heal, model } = opened(page, store);
  const error = await heal.page.getByRole('button', { name: 'Pay' }).click().catch((e: unknown) => e);
  expect(error).toBeInstanceOf(GetByNotFound);
  expect(String((error as Error).message)).toContain('mendwright did not look for it there, because mendwright does not '
    + 'follow a getBy* call into an iframe');
  expect(String((error as Error).message)).toContain("heal = open(page.frame({ name: 'payment' }), { healGetBy: true }).");
  expect(heal.heals).toEqual([]);
  const frame = page.frame({ name: 'payment' })!;
  const framed = open(frame, { healGetBy: true, provider: model, storePath: store, timeoutMs: 1000 });
  await framed.page.getByRole('button', { name: 'Pay' }).click();
  await frame.evaluate(() => { document.getElementById('pay')!.textContent = 'Confirm'; });
  await framed.page.getByRole('button', { name: 'Pay' }).click();
  expect(await frame.locator('#pay').textContent()).toBe('Confirm!');
  expect(framed.heals.map((h) => h.source)).toEqual(['fingerprint']);
  expect(model.bundles).toEqual([]);
});

test.describe('the fixtures', () => {
  const store = tempStore('getby-fixture');
  test.use({ mendwrightOptions: { healGetBy: true, timeoutMs: 1000 }, mendwrightStore: store });

  test('mendwrightOptions: { healGetBy: true } heals the page fixture\'s getBy* calls', async ({ page, heal }) => {
    const redesign = await site(page as Page, '<button id="login" type="button" onclick="done(\'login\')">Sign in</button>');
    await page.getByRole('button', { name: 'Sign in' }).click();
    expect(heal.store.candidates(KEY).map((c) => c.xpath)).toEqual([KEY]);
    await redesign('<button id="login" type="button" onclick="done(\'login\')">Log in</button>');
    await page.getByRole('button', { name: 'Sign in' }).click();
    expect(await done(page as Page)).toBe('login ');
    expect(heal.heals.map((h) => [h.key, h.source])).toEqual([[KEY, 'fingerprint']]);
    // expect() takes the call, and answers for it as written: the renamed button is Log in.
    await expect(page.getByRole('button', { name: 'Log in' })).toHaveText('Log in');
    await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
  });
});
