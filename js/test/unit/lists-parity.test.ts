// Lists against the Python package's mendwright/lists.py, run from this repository: the same
// hashes, the same list fingerprint for the same elements (read by browser/lib.ts's listRaw in a
// jsdom page, and by lists.raw_of_elements with lxml), over every page of the Python package's
// acceptance matrix (tests/test_lists_emptied.py), and one store both packages read and write.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import {
  blake2s, labelKey, listFromRaw, listMissingMessage, nodeOf, placed, shapeOf, shortHash, sigOf, validList,
} from '../../src/lists';
import { loadStore } from '../../src/store';
import { cleanEnv, tempDir, world } from '../support/env';
import { CARDS, V1 } from '../support/listpages';
import { needsPython, runPython } from '../support/python';

beforeEach(cleanEnv);

const skip = needsPython();
const PY_HEAD = 'import json, sys\n';

function libIn(html: string) {
  const dom = new JSDOM(html, { url: 'https://shop.test/x', runScripts: 'outside-only' });
  const lib = dom.window.eval('(' + mendwrightLib.toString() + ')()') as ReturnType<typeof mendwrightLib>;
  return { dom, lib, doc: dom.window.document };
}

/** What listRaw() reads in a page of html for the elements selector matches (CSS, or an XPath),
 * as the driver gets it: JSON. */
function readList(html: string, selector: string, place: boolean): unknown {
  const { lib, doc } = libIn(html);
  let els: Element[];
  if (selector.startsWith('/')) {
    const result = doc.evaluate(selector, doc, null, 7, null);
    els = Array.from({ length: result.snapshotLength }, (_, i) => result.snapshotItem(i) as Element);
  } else {
    els = Array.from(doc.querySelectorAll(selector));
  }
  return JSON.parse(JSON.stringify(lib.listRaw(els, place)));
}

interface MatrixCase {
  id: string;
  before: string;
  after: string;
  key: string;
  wanted: string | null;
  other: string | null;
  plain: unknown;
  placed: unknown;
}

let matrix: MatrixCase[] | null = null;

/** The pages of the Python package's acceptance matrix (53 emptied lists and 33 redesigns),
 * with the list fingerprint it records for each one's list on the page before. */
function pythonMatrix(): MatrixCase[] {
  if (matrix === null) {
    matrix = runPython<MatrixCase[]>(PY_HEAD + `
sys.path.insert(0, 'tests')
import lxml.html
from lxml.cssselect import CSSSelector
from mendwright import lists
from test_lists_emptied import REFUSE, REDESIGN
out = []
for param in list(REFUSE) + list(REDESIGN):
    v = param.values
    els = CSSSelector(v[2])(lxml.html.fromstring(v[0]))
    out.append({'id': param.id, 'before': v[0], 'after': v[1], 'key': v[2],
                'wanted': v[3] if len(v) == 5 else None, 'other': v[-1],
                'plain': lists.of_elements(els), 'placed': lists.of_elements(els, place=True)})
print(json.dumps(out))`);
  }
  return matrix;
}

