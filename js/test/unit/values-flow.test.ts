// expectValue on the fake page: a read through a heal must return what the key's check
// accepts (the Python package's tests/test_values.py). A read that heals onto the wrong number
// is refused and quarantined, a correct one passes and is confirmed, a read through the
// selector in your code is never checked, and the journal lines are the ones the Python
// package writes for the same call.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { AttentionRequired } from '../../src/errors';
import { Journal } from '../../src/journal';
import { FakeModel, cleanEnv, journalOf, tempDir, world } from '../support/env';
import { needsPython, runPython } from '../support/python';

// A product page before and after a redesign: the price lost its id, and a rating, a count
// and a delivery date sit next to it.
const V1 = "<html><body><main id='product'><h1>Blue mug</h1><span id='price'>$1,299.00</span>"
  + "<span class='rating'>4.5</span><span class='reviews'>(128 reviews)</span>"
  + "<span class='eta'>Oct 15, 2026</span><a class='seller' href='/s/acme'>Acme</a>"
  + "<input id='qty' name='qty' value='2'></main></body></html>";
const V2 = V1.replace("id='price'", "class='amount'");
const RATING = "//span[@class='rating']";
const AMOUNT = "//span[@class='amount']";

beforeEach(cleanEnv);

/** What a promise threw, or null. */
async function thrown(promise: Promise<unknown>): Promise<Error | null> {
  return promise.then(() => null, (error: Error) => error);
}

const events = (dir: string, name: string) => journalOf(dir).filter((e) => e.event === name);

