// expectValue in real browsers (the Python package's tests/browser/test_browser_values.py): a
// read through a heal must return what the key's check accepts. A price that heals onto the
// rating next to it is refused and quarantined; a correct heal passes and is confirmed; a read
// through the selector in your code is never checked. The texts are the browser's own, with
// its non-breaking and narrow spaces, so the locale-tolerant parsing runs on what a scraper
// really gets. No model is called: the FakeModel proposes.

import { AttentionRequired, open, parseMoney } from 'mendwright';
import { test as mwTest } from 'mendwright/test';
import { expect, test, type Page } from '@playwright/test';

import { FakeModel, journal, serve, tempStore } from './helpers';

const html = (body: string) => '<!doctype html><html><head><meta charset="utf-8"><title>Blue mug</title></head><body>' + body + '</body></html>';

const PRODUCT_V1 = html(`<main id="product"><h1>Blue mug</h1>
  <span id="price">1&#8239;299,00&nbsp;€</span>
  <span class="rating">4.5</span>
  <span class="reviews">(128 reviews)</span>
</main>`);
const PRODUCT_V2 = PRODUCT_V1.replace('id="price"', 'class="amount"');
const RATING = "//span[@class='rating']";
const AMOUNT = "//span[@class='amount']";
const NARROW_NBSP = String.fromCharCode(0x202f);

/** Serve the product page, and a function that serves another version of it and loads it. */
async function product(page: Page, body = PRODUCT_V1): Promise<(next: string) => Promise<void>> {
  const routes = { '/product': body };
  await serve(page, routes);
  return async (next: string) => {
    routes['/product'] = next;
    await page.goto('https://shop.test/product');
  };
}

function opened(page: Page, model: FakeModel, options: Parameters<typeof open>[1] = {}) {
  const store = tempStore('values');
  const heal = open(page, { provider: model, storePath: store, fingerprint: false, timeoutMs: 1000, log: () => {},
    ...options });
  return { heal, store };
}

test.describe('expectValue', () => {
  test('a price read that heals onto a wrong number is refused, quarantined, and the next lookup cannot pick it',
    async ({ page }) => {
      const load = await product(page);
      const model = new FakeModel([RATING, RATING, RATING]);
      const { heal, store } = opened(page, model);
      let price = heal.locator('price', '#price', { role: 'span', expectValue: 'money', pollMs: 50 });
      expect(parseMoney(await price.innerText())).toBe('1299.00');

      await load(PRODUCT_V2);
      price = heal.locator('price', '#price', { role: 'span', expectValue: 'money', pollMs: 50 });
      expect(await price.innerHTML()).toBe('4.5');            // a read it does not check
      const error = await price.innerText().then(() => null, (e: unknown) => e);
      expect(error).toBeInstanceOf(AttentionRequired);
      const message = String((error as Error).message);
      expect(message).toContain("'price' was healed to " + RATING);
      expect(message).toContain("its innerText() there is '4.5', which is not money (expectValue: 'money')");
      expect(heal.store.status('price', RATING)).toBe('quarantined');
      expect(model.outcomes).toEqual([['price', RATING, false]]);
      const canary = journal(store).filter((e) => e.event === 'heal.canary');
      expect(canary.map((e) => [e.success, e.reason, e.by])).toEqual([
        [false, 'expect_value: innerText() is not money', 'expect_value']]);
      expect(JSON.stringify(journal(store))).not.toContain('4.5');

      // The next lookup heals again, and the quarantine keeps the rating out of it.
      await expect(heal.locator('price', '#price', { role: 'span', pollMs: 50 }).resolve())
        .rejects.toThrow(/previously quarantined/);
    });

  test('a correct heal passes and is confirmed', async ({ page }) => {
    const load = await product(page);
    const model = new FakeModel([AMOUNT]);
    const { heal, store } = opened(page, model);
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    await load(PRODUCT_V2);
    const price = heal.locator('price', '#price', { role: 'span', expectValue: 'money', pollMs: 50 });
    const text = await price.innerText();
    expect(text).toContain(NARROW_NBSP);      // the browser's own narrow space
    expect(parseMoney(text)).toBe('1299.00');
    expect(heal.heals[0].confirmed).toBe(true);
    expect(heal.pending()).toEqual({});
    expect(model.outcomes).toEqual([['price', AMOUNT, true]]);
    expect(journal(store).filter((e) => e.event === 'heal.canary').map((e) => [e.success, e.by]))
      .toEqual([[true, 'expect_value']]);
  });

  test('an unhealed read is never blocked', async ({ page }) => {
    await product(page, PRODUCT_V1.replace('1&#8239;299,00&nbsp;€', 'Call for price'));
    const { heal, store } = opened(page, new FakeModel([]), { expectValues: { price: 'money', '#price': 'money' } });
    expect(await heal.locator('price', '#price', { role: 'span' }).innerText()).toBe('Call for price');
    expect(await heal.page.innerText('#price')).toBe('Call for price');
    expect(journal(store).filter((e) => e.event === 'heal.canary' || e.event === 'heal.quarantine')).toEqual([]);
  });

  test("heal.page's reads take the check of open({ expectValues })", async ({ page }) => {
    const load = await product(page);
    const { heal } = opened(page, new FakeModel([RATING]), { expectValues: { '#price': 'money' } });
    const shop = heal.page;
    expect(parseMoney(await shop.innerText('#price'))).toBe('1299.00');
    await load(PRODUCT_V2);
    await expect(shop.innerText('#price')).rejects.toThrow(/'#price' was healed to .*its innerText\(\) there is '4.5'/);
    expect(heal.store.status('#price', RATING)).toBe('quarantined');
    await expect(shop.locator('#price').textContent()).rejects.toThrow(AttentionRequired);   // the next lookup refuses too
  });
});

mwTest.describe('the fixtures', () => {
  const model = new FakeModel([RATING]);
  mwTest.use({
    mendwrightProvider: model, mendwrightStore: tempStore('values-fixture'),
    mendwrightOptions: { expectValues: { '#price': 'money' }, fingerprint: false, timeoutMs: 1000 },
  });

  mwTest('mendwrightOptions: { expectValues } checks the page fixture\'s reads', async ({ page, heal }) => {
    const load = await product(page as Page);
    expect(parseMoney(await page.textContent('#price'))).toBe('1299.00');
    await load(PRODUCT_V2);
    await expect(page.textContent('#price')).rejects.toThrow(/its textContent\(\) there is '4.5', which is not money/);
    expect(heal.store.status('#price', RATING)).toBe('quarantined');
    expect(heal.heals.map((h) => h.confirmed)).toEqual([false]);
  });
});
