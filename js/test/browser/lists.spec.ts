// heal.locatorAll() in a real browser: the cards of a grid, each one a HealLocator, with the list
// fingerprint recorded in one round trip; an emptied cart whose items all moved to Saved for later
// throws and is never healed onto that other list, and no model is asked; optional: true gives []
// at once. The pages are served by page.route(), and a page's body is changed between lookups by
// reloading it.

import { AttentionRequired, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, journal, readStore, serve, tempStore } from './helpers';

const PRODUCTS: [string, string][] = [['Blue mug', '$12.00'], ['Red mug', '$13.00'], ['Green mug', '$14.00'],
  ['Tea pot', '$29.00']];

function shop(cls: string): string {
  const cards = PRODUCTS.map(([n, p]) => `<div class="${cls}"><h3>${n}</h3><span class="price">${p}</span>`
    + '<button type="button" onclick="this.textContent = \'Added\'">Add to cart</button></div>').join('');
  return `<!doctype html><html><head><title>Shop</title></head><body><main id="content"><h1>Shop</h1>
  <div id="grid" class="grid">${cards}</div></main></body></html>`;
}

const ITEMS: [string, string, string][] = [['Blue mug', 'blue-mug', '$12.00'], ['Red mug', 'red-mug', '$13.00'],
  ['Tea pot', 'tea-pot', '$29.00']];

function rows(cls: string, items: [string, string, string][]): string {
  return items.map(([name, slug, price]) => `<div class="${cls}"><a href="/p/${slug}">${name}</a>`
    + `<span class="price">${price}</span><button class="delete">Delete</button></div>`).join('');
}

/** The cart: full, its rows are .cart-item (or row); emptied, every item is saved for later. */
function cart(full: boolean, row = 'cart-item'): string {
  return `<!doctype html><html><head><title>Cart</title></head><body><main><h1>Shopping cart</h1>
  <section class="cart">${rows(row, full ? ITEMS : []) || '<p>Your cart is empty.</p>'}</section>
  <h2>Saved for later</h2><section class="saved">${rows('saved-item', full ? [] : ITEMS)}</section></main></body></html>`;
}

test('the cards of a grid: a HealLocator for each, in page order, and a list fingerprint', async ({ page }) => {
  await serve(page, { '/shop': shop('product-card') });
  const store = tempStore('lists-cards');
  const model = new FakeModel([]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 500 });
  const cards = await heal.locatorAll('cards', '.product-card');
  expect(await Promise.all(cards.map((c) => c.textContent()))).toEqual(PRODUCTS.map(([n, p]) => n + p + 'Add to cart'));
  await cards[2].native!.locator('button').click();
  await expect(page.locator('.product-card').nth(2).locator('button')).toHaveText('Added');
  const entry = readStore(store).selectors.cards;
  expect(entry.expectedRole).toBe('div');
  expect(entry.listFingerprint).toMatchObject({ v: 1, count: 4, depth: 1, container: { tag: 'div', id: 'grid',
    classes: ['grid'] } });
  expect(entry.listFingerprint.items.map((i: { text: string }) => i.text)).toEqual(['Blue mug$12.00Add to cart',
    'Red mug$13.00Add to cart', 'Tea pot$29.00Add to cart']);
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
});

test('an emptied cart throws, is never healed onto the items saved for later, and asks no model', async ({ page }) => {
  const routes = { '/cart': cart(true) };
  await serve(page, routes);
  const store = tempStore('lists-emptied');
  const model = new FakeModel(["//div[@class='saved-item']"]);   // it would take the other list
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  expect(await heal.locatorAll('cart', '.cart-item')).toHaveLength(3);
  routes['/cart'] = cart(false);
  await page.reload();
  const error = await heal.locatorAll('cart', '.cart-item').catch((e: unknown) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect((error as Error).message).toBe("No matching element for: cart (tried 1 candidate(s)).\n  -> 'cart' matched 3 "
    + 'elements when its list fingerprint was recorded, and none of its selectors matches now. Either the list is empty '
    + "at this point, or its selector broke: mendwright can't tell the two apart, since the items of an emptied list can "
    + "show in another list, such as a cart's in Saved for later, so it never heals a list that matches nothing. If the "
    + 'selector broke, change it in your code.');
  expect(model.bundles).toEqual([]);
  expect(heal.heals).toEqual([]);
  expect(readStore(store).selectors.cart.candidates.filter((c: { source: string }) => c.source === 'heal')).toEqual([]);
  expect(journal(store).filter((e) => String(e.event).startsWith('heal.')).map((e) => [e.event, e.code]))
    .toEqual([['heal.declined', 'list_empty']]);
  // A fresh healer on the same store throws again: nothing was saved to replay.
  const again = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  await expect(again.locatorAll('cart', '.cart-item')).rejects.toThrow('the list is empty at this point');
  // Fixed in the code (a redesign that renamed the rows), the list is found again.
  routes['/cart'] = cart(true, 'cart-row');
  await page.reload();
  expect(await again.locatorAll('cart', '.cart-row')).toHaveLength(3);
});

test('optional: true gives [] at once for a list that matches nothing', async ({ page }) => {
  const routes = { '/cart': cart(true) };
  await serve(page, routes);
  const heal = open(page, { provider: new FakeModel([]), storePath: tempStore('lists-optional'), timeoutMs: 60_000 });
  expect(await heal.locatorAll('cart', '.cart-item')).toHaveLength(3);
  routes['/cart'] = cart(false);
  await page.reload();
  const started = Date.now();
  expect(await heal.locatorAll('cart', '.cart-item', { optional: true })).toEqual([]);
  expect(Date.now() - started).toBeLessThan(5000);
});
