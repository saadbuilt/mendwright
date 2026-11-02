// heal.locatorAll(key, selector, { nonempty: true }) on a fake page (the Python package's
// tests/test_lists_nonempty.py): a list that always has items at that point. When none of the
// key's selectors matches, mendwright waits, looks for the list from its list fingerprint, saves
// what it finds as a proposed list heal for a person to check, and throws. It never gives or acts
// on a list it found. No model is ever asked: NoModel throws if it is. The fake page gives each
// visible element a box ({ rects: true }), so the live gate sees what a browser would.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import { HealCoordinator } from '../../src/coordinator';
import { PlaywrightDriver } from '../../src/driver';
import { listGate, listLiveReason } from '../../src/gate';
import { AttentionRequired } from '../../src/errors';
import { Healer, type LocatorAllOptions } from '../../src/healer';
import { Journal } from '../../src/journal';
import { whenText } from '../../src/lists';
import { loadStore, type Candidate } from '../../src/store';
import { cleanEnv, journalOf, world, type World } from '../support/env';
import { NoModel, V1, V2, cart, todomvc } from '../support/listpages';

beforeEach(cleanEnv);

const NO_MATCH = 'No matching element for: cart (tried 1 candidate(s)).\n  -> ';
const ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]";
const REJECTED = 'it is a list heal you rejected, or one mendwright quarantined: it quarantines a list heal you accepted '
  + 'once the selector it replaced matches again, even on one element';

function shopPage(html = cart(true), url = 'https://shop.test/cart'): World {
  return world(html, { url, rects: true });
}

/** A cart page whose list 'cart' was recorded with nonempty: true, its healer and its model. */
async function recorded(options: Parameters<World['open']>[0] = {}) {
  const w = shopPage();
  const model = new NoModel();
  const heal = w.open({ provider: model, ...options });
  assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty: true })).length, 3);
  return { w, heal, model };
}

/** The text of the AttentionRequired a lookup of 'cart' with nonempty: true throws. */
async function raised(heal: Healer, selector = '.cart-item', options: LocatorAllOptions = {}): Promise<string> {
  try {
    await heal.locatorAll('cart', selector, { nonempty: true, ...options });
  } catch (error) {
    assert.ok(error instanceof AttentionRequired, String(error));
    return (error as Error).message;
  }
  throw new Error('the lookup did not throw');
}

/** The rows of key in the store file, by selector. */
function rowsOf(w: World, key = 'cart'): Record<string, Candidate> {
  if (!fs.existsSync(w.storePath)) return {};
  const entry = JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors[key] ?? {};
  return Object.fromEntries((entry.candidates ?? []).map((c: Candidate) => [c.xpath, c]));
}

const flagOf = (heal: Healer) => ' --store "' + heal.store.overridePath + '"';
const events = (w: World, ...names: string[]) => journalOf(w.dir).filter((e) => names.includes(e.event as string));

function searched(fragment: string, had = '3 elements'): string {
  return NO_MATCH + "'cart' matched " + had + ' when its list fingerprint was recorded, and none of its selectors '
    + 'matches now. nonempty: true says the list has items here, so mendwright looked for it on the page, but '
    + fragment + '. If the selector broke, change it in your code. If the list can be empty here, remove nonempty: true.';
}

function unsearched(text: string): string {
  return NO_MATCH + "nonempty: true says 'cart' has items here, and none of its selectors matches. " + text
    + ' If the selector broke, change it in your code.';
}

const proposed = (w: World, key = 'cart') => Object.values(rowsOf(w, key)).filter((c) => c.status === 'proposed')
  .map((c) => c.xpath);

