// heal.locatorAll() on a fake page (the Python package's tests/test_lists.py): it gives the
// elements of a list and records a list fingerprint, and never heals a list whose selectors
// all match nothing. Each safety rule has a test here that fails without it. No model is ever
// asked: NoModel throws if it is.

import { strict as assert } from 'node:assert';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired, InterruptionLoop, InvalidSelector } from '../../src/errors';
import type { HealLocator } from '../../src/healer';
import { loadStore, type SelectorStore } from '../../src/store';
import { FakeModel, cleanEnv, journalOf, world } from '../support/env';
import { CARDS, NoModel, PRODUCTS, TILE, V1, V2, card, grid, shop } from '../support/listpages';

beforeEach(cleanEnv);

const REASON = "'cards' matched 4 elements when its list fingerprint was recorded, and none of its selectors matches "
  + "now. Either the list is empty at this point, or its selector broke: mendwright can't tell the two apart, since "
  + "the items of an emptied list can show in another list, such as a cart's in Saved for later, so it never heals a "
  + 'list that matches nothing. If the selector broke, change it in your code.';
// What a lookup of the list 'cards', recorded with 4 cards, throws when none of its selectors
// matches.
const MISSING = 'No matching element for: cards (tried 1 candidate(s)).\n  -> ' + REASON;
const TEXTS = PRODUCTS.map(([n, p]) => n + p + 'Add to cart');

const texts = (items: HealLocator[]) => Promise.all(items.map((item) => item.innerText()));

/** The writes of store's file from now on, one per file written (a batch is one). */
function countWrites(store: SelectorStore): { count: number } {
  const writes = { count: 0 };
  const persist = store.persist.bind(store);
  store.persist = () => {
    if (!(store as unknown as { batchDepth: number }).batchDepth) writes.count += 1;
    persist();
  };
  return writes;
}

const heals = (dir: string) => journalOf(dir).filter((e) => String(e.event).startsWith('heal.'));

