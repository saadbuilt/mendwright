// The parts of heal.step() and heal.runSteps() that need no page (steps.ts), compared with the
// Python package's mendwright/steps.py on tables of inputs: the url patterns, the landmarks'
// targets, the messages, the argument checks and the journal lines. Where this package writes
// TypeScript in place of Python (optional: true for optional=True), the comparison rewrites
// Python's text first, one rewrite at a time, so each difference is in plain sight. And the
// steps the store keeps per key (setLastStep), in a store both packages read and write.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import { severalMessage } from '../../src/flow';
import { Journal } from '../../src/journal';
import { pyFixed, pyFormatG, pyRound } from '../../src/py';
import { normalize } from '../../src/selectors';
import {
  StepRun, againMessage, capMessage, changedMessage, droppedMessage, landmarkTargets, makeStep, notShownMessage,
  orderMessage, plainMessage, ranMessage, registerStep, runArgs, scopeMessage, unknownMessage, urlMatches, urlPattern,
  type Step,
} from '../../src/steps';
import { loadStore } from '../../src/store';
import { cleanEnv, tempDir } from '../support/env';
import { needsPython, runPython } from '../support/python';

beforeEach(cleanEnv);

const REVIEW = "//button[.='Place order']";
const fn = async () => undefined;

/** A Step as heal.step() builds one, for the message builders and StepRun. */
function step(name: string, when: string[], extra: Partial<Step> = {}): Step {
  return { name, fn, when, url: null, urlText: null, last: false, optional: false, ...extra };
}

/** What fn throws: [its class's name, its message]. */
function thrown(call: () => unknown): [string, string] {
  try {
    call();
  } catch (error) {
    return [(error as Error).name, (error as Error).message];
  }
  return ['ok', ''];
}

/** text with each [Python, TypeScript] spelling rewritten. */
function rewrite(text: string, pairs: [string, string][]): string {
  return pairs.reduce((out, [from, to]) => out.split(from).join(to), text);
}

// The spellings of the messages: Python's, then this package's.
const SPELLINGS: [string, string][] = [
  ['@heal.step("<name>", "<selector of something only that step shows>")',
    'heal.step("<name>", { when: "<selector of something only that step shows>" }, fn)'],
  ['update its when=:', 'update its when option:'],
  ['optional=True', 'optional: true'],
  ['last=True', 'last: true'],
  ['run_steps()', 'runSteps()'],
];

const PY_HEAD = 'import json, sys\n';