describe('a proposal', () => {
  test('a list whose rows were renamed is proposed, and never used', async () => {
    const { w, heal, model } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    const message = await raised(heal);
    const row = rowsOf(w)[ROWS];
    const pid = (row.proposal as { id: string }).id;
    const flag = flagOf(heal);
    assert.equal(message, NO_MATCH + "'cart' matched 3 elements when its list fingerprint was recorded, under 'Shopping "
      + "cart', in <section class=\"cart\">, and none of its selectors matches now. nonempty: true says the list has items "
      + "here, so mendwright looked for it and found 3 div elements, under 'Shopping cart', in <section class=\"cart\">, "
      + 'that hold 3 of the 3 items it recorded: ' + ROWS + ' (as CSS: div.cart-row). mendwright never uses a list it '
      + 'finds, because the items of an emptied list can show in another list on the page. If this is the list, look at '
      + 'it with: mendwright lists' + flag + '. Accept it with: mendwright lists accept cart ' + pid + flag + '. Then '
      + 'write it into your code with: mendwright fix --apply' + flag + '. If the list can be empty here, remove '
      + 'nonempty: true.');
    assert.deepEqual([row.status, row.list, row.css], ['proposed', true, 'div.cart-row']);
    assert.deepEqual((row.proposal as { was: { selectors: string[] } }).was.selectors, ['.cart-item']);
    assert.equal(heal.coordinator.explain('cart'), message.slice(NO_MATCH.length));
    assert.deepEqual([heal.heals, heal.pending(), model.calls], [[], {}, []]);
    assert.deepEqual(heal.coordinator.held, [{ key: 'cart', xpath: ROWS, id: pid, label: 'Shopping cart', count: 3 }]);
    const warned = w.lines.filter((l) => l.includes('found a list'));
    assert.equal(warned.length, 1);
    assert.ok(warned[0].startsWith("warning: found a list for 'cart' (3 div elements under 'Shopping cart'): .cart-item -> "
      + ROWS + ' ('), warned[0]);
    assert.ok(warned[0].endsWith('s). It is not used. Check it with: mendwright lists'), warned[0]);
    const lines = journalOf(w.dir);
    assert.deepEqual(lines.slice(-3).map((e) => e.event), ['heal.requested', 'heal.proposed', 'heal.held']);
    const held = lines[lines.length - 1];
    assert.deepEqual(Object.keys(held).filter((k) => k !== 'ts'), ['event', 'key', 'provider', 'latency_ms', 'xpath',
      'css', 'id', 'list', 'items', 'label', 'differs', 'old', 'page', 'source']);
    assert.deepEqual({ ...held, ts: null, latency_ms: null }, { ts: null, event: 'heal.held', key: 'cart',
      provider: 'fingerprint', latency_ms: null, xpath: ROWS, css: 'div.cart-row', id: pid, list: true, items: 3,
      label: 'Shopping cart', differs: [], old: ['.cart-item'], page: '/cart', source: 'fingerprint' });
    assert.ok(String(lines[lines.length - 2].reasoning).endsWith(', 3 elements, 3 of 3 recorded items'));
    // Nothing uses it: the next lookup, in this run and a fresh one, looks again and throws.
    for (const run of [heal, w.open({ provider: model })]) {
      assert.ok((await raised(run)).startsWith(NO_MATCH + "'cart' matched 3 elements"));
      assert.equal(Object.keys(rowsOf(w)).length, 2);   // the same row, again
    }
    assert.equal((rowsOf(w)[ROWS].proposal as { id: string }).id, pid);   // the same page, the same id
    assert.deepEqual(Object.values(rowsOf(w)).filter((c) => (c.status ?? 'active') === 'active').map((c) => c.xpath),
      ['.cart-item']);
    assert.deepEqual(model.calls, []);
  });

  test('an emptied cart is refused, and nothing is saved', async () => {
    const { w, heal, model } = await recorded();
    w.page.goto(cart(false));
    assert.equal(await raised(heal), searched("each group of div elements on the page is another list: the one most like "
      + "the list (3 elements) is under 'Saved for later', which labelled another part of the page when the list was "
      + "recorded, and the list was under 'Shopping cart'"));
    assert.deepEqual(proposed(w), []);
    const rejected = events(w, 'heal.rejected');
    assert.deepEqual([rejected[rejected.length - 1].list, rejected[rejected.length - 1].source], [true, 'fingerprint']);
    assert.deepEqual(events(w, 'heal.held', 'heal.applied'), []);
    assert.deepEqual(model.calls, []);
    assert.deepEqual(events(w, 'heal.declined').map((e) => e.code), ['list_not_found']);
  });
});

describe('the arguments', () => {
  test('nonempty takes only true or false, and not with optional: true, before the page is touched', () => {
    const w = shopPage();
    const heal = w.open();
    for (const [value, name] of [[1, 'number'], ['yes', 'string'], [null, 'null']] as const) {
      assert.throws(() => heal.locatorAll('cart', '.cart-item', { nonempty: value as never }), (error: unknown) =>
        error instanceof TypeError && (error as Error).message === "mendwright: heal.locatorAll('cart', ..., { nonempty: "
          + '... }) takes true or false, got ' + name + '.');
    }
    assert.throws(() => heal.locatorAll('cart', '.cart-item', { nonempty: true, optional: true }), (error: unknown) =>
      error instanceof TypeError && (error as Error).message === "mendwright: heal.locatorAll('cart', ..., { nonempty: "
        + 'true, optional: true }) asks for two opposite things: nonempty: true says the list always has items here, and '
        + 'optional: true says it may be empty. Pass one of them.');
    assert.deepEqual(w.page.calls, []);
    assert.equal(fs.existsSync(w.storePath), false);
  });
});