describe('heal.locatorAll()', () => {
  test('a grid whose card class changed is not healed: it throws, asks no model, saves nothing', async () => {
    const w = world(V1);
    const model = new NoModel();
    const heal = w.open({ provider: model });
    const cards = await heal.locatorAll('cards', CARDS);
    assert.deepEqual(await texts(cards), TEXTS);
    assert.equal(heal.store.metaOf('cards').expectedRole, 'div');
    w.page.goto(V2);
    await assert.rejects(heal.locatorAll('cards', CARDS),
      (error: unknown) => error instanceof AttentionRequired && error.message === MISSING);
    assert.deepEqual(w.lines.filter((l) => l.includes('did not heal')), ["warning: did not heal 'cards': " + REASON]);
    assert.deepEqual([heal.heals, heal.pending(), model.calls], [[], {}, []]);
    assert.deepEqual(heal.store.candidates('cards').filter((c) => c.source === 'heal'), []);
    // Nothing was looked for: the one heal event is the decline, which names why.
    assert.deepEqual(heals(w.dir).map((e) => [e.event, e.code, e.reason]), [['heal.declined', 'list_empty', REASON]]);
    // Fixed in the code, the list is found and recorded for its new selector.
    const tiles = await heal.locatorAll('cards', TILE);
    assert.equal(tiles.length, 4);
    assert.equal(await tiles[3].innerText(), 'Tea pot$29.00Add to cart');
    assert.deepEqual((heal.store.metaOf('cards').listFingerprint as { items: { classes: string[] }[] }).items[0].classes,
      ['product-tile']);
    // And a fresh healer on the same store throws again: nothing was saved to replay.
    w.page.goto(V2);
    await assert.rejects(w.open({ provider: model }).locatorAll('cards', CARDS), /the list is empty at this point/);
  });

  test('a list mendwright never saw with two or more items is [] at once, and records nothing', async () => {
    const w = world(V2);
    const heal = w.open({ timeoutMs: 60_000 });   // it would wait a minute if it waited
    const started = Date.now();
    assert.deepEqual(await heal.locatorAll('cards', CARDS), []);
    w.page.goto(shop(card('product-card', ...PRODUCTS[0])));
    assert.equal((await heal.locatorAll('cards', CARDS)).length, 1);   // one item: nothing repeats in it
    w.page.goto(V2);
    assert.deepEqual(await heal.locatorAll('cards', CARDS), []);
    assert.equal(heal.store.metaOf('cards').listFingerprint, null);
    assert.ok(Date.now() - started < 5000);
  });

  test('an optional list is [] at once and never throws', async () => {
    const w = world(V1);
    const heal = w.open({ timeoutMs: 60_000 });
    await heal.locatorAll('cards', CARDS);
    w.page.goto(V2);
    const started = Date.now();
    assert.deepEqual(await heal.locatorAll('cards', CARDS, { optional: true }), []);
    assert.ok(Date.now() - started < 5000);
    assert.deepEqual(heal.heals, []);
  });

  for (const setting of ['dry run', 'destructive', 'another page', 'guard']) {
    test('what would stop a heal changes nothing for a list: ' + setting, async () => {
      // No heal is tried, so the reason is the same with each of them.
      const w = world(V1, { url: 'https://shop.example/shop' });
      const heal = w.open({ dryRun: setting === 'dry run', guard: setting === 'guard' ? () => false : undefined });
      await heal.locatorAll('cards', CARDS);
      if (setting === 'destructive') heal.store.setMeta('cards', { destructive: true });
      w.page.goto(V2, setting === 'another page' ? 'https://shop.example/cart' : undefined);
      await assert.rejects(heal.locatorAll('cards', CARDS), (error: unknown) => (error as Error).message === MISSING);
      assert.deepEqual(heal.heals, []);
    });
  }

  for (const off of ['fingerprint: false', 'MENDWRIGHT_DISABLE=1']) {
    test('with the fingerprint tier off (' + off + ') a list that matches nothing is [] at once', async () => {
      const w = world(V1);
      await w.open().locatorAll('cards', CARDS);   // recorded with 4 cards
      if (off === 'MENDWRIGHT_DISABLE=1') process.env.MENDWRIGHT_DISABLE = '1';
      const heal = w.open({ timeoutMs: 60_000, ...(off === 'fingerprint: false' ? { fingerprint: false } : {}) });
      assert.equal((await heal.locatorAll('cards', CARDS)).length, 4);
      w.page.goto(V2);
      const started = Date.now();
      assert.deepEqual(await heal.locatorAll('cards', CARDS), []);
      assert.ok(Date.now() - started < 5000);
      assert.deepEqual(heal.heals, []);
    });
  }

  test('a new list saves its selector, its first success, its page and its list fingerprint in one write', async () => {
    const w = world(V1, { url: 'https://shop.example/shop' });
    const heal = w.open();
    const writes = countWrites(heal.store);
    assert.equal((await heal.locatorAll('cards', CARDS)).length, 4);
    assert.equal(writes.count, 1);
    const saved = loadStore(w.storePath);
    const meta = saved.metaOf('cards');
    assert.equal((meta.listFingerprint as { count: number }).count, 4);
    assert.equal(meta.expectedRole, 'div');
    assert.equal(meta.lastPage, '/shop');
    assert.ok(saved.candidates('cards')[0].lastSucceededAt);
    assert.equal((await heal.locatorAll('cards', CARDS)).length, 4);
    assert.equal(writes.count, 1);   // nothing new
  });

  test('the list fingerprint is read once per healer and selector', async () => {
    const w = world(V1);
    const heal = w.open();
    const reads = () => w.page.calls.filter(([call, selector]) => call === 'evaluateAll' && selector === 'xpath=' + CARDS)
      .length;
    await heal.locatorAll('cards', CARDS);
    const first = reads();
    assert.equal(first, 1);
    await heal.locatorAll('cards', CARDS);
    assert.equal(reads(), first);
    await w.open().locatorAll('cards', CARDS);   // a new healer reads it again
    assert.equal(reads(), first + 1);
  });

  test('a list of links keeps its role, and its lookup throws with how many it had', async () => {
    const v1 = "<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' href='/b'>B</a></li>"
      + "<li><a class='m' href='/c'>C</a></li></ul></body></html>";
    const w = world(v1);
    const heal = w.open();
    const links = await heal.locatorAll('menu', "//a[@class='m']", { role: 'link' });
    assert.deepEqual(await Promise.all(links.map((l) => l.getAttribute('href'))), ['/a', '/b', '/c']);
    assert.equal(heal.store.metaOf('menu').expectedRole, 'link');
    w.page.goto(v1.replace(/class='m'/g, "class='menu-link'"));
    await assert.rejects(heal.locatorAll('menu', "//a[@class='m']", { role: 'link' }), /'menu' matched 3 elements/);
  });

  test('a list that comes while mendwright waits for it is used', async () => {
    const w = world(V1);
    const heal = w.open({ timeoutMs: 5000 });
    await heal.locatorAll('cards', '.product-card', { pollMs: 1 });
    w.page.goto(shop(''));   // the grid is loading
    setTimeout(() => w.page.goto(V1), 150);
    assert.equal((await heal.locatorAll('cards', '.product-card', { pollMs: 5 })).length, 4);
    assert.deepEqual(heal.heals, []);
  });

  test("a heal of one card under the same key never answers for the list, and the list's elements report nothing",
    async () => {
      // heal.locatorAll('.product-card', ...) and heal.page.innerText('.product-card') share a key,
      // and the first card healed, through the model.
      const first = "//div[contains(@class,'product-tile')][h3='Blue mug']";
      const w = world(V1);
      const model = new FakeModel([first]);
      const heal = w.open({ provider: model });
      assert.equal((await heal.locatorAll('.product-card', '.product-card')).length, 4);
      w.page.goto(V2);
      assert.ok((await heal.page.innerText('.product-card')).startsWith('Blue mug'));   // the first card's
      await assert.rejects(heal.locatorAll('.product-card', '.product-card'), /its selector broke/);
      const cards = await heal.locatorAll('.product-card', '.product-tile');   // fixed in the code
      assert.equal(cards.length, 4);
      cards[1].confirm(true);   // no heal of the list to report
      cards[0].confirm(false);  // and the card's heal is not its
      await cards[2].click({ expect: () => false });   // nor an action's expect
      assert.deepEqual(heal.pending(), { '.product-card': first });
      assert.equal(heal.store.status('.product-card', first), 'active');
      assert.deepEqual(heal.heals.map((h) => [h.new, h.confirmed]), [[first, null]]);
      assert.equal(heal.confirmAll(true), 1);
      assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.canary').map((e) => [e.xpath, e.success]),
        [[first, true]]);
    });

  test("an element's failed action quarantines nothing", async () => {
    const first = "//div[contains(@class,'product-tile')][h3='Red mug']";
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([first]) });
    await heal.locatorAll('.product-card', '.product-card');
    w.page.goto(V2);
    await heal.page.innerText('.product-card');   // a heal of one card, pending
    const cards = await heal.locatorAll('.product-card', '.product-tile');
    w.page.failNext.set('css=.product-tile', 'locator.click: Error: element is detached');
    await assert.rejects(cards[0].click(), /detached/);
    assert.deepEqual(heal.pending(), { '.product-card': first });
    assert.equal(heal.store.status('.product-card', first), 'active');
    assert.deepEqual(journalOf(w.dir).filter((e) => e.event === 'heal.canary' || e.event === 'heal.quarantine'), []);
  });

  test("the reads of a list's elements are never checked, and locatorAll takes no expectValue", async () => {
    const w = world(V1);
    const heal = w.open({ expectValues: { '.product-card': 'money' } });
    const items = await heal.locatorAll('.product-card', '.product-card');
    assert.equal(await items[0].innerText(), 'Blue mug$12.00Add to cart');   // not money, and no error
    assert.throws(() => heal.locatorAll('.product-card', '.product-card', { expectValue: 'money' } as never),
      (error: unknown) => error instanceof TypeError && /expectValue/.test((error as Error).message));
  });

  test('arguments that can never work throw at once, before the page is touched', () => {
    const w = world(V1);
    const heal = w.open();
    assert.throws(() => heal.locatorAll('', '.x'), TypeError);
    assert.throws(() => heal.locatorAll('cards', ''), InvalidSelector);
    assert.throws(() => heal.locatorAll('cards', []), TypeError);
    assert.throws(() => heal.locatorAll('cards', '.x', { role: 'tab' }), /unknown role/);
    assert.throws(() => heal.locatorAll('get_by_role("button", name="Go")', '#go'), /heal\.locatorAll\(\)/);
    assert.deepEqual(w.page.calls, []);
  });

  test('a key with no selector and nothing stored throws AttentionRequired', async () => {
    await assert.rejects(world(V1).open().locatorAll('cards'), AttentionRequired);
  });

  test("a list is looked up only through the key's own rows on the page", async () => {
    // A heal, a row saved inside an iframe and one inside a shadow root are never tried.
    const w = world(V1);
    const heal = w.open();
    heal.store.addCandidate('cards', TILE, { source: 'heal' });
    heal.store.addCandidate('cards', '.product-card', { source: 'code', frameXPath: '#frame' });
    heal.store.addCandidate('cards', 'my-app >> .product-card', { source: 'code', shadowHosts: ['my-app'] });
    heal.store.addCandidate('cards', "//div[@id='grid']/div", { source: 'code' });
    w.page.goto(V2);
    assert.equal((await heal.locatorAll('cards')).length, 4);
    assert.equal(heal.store.lastSuccess('cards')?.xpath, "//div[@id='grid']/div");
    const only = w.open();
    only.store.addCandidate('only', TILE, { source: 'heal' });
    await assert.rejects(only.locatorAll('only'), /No selector candidates for: only/);
  });
});

