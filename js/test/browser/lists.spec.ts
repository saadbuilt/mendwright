// heal.locatorAll() in a real browser: the cards of a grid, each one a HealLocator, with the list
// fingerprint recorded in one round trip; an emptied cart whose items all moved to Saved for later
// throws and is never healed onto that other list, and no model is asked; optional: true gives []
// at once. With nonempty: true, a redesigned cart gives a proposal, which the Python package's
// command line lists and accepts, and the lookup still throws until the selector is in the code;
// an emptied cart is refused as another list; and the selector a list heal replaced matching
// again retires it, a proposed one superseded and an accepted one quarantined. The pages are
// served by page.route(), and a page's body is changed between lookups by reloading it.

import { AttentionRequired, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, journal, mendwrightCli, pythonCommand, readStore, serve, tempStore } from './helpers';

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

const ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]";

test('a redesigned cart with nonempty: true gives a proposal, which the Python package accepts, and is never used',
  async ({ page }) => {
    test.skip(!pythonCommand(), 'no Python that imports mendwright from this repository');
    const routes = { '/cart': cart(true) };
    await serve(page, routes);
    const store = tempStore('lists-proposal');
    const model = new FakeModel(["//div[@class='saved-item']"]);
    const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
    expect(await heal.locatorAll('cart', '.cart-item', { nonempty: true })).toHaveLength(3);
    expect(readStore(store).selectors.cart.listFingerprint.label).toBe('Shopping cart');
    routes['/cart'] = cart(true, 'cart-row');
    await page.reload();
    const error = await heal.locatorAll('cart', '.cart-item', { nonempty: true }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AttentionRequired);
    const row = readStore(store).selectors.cart.candidates.find((c: { list?: boolean }) => c.list);
    expect(row).toMatchObject({ xpath: ROWS, status: 'proposed', list: true, css: 'div.cart-row' });
    const pid = row.proposal.id as string;
    const flag = ' --store "' + store + '"';
    expect((error as Error).message).toContain("found 3 div elements, under 'Shopping cart', in <section "
      + 'class="cart">, that hold 3 of the 3 items it recorded: ' + ROWS + ' (as CSS: div.cart-row).');
    expect((error as Error).message).toContain('Accept it with: mendwright lists accept cart ' + pid + flag + '.');
    expect(journal(store).filter((e) => e.event === 'heal.held')).toMatchObject([{ key: 'cart', xpath: ROWS, id: pid,
      list: true, items: 3, label: 'Shopping cart' }]);
    expect(model.bundles).toEqual([]);
    expect(heal.heals).toEqual([]);

    const [listed, out, listErr] = mendwrightCli(['lists', '--store', store]);
    expect(listed, listErr).toBe(0);
    expect(out.split(/\r?\n/)).toContain('cart  (div elements)  id ' + pid);
    const [accepted, said, acceptErr] = mendwrightCli(['lists', 'accept', 'cart', pid, '--store', store]);
    expect(accepted, acceptErr).toBe(0);
    expect(said).toContain("Accepted the list heal of 'cart' (" + pid + '): ' + ROWS);

    // An accepted proposal still throws, in this run and the next, until the selector is in the code.
    for (const run of [heal, open(page, { provider: model, storePath: store, timeoutMs: 300 })]) {
      await expect(run.locatorAll('cart', '.cart-item', { nonempty: true })).rejects.toThrow("'cart' matched 3 elements "
        + 'when its list fingerprint was recorded, and none of its selectors matches now. You accepted a list heal for '
        + 'it on ');
    }
    expect(model.bundles).toEqual([]);
    const fixed = await open(page, { provider: model, storePath: store, timeoutMs: 300 })
      .locatorAll('cart', 'div.cart-row', { nonempty: true });
    expect(await Promise.all(fixed.map((r) => r.native!.locator('a').textContent()))).toEqual(['Blue mug', 'Red mug',
      'Tea pot']);
  });

test('an emptied cart with nonempty: true is refused as another list, and nothing is proposed', async ({ page }) => {
  const routes = { '/cart': cart(true) };
  await serve(page, routes);
  const store = tempStore('lists-nonempty-emptied');
  const heal = open(page, { provider: new FakeModel([]), storePath: store, timeoutMs: 300 });
  expect(await heal.locatorAll('cart', '.cart-item', { nonempty: true })).toHaveLength(3);
  routes['/cart'] = cart(false);
  await page.reload();
  await expect(heal.locatorAll('cart', '.cart-item', { nonempty: true })).rejects.toThrow("but each group of div "
    + "elements on the page is another list: the one most like the list (3 elements) is under 'Saved for later', which "
    + "labelled another part of the page when the list was recorded, and the list was under 'Shopping cart'.");
  expect(readStore(store).selectors.cart.candidates.filter((c: { list?: boolean }) => c.list)).toEqual([]);
});

test('the selector a list heal replaced matching again retires it: a proposed one, then an accepted one',
  async ({ page }) => {
    test.skip(!pythonCommand(), 'no Python that imports mendwright from this repository');
    const routes = { '/cart': cart(true) };
    await serve(page, routes);
    const store = tempStore('lists-retire');
    const lines: string[] = [];
    const options = { provider: new FakeModel([]), storePath: store, timeoutMs: 300,
                      log: (level: string, message: string) => lines.push(level + ': ' + message) };
    const heal = open(page, options);
    expect(await heal.locatorAll('cart', '.cart-item', { nonempty: true })).toHaveLength(3);
    const row = () => readStore(store).selectors.cart.candidates.find((c: { xpath: string }) => c.xpath === ROWS);
    const renamed = async () => {
      routes['/cart'] = cart(true, 'cart-row');
      await page.reload();
      await expect(heal.locatorAll('cart', '.cart-item', { nonempty: true })).rejects.toThrow('Accept it with:');
    };
    await renamed();
    expect(row().status).toBe('proposed');
    routes['/cart'] = cart(true);   // the old selector works again
    await page.reload();
    expect(await heal.locatorAll('cart', '.cart-item')).toHaveLength(3);
    expect(row().status).toBe('superseded');
    expect(lines.filter((l) => l.startsWith("info: .cart-item matches 'cart' again, so the list heal found for it on ")))
      .toHaveLength(1);
    // Proposed again, accepted with the Python package's command line, and the old selector
    // matches once more, in the next run: quarantined.
    await renamed();
    const [accepted, , err] = mendwrightCli(['lists', 'accept', 'cart', row().proposal.id, '--store', store]);
    expect(accepted, err).toBe(0);
    routes['/cart'] = cart(true);
    await page.reload();
    expect(await open(page, options).locatorAll('cart', '.cart-item', { nonempty: true })).toHaveLength(3);
    expect(row().status).toBe('quarantined');
    expect(lines.filter((l) => l.startsWith("warning: the selector in your code for 'cart', .cart-item, matches 3 "
      + 'elements again, so the list heal you accepted for it on '))).toHaveLength(1);
    expect(journal(store).filter((e) => e.event === 'heal.quarantine')).toMatchObject([{ key: 'cart', xpath: ROWS,
      list: true, reason: 'the selector it replaced matches again' }]);
  });