describe('recording where the list sits', () => {
  test('nonempty: true records where the list sits and its heading, and every later recording of it does too',
    async () => {
      const w = shopPage();
      const heal = w.open();
      await heal.locatorAll('cart', '.cart-item');   // a default call first
      assert.equal(placedOf(heal), false);
      await heal.locatorAll('cart', '.cart-item', { nonempty: true });   // not blocked by it
      const lfp = heal.store.metaOf('cart').listFingerprint as { label: string };
      assert.equal(placedOf(heal), true);
      assert.equal(lfp.label, 'Shopping cart');
      // Once placed, a default call of a later run records it placed again.
      const again = w.open();
      const writes: unknown[] = [];
      const setMeta = again.store.setMeta.bind(again.store);
      again.store.setMeta = (key, fields) => {
        writes.push(fields.listFingerprint);
        setMeta(key, fields);
      };
      await again.locatorAll('cart', '.cart-item');
      assert.equal(writes.filter((lfp) => lfp && (lfp as { place?: unknown }).place).length, 1);
      assert.equal(placedOf(w.open()), true);
    });

  test('a read of the place that fails keeps the stored place', async () => {
    const { w } = await recorded();
    const blind = w.open();
    blind.driver.listFingerprint = async () => null;   // the read gives nothing
    await blind.locatorAll('cart', '.cart-item', { nonempty: true });
    assert.equal(placedOf(w.open()), true);
  });

  test('a list looked up with nonempty: true saves its page and address', async () => {
    const w = world(cart(true), { url: 'https://Shop.Test/Cart/10482/?step=2', rects: true });
    const heal = w.open();
    assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty: true })).length, 3);
    const entry = JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors.cart;
    assert.deepEqual([entry.lastUrl, entry.lastPage], ['https://shop.test/Cart/*/', '/cart/*']);
  });
});

function placedOf(heal: Healer): boolean {
  const lfp = heal.store.metaOf('cart').listFingerprint as { label?: unknown; place?: unknown } | null;
  return !!lfp && typeof lfp.label === 'string' && !!lfp.place;
}

describe('what the search says and saves', () => {
  test('another page state is a proposal of its own, with an id of its own', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    await raised(heal);
    const first = (rowsOf(w)[ROWS].proposal as { id: string }).id;
    w.page.goto(cart(true, { row: 'cart-row' }).replace("<div class='cart-row'><a href='/p/tea-pot'>",
      "<div class='cart-row'><a href='/p/fork'>Fork</a><span class='price'>$1</span><input class='qty' value='1'>"
      + "<button class='delete'>Delete</button></div><div class='cart-row'><a href='/p/tea-pot'>"));
    await raised(heal);
    assert.notEqual((rowsOf(w)[ROWS].proposal as { id: string }).id, first);   // what it holds changed
    w.page.goto(cart(true, { row: 'basket-line' }));
    const message = await raised(heal);
    assert.equal(proposed(w).length, 2);
    assert.ok(message.includes('basket-line'));
  });

  test('what is not the same is said in the message', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }).replace('<h1>Shopping cart</h1>', '<h1>Your bag</h1>')
      .replace("<section class='cart'>", "<section class='bag'>"));
    const message = await raised(heal);
    assert.ok(message.includes(" The heading is not the same: the list was under 'Shopping cart', and these are under "
      + "'Your bag'. The container is not the same: the list was in <section class=\"cart\">, and these are in <section "
      + 'class="bag">. mendwright never uses'), message);
    assert.deepEqual((rowsOf(w)[ROWS].proposal as { differs: string[] }).differs, ['heading', 'container']);
  });

  test('page text is made printable in the store and the message', async () => {
    const w = shopPage(cart(true).replace('Shopping cart', 'Shopping\x9b31mcart'));
    const heal = w.open();
    await heal.locatorAll('cart', '.cart-item', { nonempty: true });
    w.page.goto(cart(true, { row: 'cart-row' }).replace('Shopping cart', 'Shopping\x9b31mcart'));
    const message = await raised(heal);
    const proposal = rowsOf(w)[ROWS].proposal as { was: { label: string }; now: { label: string } };
    assert.deepEqual([proposal.was.label, proposal.now.label], ['Shopping?31mcart', 'Shopping?31mcart']);
    assert.ok(message.includes("under 'Shopping?31mcart'") && !message.includes('\x9b'), message);
  });

  test('a key that needs quotes is quoted for the shell, and one with a control character is never printed raw',
    async () => {
      const w = shopPage();
      const heal = w.open();
      await heal.locatorAll('my cart', '.cart-item', { nonempty: true });
      const key = 'cart\x1b[31m\x9b';
      await heal.locatorAll(key, '.cart-item', { nonempty: true });
      w.page.goto(cart(true, { row: 'cart-row' }));
      const quoted = await heal.locatorAll('my cart', '.cart-item', { nonempty: true }).catch((e: Error) => e.message);
      assert.ok(String(quoted).includes('mendwright lists accept "my cart" '), String(quoted));
      const message = String(await heal.locatorAll(key, '.cart-item', { nonempty: true }).catch((e: Error) => e.message));
      const pid = (rowsOf(w, key)[ROWS].proposal as { id: string }).id;
      assert.ok(message.includes("'cart?[31m?' matched 3 elements"), message);
      assert.ok(message.includes('Accept it with: mendwright lists accept KEY ' + pid + flagOf(heal)
        + " (put the key 'cart\\x1b[31m\\x9b' in quotes for your shell)"), message);
      // The no-match line before it names the key as the code passed it.
      assert.ok(!/[\x00-\x09\x0b-\x1f\x7f-\x9f]/.test(message.split('\n  -> ')[1]));
    });
});