describe('url patterns', () => {
  test('a pattern is normalized as the page guard normalizes a path', () => {
    const cases: [string, string][] = [['/checkout/payment', '/checkout/payment'], ['/Checkout//Payment/', '/checkout/payment'],
      ['/orders/123/pay', '/orders/*/pay'], ['*/payment*', '*/payment*'], ['*payment', '*payment'],
      ['  /checkout/*  ', '/checkout/*']];
    for (const [pattern, normalized] of cases) assert.equal(urlPattern('payment', pattern), normalized, pattern);
  });

  test("a page matches when its path does: '*' is any characters, '/' included", () => {
    const cases: [string, string, boolean][] = [
      ['/checkout/payment', 'https://shop.test/Checkout/Payment/?step=2', true],
      ['/checkout/payment', 'https://shop.test/checkout/payment/card', false],
      ['/checkout/*', 'https://shop.test/checkout/payment/card', true],
      ['/*/payment*', 'file:///C:/site/checkout/payment.html', true],
      ['*/payment*', 'https://shop.test/payment', true],
      ['*/payment*', 'https://shop.test/checkout/payment', true],
      ['*/payment*', 'https://shop.test/repayment', false],
      ['/orders/*/pay', 'https://shop.test/orders/507f1f77bcf86cd799439011/pay', true],
      ['/*', 'about:blank', false],
      ['/*', 'https://shop.test/', false],
    ];
    for (const [pattern, url, expected] of cases) assert.equal(urlMatches(pattern, url), expected, pattern + ' ' + url);
  });

  test('the patterns, their errors and what each matches are the Python package\'s', { skip: needsPython() }, () => {
    const patterns = ['/checkout/payment', '/Checkout//Payment/', '/orders/123/pay', '*/payment*', '*payment',
      '  /checkout/*  ', '/checkout/[ab]', '/checkout/[!ab]', '/step-[a-c]', '/x[', '/[]]', '/[!]]', '/[!]', '/a?c',
      '/[z-a]', '/[!z-a]x', '/a[b-]', '/a[-b]', '/a[!-b]', '/caf\u00e9', '/\u{1F600}?', '/a*b*c', '/[^a]', '/[a^]',
      '/a[\\]-]', '*', '/*/*', '/checkout/*/pay*', '/a[[]', '/[a-c-e]', '/[--/]', '/a.b', '/(x)', '/a+b', '/a{2}',
      '/a|b', '/a$', '/a\\b', '/[a&&b]', '/[~|]', '/*a*a*a*b', '/a**b', '/[\u{1F600}-\u{1F64F}]', '/[\\-z]', '***x'];
    const bad = [5, null, true, 'checkout/review', 'https://shop.test/review', '/', '  ', '', '//', '  //  '];
    const urls = ['https://shop.test/checkout/payment', 'https://shop.test/Checkout/Payment/?step=2',
      'https://shop.test/checkout/payment/card', 'file:///C:/site/checkout/payment.html', 'https://shop.test/payment',
      'https://shop.test/repayment', 'https://shop.test/orders/507f1f77bcf86cd799439011/pay', 'about:blank',
      'https://shop.test/', 'https://shop.test/checkout/a', 'https://shop.test/checkout/b', 'https://shop.test/checkout/c',
      'https://shop.test/step-b', 'https://shop.test/step-d', 'https://shop.test/x[', 'https://shop.test/]',
      'https://shop.test/abc', 'https://shop.test/a-c', 'https://shop.test/caf\u00e9', 'https://shop.test/\u{1F600}x',
      'https://shop.test/\u{1F600}', 'https://shop.test/a-b', 'https://shop.test/a!', 'https://shop.test/a.b',
      'https://shop.test/axb', 'https://shop.test/(x)', 'https://shop.test/aab', 'https://shop.test/a|b',
      'https://shop.test/a$', 'https://shop.test/a\\b', 'https://shop.test/a[', 'https://shop.test/^',
      'https://shop.test/x/y', 'https://shop.test/checkout/77/payment', 'data:text/html,hi', 'https://shop.test/a/b/c',
      'https://shop.test/abxbc', 'https://shop.test/-', 'https://shop.test/.', 'https://shop.test/&', 'https://shop.test/~',
      'https://shop.test/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab', 'https://shop.test/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaac',
      'https://shop.test/ab', 'https://shop.test/a\u{1F610}', 'https://shop.test/z', 'https://shop.test/\\'];
    const py = runPython<{ patterns: [string, string][]; bad: [string, string][]; matches: (boolean | string)[][] }>(PY_HEAD + `
from mendwright.steps import url_matches, url_pattern
d = json.load(sys.stdin)
def pattern(value):
    try:
        return ['ok', url_pattern('payment', value)]
    except Exception as exc:
        return [type(exc).__name__, str(exc)]
def matches(p, u):
    try:
        return url_matches(p, u)
    except Exception as exc:
        return 'error: ' + type(exc).__name__
patterns = [pattern(p) for p in d['patterns']]
print(json.dumps({'patterns': patterns, 'bad': [pattern(p) for p in d['bad']],
                  'matches': [[matches(p, u) for u in d['urls']] for kind, p in patterns if kind == 'ok']}))`,
    { patterns, bad, urls });
    const ours = (value: unknown): [string, string] => {
      const [kind, message] = thrown(() => urlPattern('payment', value));
      return kind === 'ok' ? ['ok', urlPattern('payment', value)] : [kind, message];
    };
    assert.deepEqual(patterns.map(ours), py.patterns);
    // Python raises TypeError or ValueError; this package TypeError for both, as for every
    // argument that can never work.
    const tsBad = bad.map(ours);
    py.bad.forEach(([kind, message], i) => {
      assert.ok(['TypeError', 'ValueError'].includes(kind), kind);
      assert.equal(tsBad[i][0], 'TypeError');
      assert.equal(tsBad[i][1], rewrite(message, [["heal.step('payment', url=...)", "heal.step('payment', { url })"],
        ['got None.', 'got null.'], ['got True.', 'got true.'], ['Use when= for', 'Use when for']])
        .replace(/heal\.step\('payment', url=(.*?)\) (takes|names)/, "heal.step('payment', { url: $1 }) $2"), String(bad[i]));
    });
    const ok = patterns.filter((_, i) => py.patterns[i][0] === 'ok').map((p) => urlPattern('payment', p));
    assert.deepEqual(ok.map((p) => urls.map((u) => urlMatches(p, u))), py.matches);
    assert.ok(py.matches.flat().filter((m) => m === true).length > 40);
  });
});

