// The search for a list (lists.findList, with browser/lib.ts's listScan and listXpath in a jsdom
// page) against the Python package's lists.find(), run from this repository: on every page of
// its acceptance matrix (tests/test_lists_emptied.py) and the pages of tests/test_lists_find.py,
// the same answer, the same XPath and CSS, the same scores to the last digit, the same refusal
// with the same words, the same evidence and the same proposal id. And the rules that work on
// text alone: state words, printable text, the commands' keys, dates and ordinals.

import { strict as assert } from 'node:assert';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { HealCoordinator } from '../../src/coordinator';
import { NullJournal } from '../../src/journal';
import {
  commandKey, findList, itemRole, nonemptySearched, nonemptyUnsearched, ordinal, printable, proposalEvidence, proposalId,
  proposalMessage, shownContainer, stateWord, waitsForFixMessage, whenText, type Evidence, type ListFingerprint,
  type ListMatch,
} from '../../src/lists';
import { loadStore } from '../../src/store';
import { cleanEnv, tempDir } from '../support/env';
import { CARDS, NoModel, PRODUCTS, TILE, V1, V2, card, grid, shop } from '../support/listpages';
import { needsPython, runPython } from '../support/python';

beforeEach(cleanEnv);

const PY_HEAD = 'import json, sys\n';
const AT = '2026-11-02T10:14:03+00:00';

/** The page library in a jsdom window, as the driver runs it: what it returns, as JSON. */
function pageLib() {
  const dom = new JSDOM('<html><body></body></html>', { url: 'https://shop.test/x', runScripts: 'outside-only' });
  const lib = dom.window.eval('(' + mendwrightLib.toString() + ')()') as Record<string, (arg: unknown) => unknown>;
  return async <T>(name: string, arg?: unknown): Promise<T> => JSON.parse(JSON.stringify(lib[name](arg))) as T;
}

/** What a match says, as the Python package's ListMatch says it. */
function shown(match: ListMatch) {
  return { code: match.code, xpath: match.xpath, css: match.css, count: match.count, score: match.score,
           runner_up: match.runnerUp, held: match.held, recorded: match.recorded, label: match.label,
           alike: match.alike, reason: match.reason, fragment: match.fragment, emptied: match.emptied };
}

const PY_SHOWN = `
def shown(m):
    return {'code': m.code, 'xpath': m.xpath, 'css': m.css, 'count': m.count, 'score': m.score,
            'runner_up': m.runner_up, 'held': m.held, 'recorded': m.recorded, 'label': m.label, 'alike': m.alike,
            'reason': m.reason, 'fragment': m.fragment, 'emptied': m.emptied}
`;

interface PyCase {
  id: string;
  key: string;
  lfp: ListFingerprint;
  after: string;
  role: string;
  match: ReturnType<typeof shown>;
  state: [string, string] | null;
  evidence: Record<string, unknown> | null;
}