describe('the refusals of the search', () => {
  test('with the fingerprint tier off, nonempty: true waits and says what turned it off', async () => {
    for (const [off, cause] of [['fingerprint: false', 'fingerprint: false'],
      ['MENDWRIGHT_FINGERPRINT=0', 'MENDWRIGHT_FINGERPRINT=0'], ['MENDWRIGHT_DISABLE=1', 'MENDWRIGHT_DISABLE=1']]) {
      cleanEnv();
      const { w } = await recorded();
      if (off !== 'fingerprint: false') process.env[off.split('=')[0]] = off.split('=')[1];
      w.page.goto(cart(true, { row: 'cart-row' }));
      const heal = w.open(off === 'fingerprint: false' ? { fingerprint: false } : {});
      assert.equal(await raised(heal), unsearched('mendwright looks for a list only while the fingerprint tier is on, and '
        + cause + ' turns it off.'), off);
      assert.deepEqual(events(w, 'heal.held', 'heal.requested'), [], off);
    }
  });

  test("a coordinator built by hand with the tier off is named as the HealCoordinator's fingerprint: false",
    async () => {
      const w = shopPage(cart(false));
      const store = loadStore(w.storePath);
      const driver = new PlaywrightDriver(w.page as never);
      const coordinator = new HealCoordinator(store, { provider: new NoModel(), driver, journal: new Journal(w.dir + '/j') });
      const heal = new Healer(w.page as never, driver, store, coordinator, 0);
      assert.equal(await raised(heal), unsearched('mendwright looks for a list only while the fingerprint tier is on, and '
        + "the HealCoordinator's fingerprint: false turns it off."));
    });

  test('a list never seen with items waits the whole timeout, then says so', async () => {
    const w = shopPage(cart(false));
    const heal = w.open({ timeoutMs: 300 });
    const started = Date.now();
    assert.equal(await raised(heal, '.cart-item', { pollMs: 20 }), unsearched('mendwright has never seen this list '
      + "match 2 or more elements, so it has nothing to look for. It records a list each time the list's selector "
      + 'matches 2 or more.'));
    assert.ok(Date.now() - started >= 290);
  });

  test('a list recorded before a call with nonempty: true is not looked for until one records it', async () => {
    const w = shopPage();
    const heal = w.open();
    await heal.locatorAll('cart', '.cart-item');   // recorded without where it sits
    w.page.goto(cart(true, { row: 'cart-row' }));
    assert.equal(await raised(heal), searched("its list fingerprint was recorded by mendwright 0.8.0 or before a call "
      + "with nonempty: true matched the list, so it doesn't hold where the list was or its heading, and another list "
      + "that holds its items can't be told from it. mendwright records them the next time the list matches in a call "
      + 'with nonempty: true'));
    w.page.goto(cart(true));
    await heal.locatorAll('cart', '.cart-item', { nonempty: true });
    w.page.goto(cart(true, { row: 'cart-row' }));
    assert.ok((await raised(heal)).includes('Accept it with: mendwright lists accept cart'));
  });

  test('a key marked destructive later is refused before anything', async () => {
    const { w, heal } = await recorded();
    heal.store.setMeta('cart', { destructive: true });
    w.page.goto(cart(true, { row: 'cart-row' }));
    assert.equal(await raised(heal), NO_MATCH + "'cart' is marked as a destructive control, and mendwright never "
      + 'auto-heals those, because a validated but wrong selector here could act on a live account. Fix the selector in '
      + 'your code by hand.');
    assert.deepEqual(events(w, 'heal.requested'), []);
    assert.deepEqual(events(w, 'heal.declined').map((e) => e.code), ['destructive']);
  });

  test('another page is refused, and healAcrossPages: true lets it look', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }), 'https://shop.test/checkout');
    assert.equal(await raised(heal), NO_MATCH + "'cart' was last found on /cart, and this page is /checkout. That looks "
      + 'like a change in the flow, not a renamed element, so mendwright did not heal it. If the step moved, update your '
      + 'script. If the URL itself changed in a redesign, pass healAcrossPages: true to mendwright.open().');
    assert.ok((await raised(w.open({ healAcrossPages: true }))).includes('Accept it with:'));
  });

  test('an unknown role names heal.locatorAll()', async () => {
    const { w, heal } = await recorded();
    heal.store.guessRole('cart', 'unknown', true);
    w.page.goto(cart(true, { row: 'cart-row' }));
    assert.equal(await raised(heal), NO_MATCH + "mendwright does not know what kind of element 'cart' is (its expected "
      + 'role is unknown), so it cannot check a repaired selector. Pass role to heal.locatorAll(), for example role: '
      + "'textbox' for a text field, 'button', 'checkbox', 'link' or 'combobox'.");
  });

  test('the guard stops the search', async () => {
    const { w } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    const heal = w.open({ guard: () => false });
    assert.ok((await raised(heal)).endsWith('a transaction guard is currently blocking heals (a payment or registration '
      + 'submission is pending), so mendwright did not try to heal. Run the step again once the guard allows heals.'));
    assert.deepEqual(events(w, 'heal.requested'), []);
  });

  test('a list recorded inside a shadow root is never looked for', async () => {
    const page = (cls: string) => '<html><head><title>t</title></head><body><section><h2>Saved</h2><ul class="saved">'
      + ['A', 'B', 'C'].map((n) => '<li class="row"><a href="/p/' + n + '">' + n + '</a></li>').join('')
      + '</ul></section><my-app id="root"><template shadowrootmode="open"><section><h2>Cart</h2><ul class="cart">'
      + ['A', 'B', 'C'].map((n) => '<li class="' + cls + '"><a href="/p/' + n + '">' + n + '</a></li>').join('')
      + '</ul></section></template></my-app></body></html>';
    const w = world(page('card'), { url: 'https://shop.test/cart', rects: true });
    const heal = w.open();
    assert.equal((await heal.locatorAll('cart', '.card', { role: 'li', nonempty: true, pollMs: 0 })).length, 3);
    assert.equal((heal.store.metaOf('cart').listFingerprint as { shadow?: boolean }).shadow, true);
    w.page.goto(page('item'));
    const message = String(await w.open().locatorAll('cart', '.card', { role: 'li', nonempty: true, pollMs: 0 })
      .catch((e: Error) => e.message));
    assert.ok(message.includes('The list was inside a shadow root'), message);
    assert.deepEqual(Object.values(rowsOf(w)).filter((c) => c.list), []);
  });

  test('a group hidden on the live page is no list', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }).replace("<section class='cart'>",
      "<section class='cart' style='display:none'>"));
    assert.equal(await raised(heal), searched('no group of div elements is visible on the live page'));
  });

  test('a group picked by a state word the list did not have is refused', async () => {
    const w = world(todomvc('todo'), { url: 'https://todo.test/', rects: true });
    const heal = w.open();
    await heal.locatorAll('cart', 'li.todo', { nonempty: true });
    w.page.goto(todomvc('completed'));
    const xpath = "//li[contains(concat(' ', normalize-space(@class), ' '), ' completed ')]";
    assert.equal(await raised(heal, 'li.todo'), searched('the group it found, ' + xpath + ", is picked by 'completed', "
      + "and 'completed' names a state the recorded list didn't have, so these are most likely the list's items in "
      + 'another state, or another list, such as completed tasks or items saved for later', '4 elements'));
    const rejected = events(w, 'heal.rejected');
    assert.equal(rejected[rejected.length - 1].xpath, xpath);
    assert.deepEqual(proposed(w), []);
  });

  test('a rejected list heal is refused, and so are its elements under another spelling', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    await raised(heal);
    heal.store.quarantine('cart', ROWS);   // as mendwright lists reject does
    assert.equal(await raised(w.open()), searched('the group it found, ' + ROWS + ", failed mendwright's list checks ("
      + REJECTED + ')'));
    // A quarantined selector of the key that reaches the same elements under another spelling
    // takes them out too.
    const other = await recorded();
    other.heal.store.addCandidate('cart', "//section[@class='cart']/div", { source: 'heal' });
    other.heal.store.quarantine('cart', "//section[@class='cart']/div");
    other.w.page.goto(cart(true, { row: 'cart-row' }));
    assert.equal(await raised(other.heal), searched('the group it found, ' + ROWS + ", failed mendwright's list checks "
      + '(it takes elements of a list heal you rejected, or of a quarantined selector of this key)'));
    assert.deepEqual(proposed(other.w), []);
  });

  test('dry run checks the list it finds and saves nothing', async () => {
    const { w } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    const heal = w.open({ dryRun: true });
    assert.equal(await raised(heal), NO_MATCH + "dry-run mode is on, so the list mendwright found for 'cart', " + ROWS
      + ', was checked but not saved. Turn off dryRun to save it for you to check.');
    assert.ok(w.lines.includes('info: [heal dry-run] key=cart proposed=' + ROWS + ' (list) valid=true (not saved)'),
      w.lines.join('\n'));
    assert.equal(ROWS in rowsOf(w), false);
    assert.deepEqual(events(w, 'heal.held'), []);
    assert.deepEqual(events(w, 'heal.declined').slice(-1).map((e) => e.code), ['dry_run']);
  });

  test('what the search refuses is said, and nothing is saved', async () => {
    const skeleton = "<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
      + "<button type='button' disabled></button></div>";
    const cases: [string, string, string, string][] = [
      ['L12', cart(true), cart(true).replace(/<div class='cart-item'>.*?<\/div>/g, skeleton),
        'the only groups of div elements look like placeholders (no text where the recorded items had text, or marked '
        + 'aria-busy="true"), so the list may still be loading'],
      ['L13', cart(true), "<html><body><main><h1>Shopping cart</h1><section class='cart'><div class='cart-row'><a "
        + "href='/p/blue-mug'>Blue mug</a></div></section></main></body></html>", 'the page has no group of 2 or more div '
        + 'elements that repeat the same structure'],
    ];
    for (const [code, before, after, fragment] of cases) {
      const w = shopPage(before);
      const heal = w.open();
      await heal.locatorAll('cart', '.cart-item', { nonempty: true });
      w.page.goto(after);
      const message = await raised(heal);
      assert.ok(message.includes('nonempty: true says the list has items here, so mendwright looked for it on the page, '
        + 'but ' + fragment), code + ': ' + message);
      assert.ok(message.endsWith('. If the selector broke, change it in your code. If the list can be empty here, remove '
        + 'nonempty: true.'), code);
      assert.deepEqual(proposed(w), [], code);
      const rejected = events(w, 'heal.rejected');
      assert.equal(rejected[rejected.length - 1].list, true, code);
      assert.deepEqual(events(w, 'heal.held', 'heal.applied'), [], code);
    }
  });

  test('a page that cannot be parsed is said, with no heal.rejected', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    w.page.content = async () => '';
    assert.equal(await raised(heal), searched('the captured page could not be parsed'));
    assert.deepEqual(events(w, 'heal.rejected'), []);
  });
});