describe('landmarks', () => {
  test("the targets of a landmark that matches several: '(xpath)[n]', else '>> nth=n', the first 5", () => {
    assert.deepEqual(landmarkTargets('#a', 0), []);
    assert.deepEqual(landmarkTargets('#a', 1), ['#a']);
    assert.deepEqual(landmarkTargets('//h1', 2), ['xpath=(//h1)[1]', 'xpath=(//h1)[2]']);
    assert.deepEqual(landmarkTargets('text=Place order', 7), [0, 1, 2, 3, 4].map((i) => 'text=Place order >> nth=' + i));
    assert.deepEqual(landmarkTargets('#a', 2), ['css=#a >> nth=0', 'css=#a >> nth=1']);
  });

  test("they are the Python package's Playwright targets", { skip: needsPython() }, () => {
    const selectors = ['#a', '//h1', 'text=Place order', "xpath=//h1[.='x']", 'css=.t', '(//a)[2]', 'role=button[name="Go"]',
      'css=//odd', '  #pad  ', "//button[normalize-space()='Place order']", 'div >> text=Go', './/li'];
    const counts = [0, 1, 2, 5, 7];
    const py = runPython<string[][][]>(PY_HEAD + `
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.selectors import normalize
from mendwright.steps import landmark_targets
d = json.load(sys.stdin)
driver = PlaywrightSyncDriver.__new__(PlaywrightSyncDriver)
print(json.dumps([[landmark_targets(driver, normalize(s), c) for c in d['counts']] for s in d['selectors']]))`,
    { selectors, counts });
    assert.deepEqual(selectors.map((s) => counts.map((c) => landmarkTargets(normalize(s), c))), py);
  });
});