describe('a read through a heal', () => {
  test('that heals onto the wrong number is refused, quarantined, and its value never written', async () => {
    const w = world(V1);
    const model = new FakeModel([RATING]);
    const heal = w.open({ provider: model, fingerprint: false });
    // The selector in the code: not checked.
    assert.equal(await heal.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText(), '$1,299.00');
    w.page.goto(V2);
    const price = heal.locator('price', '#price', { role: 'span', expectValue: 'money' });
    const error = await thrown(price.innerText());
    assert.ok(error instanceof AttentionRequired, String(error));
    const message = error.message;
    assert.ok(message.startsWith("mendwright: 'price' was healed to " + RATING + ', '), message);
    assert.ok(message.includes("its innerText() there is '4.5', which is not money (expectValue: 'money')"), message);
    assert.ok(message.includes('mendwright quarantined it and did not return the value'), message);
    assert.ok(w.lines.includes('warning: ' + message.slice('mendwright: '.length)), 'the same text is logged as a WARNING');
    assert.equal(heal.store.status('price', RATING), 'quarantined');
    assert.equal(heal.heals[heal.heals.length - 1].confirmed, false);
    assert.deepEqual(heal.pending(), {});
    assert.deepEqual(model.outcomes, [['price', RATING, false]]);
    const canary = events(w.dir, 'heal.canary');
    assert.deepEqual(canary.map((e) => [e.key, e.xpath, e.success, e.reason, e.by]),
      [['price', RATING, false, 'expect_value: innerText() is not money', 'expect_value']]);
    assert.deepEqual(events(w.dir, 'heal.quarantine').map((e) => [e.key, e.xpath, e.reason, e.by]),
      [['price', RATING, 'expect_value: innerText() is not money', undefined]]);
    assert.ok(!fs.readFileSync(path.join(w.dir, 'journal.jsonl'), 'utf-8').includes('4.5'), 'a page value reached the journal');
  });

  test('the next lookup heals again, and cannot pick the element it refused', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING, RATING, RATING]), fingerprint: false });
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    w.page.goto(V2);
    assert.ok(await thrown(heal.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText()));
    const again = await thrown(heal.locator('price', '#price', { role: 'span' }).resolve());
    assert.match(String(again), /previously quarantined/);
    assert.equal(heal.store.status('price', RATING), 'quarantined');
  });

  test('a correct heal passes, is confirmed by expect_value, and is checked again on each read', async () => {
    const w = world(V1);
    const model = new FakeModel([AMOUNT]);
    const heal = w.open({ provider: model, fingerprint: false });
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    w.page.goto(V2.replace('$1,299.00', '1.299,00 €'));   // and the shop went to euros
    const price = heal.locator('price', '#price', { role: 'span', expectValue: 'money' });
    assert.equal(await price.innerText(), '1.299,00 €');
    assert.equal(heal.heals[heal.heals.length - 1].confirmed, true);
    assert.deepEqual(heal.pending(), {});
    assert.deepEqual(model.outcomes, [['price', AMOUNT, true]]);
    assert.equal(await price.innerText(), '1.299,00 €');   // again: still checked, passes
    assert.deepEqual(events(w.dir, 'heal.canary').map((e) => [e.success, e.by, 'reason' in e]), [[true, 'expect_value', false]]);
    assert.deepEqual(events(w.dir, 'heal.quarantine'), []);
    // A value that fails later, through the same confirmed heal, is still refused.
    w.page.goto(V2.replace('$1,299.00', 'Sold out'));
    assert.match(String(await thrown(price.innerText())), /there is 'Sold out', which is not money/);
    assert.equal(heal.store.status('price', AMOUNT), 'quarantined');
  });

  test("an earlier run's heal is checked too: quarantined, with a heal.quarantine and no canary", async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false });
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    w.page.goto(V2);
    assert.equal(await heal.locator('price', '#price', { role: 'span' }).innerText(), '4.5');   // healed, never checked
    const later = w.open({ provider: new FakeModel([]), fingerprint: false });    // the next run
    const error = await thrown(later.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText());
    assert.match(String(error), /'price' was healed to \/\/span\[@class='rating'\]/);
    assert.equal(later.store.status('price', RATING), 'quarantined');
    assert.deepEqual(later.pending(), {});
    assert.deepEqual(events(w.dir, 'heal.canary'), []);
    assert.deepEqual(events(w.dir, 'heal.quarantine').map((e) => [e.key, e.xpath, e.reason]),
      [['price', RATING, 'expect_value: innerText() is not money']]);
  });

  test('a read through the selector in the code is never checked', async () => {
    const w = world(V1.replace('$1,299.00', 'Call for price'));
    const heal = w.open({ provider: new FakeModel([]), fingerprint: false, expectValues: { price: 'money', '#qty': 'date' } });
    assert.equal(await heal.locator('price', '#price', { role: 'span' }).innerText(), 'Call for price');
    assert.equal(await heal.locator('qty', '#qty', { expectValue: 'date' }).inputValue(), '2');
    assert.equal(await heal.page.inputValue('#qty'), '2');
    assert.equal(await heal.locator('seller', "//a[@class='seller']", { expectValue: 'email' }).getAttribute('href'), '/s/acme');
    assert.deepEqual(journalOf(w.dir), []);
    assert.equal(heal.store.status('price', '#price'), 'active');
  });

  test('the reads it checks: getAttribute(), textContent() and inputValue(), and null passes no check', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false });
    await heal.locator('seller', '#seller-link', { role: 'link' }).resolve();
    heal.coordinator.pending.clear();   // as if confirmed earlier
    const seller = heal.locator('seller', '#seller-link', { role: 'link', expectValue: 'url' });
    assert.equal(await seller.getAttribute('href'), '/s/acme');
    assert.match(String(await thrown(seller.getAttribute('title'))), /its getAttribute\('title'\) there is null, which is not a URL \(expectValue: 'url'\)/);
    assert.equal(events(w.dir, 'heal.quarantine')[0].reason, "expect_value: getAttribute('title') is not a URL");

    const w2 = world(V1);
    const heal2 = w2.open({ provider: new FakeModel(["//span[@class='reviews']", "//span[@class='reviews']",
      "//span[@class='reviews']"]), fingerprint: false });
    assert.match(String(await thrown(heal2.locator('count', '#count', { role: 'span', expectValue: 'integer' }).textContent())),
      /textContent\(\) there is '\(128 reviews\)', which is not a whole number/);
    assert.match(String(await thrown(heal2.locator('count', '#count', { role: 'span', expectValue: 'integer' }).resolve())),
      /previously quarantined/);

    const w3 = world(V1);
    const heal3 = w3.open({ provider: new FakeModel(["//input[@name='qty']"]), fingerprint: false });
    assert.match(String(await thrown(heal3.locator('qty', '#quantity', { role: 'input', expectValue: 'date' }).inputValue())),
      /'qty' was healed to \/\/input\[@name='qty'\], and its inputValue\(\) there is '2', which is not a date/);
  });

  test('a function check gets the value, is not called for null, and refuses when it throws', async () => {
    const seen: unknown[] = [];
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false });
    const isPath = (value: string) => {
      seen.push(value);
      return value.startsWith('/');
    };
    await heal.locator('seller', '#seller-link', { role: 'link' }).resolve();
    heal.coordinator.pending.clear();
    const seller = heal.locator('seller', '#seller-link', { role: 'link', expectValue: isPath });
    assert.equal(await seller.getAttribute('href'), '/s/acme');
    assert.match(String(await thrown(seller.getAttribute('title'))), /there is null, which was refused by isPath\(\) \(expectValue: isPath\)/);
    assert.deepEqual(seen, ['/s/acme']);
    const w2 = world(V1);
    const heal2 = w2.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false });
    const boom = heal2.locator('seller', '#seller-link', { role: 'link', expectValue: () => { throw new Error('boom'); } });
    assert.match(String(await thrown(boom.getAttribute('href'))), /which was refused by the function \(expectValue: a function\)/);
    const w3 = world(V1);
    const heal3 = w3.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false });
    const sku = heal3.locator('seller', '#seller-link', { role: 'link', expectValue: /^\/s\/\w+$/g });
    assert.equal(await sku.getAttribute('href'), '/s/acme');
    assert.equal(await sku.getAttribute('href'), '/s/acme');   // a g flag's lastIndex never leaks into the next read
  });

  test('the other reads and the actions are not checked', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false });
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    w.page.goto(V2);
    const price = heal.locator('price', '#price', { role: 'span', expectValue: 'money' });
    assert.equal(await price.isVisible(), true);
    assert.equal(await price.isHidden(), false);
    await price.hover();
    assert.deepEqual(heal.pending(), { price: RATING });
    assert.deepEqual(events(w.dir, 'heal.canary'), []);
  });
});