describe('a list that comes', () => {
  test('a list that comes while mendwright waits is used', async () => {
    const { w } = await recorded();
    const heal = w.open({ timeoutMs: 5000 });
    w.page.goto(cart(true).replace(/class='cart-item'/g, "class='skeleton'"));
    setTimeout(() => w.page.goto(cart(true)), 150);
    assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty: true, pollMs: 5 })).length, 3);
    assert.deepEqual(events(w, 'heal.held'), []);
  });

  test('a list that came during the search is used as it is', async () => {
    const { w } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    const content = w.page.content.bind(w.page);
    let once = true;
    w.page.content = async () => {
      const html = await content();
      if (once) w.page.goto(cart(true));   // the code's selector matches once the page was captured
      once = false;
      return html;
    };
    const heal = w.open();
    assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty: true })).length, 3);
    assert.equal(ROWS in rowsOf(w), false);
    assert.ok(w.lines.includes("info: 'cart' came while mendwright looked for it, so .cart-item was used as it is."),
      w.lines.join('\n'));
    assert.deepEqual(events(w, 'heal.held', 'heal.rejected'), []);
  });

  test('a list that came and went during the search is refused', async () => {
    const { w } = await recorded();
    const renamed = cart(true, { row: 'cart-row' });
    w.page.goto(renamed);
    const content = w.page.content.bind(w.page);
    let once = true;
    w.page.content = async () => {
      const html = await content();
      if (once) w.page.goto(cart(true));
      once = false;
      return html;
    };
    const heal = w.open();
    const count = heal.driver.count.bind(heal.driver);
    heal.driver.count = async (selector: string, ...rest: unknown[]) => {
      const found = await count(selector, ...(rest as []));
      if (selector === '.cart-item' && found) w.page.goto(renamed);   // it went away again
      return found;
    };
    assert.equal(await raised(heal), searched('the group it found, ' + ROWS + ", failed mendwright's list checks (the "
      + 'selector in your code, .cart-item, matched while mendwright looked and matches nothing now)'));
  });

  test('interruptions are handled while a list with nonempty: true waits', async () => {
    const { w, heal } = await recorded();
    const popup = "<div id='promo' role='dialog'><button id='close'>Close</button></div>";
    w.page.goto(cart(true).replace('<main>', '<main>' + popup).replace(/class='cart-item'/g, "class='cart-item-hidden'"));
    let clicks = 0;
    heal.interrupt('promo', '#promo', {
      run: async () => {
        clicks += 1;
        w.page.goto(cart(true));
      },
    });
    assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty: true })).length, 3);
    assert.equal(clicks, 1);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['promo', 'cart']]);
  });

  test("a run function's AttentionRequired is thrown as it is", async () => {
    const { w, heal } = await recorded();
    const error = new AttentionRequired('mendwright: the sign-in form changed');
    heal.interrupt('relogin', '#relogin', {
      run: async () => {
        throw error;
      },
    });
    w.page.goto(cart(true, { row: 'cart-row' }).replace('<main>', "<main><div id='relogin'>x</div>"));
    await assert.rejects(heal.locatorAll('cart', '.cart-item', { nonempty: true }), (caught: unknown) => caught === error);
    assert.deepEqual(events(w, 'heal.held'), []);
  });
});