describe('lists: parity with the Python package', { skip }, () => {
  test('the short hash is hashlib.blake2s(digest_size=4), and the full one too', () => {
    const texts = ['', 'a', 'abc', 'x'.repeat(63), 'x'.repeat(64), 'x'.repeat(65), 'y'.repeat(128), 'y'.repeat(129),
      'ünïcödé', '\u{1F600} cart', 'div#grid[].grid<main#content[]', 'z'.repeat(1000)];
    const py = runPython<{ short: string[]; full: string[] }>(PY_HEAD + `
import hashlib
texts = json.load(sys.stdin)
print(json.dumps({'short': [hashlib.blake2s(t.encode('utf-8'), digest_size=4).hexdigest() for t in texts],
                  'full': [hashlib.blake2s(t.encode('utf-8')).hexdigest() for t in texts]}))`, texts);
    assert.deepEqual(texts.map(shortHash), py.short);
    assert.deepEqual(texts.map((t) => Buffer.from(blake2s(new TextEncoder().encode(t), 32)).toString('hex')), py.full);
  });

  test('labels, element hashes, shapes and containers are normalized as the Python package does', () => {
    const labels = ['Cart (3)', 'Shopping cart', 'SHOP (5)', '  a \t b\n', 'Ünïcode ²³ 12 x', 'İstanbul', '', '123',
      'Saved for later (0 items)', 'a b c'];
    const raws = [['div', 'grid', '', 'grid', 'main', 'content', '', ''],
      ['section', ':r1:', 'cart', 'cart cart--full css-1x2y3', 'body', '', '', 'sc-a1b2c3 layout'],
      ['UL', '  menu ', '', 'b a  c', 'NAV', 'mui-generated-8842', 'nav', 'is-open top'],
      ['li', '', '', '', '', '', '', '']];
    const shapes = [{ '>div': 2, div: 2, h3: 1, ' SPAN ': 3, b: 0, i: -1, x: 1.7 }, {}, { a: 1, b: 1, '>a': 1 }];
    const nodes = [{ tag: ' DIV ', attrs: { id: ' grid ', class: 'grid css-1x2y3 active', role: 'List',
      'aria-label': 'Cart', 'data-test': '', 'data-qa': 'q' } }, { tag: '' }, { tag: 'ul', attrs: 'x' }, null];
    const py = runPython<{ labels: string[]; sigs: string[]; shapes: unknown[]; nodes: unknown[] }>(PY_HEAD + `
from mendwright import lists
d = json.load(sys.stdin)
print(json.dumps({'labels': [lists._label_key(t) for t in d['labels']], 'sigs': [lists._sig(r) for r in d['raws']],
                  'shapes': [lists._shape(s) for s in d['shapes']], 'nodes': [lists._node(n) for n in d['nodes']]}))`,
    { labels, raws, shapes, nodes });
    assert.deepEqual(labels.map(labelKey), py.labels);
    assert.deepEqual(raws.map(sigOf), py.sigs);
    assert.deepEqual(shapes.map(shapeOf), py.shapes);
    assert.deepEqual(nodes.map(nodeOf), py.nodes);
  });

  test('the list fingerprint of every list of the acceptance matrix is the one the Python package records', () => {
    const cases = pythonMatrix();
    assert.equal(cases.length, 86);
    for (const c of cases) {
      assert.deepEqual(listFromRaw(readList(c.before, c.key, false)), c.plain, c.id + ' (plain)');
      assert.deepEqual(listFromRaw(readList(c.before, c.key, true)), c.placed, c.id + ' (placed)');
      assert.ok(placed(c.placed) && !placed(c.plain) && validList(c.plain), c.id);
    }
  });

  test('a list read with its place keeps its label and where it was, and one without them is a plain one', () => {
    const plain = listFromRaw(readList(V1, CARDS, false));
    const withPlace = listFromRaw(readList(V1, CARDS, true));
    const py = runPython<{ plain: unknown; placed: unknown }>(PY_HEAD + `
import lxml.html
from mendwright import lists
d = json.load(sys.stdin)
els = lxml.html.fromstring(d['html']).xpath(d['xpath'])
print(json.dumps({'plain': lists.of_elements(els), 'placed': lists.of_elements(els, place=True)}))`,
    { html: V1, xpath: CARDS });
    assert.deepEqual(plain, py.plain);
    assert.deepEqual(withPlace, py.placed);
    assert.equal(withPlace?.label, 'Shop');
    assert.deepEqual(Object.keys(plain as object), ['v', 'count', 'depth', 'items', 'container']);
    assert.equal(listFromRaw({ count: 1, items: [] }), null);
    assert.equal(validList({ v: 1, count: 4, items: [] }), false);
  });

  test("a list inside a shadow root says so, and only then", () => {
    const html = '<html><body><section><h2>Saved</h2><ul class="saved"><li class="row"><a href="/p/A">A</a></li>'
      + '<li class="row"><a href="/p/B">B</a></li></ul></section><my-app id="root"></my-app></body></html>';
    const { lib, doc, dom } = libIn(html);
    const root = (doc.getElementById('root') as Element).attachShadow({ mode: 'open' });
    root.innerHTML = '<ul class="cart"><li class="card">A</li><li class="card">B</li><li class="card">C</li></ul>';
    const inShadow = listFromRaw(JSON.parse(JSON.stringify(lib.listRaw(Array.from(root.querySelectorAll('.card')), true))));
    assert.equal(inShadow?.shadow, true);
    assert.equal(inShadow?.count, 3);
    const onPage = listFromRaw(JSON.parse(JSON.stringify(lib.listRaw(Array.from(doc.querySelectorAll('.row')), true))));
    assert.equal('shadow' in (onPage as object), false);
    dom.window.close();
  });

  test('the refusal of a list that matches nothing is the Python package\'s', () => {
    const counts = [4, 2, 1, null, 'x', true];
    const py = runPython<string[]>(PY_HEAD + `
from mendwright.coordinator import _list_missing_message
print(json.dumps([_list_missing_message('cart', c) for c in json.load(sys.stdin)]))`, counts);
    assert.deepEqual(counts.map((c) => listMissingMessage('cart', c)), py);
  });

  describe('one store, both packages', () => {
    test('the Python package reads the list fingerprint this package recorded, and keeps it', async () => {
      const w = world(V1);
      await w.open().locatorAll('cards', CARDS);
      const ours = loadStore(w.storePath).metaOf('cards').listFingerprint;
      const py = runPython<{ lfp: unknown; valid: boolean; placed: boolean; role: string }>(PY_HEAD + `
from mendwright import lists
from mendwright.store import load_store
d = json.load(sys.stdin)
store = load_store(None, d['file'])
lfp = store.meta('cards')['listFingerprint']
store.set_code_candidates('python_after', ['#again'])
print(json.dumps({'lfp': lfp, 'valid': lists.valid(lfp), 'placed': lists.placed(lfp),
                  'role': store.meta('cards')['expectedRole']}))`, { file: w.storePath });
      assert.deepEqual(py.lfp, ours);
      assert.deepEqual([py.valid, py.placed, py.role], [true, false, 'div']);
      assert.deepEqual(loadStore(w.storePath).metaOf('cards').listFingerprint, ours);
    });

    test('this package reads the list fingerprint the Python package recorded, and keeps it through its writes', () => {
      const file = path.join(tempDir(), 'overrides.json');
      const lfp = runPython(PY_HEAD + `
import lxml.html
from mendwright import lists
from mendwright.store import load_store
d = json.load(sys.stdin)
store = load_store(None, d['file'])
store.set_code_candidates('cards', [d['xpath']])
lfp = lists.of_elements(lxml.html.fromstring(d['html']).xpath(d['xpath']), place=True)
store.set_meta('cards', role='div', list_fingerprint=lfp)
print(json.dumps(lfp))`, { file, html: V1, xpath: CARDS });
      const store = loadStore(file);
      assert.deepEqual(store.metaOf('cards').listFingerprint, lfp);
      store.setCodeCandidates('other', ['#x']);
      store.setMeta('cards', { role: 'div' });
      const raw = JSON.parse(fs.readFileSync(file, 'utf-8'));
      assert.deepEqual(raw.selectors.cards.listFingerprint, lfp);
      assert.throws(() => store.setMeta('cards', { listFingerprint: { v: 1 } as never }), TypeError);
    });
  });
});