describe("the elements' interruptions", () => {
  const BANNER = "<div id='banner'><button id='accept' data-click-removes='#banner'>Accept</button></div>";

  test('the first read of a list handles the registered interruptions once for the whole list', async () => {
    const w = world(V1);
    const heal = w.open();
    heal.interrupt('banner', '#accept');
    const cards = await heal.locatorAll('cards', '.product-card');
    w.page.goto(V1.replace('<main', BANNER + '<main'));
    const counts = () => w.page.calls.filter(([call, selector]) => call === 'count' && selector === 'css=#accept').length;
    const before = counts();
    assert.equal(await cards[0].innerText(), TEXTS[0]);
    const after = counts();
    assert.ok(after > before);
    assert.deepEqual(w.page.actions, [['click', 'css=#accept']]);
    w.page.goto(V1.replace('<main', BANNER + '<main'));   // the banner came back
    assert.equal(await cards[1].innerText(), TEXTS[1]);
    assert.equal(await cards[0].innerText(), TEXTS[0]);
    assert.equal(counts(), after);   // looked at once, for the whole list
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['banner', 'cards']]);
  });

  test("an element's action handles the registered interruptions first, as heal.locator()'s does", async () => {
    const w = world(V1);
    const heal = w.open();
    heal.interrupt('banner', '#accept');
    const cards = await heal.locatorAll('cards', '.product-card');
    w.page.goto(V1.replace('<main', BANNER + '<main'));
    await cards[1].click();
    assert.deepEqual(w.page.actions, [['click', 'css=#accept'], ['click', 'css=.product-card']]);
    assert.equal(w.page.clicked[1].querySelector('h3')?.textContent, 'Red mug');
  });

  test('an interruption that keeps coming back throws InterruptionLoop from a list lookup', async () => {
    const w = world(V1);
    const heal = w.open({ timeoutMs: 50 });
    await heal.locatorAll('cards', '.product-card');
    heal.interrupt('banner', '#accept', { action: 'hover' });   // hovering never closes it
    w.page.goto(V2.replace('<main', "<div id='banner'><button id='accept'>Accept</button></div><main"));
    await assert.rejects(heal.locatorAll('cards', '.product-card', { pollMs: 5 }), InterruptionLoop);
  });

  test("an AttentionRequired a run function throws is thrown as it is, not taken for the list's miss", async () => {
    const w = world(V1);
    const model = new NoModel();
    const heal = w.open({ provider: model });
    await heal.locatorAll('cards', CARDS);
    const error = new AttentionRequired('mendwright: the sign-in form changed');
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', {
      run: async (page) => {
        calls.push(page);
        throw error;
      },
    });
    w.page.goto(shop(grid('product-tile'), "<div id='relogin' role='dialog'><button>Sign in</button></div>"));
    await assert.rejects(heal.locatorAll('cards', CARDS), (caught: unknown) => caught === error);
    assert.deepEqual([calls.length, heal.heals, model.calls], [1, [], []]);
    assert.deepEqual(heals(w.dir), []);
  });
});