describe('the list gate', () => {
  const TILE = "//div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]";
  const CARDS_XPATH = "//div[contains(@class,'product-card')]";

  /** A store whose key 'cards' was recorded on V1 with code, and a driver on page html. */
  async function gate(code: string, html: string) {
    const w = world(V1, { url: 'https://shop.test/shop', rects: true });
    const heal = w.open();
    await heal.locatorAll('cards', code, { nonempty: true });
    w.page.goto(html);
    return { w, store: heal.store, meta: heal.store.metaOf('cards'), driver: new PlaywrightDriver(w.page as never) };
  }

  test('the live check of a list: two or more of the role, one of them visible, or it fails closed', async () => {
    const { driver } = await gate(CARDS_XPATH, V2);
    const meta = { expectedRole: 'div' };
    assert.equal(await listLiveReason(driver, TILE, meta), '');
    assert.equal(await listLiveReason(driver, '//h1', meta), 'it matches 1 element on the live page, and a list heal '
      + 'needs 2 or more');
    assert.equal(await listLiveReason(driver, TILE, { expectedRole: 'li' }), 'live element role mismatch: got div want li');
    const hidden = world(V2.replace("id='grid' class='grid'", "id='grid' class='grid' style='display:none'"),
      { rects: true });
    assert.equal(await listLiveReason(new PlaywrightDriver(hidden.page as never), TILE, meta),
      'none of its 4 elements is visible on the live page');
    driver.listItems = async () => {
      throw new Error('Target closed');
    };
    assert.equal(await listLiveReason(driver, TILE, meta), 'the live page could not be read');
  });

  test('what the gate takes, refuses and names', async () => {
    const { store, meta, driver } = await gate(CARDS_XPATH, V2);
    assert.deepEqual(await listGate(store, 'cards', TILE, V2, meta, driver, 'div.product-tile'),
      ['ok', 'ok', 4, null]);   // the code's selector is XPath: no CSS form
    const css = await gate('.product-card', V2);
    assert.deepEqual(await listGate(css.store, 'cards', TILE, V2, css.meta, css.driver, 'div.product-tile'),
      ['ok', 'ok', 4, 'div.product-tile']);
    assert.equal((await listGate(css.store, 'cards', TILE, V2, css.meta, css.driver, 'div.nope'))[3], null);
    assert.deepEqual((await listGate(store, 'cards', '//section/div', V2, meta, driver)).slice(0, 2), ['refused', 'no match']);
    assert.equal((await listGate(store, 'cards', "//div[@id='grid']/div[2]", V2, meta, driver))[1],
      'it uses a position ([2], last() or position()), and a list heal never does');
    assert.equal((await listGate(store, 'cards', '/html/body[1]/main/div/div', V2, meta, driver))[1],
      'brittle absolute-index path');
    assert.equal((await listGate(store, 'cards', '//h1', V2, meta, driver))[1],
      'it matches 1 element, and a list heal needs 2 or more');
    assert.equal((await listGate(store, 'cards', '//div', V2, meta, driver))[1], 'its elements are inside one another');
    // The captured page has them, and the live page does not show them, or can't be read.
    const hidden = world(V2.replace("id='grid' class='grid'", "id='grid' class='grid' style='display:none'"),
      { rects: true });
    assert.deepEqual(await listGate(store, 'cards', TILE, V2, meta, new PlaywrightDriver(hidden.page as never)),
      ['refused', 'none of its 4 elements is visible on the live page', 4, null]);
    const closed = new PlaywrightDriver(world(V2, { rects: true }).page as never);
    closed.listItems = async () => {
      throw new Error('Target closed');
    };
    assert.deepEqual(await listGate(store, 'cards', TILE, V2, meta, closed),
      ['refused', 'the live page could not be read', 4, null]);
    // The list came while mendwright looked: the code's selector matches now, and that is checked first.
    const came = await gate(CARDS_XPATH, V1);
    assert.deepEqual(await listGate(came.store, 'cards', TILE, V2, came.meta, came.driver), ['came', CARDS_XPATH, 0, null]);
  });

  test('a list heal proposed may be proposed again; one accepted is named; one rejected, or its elements, refused',
    async () => {
      const { store, meta, driver } = await gate(CARDS_XPATH, V2);
      store.proposeListHeal('cards', TILE, { proposal: { id: 'a', at: 'b', count: 4 } });
      assert.equal((await listGate(store, 'cards', TILE, V2, meta, driver))[0], 'ok');
      const row = store.listHeals('cards')[0];
      assert.deepEqual([row.status, row.list, (row.proposal as { id: string }).id], ['proposed', true, 'a']);
      store.quarantine('cards', TILE);
      assert.equal((await listGate(store, 'cards', TILE, V2, meta, driver))[1], REJECTED);
      assert.equal((await listGate(store, 'cards', "//div[@id='grid']/div", V2, meta, driver))[1],
        'it takes elements of a list heal you rejected, or of a quarantined selector of this key');
      const accepted = await gate(CARDS_XPATH, V2);
      accepted.store.proposeListHeal('cards', TILE, { proposal: { id: 'a', at: 'b', count: 4 } });
      (accepted.store as unknown as { data: { selectors: Record<string, { candidates: Candidate[] }> } }).data
        .selectors.cards.candidates.find((c) => c.xpath === TILE)!.status = 'accepted';
      assert.deepEqual(await listGate(accepted.store, 'cards', TILE, V2, accepted.meta, accepted.driver),
        ['accepted', '', 0, null]);
      assert.throws(() => accepted.store.proposeListHeal('cards', TILE, { proposal: { id: 'b', at: 'c', count: 4 } }),
        /is accepted for 'cards', so it is not proposed again/);
    });
});