describe('open({ expectValues })', () => {
  test("reaches heal.page's reads, whose key is the selector", async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false, expectValues: { '#price': 'money' } });
    assert.equal(await heal.page.innerText('#price'), '$1,299.00');
    w.page.goto(V2);
    assert.match(String(await thrown(heal.page.innerText('#price'))), /'#price' was healed to \/\/span\[@class='rating'\]/);
    assert.equal(heal.store.status('#price', RATING), 'quarantined');
  });

  test("reaches heal.page.locator()'s reads, and getAttribute() of the page", async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false, expectValues: { '#price': 'money' } });
    await heal.page.locator('#price').textContent();
    w.page.goto(V2);
    assert.match(String(await thrown(heal.page.locator('#price').textContent())), /its textContent\(\) there is '4.5'/);

    const w2 = world(V1);
    const heal2 = w2.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false,
      expectValues: { '#seller-link': 'url' } });
    await heal2.locator('#seller-link', '#seller-link', { role: 'link' }).resolve();
    heal2.coordinator.pending.clear();
    assert.equal(await heal2.page.getAttribute('#seller-link', 'href'), '/s/acme');
    assert.match(String(await thrown(heal2.page.getAttribute('#seller-link', 'title'))),
      /its getAttribute\('title'\) there is null/);
  });

  test('heal.page.inputValue() goes through heal.locator()', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([]), fingerprint: false });
    assert.equal(await heal.page.inputValue('#qty'), '2');
    assert.equal(heal.store.candidates('#qty')[0].xpath, '#qty');
  });

  test("covers heal.locator()'s keys, and an expectValue passed to heal.locator() wins over it", async () => {
    // 4.5 is a number: the check of open() passes it, and confirms the heal.
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false, expectValues: { price: 'number' } });
    await heal.locator('price', '#price', { role: 'span' }).resolve();
    w.page.goto(V2);
    assert.equal(await heal.locator('price', '#price', { role: 'span' }).innerText(), '4.5');
    assert.equal(heal.heals[0].confirmed, true);
    // heal.locator()'s money wins over it, and refuses the same value.
    const w2 = world(V1);
    const heal2 = w2.open({ provider: new FakeModel([RATING]), fingerprint: false, expectValues: { price: 'number' } });
    await heal2.locator('price', '#price', { role: 'span' }).resolve();
    w2.page.goto(V2);
    assert.match(String(await thrown(heal2.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText())),
      /which is not money \(expectValue: 'money'\)/);
    // Neither is saved.
    assert.ok(!fs.readFileSync(w2.storePath, 'utf-8').includes('money'));
    assert.ok(!fs.readFileSync(w2.storePath, 'utf-8').includes('number'));
  });

  test('an argument that can never work throws at once, before the page is touched; a skipped step is skipped', async () => {
    const w = world(V1);
    const heal = w.open({ provider: new FakeModel([]), fingerprint: false });
    assert.throws(() => heal.locator('price', '#price', { expectValue: 'cash' as never }),
      (e: unknown) => e instanceof TypeError && /expectValue for 'price' takes 'money'/.test((e as Error).message));
    assert.throws(() => heal.locator('price', '#price', { expectValue: 5 as never }), /got number\./);
    assert.throws(() => w.open({ expectValues: 'money' as never }), /^TypeError: mendwright: expectValues takes an object/);
    assert.throws(() => w.open({ expectValues: { '#price': 'cash' as never } }), /expectValue for '#price' takes/);
    assert.deepEqual(w.page.calls, []);
    const promo = heal.locator('promo', '#promo', { optional: true, expectValue: 'money' });
    await promo.resolve();
    assert.equal(promo.present, false);
  });
});