describe('the messages', () => {
  test('the order WARNING names every step that ran first', () => {
    assert.equal(orderMessage(['payment'], 'shipping'), "ran 'payment' before 'shipping': the site changed the order of the steps");
    assert.equal(orderMessage(['payment', 'gift'], 'shipping'),
      "ran 'payment' and 'gift' before 'shipping': the site changed the order of the steps");
  });

  test("they are the Python package's, in TypeScript's spelling", { skip: needsPython() }, () => {
    const py = runPython<Record<string, string[]>>(PY_HEAD + `
from mendwright.flow import several_message
from mendwright.steps import (Step, again_message, cap_message, changed_message, dropped_message, not_shown_message,
                              order_message, plain_message, ran_message, scope_message, unknown_message)
fn = lambda page: None
waiting = [Step('payment', fn, ('#card-number',)), Step('review', fn, ('text=Place order',), last=True),
           Step('gift', fn, (), url='/checkout/gift', url_text='/Checkout/Gift'),
           Step('upsell', fn, ("//h1[.='Add insurance?']", '#upsell'), url='*/upsell', url_text='*/Upsell', optional=True)]
print(json.dumps({
    'ran': [ran_message('payment', '/checkout/step-a', 0.4), ran_message("it's", None, 12.25), ran_message('x', '/a', 0.05),
            ran_message('x', '/a', 0), ran_message('x', '/a', 1234.56789), ran_message('x', '/a', 0.25)],
    'order': [order_message(['payment'], 'shipping'), order_message(['payment', 'gift'], 'shipping'),
              order_message(['a', 'b', "c'd"], 'e')],
    'dropped': [dropped_message('gift', 'review'), dropped_message("it's", 'done')],
    'notShown': [not_shown_message('upsell'), not_shown_message('a "b"')],
    'again': [again_message('payment', '/checkout/payment', 10000, ['shipping', 'payment']),
              again_message('payment', None, 40, []), again_message('p', '/x', 1000000, ['a']),
              again_message('p', '/x', 1500.25, ['a']), again_message('p', '/x', 0.5, ['a'])],
    'changed': [changed_message('shipping', '/checkout/shipping', '/checkout/gift', 10000, ['shipping']),
                changed_message('s', '/a', '/b', 0.5, [])],
    'unknown': [unknown_message('/checkout/gift', waiting[:2], ['shipping'], 10000),
                unknown_message(None, waiting, [], 30, no_last=True),
                unknown_message('/x', waiting[2:], ['a', 'b'], 10000000, no_last=True),
                unknown_message('/x', waiting[:1], ['a'], 70)],
    'cap': [cap_message(3, 1), cap_message(12, 4)],
    'scope': [scope_message('#ship-name', 'shipping', '/checkout/step-b', '/checkout/step-a'),
              scope_message('k', "it's", '/b', None)],
    'several': [several_message('landmark', 'payment', "//h1[.='Payment']", 6, 5, [], visible=None, playwright=True),
                several_message('landmark', "it's", '.t', 7, 5, [], visible=None, playwright=True),
                several_message('interruption', 'promo', '.x', 3, 0, [], visible=None, playwright=True),
                several_message('interruption', 'promo', '//x', 9, 5, [], visible=None, playwright=True)],
    'plain': [plain_message('review', None), plain_message('review', 3), plain_message('review', 'done')],
}))`);
    const waiting = [step('payment', ['#card-number']), step('review', ['text=Place order'], { last: true }),
      step('gift', [], { url: '/checkout/gift', urlText: '/Checkout/Gift' }),
      step('upsell', ["//h1[.='Add insurance?']", '#upsell'], { url: '*/upsell', urlText: '*/Upsell', optional: true })];
    const ts = (texts: string[]) => texts.map((t) => rewrite(t, SPELLINGS));
    assert.deepEqual([ranMessage('payment', '/checkout/step-a', 0.4), ranMessage("it's", null, 12.25), ranMessage('x', '/a', 0.05),
      ranMessage('x', '/a', 0), ranMessage('x', '/a', 1234.56789), ranMessage('x', '/a', 0.25)], py.ran);
    assert.deepEqual([orderMessage(['payment'], 'shipping'), orderMessage(['payment', 'gift'], 'shipping'),
      orderMessage(['a', 'b', "c'd"], 'e')], py.order);
    assert.deepEqual([droppedMessage('gift', 'review'), droppedMessage("it's", 'done')], ts(py.dropped));
    assert.deepEqual([notShownMessage('upsell'), notShownMessage('a "b"')], ts(py.notShown));
    assert.deepEqual([againMessage('payment', '/checkout/payment', 10000, ['shipping', 'payment']),
      againMessage('payment', null, 40, []), againMessage('p', '/x', 1000000, ['a']), againMessage('p', '/x', 1500.25, ['a']),
      againMessage('p', '/x', 0.5, ['a'])], py.again);
    assert.deepEqual([changedMessage('shipping', '/checkout/shipping', '/checkout/gift', 10000, ['shipping']),
      changedMessage('s', '/a', '/b', 0.5, [])], ts(py.changed));
    assert.deepEqual([unknownMessage('/checkout/gift', waiting.slice(0, 2), ['shipping'], 10000),
      unknownMessage(null, waiting, [], 30, true), unknownMessage('/x', waiting.slice(2), ['a', 'b'], 10000000, true),
      unknownMessage('/x', waiting.slice(0, 1), ['a'], 70)], ts(py.unknown));
    assert.deepEqual([capMessage(3, 1), capMessage(12, 4)], ts(py.cap));
    assert.deepEqual([scopeMessage('#ship-name', 'shipping', '/checkout/step-b', '/checkout/step-a'),
      scopeMessage('k', "it's", '/b', null)], py.scope);
    assert.deepEqual([severalMessage('payment', "//h1[.='Payment']", 6, 5, 'landmark'), severalMessage("it's", '.t', 7, 5, 'landmark'),
      severalMessage('promo', '.x', 3, 0), severalMessage('promo', '//x', 9, 5)], py.several);
    // The first sentence of the TypeError for a function that returned no promise, with the
    // value as TypeScript writes it. The rest is advice for each language's own promises.
    const values: unknown[] = [undefined, 3, 'done'];
    py.plain.forEach((text, i) => {
      const head = text.split(' On mendwright.aio.open()')[0].replace(/returned None, /, 'returned undefined, ')
        .split('not an awaitable.').join('not a promise.');
      assert.equal(plainMessage('review', values[i]).split(" heal.page's calls return promises")[0], head);
    });
    // The examples the guide quotes, as this package writes them.
    assert.ok(ranMessage('payment', '/checkout/step-a', 0.4) === "ran step 'payment' on /checkout/step-a (0.4s)");
  });

  test("the numbers in them are formatted as Python's format() and round() format them", { skip: needsPython() }, () => {
    const g = [0, -0, 40, 10000, 100000, 999999, 999999.5, 1e6, 1234567, 1234565, 1234575, 0.5, 1500.25, 0.0001, 0.00001,
      0.000123456789, 1e16, 123456789, 1e21, 1e22, 2.5e-7, 40.5, 1 / 3, 60, 70, 0.1, 1e100, 5e-324, -1234567, Infinity];
    const f = [0, 0.25, 0.35, 0.75, 1.25, 0.05, 0.15, 2.5, 0.04, 0.06, 12.345, 1e21, 0.0625, 0.1875, 0.0005, 3.14159, 1e-9,
      99.95, 9.95, 12.25, 1.23456, 0.1234];
    const py = runPython<{ g: string[]; f1: string[]; r3: number[] }>(PY_HEAD + `
d = json.load(sys.stdin)
g = [float('inf') if x == 'inf' else x for x in d['g']]
g[1] = -0.0
print(json.dumps({'g': [format(x, 'g') for x in g], 'f1': [format(x, '.1f') for x in d['f']],
                  'r3': [round(x, 3) for x in d['f']]}))`, { g: g.map((x) => (x === Infinity ? 'inf' : x)), f });
    assert.deepEqual(g.map(pyFormatG), py.g);
    assert.deepEqual(f.map((x) => pyFixed(x, 1)), py.f1);
    assert.deepEqual(f.map((x) => pyRound(x, 3)), py.r3);
  });
});