/** Accept the proposal of ROWS in the store file as the Python package's mendwright lists accept
 * does: its status, and when. */
function accept(w: World, at = '2026-11-02T10:20:00+00:00'): void {
  const data = JSON.parse(fs.readFileSync(w.storePath, 'utf-8'));
  const row = data.selectors.cart.candidates.find((c: Candidate) => c.xpath === ROWS);
  row.status = 'accepted';
  row.acceptedAt = at;
  fs.writeFileSync(w.storePath, JSON.stringify(data));
}

describe('retiring a list heal', () => {
  for (const nonempty of [true, false]) {
    test('the selector it replaced matching again retires it, with nonempty: ' + nonempty, async () => {
      const { w, heal } = await recorded();
      w.page.goto(cart(true, { row: 'cart-row' }));
      await raised(heal);
      const at = (rowsOf(w)[ROWS].proposal as { at: string }).at;
      w.page.goto(cart(true));   // the old selector works
      assert.equal((await heal.locatorAll('cart', '.cart-item', { nonempty })).length, 3);
      assert.equal(rowsOf(w)[ROWS].status, 'superseded');
      assert.ok(w.lines.includes("info: .cart-item matches 'cart' again, so the list heal found for it on " + whenText(at)
        + ' is no longer proposed.'), w.lines.join('\n'));
      // Found again, accepted, and the old selector matches once more: quarantined.
      w.page.goto(cart(true, { row: 'cart-row' }));
      await raised(heal);
      assert.equal(rowsOf(w)[ROWS].status, 'proposed');
      accept(w);
      w.page.goto(cart(true));
      const again = w.open();
      assert.equal((await again.locatorAll('cart', '.cart-item', { nonempty })).length, 3);
      assert.equal(rowsOf(w)[ROWS].status, 'quarantined');
      assert.ok(w.lines.includes("warning: the selector in your code for 'cart', .cart-item, matches 3 elements again, so "
        + 'the list heal you accepted for it on 2026-11-02 10:20:00 UTC, ' + ROWS + ', is quarantined: the list was not '
        + "broken, or the key is used on more than one page. mendwright fix won't write it."), w.lines.join('\n'));
      const quarantined = events(w, 'heal.quarantine');
      assert.deepEqual({ ...quarantined[quarantined.length - 1], ts: null }, { ts: null, event: 'heal.quarantine',
        key: 'cart', xpath: ROWS, list: true, reason: 'the selector it replaced matches again' });
    });
  }

  test('one element of the selector it replaced quarantines an accepted list heal; the way back is the code',
    async () => {
      // A list with one item is not empty, so one element that keeps the old class (a mini cart)
      // is enough: mendwright can't tell it from the cart, and fails closed.
      const { w, heal } = await recorded();
      w.page.goto(cart(true, { row: 'cart-row' }));
      await raised(heal);
      accept(w);
      w.page.goto(cart(true, { row: 'cart-row' }).replace('<h2>Saved for later',
        "<aside><div class='cart-item'>Blue mug</div></aside><h2>Saved for later"));
      assert.equal((await w.open().locatorAll('cart', '.cart-item')).length, 1);
      assert.equal(rowsOf(w)[ROWS].status, 'quarantined');
      w.page.goto(cart(true, { row: 'cart-row' }));
      const again = w.open();
      assert.equal(await raised(again), searched('the group it found, ' + ROWS + ", failed mendwright's list checks ("
        + REJECTED + ')'));
      // The way back: the selector in the code, by hand.
      assert.equal((await again.locatorAll('cart', ROWS, { nonempty: true })).length, 3);
      assert.equal(rowsOf(w)[ROWS].status, 'active');
    });

  test('another selector of the key retires nothing', async () => {
    const { w, heal } = await recorded();
    w.page.goto(cart(true, { row: 'cart-row' }));
    await raised(heal);
    assert.equal((await heal.locatorAll('cart', '.cart-row', { nonempty: true })).length, 3);   // another page's
    assert.equal(rowsOf(w)[ROWS].status, 'proposed');
    assert.deepEqual(events(w, 'heal.quarantine'), []);
  });

  test('a list heal with no proposal is retired by a selector of the code, and only by one', () => {
    const w = shopPage();
    const store = loadStore(w.storePath);
    store.setCodeCandidates('cart', ['.cart-item']);
    store.proposeListHeal('cart', ROWS, { proposal: { id: 'a', at: 'b', count: 3 } });
    const data = JSON.parse(fs.readFileSync(w.storePath, 'utf-8'));
    delete data.selectors.cart.candidates.find((c: Candidate) => c.xpath === ROWS).proposal;   // as 0.8.0 wrote it
    fs.writeFileSync(w.storePath, JSON.stringify(data));
    const again = loadStore(w.storePath);
    again.setCodeCandidates('cart', ['.cart-item']);
    assert.deepEqual(again.retireListHeals('cart', '.other'), []);
    assert.deepEqual(again.retireListHeals('cart', '.cart-item'), [[ROWS, 'proposed', 'superseded']]);
    assert.equal(loadStore(w.storePath).status('cart', ROWS), 'superseded');
  });
});