describe('the journal', () => {
  test("a refused read writes the heal.canary and heal.quarantine lines the Python package's _check_value writes",
    { skip: needsPython() }, async () => {
      const dir = tempDir();
      const file = path.join(dir, 'ts.jsonl');
      const journal = new Journal(file, { clock: () => 'T', run: null });
      // In this process: pending, refused by innerText(); then passed by a read that confirms.
      let w = world(V1);
      let heal = w.open({ provider: new FakeModel([RATING]), fingerprint: false, journal });
      await heal.locator('price', '#price', { role: 'span' }).resolve();
      w.page.goto(V2);
      assert.ok(await thrown(heal.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText()));
      w = world(V1);
      heal = w.open({ provider: new FakeModel([AMOUNT]), fingerprint: false, journal });
      await heal.locator('price', '#price', { role: 'span' }).resolve();
      w.page.goto(V2);
      await heal.locator('price', '#price', { role: 'span', expectValue: 'money' }).innerText();
      // An earlier run's heal, refused by getAttribute('title').
      w = world(V1);
      heal = w.open({ provider: new FakeModel(["//a[@class='seller']"]), fingerprint: false, journal });
      await heal.locator('seller', '#seller-link', { role: 'link' }).resolve();
      heal.coordinator.pending.clear();
      assert.ok(await thrown(heal.locator('seller', '#seller-link', { role: 'link', expectValue: 'url' }).getAttribute('title')));
      const ts = fs.readFileSync(file, 'utf-8').split('\n').filter(Boolean).map((l) => JSON.parse(l))
        .filter((e) => e.event === 'heal.canary' || e.event === 'heal.quarantine');

      // The same reads through the Python package's own Healer._check_value and HealCoordinator.
      const py = runPython<string[]>(`
import json, sys
from types import SimpleNamespace
from mendwright.coordinator import HealCoordinator
from mendwright.facade import Healer
from mendwright.journal import Journal
from mendwright.values import expectation
d = json.load(sys.stdin)
journal = Journal(d['file'], clock=lambda: 'T', run=None)
class Store:
    def quarantine(self, key, xpath):
        pass
class Provider:
    def report_outcome(self, key, xpath, success):
        pass
def healer():
    coordinator = HealCoordinator(Store(), provider=Provider(), html_provider=lambda: '',
                                  inventory_provider=lambda: [], guard=lambda: True, journal=journal,
                                  log=lambda message: None)
    return SimpleNamespace(coordinator=coordinator, store=coordinator.store)
for key, xpath, verb, args, value, check, pending in d['reads']:
    h = healer()
    if pending:
        h.coordinator.pending[key] = xpath
    try:
        Healer._check_value(h, key, xpath, verb, tuple(args), value, expectation(check))
    except Exception:
        pass
print(json.dumps(open(d['file'], encoding='utf-8').read().splitlines()))`, {
        file: path.join(dir, 'py.jsonl'),
        reads: [['price', RATING, 'inner_text', [], '4.5', 'money', true],
          ['price', AMOUNT, 'inner_text', [], '$1,299.00', 'money', true],
          ['seller', "//a[@class='seller']", 'get_attribute', ['title'], null, 'url', false]],
      });
      const idiom = (e: Record<string, unknown>) => (typeof e.reason === 'string'
        ? { ...e, reason: e.reason.replace('inner_text(', 'innerText(').replace('get_attribute(', 'getAttribute(') } : e);
      const python = py.map((line) => idiom(JSON.parse(line)));
      assert.deepEqual(ts.map((e) => e.event), ['heal.canary', 'heal.quarantine', 'heal.canary', 'heal.quarantine']);
      assert.equal(ts.length, python.length);
      ts.forEach((line, i) => {
        assert.deepEqual(line, python[i]);
        assert.deepEqual(Object.keys(line), Object.keys(python[i]));   // the same fields, in the same order
      });
      assert.equal(ts[0].by, 'expect_value');
      assert.equal(ts[2].reason, undefined);   // a read that passed: no reason, as Python leaves None out
    });
});