describe('the arguments', () => {
  test('each argument that can never work throws TypeError or InvalidSelector at once, naming the fix', () => {
    const run = async () => undefined;
    const cases: [() => unknown, string, RegExp][] = [
      [() => makeStep('', { when: REVIEW }, run), 'TypeError', /needs a name, a non-empty string such as 'payment', got ''/],
      [() => makeStep(5, { when: REVIEW }, run), 'TypeError', /needs a name/],
      [() => makeStep('review', REVIEW, run), 'TypeError', /takes its options as an object and the function after them, such as heal\.step\('review', \{ when: '#card-number' \}, async \(page\) => \{ \.\.\. \}\), got "\/\/button\[\.='Place order'\]"\./],
      [() => makeStep('review', [REVIEW], run), 'TypeError', /takes its options as an object/],
      [() => makeStep('review', { when: REVIEW, run }, undefined), 'TypeError', /got run, which it does not take\. Its options are when, url, last and optional, and the function comes after them/],
      [() => makeStep('review', { when: REVIEW, timeout: 5, 'a b': 1 }, run), 'TypeError', /got timeout and 'a b', which it does not take/],
      [() => makeStep('review', { when: REVIEW }, 'click'), 'TypeError', /takes the function that does the step, got 'click'\. Pass it after the options/],
      [() => makeStep('review', { when: REVIEW }, undefined), 'TypeError', /takes the function that does the step, got undefined/],
      [() => makeStep('review', {}, run), 'TypeError', /needs when, a selector of something only that step shows/],
      [() => makeStep('review', { when: null, url: null }, run), 'TypeError', /needs when/],
      [() => makeStep('review', { when: [] }, run), 'TypeError', /heal\.step\('review', \{ when: \[\] \}\) got an empty list/],
      [() => makeStep('review', { when: 5 as never }, run), 'TypeError', /got number where a selector was expected/],
      [() => makeStep('review', { when: '' }, run), 'InvalidSelector', /the selector for 'review' is not usable/],
      [() => makeStep('review', { when: [REVIEW, 'css='] }, run), 'InvalidSelector', /the selector for 'review' is not usable/],
      [() => makeStep('review', { url: 5 as never }, run), 'TypeError', /takes the path of the step's page as a string/],
      [() => makeStep('review', { url: 'checkout/review' }, run), 'TypeError', /starting with \/ or \*/],
      [() => makeStep('review', { url: 'https://shop.test/review' }, run), 'TypeError', /not a whole address/],
      [() => makeStep('review', { url: '/' }, run), 'TypeError', /the site root/],
      [() => makeStep('review', { when: REVIEW, last: 'yes' as never }, run), 'TypeError', /takes last: true or last: false, got last: 'yes'\./],
      [() => makeStep('review', { when: REVIEW, last: null as never }, run), 'TypeError', /got last: null\./],
      [() => makeStep('review', { when: REVIEW, optional: 1 as never }, run), 'TypeError', /got optional: 1\./],
      [() => makeStep('review', { when: REVIEW, last: true, optional: true }, run), 'TypeError', /cannot be optional\. Drop optional: true\./],
    ];
    for (const [call, kind, words] of cases) {
      const [name, message] = thrown(call);
      assert.equal(name, kind, String(words));
      assert.match(message, words);
      assert.ok(message.startsWith('mendwright: '), message);
    }
  });

  test('a flow has one last step; registering a name again replaces it in its place', () => {
    const book = new Map<string, Step>();
    const review = makeStep('review', { when: REVIEW, last: true }, fn);
    registerStep(book, review);
    assert.match(thrown(() => registerStep(book, makeStep('done', { when: '//h1', last: true }, fn)))[1],
      /'review' is the last step already/);
    registerStep(book, makeStep('review', { when: REVIEW, last: true }, fn));   // the same step again is fine
    const other = async () => 'shipped';
    registerStep(book, makeStep('shipping', { when: "//h1[.='Shipping']" }, fn));
    registerStep(book, makeStep('payment', { when: ["//h1[.='Payment']", '#card-number', "//h1[.='Payment']"] }, fn));
    registerStep(book, makeStep('shipping', { when: 'css=#ship-name' }, other));
    assert.deepEqual([...book.keys()], ['review', 'shipping', 'payment']);
    assert.deepEqual(book.get('shipping')!.when, ['#ship-name']);
    assert.equal(book.get('shipping')!.fn, other);
    assert.deepEqual(book.get('payment')!.when, ["//h1[.='Payment']", '#card-number']);
    assert.deepEqual([book.get('payment')!.url, makeStep('x', { url: ' /A/1 ' }, fn).urlText], [null, '/A/1']);
  });

  test("runSteps()' options and the steps it needs", () => {
    const book = new Map<string, Step>();
    assert.match(thrown(() => runArgs(book, {}, 100))[1], /runSteps\(\) has no steps to run/);
    registerStep(book, makeStep('upsell', { when: '//h1', optional: true }, fn));
    assert.match(thrown(() => runArgs(book, {}, 100))[1], /every step is optional and none has last: true/);
    registerStep(book, makeStep('review', { when: REVIEW, last: true }, fn));
    assert.deepEqual(runArgs(book, {}, 100), [100, 200]);
    assert.deepEqual(runArgs(book, { timeoutMs: null, pollMs: 0 }, 100), [100, 0]);
    assert.deepEqual(runArgs(book, { timeoutMs: Infinity }, 100), [Infinity, 200]);
    const cases: [unknown, RegExp][] = [
      [{ timeoutMs: '10' }, /takes timeoutMs as a number of milliseconds, got timeoutMs: '10'\./],
      [{ timeoutMs: NaN }, /got timeoutMs: NaN\./],
      [{ timeoutMs: -5 }, /takes timeoutMs of 0 or more, got timeoutMs: -5\./],
      [{ pollMs: null }, /takes pollMs as a number of milliseconds, got pollMs: null\./],
      [{ pollMs: -1 }, /takes pollMs of 0 or more, got pollMs: -1\./],
      [{ pollMs: true }, /got pollMs: true\./],
      [5000, /takes its options as an object, such as \{ timeoutMs: 5000 \}, got 5000\./],
      [null, /got null\./],
      [{ timeout: 5000 }, /got timeout, which it does not take\. Its options are timeoutMs and pollMs\./],
    ];
    for (const [options, words] of cases) {
      const [name, message] = thrown(() => runArgs(book, options, 100));
      assert.equal(name, 'TypeError');
      assert.match(message, words);
    }
  });

  test("the messages are the Python package's, in TypeScript's spelling", { skip: needsPython() }, () => {
    const py = runPython<[string, string][]>(PY_HEAD + `
from mendwright.steps import make_step, register, run_args, step_conditions
REVIEW = "//button[.='Place order']"
fn = lambda page: None
def message(call):
    try:
        call()
    except Exception as exc:
        return [type(exc).__name__, str(exc)]
    return ['ok', '']
book = {}
register(book, make_step('review', fn, (REVIEW,), None, last=True))
optional = {'u': make_step('u', fn, ('//h1',), None, optional=True)}
print(json.dumps([
    message(lambda: step_conditions(5)), message(lambda: step_conditions('')),
    message(lambda: step_conditions('review', REVIEW, last='yes')),
    message(lambda: step_conditions('review', REVIEW, optional=1)),
    message(lambda: step_conditions('review', REVIEW, last=True, optional=True)),
    message(lambda: step_conditions('review', [])), message(lambda: step_conditions('review')),
    message(lambda: make_step('review', 'click', (REVIEW,), None)),
    message(lambda: register(book, make_step('done', fn, ('//h1',), None, last=True))),
    message(lambda: run_args(book, '10', 200, 100)), message(lambda: run_args(book, None, None, 100)),
    message(lambda: run_args(book, None, -1, 100)), message(lambda: run_args(book, -5, 200, 100)),
    message(lambda: run_args({}, None, 200, 100)), message(lambda: run_args(optional, None, 200, 100)),
]))`);
    const book = new Map<string, Step>();
    registerStep(book, makeStep('review', { when: REVIEW, last: true }, fn));
    const optional = new Map([['u', makeStep('u', { when: '//h1', optional: true }, fn)]]);
    const ours: [() => unknown, [string, string][]][] = [
      [() => makeStep(5, {}, fn), []],
      [() => makeStep('', {}, fn), []],
      [() => makeStep('review', { when: REVIEW, last: 'yes' }, fn),
        [['last=True or last=False, got last=', 'last: true or last: false, got last: ']]],
      [() => makeStep('review', { when: REVIEW, optional: 1 }, fn),
        [['optional=True or optional=False, got optional=', 'optional: true or optional: false, got optional: ']]],
      [() => makeStep('review', { when: REVIEW, last: true, optional: true }, fn),
        [["heal.step('review', ..., last=True, optional=True)", "heal.step('review', { last: true, optional: true })"],
          ['Drop optional=True.', 'Drop optional: true.']]],
      [() => makeStep('review', { when: [] }, fn), [["heal.step('review', [])", "heal.step('review', { when: [] })"]]],
      [() => makeStep('review', {}, fn), [['needs when=, a selector', 'needs when, a selector'], ['or url=, the path', 'or url, the path'],
        ["heal.step('review', '#card-number') or heal.step('review', url='/checkout/payment').",
          "heal.step('review', { when: '#card-number' }, fn) or heal.step('review', { url: '/checkout/payment' }, fn)."]]],
      [() => makeStep('review', { when: REVIEW }, 'click'),
        [[". Use it as a decorator, @heal.step('review', '#card-number'), or pass the function: heal.step('review', "
          + "'#card-number', fn).", ". Pass it after the options: heal.step('review', { when: '#card-number' }, async (page) => "
          + '{ ... }).']]],
      [() => registerStep(book, makeStep('done', { when: '//h1', last: true }, fn)),
        [["heal.step('done', ..., last=True)", "heal.step('done', { last: true })"], ...SPELLINGS]],
      [() => runArgs(book, { timeoutMs: '10' }, 100), [['run_steps() takes timeout_ms= as', 'runSteps() takes timeoutMs as'],
        ["got timeout_ms='10'", "got timeoutMs: '10'"]]],
      [() => runArgs(book, { pollMs: null }, 100), [['run_steps() takes poll_ms= as', 'runSteps() takes pollMs as'],
        ['got poll_ms=None', 'got pollMs: null']]],
      [() => runArgs(book, { pollMs: -1 }, 100), [['run_steps() takes poll_ms= of', 'runSteps() takes pollMs of'],
        ['got poll_ms=-1', 'got pollMs: -1']]],
      [() => runArgs(book, { timeoutMs: -5 }, 100), [['run_steps() takes timeout_ms= of', 'runSteps() takes timeoutMs of'],
        ['got timeout_ms=-5', 'got timeoutMs: -5']]],
      [() => runArgs(new Map(), {}, 100), [["for example @heal.step('payment', '#card-number').",
        "for example heal.step('payment', { when: '#card-number' }, async (page) => { ... })."], ...SPELLINGS]],
      [() => runArgs(optional, {}, 100), SPELLINGS],
    ];
    ours.forEach(([call, pairs], i) => {
      const [kind, message] = thrown(call);
      assert.ok(['TypeError', 'ValueError'].includes(py[i][0]), py[i][0]);
      assert.equal(kind, 'TypeError');
      assert.equal(message, rewrite(py[i][1], pairs), String(i));
    });
  });
});

describe('one runSteps() call', () => {
  /** Run the same decisions through this package's StepRun, with a journal in file. */
  function decisions(file: string): { lines: string[]; errors: string[] } {
    const journal = new Journal(file, { clock: () => 'T', run: null });
    const lines: string[] = [];
    const book = new Map<string, Step>();
    for (const s of [step('shipping', ['#a']), step('payment', ['#b']), step('gift', ['#g']),
      step('upsell', ['#u'], { optional: true }), step('review', ['#r'], { last: true })]) registerStep(book, s);
    const flow = new StepRun(book, { emit: (level, message) => lines.push(level + ': ' + message), journal }, 40);
    const errors: string[] = [];
    flow.starting(book.get('payment')!);
    flow.record(book.get('payment')!, '/checkout/a', 0.1234);
    flow.starting(book.get('shipping')!);
    flow.record(book.get('shipping')!, '/checkout/b', 1.23456);
    flow.record(book.get('review')!, null, 0.5, true, Object.assign(new Error('the card form is broken'), { name: 'ValueError' }));
    assert.equal(flow.done(), false);   // a last step that failed did not end the flow
    flow.record(book.get('review')!, '/checkout/c', 2.5);
    assert.equal(flow.done(), true);
    assert.deepEqual(flow.finish(), ['payment', 'shipping', 'review', 'review']);
    errors.push(flow.stuck([book.get('payment')!], '/checkout/a').message);
    errors.push(flow.stuck([book.get('shipping')!], '/checkout/x').message);
    errors.push(flow.stuck([], '/checkout/z').message);
    errors.push(flow.stuck([], null).message);
    const one = new Map([['loop', step('loop', ['//h1'])]]);
    const capped = new StepRun(one, { emit: () => undefined, journal }, 40);
    for (let i = 0; i < 3; i++) capped.record(one.get('loop')!, '/x', 0.001);
    try {
      capped.checkCap();
    } catch (error) {
      errors.push((error as Error).message);
    }
    return { lines, errors };
  }

  test('the flow.step and flow.stop lines, the log lines and the errors are the Python package\'s',
    { skip: needsPython() }, () => {
      const dir = tempDir();
      const ours = decisions(path.join(dir, 'ts.jsonl'));
      const py = runPython<{ journal: string[]; lines: string[]; errors: string[] }>(PY_HEAD + `
import logging, types
from mendwright.exceptions import AttentionRequired
from mendwright.journal import Journal
from mendwright.steps import Run, Step, register
d = json.load(sys.stdin)
j = Journal(d['file'], clock=lambda: 'T', run=None)
lines = []
emit = lambda level, message: lines.append(('info' if level == logging.INFO else 'warning') + ': ' + message)
fn = lambda page: None
core = types.SimpleNamespace(_step_book={}, coordinator=types.SimpleNamespace(journal=j, _emit=emit))
for s in [Step('shipping', fn, ('#a',)), Step('payment', fn, ('#b',)), Step('gift', fn, ('#g',)),
          Step('upsell', fn, ('#u',), optional=True), Step('review', fn, ('#r',), last=True)]:
    register(core._step_book, s)
book = core._step_book
flow = Run(core, 40)
flow.starting(book['payment'])
flow.record(book['payment'], '/checkout/a', 0.1234)
flow.starting(book['shipping'])
flow.record(book['shipping'], '/checkout/b', 1.23456)
flow.record(book['review'], None, 0.5, ValueError('the card form is broken'))
flow.record(book['review'], '/checkout/c', 2.5)
flow.finish()
errors = [str(flow.stuck([book['payment']], '/checkout/a')), str(flow.stuck([book['shipping']], '/checkout/x')),
          str(flow.stuck([], '/checkout/z')), str(flow.stuck([], None))]
one = types.SimpleNamespace(_step_book={'loop': Step('loop', fn, ('//h1',))},
                            coordinator=types.SimpleNamespace(journal=j, _emit=lambda level, message: None))
capped = Run(one, 40)
for i in range(3):
    capped.record(one._step_book['loop'], '/x', 0.001)
try:
    capped.check_cap()
except AttentionRequired as exc:
    errors.append(str(exc))
print(json.dumps({'journal': open(d['file'], encoding='utf-8').read().splitlines(), 'lines': lines, 'errors': errors}))`,
      { file: path.join(dir, 'py.jsonl') });
      assert.deepEqual(fs.readFileSync(path.join(dir, 'ts.jsonl'), 'utf-8').split('\n').filter(Boolean), py.journal);
      assert.deepEqual(ours.lines, py.lines.map((l) => rewrite(l, SPELLINGS)));
      assert.deepEqual(ours.errors, py.errors.map((e) => rewrite(e, SPELLINGS)));
      // The lines hold each field the Python package writes, in its order, and nothing more.
      assert.deepEqual(py.journal.slice(0, 3).map((l) => Object.keys(JSON.parse(l))), [
        ['ts', 'event', 'key', 'position', 'registered', 'page', 'seconds', 'outcome'],
        ['ts', 'event', 'key', 'position', 'registered', 'page', 'seconds', 'outcome'],
        ['ts', 'event', 'key', 'position', 'registered', 'seconds', 'outcome', 'error']]);
    });
});

describe('the steps a store keeps per key', () => {
  test('setLastStep keeps 5 names, most recent first, and writes only when the set changes', () => {
    const file = path.join(tempDir(), 'overrides.json');
    const store = loadStore(file);
    store.setCodeCandidates('next', ['#next']);
    let writes = 0;
    const persist = store.persist.bind(store);
    store.persist = () => {
      writes += 1;
      persist();
    };
    for (const name of ['a', 'b', 'a']) store.setLastStep('next', name);
    assert.deepEqual(store.metaOf('next').steps, ['a', 'b']);
    assert.equal(writes, 2);   // 'a' again changed the order only
    for (const name of 'cdefg') store.setLastStep('next', name);
    assert.deepEqual(store.metaOf('next').steps, ['g', 'f', 'e', 'd', 'c']);
    store.setLastStep('next', '');
    store.setLastStep('next', null);
    store.setLastStep('next', 5 as never);
    assert.deepEqual(loadStore(file).metaOf('next').steps, ['g', 'f', 'e', 'd', 'c']);
  });

  test("two processes keep each other's steps", () => {
    const file = path.join(tempDir(), 'overrides.json');
    const first = loadStore(file);
    first.setCodeCandidates('next', ['#next']);
    const second = loadStore(file);
    first.setLastStep('next', 'shipping');
    second.setLastStep('next', 'payment');
    assert.deepEqual(loadStore(file).metaOf('next').steps.sort(), ['payment', 'shipping']);
  });

  describe('in a store shared with the Python package', { skip: needsPython() }, () => {
    const PY_STEPS = PY_HEAD + `
from mendwright.store import load_store
d = json.load(sys.stdin)
store = load_store(None, d['file'])
before = store.meta('next')['steps']
for name in d.get('add', []):
    store.set_last_step('next', name)
store.set_code_candidates('python_after', ['#again'])
print(json.dumps({'before': before, 'after': load_store(None, d['file']).meta('next')['steps']}))`;

    test('the Python package reads the steps this package saved, and keeps them when it writes', () => {
      const file = path.join(tempDir(), 'overrides.json');
      const store = loadStore(file);
      store.setCodeCandidates('next', ['#next']);
      store.setLastStep('next', 'shipping');
      store.setLastStep('next', 'payment');
      const py = runPython<{ before: string[]; after: string[] }>(PY_STEPS, { file });
      assert.deepEqual(py.before, ['payment', 'shipping']);
      assert.deepEqual(py.after, ['payment', 'shipping']);
      const again = runPython<{ before: string[]; after: string[] }>(PY_STEPS, { file, add: ['review'] });
      assert.deepEqual(again.after, ['review', 'payment', 'shipping']);
      assert.deepEqual(loadStore(file).metaOf('next').steps, ['review', 'payment', 'shipping']);
    });

    test('this package merges the steps the Python package wrote since it read the file', () => {
      const file = path.join(tempDir(), 'overrides.json');
      const store = loadStore(file);
      store.setCodeCandidates('next', ['#next']);
      store.setLastStep('next', 'shipping');
      runPython(PY_STEPS, { file, add: ['payment'] });
      store.setLastStep('next', 'review');   // this store has not seen 'payment'
      const py = runPython<{ before: string[] }>(PY_STEPS, { file });
      assert.deepEqual([...py.before].sort(), ['payment', 'review', 'shipping']);
    });
  });
});
