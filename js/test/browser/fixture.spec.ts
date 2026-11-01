// The Playwright Test fixtures in a real browser: the one-line change (import test from
// mendwright/test), confirmation when a test passes, and strict mode.

import * as fs from 'node:fs';

import { expect, test } from 'mendwright/test';

import { SIGNUP_V1, SIGNUP_V2, journal, readStore, serve, tempStore } from './helpers';

test.describe('the page fixture', () => {
  const store = tempStore('fixture');
  test.use({ mendwrightStore: store, mendwrightOptions: { timeoutMs: 1500 } });
  test.describe.configure({ mode: 'serial' });

  test('records the element each selector matches', async ({ page }) => {
    await serve(page, { '/signup': SIGNUP_V1 });
    await page.fill('#email', 'ada@example.com');
    await page.check('#terms');
    await page.locator('#create').click();
    const data = readStore(store);
    expect(data.schema).toBe(2);
    expect(data.selectors['#email'].fingerprint.attrs.name).toBe('email');
    expect(data.selectors['#email'].expectedRole).toBe('input');
    expect(data.selectors['#terms'].expectedRole).toBe('checkbox');
    expect(data.selectors['#create'].lastPage).toBe('/signup');
  });

  test('heals the same steps after a redesign, with no model, and expect() still works', async ({ page, heal }) => {
    await serve(page, { '/signup': SIGNUP_V2 });
    await page.fill('#email', 'ada@example.com');
    await page.check('#terms');
    await page.locator('#create').click();
    await expect(page.locator('#user-email')).toHaveValue('ada@example.com');
    await expect(page.locator('#accept-terms')).toBeChecked();
    await expect(page).toHaveTitle('Sign up');
    expect(heal.heals.map((h) => [h.key, h.new, h.source])).toEqual([
      ['#email', "//input[@id='user-email']", 'fingerprint'],
      ['#terms', "//input[@data-testid='terms']", 'fingerprint'],
      ['#create', "//button[@id='create-account']", 'fingerprint'],
    ]);
  });

  test('confirmed the heals of the test that passed', async () => {
    const canaries = journal(store).filter((e) => e.event === 'heal.canary');
    expect(canaries.map((e) => [e.key, e.success, e.by])).toEqual([
      ['#email', true, 'test'], ['#terms', true, 'test'], ['#create', true, 'test'],
    ]);
  });

  test('the next run uses the saved heals at once', async ({ page, heal }) => {
    await serve(page, { '/signup': SIGNUP_V2 });
    const started = Date.now();
    await page.fill('#email', 'again');
    expect(Date.now() - started).toBeLessThan(1500);
    expect(heal.heals).toEqual([]);
  });
});

test.describe('a test that fails', () => {
  const store = tempStore('failing');
  test.use({ mendwrightStore: store, mendwrightOptions: { timeoutMs: 1000 } });
  test.describe.configure({ mode: 'serial' });

  test('keeps its heals unconfirmed', async ({ page }) => {
    test.fail();
    await serve(page, { '/signup': SIGNUP_V1 });
    await page.fill('#email', 'x');
    // The last route added answers first: the redesign.
    await page.route('https://shop.test/signup', (route) => route.fulfill({ contentType: 'text/html', body: SIGNUP_V2 }));
    await page.goto('https://shop.test/signup');
    await page.fill('#email', 'y');
    throw new Error('an unrelated failure');
  });

  test('so nothing was confirmed', async () => {
    expect(journal(store).filter((e) => e.event === 'heal.canary')).toEqual([]);
    expect(readStore(store).selectors['#email'].candidates.map((c: any) => c.status)).toEqual(['active', 'active']);
  });
});

test.describe('strict', () => {
  test.use({ mendwrightStore: tempStore('strict'), mendwrightStrict: true, mendwrightOptions: { timeoutMs: 1000 } });

  test('fails a test that passed only because a selector was healed', async ({ page }) => {
    test.fail();
    await serve(page, { '/signup': SIGNUP_V1 });
    await page.fill('#email', 'x');
    await page.route('https://shop.test/signup', (route) => route.fulfill({ contentType: 'text/html', body: SIGNUP_V2 }));
    await page.goto('https://shop.test/signup');
    await page.fill('#email', 'y');
  });
});

test.describe('mendwrightPage: false', () => {
  test.use({ mendwrightPage: false, mendwrightStore: tempStore('plain') });

  test('gives the plain page, and heal still works', async ({ page, heal }) => {
    await serve(page, { '/signup': SIGNUP_V1 });
    expect((page as unknown as { native?: unknown }).native).toBeUndefined();
    await heal.locator('email', '#email').fill('ada@example.com');
    await expect(page.locator('#email')).toHaveValue('ada@example.com');
    expect(fs.existsSync(heal.store.overridePath)).toBe(true);
  });
});