describe('the search for a list: parity with the Python package', { skip: needsPython() }, () => {
  test('on every page of the acceptance matrix: the same answer, scores, words, evidence and id', async () => {
    const cases = runPython<PyCase[]>(PY_HEAD + PY_SHOWN + `
sys.path.insert(0, 'tests')
import lxml.html
from lxml.cssselect import CSSSelector
from mendwright import lists
from test_lists_emptied import REFUSE, REDESIGN
out = []
for param in list(REFUSE) + list(REDESIGN):
    v = param.values
    lfp = lists.of_elements(CSSSelector(v[2])(lxml.html.fromstring(v[0])), place=True)
    role = lists.item_role(lfp)
    m = lists.find(lfp, v[1], role)
    out.append({'id': param.id, 'key': v[2], 'lfp': lfp, 'after': v[1], 'role': role, 'match': shown(m),
                'state': lists.state_word(m.xpath, lfp) if m.xpath else None,
                'evidence': lists.proposal_evidence('cart', lfp, m, m.xpath, m.css, [v[2]], '/x', '${AT}')
                if m.xpath else None})
print(json.dumps(out))`);
    assert.equal(cases.length, 86);
    const lib = pageLib();
    const codes: Record<string, number> = {};
    for (const c of cases) {
      assert.equal(itemRole(c.lfp), c.role, c.id);
      const match = await findList(c.lfp, c.after, c.role, lib);
      assert.deepEqual(shown(match), c.match, c.id);
      assert.deepEqual(match.xpath ? stateWord(match.xpath, c.lfp) : null, c.state, c.id);
      if (match.xpath) {
        assert.deepEqual(proposalEvidence('cart', c.lfp, match, match.xpath, match.css, [c.key], '/x', AT), c.evidence,
          c.id);
      }
      codes[match.code || 'found'] = (codes[match.code || 'found'] ?? 0) + 1;
    }
    // Both kinds of outcome are in the table: groups found and groups refused for each reason.
    assert.ok(codes.found >= 20 && codes.L11 >= 40, JSON.stringify(codes));
  });

  test("on the pages of the Python package's tests of the search", async () => {
    const NEW_PRODUCTS: [string, string][] = [['Lamp', '$40.00'], ['Chair', '$90.00'], ['Desk', '$250.00'],
      ['Sofa', '$900.00']];
    const skeleton = "<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
      + "<button type='button' disabled></button></div>";
    const results = (cls: string, names: string[], attrs = '') => "<html><body><main id='search'><h1>Search</h1>"
      + "<div id='results'>" + names.map((n) => "<div class='" + cls + "'" + attrs + "><img src='/i/x.png' alt='" + n
      + "'><h3>" + n + "</h3><span class='price'>$9.00</span></div>").join('') + '</div></main></body></html>';
    const MUGS = ['Blue mug', 'Red mug', 'Green mug', 'Mug set'];
    const POPULAR = ['Tea pot', 'Kettle', 'Coffee grinder', 'French press'];
    const many: [string, string][] = Array.from({ length: 450 }, (_, i) => ['Mug ' + i, '$' + i + '.00']);
    const form = (errors: string[] | null, cls = 'error') => {
      const hints = ['We never share your email', '8 characters or more'];
      const fields = ['email', 'password'].map((name, i) => "<div class='field'><label for='" + name + "'>"
        + name[0].toUpperCase() + name.slice(1) + "</label><input id='" + name + "' name='" + name + "'><span class='hint'>"
        + hints[i] + '</span>' + (errors ? "<span class='" + cls + "'>" + errors[i] + '</span>' : '') + '</div>').join('');
      return "<html><body><form id='signup'>" + fields + '<button>Sign up</button></form></body></html>';
    };
    const ERRORS = ['Email is required', 'Password is required'];
    const recent = "<section id='recent'><h2>Recently viewed</h2>" + grid('product-tile', PRODUCTS.slice(0, 3)) + '</section>';
    const twoGrids = shop(grid('product-tile'), "<div id='m' class='grid'>" + grid('product-tile') + '</div>',
      "id='d' class='grid'");
    const hidden = "//div[@id='m']/div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]";
    const menu = "<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' href='/b'>B</a></li>"
      + "<li><a class='m' href='/c'>C</a></li></ul></body></html>";
    const wrapped = PRODUCTS.map(([n, p]) => "<div class='col'>" + card('product-tile', n, p) + '</div>').join('');
    // [name, page the list was on, its XPath, page now, visible: undefined, 'all', 'none' or an XPath that is hidden]
    const table: [string, string, string, string, string?][] = [
      ['class changed', V1, CARDS, V2],
      ['new products', V1, CARDS, shop(grid('product-tile', NEW_PRODUCTS))],
      ['emptied errors', form(ERRORS), "//span[@class='error']", form(null)],
      ['renamed errors', form(ERRORS), "//span[@class='error']", form(ERRORS, 'field-error')],
      ['no results', results('result-card', MUGS), "//div[@class='result-card']", results('popular-card', POPULAR)],
      ['one result', results('result-card', MUGS), "//div[@class='result-card']",
        results('popular-card', ['Blue mug'].concat(POPULAR))],
      ['shared test id', results('result-card', MUGS, " data-testid='card'"), "//div[@class='result-card']",
        results('popular-card', POPULAR, " data-testid='card'")],
      ['shared test id, same items', results('result-card', MUGS, " data-testid='card'"), "//div[@class='result-card']",
        results('popular-card', MUGS, " data-testid='card'")],
      ['450 items', shop(grid('product-card', many)), CARDS, shop(grid('product-tile', many))],
      ['placeholders', V1, CARDS, shop(skeleton.repeat(4))],
      ['busy', V1, CARDS, shop(grid('product-tile'), '', "id='grid' class='grid' aria-busy='true'")],
      ['placeholders after', V1, CARDS, shop(grid('product-tile') + skeleton.repeat(4))],
      ['look-alike outside', V1, CARDS, shop(grid('product-tile'), recent)],
      ['look-alike grid', V1, CARDS, shop(grid('product-tile'), "<div id='recent' class='grid'>" + grid('product-tile')
        + '</div>')],
      ['one element', V1, CARDS, shop(card('product-tile', ...PRODUCTS[0]) + "<div class='banner'><p>Sale!</p></div>")],
      ['alone', V1, CARDS, "<html><body><main id='content'><section>" + card('product-tile', ...PRODUCTS[0])
        + '</section></main></body></html>'],
      ['two alike', shop(grid('product-card'), '', "class='grid'"), CARDS, twoGrids],
      ['two alike, one hidden', shop(grid('product-card'), '', "class='grid'"), CARDS, twoGrids, hidden],
      ['two alike, none visible', shop(grid('product-card'), '', "class='grid'"), CARDS, twoGrids, 'none'],
      ['two alike, unknown', shop(grid('product-card'), '', "class='grid'"), CARDS, twoGrids, 'unknown'],
      ['wrapped', V1, CARDS, shop(wrapped)],
      ['links', menu, "//a[@class='m']", menu.replace(/class='m'/g, "class='menu-link'")],
      ['renamed heading and container', V1, CARDS, V2.replace('<h1>Shop</h1>', '<h1>Catalog</h1>')
        .replace("id='grid' class='grid'", "id='grid' class='catalog'")],
      ['count in the heading', V1.replace('<h1>Shop</h1>', '<h1>Shop (4)</h1>'), CARDS,
        V2.replace('<h1>Shop</h1>', '<h1>SHOP (5)</h1>')],
      ['no page', V1, CARDS, ''],
    ];
    const py = runPython<{ lfp: ListFingerprint; role: string; match: ReturnType<typeof shown>;
                           evidence: Record<string, unknown> | null }[]>(PY_HEAD + PY_SHOWN + `
import lxml.html
from mendwright import lists
out = []
for name, before, xpath, after, visible in json.load(sys.stdin):
    lfp = lists.of_elements(lxml.html.fromstring(before).xpath(xpath), place=True)
    role = lists.item_role(lfp)
    see = None
    if visible == 'none':
        see = lambda x: False
    elif visible == 'unknown':
        see = lambda x: None
    elif visible:
        see = lambda x, hidden=visible: x != hidden
    m = lists.find(lfp, after, role, visible=see)
    out.append({'lfp': lfp, 'role': role, 'match': shown(m),
                'evidence': lists.proposal_evidence('cards', lfp, m, m.xpath, m.css, [xpath], '/shop', '${AT}')
                if m.xpath else None})
print(json.dumps(out))`, table.map((row) => [...row.slice(0, 4), row[4] ?? null]));
    const lib = pageLib();
    for (const [i, [name, , xpath, after, visible]] of table.entries()) {
      const { lfp, role } = py[i];
      const see = visible === 'none' ? async () => false : visible === 'unknown' ? async () => null
        : visible ? async (x: string) => x !== visible : null;
      const match = await findList(lfp, after, role, lib, { visible: see });
      assert.deepEqual(shown(match), py[i].match, name);
      if (match.xpath) {
        assert.deepEqual(proposalEvidence('cards', lfp, match, match.xpath, match.css, [xpath], '/shop', AT), py[i].evidence,
          name);
      }
    }
    // What the Python package's tests say of a few of them, here too.
    assert.equal(py[0].match.xpath, TILE);
    assert.equal(py[0].match.css, 'div.product-tile');
    assert.deepEqual(py.map((p) => p.match.code), ['', 'L18c', 'L18c', '', 'L18c', 'L18a', 'L18b', '', '', 'L12', 'L12',
      '', '', '', 'L17', 'L13', 'L16', '', 'L14', 'L16', '', '', '', '', 'L10']);
  });

  test("the texts of a lookup with nonempty: true are the Python package's, in this package's spellings", () => {
    const file = path.join(tempDir(), 'overrides.json');
    const other = "//li[@data-list-heal='0']";
    const py = runPython<{ lfp: ListFingerprint; match: { xpath: string; css: string }; evidence: Evidence;
                           unsearched: string; searched: string[]; proposal: string; differs: string; accepted: string;
                           waits: string; went: string; lines: string[] }>(PY_HEAD + `
import lxml.html
from mendwright import lists
from mendwright.coordinator import HealCoordinator, nonempty_searched, nonempty_unsearched
from mendwright.store import load_store
d = json.load(sys.stdin)
store = load_store(None, d['file'])
store.set_code_candidates('cart', ['.cart-item'])
lines = []
coord = HealCoordinator(store, provider=object(), html_provider=lambda: '', inventory_provider=list,
                        guard=lambda: True, log=lines.append)
lfp = lists.of_elements(lxml.html.fromstring(d['before']).xpath(d['xpath']), place=True)
m = lists.find(lfp, d['after'], 'div')
evidence = lists.proposal_evidence('cart', lfp, m, m.xpath, m.css, ['.cart-item'], '/shop', '${AT}')
differs = dict(evidence, differs=['heading', 'container'], now=dict(evidence['now'], alike=[2, 3], label='',
                                                                  container=''))
out = {'lfp': lfp, 'match': {'xpath': m.xpath, 'css': m.css}, 'evidence': evidence,
       'unsearched': nonempty_unsearched('cart', 'TEXT.'),
       'searched': [nonempty_searched('cart', c, 'FRAGMENT') for c in (4, None)],
       'proposal': coord._proposal_message('cart', lfp, m.xpath, m.css, 'div', evidence),
       'differs': coord._proposal_message('cart', lfp, m.xpath, None, 'div', differs)}
store.propose_list_heal('cart', d['other'], proposal={'id': 'aaaa0000', 'at': '2026-10-01T08:00:00+00:00', 'count': 2})
store._find('cart', d['other'])['status'] = 'accepted'
store._find('cart', d['other'])['acceptedAt'] = '2026-10-30T08:00:00+00:00'
out['accepted'] = coord._proposal_message('cart', lfp, m.xpath, m.css, 'div', evidence)
out['waits'] = coord.waits_for_fix('cart', {'listFingerprint': lfp}, {'xpath': d['other'],
                                   'acceptedAt': '2026-10-30T08:00:00+00:00'}, 3)
out['went'] = coord.came_and_went('cart', {'listFingerprint': lfp}, m.xpath, '.cart-item')
coord.came('cart', '.cart-item')
out['lines'] = lines
print(json.dumps(out))`, { file, before: V1, after: V2, xpath: CARDS, other });
    const python = (text: string) => text.split('nonempty=True').join('nonempty: true');
    const flag = ' --store "' + file + '"';
    const { lfp, evidence, match } = py;
    assert.equal(nonemptyUnsearched('cart', 'TEXT.'), python(py.unsearched));
    assert.deepEqual([nonemptySearched('cart', 4, 'FRAGMENT'), nonemptySearched('cart', null, 'FRAGMENT')],
      py.searched.map(python));
    assert.equal(proposalMessage('cart', lfp, match.xpath, match.css, 'div', evidence, flag, null), python(py.proposal));
    const differs = { ...evidence, differs: ['heading', 'container'], now: { ...evidence.now, alike: [2, 3], label: '',
                                                                           container: '' } };
    assert.equal(proposalMessage('cart', lfp, match.xpath, null, 'div', differs, flag, null), python(py.differs));
    assert.equal(proposalMessage('cart', lfp, match.xpath, match.css, 'div', evidence, flag,
      { xpath: other, acceptedAt: '2026-10-30T08:00:00+00:00' }), python(py.accepted));
    assert.equal(waitsForFixMessage('cart', lfp.count, { xpath: other, acceptedAt: '2026-10-30T08:00:00+00:00' }, 3, flag),
      python(py.waits));
    const lines: string[] = [];
    const coordinator = new HealCoordinator(loadStore(path.join(tempDir(), 'o.json')), {
      provider: new NoModel(), driver: null as never, journal: new NullJournal(),
      log: (_level, message) => lines.push(message) });
    const meta = { listFingerprint: lfp } as never;
    assert.equal(coordinator.cameAndWent('cart', meta, match.xpath, '.cart-item'), python(py.went));
    coordinator.came('cart', '.cart-item');
    // The decline before them is logged as any heal that did not apply is, which Python logs too.
    assert.deepEqual(lines, py.lines.slice(-2).map(python));
    assert.equal(py.lines[py.lines.length - 3], "'cart' waits for mendwright fix: " + py.waits);
  });

  test('the state words, printable text, container names, command keys, dates and ordinals', () => {
    const lfp = { v: 1, count: 2, depth: 1, items: [{ v: 1, tag: 'li', classes: ['done', 'todo-item'], shape: {} }],
                  container: { tag: 'ul', id: 'tasks-list', classes: ['list'] }, label: 'Completed tasks' } as ListFingerprint;
    const xpaths = ["//div[contains(concat(' ', normalize-space(@class), ' '), ' saved-item ')]", '//li[@id="done-list"]/li',
      "//div[text()='Reader']", "//li[@class='archived']", "//li[@class='completed']", "//li[@class=' Saved  ']",
      '//li', null, "//li[@class=\"read-more\"][@id='x']"];
    const texts = ['a\x00b\x7fc\x85d', 'Shop\x1b[31m', 'plain', '', 'x\x9bJ'];
    const nodes = [{ tag: 'ul', id: 'a"b', classes: ['x', 'y'] }, null, { tag: 'div' }, { tag: 'section', classes: [] },
      { tag: 'x', id: '', classes: ['q"r'] }];
    const keys = ['cart', 'my cart', 'cart"s', "it's \"x\"", 'cart\x1b[31m\x9b', 'a$b', 'a`b', 'a\\b', '', 'é'];
    const stamps = ['2026-10-10T09:30:00+00:00', '2026-10-10T09:30:00Z', '2026-10-10T09:30:00', '', null, 'TTT'];
    const numbers = [1, 2, 3, 4, 10, 11, 12, 13, 21, 22, 23, 101, 111, 112];
    const py = runPython<{ states: unknown[]; printable: string[]; nodes: string[]; keys: [string, string][];
                           stamps: string[]; numbers: string[]; ids: string[] }>(PY_HEAD + `
from mendwright import lists
from mendwright.cli import _when, command_key
d = json.load(sys.stdin)
print(json.dumps({'states': [lists.state_word(x, d['lfp']) for x in d['xpaths']],
                  'printable': [lists._printable(t) for t in d['texts']],
                  'nodes': [lists.shown_container(n) for n in d['nodes']],
                  'keys': [list(command_key(k)) for k in d['keys']],
                  'stamps': [_when(s) for s in d['stamps']], 'numbers': [lists._ordinal(n) for n in d['numbers']],
                  'ids': [lists.proposal_id(k, '//li', c, e) for k, c, e in d['ids']]}))`, {
      lfp, xpaths, texts, nodes, keys, stamps, numbers, ids: [
        ['cart', 'li.row', { page: '/cart', count: 3, found: 2, recorded: 3, was: { selectors: ['.x'] }, now: {}, differs: [] }],
        ['café \u{1F600}', null, { page: '', count: 2, was: { label: 'é\n"q"\\' }, differs: ['heading'] }],
        ['k', '', {}]],
    });
    assert.deepEqual(xpaths.map((x) => stateWord(x, lfp)), py.states);
    assert.deepEqual(texts.map(printable), py.printable);
    assert.deepEqual(nodes.map(shownContainer), py.nodes);
    assert.deepEqual(keys.map(commandKey), py.keys);
    assert.deepEqual(stamps.map(whenText), py.stamps);
    assert.deepEqual(numbers.map(ordinal), py.numbers);
    assert.deepEqual([
      proposalId('cart', '//li', 'li.row', { page: '/cart', count: 3, found: 2, recorded: 3,
                                            was: { selectors: ['.x'] } as never, now: {} as never, differs: [] }),
      proposalId('café \u{1F600}', '//li', null, { page: '', count: 2, was: { label: 'é\n"q"\\' } as never,
                                                       differs: ['heading'] }),
      proposalId('k', '//li', '', {})], py.ids);
  });
});
