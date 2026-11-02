// The keys of heal.page.getBy*() calls (getby.ts), how they are read back (pysyntax.ts), and the
// calls that are never keys, against the Python package's mendwright/getby.py: the same key for
// each call, the same call read from each spelling, and every key this package writes read back
// by the Python package to the same key. No browser, no model.

import { strict as assert } from 'node:assert';
import { describe, test } from 'node:test';

import { InvalidSelector } from '../../src/errors';
import {
  BIND_REASON, FLAGS_REASON, HIDDEN_REASON, LONG_REASON, MAX_KEY, METHODS, NO_MODEL, NOT_FOLLOWED, PATTERN_REASON,
  ROLE_UNREADABLE, SURROGATE_REASON, TS_METHODS, TYPE_REASON, build, callOf, canonical, canonicalKey, emptyReason, getByKey,
  isKey, noFingerprintMessage, notHealedMessage, onObject, onObjectMessage, parse, reservedKeyMessage, roleMismatch,
  roleUnknownMessage, stateReason, throughMessage, tierOffMessage, twoSpellingsMessage, unknownReason, type GetByCall,
  type GetByMethod,
} from '../../src/getby';
import { pyReadable } from '../../src/pysyntax';
import { inferRole } from '../../src/roles';
import { checkSyntax, classify, frameSpelling, normalize } from '../../src/selectors';
import { needsPython, runPython } from '../support/python';

const skip = needsPython();

/** The key of a call, or the reason it has none. */
function keyOf(method: string, ...args: unknown[]): string {
  const [call, why] = callOf(method, args);
  return call === null ? 'never: ' + why : canonical(call);
}

// The table of the guide, in TypeScript: each call on heal.page, and its key.
const KEYS: [string, unknown[], string][] = [
  ['getByRole', ['button', { name: 'Sign in' }], 'get_by_role("button", name="Sign in")'],
  ['getByRole', ['button', { name: 'Sign in', exact: false }], 'get_by_role("button", name="Sign in")'],
  ['getByRole', ['button', { name: 'Sign in', exact: true }], 'get_by_role("button", name="Sign in", exact=True)'],
  ['getByRole', ['heading', { name: 'Cart', level: 2 }], 'get_by_role("heading", name="Cart", level=2)'],
  ['getByRole', ['button', { name: /sign\s?in/i }], 'get_by_role("button", name=re.compile("sign\\\\s?in", re.IGNORECASE))'],
  ['getByRole', ['navigation'], 'get_by_role("navigation")'],
  ['getByRole', ['button', { exact: true }], 'get_by_role("button")'],
  ['getByText', ['Free shipping'], 'get_by_text("Free shipping")'],
  ['getByText', ['Say "hi"'], 'get_by_text("Say \\"hi\\"")'],
  ['getByLabel', ['Email', { exact: true }], 'get_by_label("Email", exact=True)'],
  ['getByPlaceholder', ['you@example.com'], 'get_by_placeholder("you@example.com")'],
  ['getByAltText', ['Logo'], 'get_by_alt_text("Logo")'],
  ['getByTitle', ['Close'], 'get_by_title("Close")'],
  ['getByTestId', ['login'], 'get_by_test_id("login")'],
];

describe('the key of a call', () => {
  test('each method has the key of the guide', () => {
    for (const [method, args, key] of KEYS) {
      assert.equal(keyOf(method, ...args), key, method);
      assert.equal(getByKey(method as GetByMethod, ...(args as [never])), key, method);
    }
  });

  test('the options are spelled one way', () => {
    assert.equal(keyOf('getByRole', 'button', { exact: undefined }), 'get_by_role("button")');
    assert.equal(keyOf('getByText', 'Save', { exact: false }), 'get_by_text("Save")');
    assert.equal(keyOf('getByText', 'Save', { exact: null }), 'get_by_text("Save")');
    assert.equal(keyOf('getByText', 'Save', undefined), 'get_by_text("Save")');
    // exact changes nothing for a regular expression, so the key leaves it out.
    assert.equal(keyOf('getByText', /save/, { exact: true }), 'get_by_text(re.compile("save"))');
    assert.equal(keyOf('getByRole', 'button', { name: /go/, exact: true }), 'get_by_role("button", name=re.compile("go"))');
    // name, exact, level, whatever order they were passed in.
    assert.equal(keyOf('getByRole', 'heading', { level: 3, exact: true, name: 'Cart' }),
      'get_by_role("heading", name="Cart", exact=True, level=3)');
    assert.equal(keyOf('getByRole', 'button', { includeHidden: false }), 'get_by_role("button")');
    assert.equal(keyOf('getByText', /a/smi), 'get_by_text(re.compile("a", re.IGNORECASE | re.MULTILINE | re.DOTALL))');
    // An option whose value is undefined is not there, as for Playwright.
    assert.equal(keyOf('getByRole', 'button', { name: 'Go', checked: undefined, description: undefined }),
      'get_by_role("button", name="Go")');
  });

  test('the text is escaped as JSON, kept as written, and read back', () => {
    for (const text of ['back\\slash', 'line\nbreak', 'tab\there', 'bell\x07', 'Café ✓ 日本', "it's", '"quoted"',
      'line\u2028separator', 'emoji 😀', 'del\x7f']) {
      const key = keyOf('getByText', text);
      assert.ok(key.startsWith('get_by_text("') && key.endsWith('")'), key);
      assert.deepEqual(parse(key), { method: 'get_by_text', value: text, name: null, exact: false, level: null });
    }
    assert.equal(keyOf('getByText', 'a\nb'), 'get_by_text("a\\nb")');
    assert.equal(keyOf('getByText', 'Café'), 'get_by_text("Café")');
    assert.notEqual(keyOf('getByText', 'Save '), keyOf('getByText', 'Save'));
    assert.equal(keyOf('getByRole', 'Button'), 'get_by_role("Button")');
  });

  test('a key reads back as its call, and spells it the same', () => {
    for (const [, , key] of KEYS) assert.equal(canonical(parse(key) as GetByCall), key);
    const words = ['Save', 'Sign in', 'a"b', 'back\\', 'é', 'x\ny', '  pad ', '日本'];
    let seed = 9001;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
    const pick = <T>(items: readonly T[]): T => items[Math.floor(rnd() * items.length)];
    for (let n = 0; n < 300; n++) {
      const method = pick(TS_METHODS);
      let text: string | RegExp = pick(words) + pick(['', '!', ' 2']);
      if (rnd() < 0.3) text = new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), pick(['', 'i', 'is', 'm']));
      const options: Record<string, unknown> = {};
      let args: unknown[];
      if (method === 'getByRole') {
        if (rnd() < 0.7) options.name = text;
        if (rnd() < 0.5) options.exact = pick([true, false, undefined]);
        if (rnd() < 0.3) options.level = 1 + Math.floor(rnd() * 6);
        args = ['button', options];
      } else if (method === 'getByTestId') {
        args = [text];
      } else {
        args = [text, { exact: pick([true, false, undefined]) }];
      }
      const [call, why] = callOf(method, args);
      assert.ok(call, why);
      const key = canonical(call);
      assert.deepEqual(parse(key), call, key);
      assert.equal(canonical(parse(key) as GetByCall), key);
    }
  });

  test('other spellings read as the canonical key', () => {
    assert.equal(canonical(parse("get_by_role('button', exact=False)") as GetByCall), 'get_by_role("button")');
    assert.equal(canonical(parse('get_by_role(role="button")') as GetByCall), 'get_by_role("button")');
    assert.equal(canonical(parse("get_by_text(re.compile('go', re.I | re.S))") as GetByCall),
      'get_by_text(re.compile("go", re.IGNORECASE | re.DOTALL))');
    assert.equal(canonical(parse("  get_by_text('Sa' 've')  ") as GetByCall), 'get_by_text("Save")');
    assert.equal(normalize("get_by_label('Email', exact=None)"), 'get_by_label("Email")');
    assert.ok(isKey('get_by_test_id("go")') && !isKey('#go') && !isKey(null));
  });

  test('canonicalKey reads a call in any spelling and leaves other keys as they are', () => {
    assert.equal(canonicalKey("get_by_role('button', name='Pay')"), 'get_by_role("button", name="Pay")');
    assert.equal(canonicalKey('get_by_role("button", name="Pay")'), 'get_by_role("button", name="Pay")');
    for (const key of ['#pay', 'pay_button', "page.get_by_role('button', name='Pay')", "get_by_role('checkbox', checked=True)",
      null]) {
      assert.equal(canonicalKey(key), key);
    }
    assert.equal(twoSpellingsMessage('expectValues', 'get_by_test_id("t")', "get_by_test_id('t')"),
      'mendwright: expectValues has two keys for the call get_by_test_id("t"): \'get_by_test_id("t")\' and '
      + "\"get_by_test_id('t')\". Keep one of them.");
  });

  test('a call written on an object is found, but stays a key of its own', () => {
    const pay = parse('get_by_role("button", name="Pay")');
    for (const key of ["page.get_by_role('button', name='Pay')", 'heal.page.get_by_role("button", name="Pay")',
      " self._page.get_by_role('button', name='Pay')\n"]) {
      assert.deepEqual(onObject(key), pay);
      assert.equal(canonicalKey(key), key);
    }
    for (const key of ['get_by_role("button", name="Pay")', '#pay', 'page.locator("#pay")',
      "page.get_by_role('checkbox', checked=True)", "page.get_by_role('button').click()", ".get_by_role('button')",
      'page.get_by_role(' + 'x'.repeat(1000) + ')', null]) {
      assert.equal(onObject(key), null, String(key));
    }
    assert.equal(onObjectMessage('destructiveKeys', "page.get_by_role('button', name='Pay')", pay as GetByCall),
      "destructiveKeys has the key \"page.get_by_role('button', name='Pay')\", which no heal.page.getByRole() call "
      + 'uses. That call\'s key is get_by_role("button", name="Pay"), as getByKey() writes it.');
  });
});

describe('calls that are never keys', () => {
  const NEVER: [string, unknown[], string][] = [
    ['getByRole', ['checkbox', { name: 'Terms', checked: true }], stateReason('checked')],
    ['getByRole', ['button', { pressed: false, disabled: true }], stateReason('disabled')],
    ['getByRole', ['tab', { selected: true }], stateReason('selected')],
    ['getByRole', ['button', { expanded: false }], stateReason('expanded')],
    ['getByRole', ['button', { checked: null }], 'get_by_role("button")'],
    ['getByRole', ['button', { includeHidden: true }], HIDDEN_REASON],
    ['getByRole', ['  '], emptyReason('role')],
    ['getByRole', ['button', { name: '' }], emptyReason('name')],
    ['getByText', ['\n'], emptyReason('text')],
    ['getByText', ['\u3000\u2028'], emptyReason('text')],
    ['getByTestId', [''], emptyReason('test id')],
    ['getByLabel', [new RegExp('')], emptyReason('text')],
    ['getByLabel', [/(?:)/], emptyReason('text')],
    ['getByRole', [1], TYPE_REASON],
    ['getByRole', [/button/], TYPE_REASON],
    ['getByRole', [null], TYPE_REASON],
    ['getByText', [['Save']], TYPE_REASON],
    ['getByText', ['Save', { exact: 1 }], TYPE_REASON],
    ['getByRole', ['heading', { level: true }], TYPE_REASON],
    ['getByRole', ['heading', { level: '2' }], TYPE_REASON],
    ['getByRole', ['heading', { level: -1 }], TYPE_REASON],
    ['getByRole', ['heading', { level: 1.5 }], TYPE_REASON],
    ['getByRole', ['heading', { level: 2 ** 60 }], TYPE_REASON],
    ['getByRole', ['button', { includeHidden: 'yes' }], TYPE_REASON],
    ['getByRole', ['button', { name: 42 }], TYPE_REASON],
    ['getByText', ['Sa\ud800ve'], SURROGATE_REASON],
    ['getByText', [new RegExp('\ud800')], SURROGATE_REASON],
    ['getByText', [/save/g], FLAGS_REASON],
    ['getByText', [/save/y], FLAGS_REASON],
    ['getByText', [/save/u], FLAGS_REASON],
    ['getByText', [/save/d], FLAGS_REASON],
    ['getByText', [/\p{L}+/u], FLAGS_REASON],
    ['getByText', [/(?<word>save)/], PATTERN_REASON],
    ['getByText', [/\cA/], PATTERN_REASON],
    ['getByText', [/(?<=a+)b/], PATTERN_REASON],
    ['getByText', [new RegExp('\\p{L}')], PATTERN_REASON],
    ['getByRole', ['button', { description: 'Go' }], unknownReason('description')],
    ['getByText', ['Save', { exactly: true }], unknownReason('exactly')],
    ['getByTestId', ['go', { exact: true }], unknownReason('exact')],
    ['getByText', ['Save', 'exact'], BIND_REASON],
    ['getByText', ['Save', {}, {}], BIND_REASON],
    ['getByText', [], BIND_REASON],
    ['getByText', ['x'.repeat(990)], LONG_REASON],
    ['getByText', ['😀'.repeat(990)], LONG_REASON],
  ];

  test('each says why, and goes to Playwright as written', () => {
    for (const [method, args, why] of NEVER) {
      const [call, reason] = callOf(method, args);
      assert.equal(call === null ? reason : canonical(call), why, method + ' ' + String(args[0]));
    }
    assert.equal(keyOf('getByText', 'x'.repeat(980)).length, 'get_by_text("")'.length + 980);
    assert.equal(notHealedMessage("page.getByRole('checkbox', { name: 'Terms', checked: true })", stateReason('checked')),
      "page.getByRole('checkbox', { name: 'Terms', checked: true }) is not healed: its checked option picks the element "
      + 'by its state, which a saved selector cannot keep. It goes to Playwright as written.');
  });

  test('getByKey says which', () => {
    assert.equal(getByKey('get_by_role', 'button', { name: 'Pay' }), 'get_by_role("button", name="Pay")');
    assert.equal(getByKey('getByTestId', 'total'), 'get_by_test_id("total")');
    assert.throws(() => getByKey('getByName' as GetByMethod, 'Pay' as never), (error: Error) => error instanceof TypeError
      && error.message === "mendwright: getByKey(): 'getByName' is not a getBy* method mendwright heals. Use one of "
      + 'getByRole, getByText, getByLabel, getByPlaceholder, getByAltText, getByTitle or getByTestId.');
    assert.throws(() => getByKey('getByRole', 'checkbox', { checked: true }), (error: Error) => error instanceof TypeError
      && error.message === "mendwright: getByKey(): page.getByRole('checkbox', { checked: true }) is never healed, so it "
      + 'has no key: its checked option picks the element by its state, which a saved selector cannot keep.');
    assert.throws(() => getByKey('getByRole', 'button', { nmae: 'Pay' } as never),
      /page\.getByRole\('button', \{ nmae: 'Pay' \}\) is never healed, so it has no key: it passes the option nmae/);
    assert.throws(() => (getByKey as (...a: unknown[]) => string)('getByText'), /it passes arguments this version/);
  });
});

describe('reading a key', () => {
  test('nothing in it is run, and what is not a call of literals reads as none', () => {
    const texts = ["get_by_role(__import__('os').system('x'))", 'get_by_role(x)', 'get_by_role(*a)', 'get_by_role(**k)',
      "get_by_role(f'x')", "get_by_role(b'x')", "get_by_role(rb'x')", "get_by_role(t'x')", "get_by_role('a').first",
      "get_by_role('a', level=-1)", "get_by_role('a', level=+1)", "get_by_text(re.compile('a', re.X))",
      "get_by_text(re.compile('a', flags=re.I))", 'get_by_text(re.compile(x))', "get_by_text(os.compile('a'))",
      "get_by_role('a', name='b', name='c')", "get_by_role('a', 1.5)", "get_by_role('a', level=2.0)",
      "get_by_role('a', level=2j)", 'get_by_text("' + 'x'.repeat(1000) + '")',
      'get_by_text(' + '('.repeat(300) + '"x"' + ')'.repeat(300) + ')', 'get_by_name("x")', 'get_by_role("a"); import os',
      'get_by_role("a") or 1', 'get_by_text(re.compile("("))', "get_by_role('a',,)", "get_by_role(,)",
      "get_by_role('a' 'b' f'c')", "get_by_role(name='x', 'button')", "get_by_role('a', level=01)",
      "get_by_role('a', level=1_)", "get_by_role\n('a')", "get_by_role('a'\n", "get_by_role('a\nb')",
      "get_by_role('a', name=('b',))", "get_by_role('a', name=())", "get_by_role('a', name=re.compile('b', re.I + re.S))",
      "get_by_role('a', name=re.compile('b', 0))", "get_by_role('a', name=re.compile('b', re.U))",
      "get_by_role('a') b", 'get_by_role("\\N{LATIN SMALL LETTER A}")', "get_by_role('a', naïme='b')",
      "get_by_role(\u00a0'a')", 'get_by_role("a\0")', "get_by_role('\\xZZ')",
      "get_by_role('\\U00110000')", "get_by_role(ur'a')", "get_by_role(x'a')", "get_by_role(r 'a')"];
    for (const text of texts) assert.equal(parse(text), null, text);
  });

  test('classify reads calls and nothing else', () => {
    assert.equal(classify('get_by_role("button")'), 'get_by');
    for (const text of ['get_by_role', 'get_by_rolex(1)', '#get_by_role', 'get_by_role(']) {
      assert.equal(classify(text), 'css', text);
    }
    assert.throws(() => checkSyntax('get_by_role("button", name="Go")'), (error: unknown) => error instanceof InvalidSelector
      && error.forKey('go').message === "mendwright: the selector for 'go' is not usable: "
      + '\'get_by_role("button", name="Go")\' is a get_by_* call, not a selector. Call heal.page.getByRole(...) on '
      + 'open(page, { healGetBy: true }), or pass an XPath, CSS or Playwright selector.');
    assert.equal(checkSyntax('get_by_role("button")', { calls: true }), 'get_by');
    assert.equal(frameSpelling('get_by_role("button")'), null);
    assert.equal(inferRole('get_by_role("button")'), 'unknown');
  });

  test('build calls the method with the key\'s arguments', () => {
    const calls: [string, unknown[]][] = [];
    const ctx = new Proxy({}, { get: (_t, method: string) => (...args: unknown[]) => calls.push([method, args]) });
    build(ctx as never, parse('get_by_role("heading", name="Cart", exact=True, level=2)') as GetByCall);
    build(ctx as never, parse('get_by_role("navigation")') as GetByCall);
    build(ctx as never, parse('get_by_label("Email", exact=True)') as GetByCall);
    build(ctx as never, parse('get_by_test_id("go")') as GetByCall);
    build(ctx as never, parse('get_by_text(re.compile("sign\\\\s?in", re.IGNORECASE | re.DOTALL))') as GetByCall);
    assert.deepEqual(calls.slice(0, 4), [['getByRole', ['heading', { name: 'Cart', exact: true, level: 2 }]],
      ['getByRole', ['navigation', {}]], ['getByLabel', ['Email', { exact: true }]], ['getByTestId', ['go']]]);
    const [method, [re]] = calls[4];
    assert.equal(method, 'getByText');
    assert.ok(re instanceof RegExp && re.source === 'sign\\s?in' && re.flags === 'is');
  });

  test('the messages a heal of a call writes', () => {
    assert.equal(roleMismatch(parse('get_by_role("tab", name="Settings")') as GetByCall),
      "live element role mismatch: Playwright does not give it the ARIA role 'tab'");
    assert.equal(roleMismatch(parse('get_by_role("heading", name="Cart", level=2)') as GetByCall),
      "live element role mismatch: Playwright does not give it the ARIA role 'heading' at level 2");
    assert.equal(reservedKeyMessage('get_by_role("button", name="Go")', 'heal.locator()'),
      'mendwright: \'get_by_role("button", name="Go")\' is how mendwright names a heal.page.getByRole() call, so '
      + 'heal.locator() cannot take it as a key. Pick another key.');
    assert.equal(throughMessage('get_by_role("button")', 'matches 2 elements'),
      'get_by_role("button") matches 2 elements; Playwright runs it as written.');
    assert.ok(tierOffMessage('k').includes('(fingerprint: false or MENDWRIGHT_FINGERPRINT=0)'));
    assert.ok(noFingerprintMessage('k').startsWith('mendwright heals a getBy* call only from the fingerprint'));
    assert.equal(NOT_FOLLOWED, 'mendwright does not follow a getBy* call into an iframe');
    assert.equal(ROLE_UNREADABLE, "could not check the element's ARIA role on the live page");
    assert.ok(NO_MODEL.startsWith('mendwright asks no model to heal a getBy* call'));
    assert.ok(roleUnknownMessage('k').startsWith("mendwright does not know what kind of element 'k' is"));
    assert.equal(MAX_KEY, 1000);
    assert.deepEqual([...METHODS].length, TS_METHODS.length);
  });
});

// --- against the Python package -----------------------------------------------------------

/** A TypeScript call as the Python package's call_of() takes it: its method, positional
 * arguments and keyword arguments, a RegExp as { re, flags }. */
function asPython(method: string, args: unknown[]): [string, unknown[], Record<string, unknown>] {
  const py = METHODS[TS_METHODS.indexOf(method as never)];
  const value = (v: unknown): unknown => (v instanceof RegExp ? { re: v.source, flags: v.flags } : v);
  const kwargs: Record<string, unknown> = {};
  for (const [name, v] of Object.entries((args[1] ?? {}) as Record<string, unknown>)) {
    if (v !== undefined) kwargs[name === 'includeHidden' ? 'include_hidden' : name] = value(v);
  }
  return [py, [value(args[0])], kwargs];
}

const PY_HEAD = `
import json, re, sys
from mendwright.getby import call_of, canonical, parse

def value(v):
    if isinstance(v, dict) and 're' in v:
        flags = 0
        for letter in v['flags']:
            flags |= {'i': re.I, 'm': re.M, 's': re.S}[letter]
        return re.compile(v['re'], flags)
    return v
`;

// Calls whose keys both packages build, with the escapes JSON writes and the characters it
// leaves as they are.
const PARITY: [string, unknown[]][] = [
  ...KEYS.map(([method, args]) => [method, args] as [string, unknown[]]),
  ['getByText', ['tab\tand\nnew\rline\fform\bback']],
  ['getByText', ['ctl\x01\x1f\x7f del']],
  ['getByText', ['quote " and \\ backslash / slash']],
  ['getByText', ["single ' quote"]],
  ['getByText', ['Café ✓ 日本 😀 \u2028\u2029 \u00a0nbsp \ufeffbom']],
  ['getByLabel', ['  padded  ']],
  ['getByRole', ['button', { name: 'Ünïcödé', exact: true, level: 0 }]],
  ['getByRole', ['heading', { name: /cart/, level: 6 }]],
  ['getByRole', ['button', { name: /\d+ items?/m }]],
  ['getByRole', ['button', { name: /a\/b/ }]],
  ['getByRole', ['button', { name: new RegExp('a/b') }]],
  ['getByText', [/say "hi"/i]],
  ['getByText', [/it's/]],
  ['getByText', [/back\\slash/]],
  ['getByText', [/\s+café\s*/i]],
  ['getByText', [/日本|😀/]],
  ['getByText', [/(?<=\$)\d+(?:\.\d\d)?/]],
  ['getByText', [/(?<!no )thanks/i]],
  ['getByText', [/(?=sign)\w+/]],
  ['getByText', [/^\[x\]$/]],
  ['getByText', [/[a-z0-9_-]+@[\w.]+/i]],
  ['getByText', [/\x41é\t\n\v\f\r\0/]],
  ['getByText', [/a{2}b{1,}c{0,3}?/]],
  ['getByText', [/[\b]\b\B./s]],
  ['getByText', [new RegExp('line\nbreak')]],
  ['getByAltText', [/logo/i, { exact: true }]],
  ['getByTitle', ['Close', { exact: true }]],
  ['getByPlaceholder', [/name@/]],
  ['getByTestId', [/^login-\d+$/]],
  ['getByText', ['x'.repeat(980)]],
];

describe('the same keys as the Python package', { skip }, () => {
  test('a call has the key the Python package gives it', () => {
    const python = runPython<(string | null)[]>(PY_HEAD + `
out = []
for method, args, kwargs in json.load(sys.stdin):
    call, why = call_of(method, tuple(value(a) for a in args), {k: value(v) for k, v in kwargs.items()})
    out.append(canonical(call) if call else 'never: ' + why)
print(json.dumps(out, ensure_ascii=False))`, PARITY.map(([method, args]) => asPython(method, args)));
    PARITY.forEach(([method, args], i) => assert.equal(keyOf(method, ...args), python[i], method + ' ' + String(args[0])));
  });

  test('every key this package writes, the Python package reads back as the same key', () => {
    const keys = PARITY.map(([method, args]) => keyOf(method, ...args));
    const python = runPython<(string | null)[]>(PY_HEAD + `
out = []
for key in json.load(sys.stdin):
    call = parse(key)
    out.append(canonical(call) if call else None)
print(json.dumps(out, ensure_ascii=False))`, keys);
    keys.forEach((key, i) => assert.equal(python[i], key));
  });

  test('any spelling of a call reads as the same call in both packages, or as none in both', () => {
    const spellings = [
      "get_by_role('button', name='Pay')", 'get_by_role(role="button", name="Pay", exact=False, level=None)',
      "get_by_role ( 'button' , name = 'Pay' , )", "get_by_role(\n  'button',\n  name='Pay',  # the pay button\n)",
      "get_by_role('button', name=r'P\\ay')", "get_by_role('button', name=R'P\\'ay')", "get_by_role('button', name=u'Pay')",
      "get_by_role('button', name=U'Pay')", "get_by_role('button', name='''Pa\ny''')", 'get_by_role("button", name="""P"ay""")',
      "get_by_role('button', name='P' \"a\" r'y')", "get_by_role('button', name=('Pay'))", "get_by_role('button', name=(('P' 'ay')))",
      "get_by_role('button', name='\\x50\\u0061\\U00000079 \\101\\0\\7')", "get_by_role('button', name='a\\\nb')",
      "get_by_role('button', name='\\d\\s\\/\\q')", "get_by_role('button', name='\\'\\\"\\\\\\a\\b\\f\\n\\r\\t\\v')",
      "get_by_role('button', name='\\ud83d\\ude00')", "get_by_role('button', name='\\ud800')",
      "get_by_role('heading', level=0x2)", "get_by_role('heading', level=0o2)", "get_by_role('heading', level=0b10)",
      "get_by_role('heading', level=1_0)", "get_by_role('heading', level=00)", "get_by_role('heading', level=True)",
      "get_by_role('heading', level=-1)", "get_by_role('heading', level=99999999999999999999)",
      "get_by_role('button', exact=True)", "get_by_role('button', name='Pay', exact=True)",
      "get_by_role('button', name='Pay', exact=1)", "get_by_role('button', include_hidden=False)",
      "get_by_role('button', include_hidden=True)", "get_by_role('button', checked=None)", "get_by_role('button', checked=False)",
      "get_by_role('button', description='x')", "get_by_role('button', 'Pay')", "get_by_role()", "get_by_role(role='a', role='b')",
      "get_by_role('a', role='b')", "get_by_text(text='Save')", "get_by_text('Save', exact=True)", "get_by_test_id(test_id='go')",
      "get_by_test_id('go', exact=True)", "get_by_alt_text('Logo')", "get_by_title('Close', exact=False)",
      "get_by_placeholder('you@example.com')", "get_by_label('Email', exact=None)",
      "get_by_text(re.compile('go'))", "get_by_text(re.compile(r'sign\\s?in', re.I))", "get_by_text(re.compile('a', re.I | re.S | re.M))",
      "get_by_text(re.compile('a', (re.I | re.S)))", "get_by_text(re.compile('a', re.IGNORECASE|re.MULTILINE))",
      "get_by_text(re.compile('a', re.DOTALL,))", "get_by_text(re.compile('a',))", "get_by_text(re.compile ( 'a' ) )",
      "get_by_text(re . compile('a', re . I))", "get_by_text(re.compile('', re.I))", "get_by_text(re.compile('(?i)a'))",
      "get_by_text(re.compile('(?P<n>a)'))", "get_by_text(re.compile('(?<n>a)'))", "get_by_text(re.compile(r'\\d+(?<=\\d)'))",
      "get_by_text(re.compile(r'(?<=ab|cd)x'))", "get_by_text(re.compile(r'(?<=a|bc)x'))", "get_by_text(re.compile('[a-'))",
      "get_by_text(re.compile(r'\\p{L}'))", "get_by_text(re.compile(r'(a)\\1'))", "get_by_text(re.compile(r'a{,3}'))",
      "get_by_text(re.compile('😀+'))", "get_by_text(re.compile('[😀]'))", "get_by_text(re.compile(r'[\\w-]'))",
      "get_by_text('Save') # a comment", "get_by_text('Save')\n# a comment", "get_by_text('Save' # inside\n)",
      "get_by_text( \\\n 'Save')", "get_by_text('Save')  ", "\n get_by_text('Save')\t", "get_by_text('x', exact=True) ",
      "get_by_text('Sa\\\nve')", 'get_by_text("\\N{BULLET}")', "get_by_text('a' if 1 else 'b')", "get_by_text(('a', 'b'))",
    ];
    const python = runPython<(string | null)[]>(PY_HEAD + `
out = []
for text in json.load(sys.stdin):
    call = parse(text)
    out.append(canonical(call) if call else None)
print(json.dumps(out, ensure_ascii=False))`, spellings);
    // What the Python package reads and this package does not: a \N{name} escape, a whole
    // number past JavaScript's safe integers, and a regular expression outside what both read
    // (pysyntax.pyReadable): an inline flag, a Python named group, a backreference, {,3} and an
    // astral character in a class. No call here has such a key: this package refuses such a
    // RegExp as a key, or JavaScript has none.
    const unreadHere = new Set(['get_by_text("\\N{BULLET}")', "get_by_role('heading', level=99999999999999999999)",
      "get_by_text(re.compile('(?i)a'))", "get_by_text(re.compile('(?P<n>a)'))", "get_by_text(re.compile(r'(a)\\1'))",
      "get_by_text(re.compile(r'a{,3}'))", "get_by_text(re.compile('[😀]'))"]);
    const differ = spellings.filter((text, i) => {
      const call = parse(text);
      return (call === null ? null : canonical(call)) !== (unreadHere.has(text) ? null : python[i]);
    });
    assert.deepEqual(differ, []);
    assert.equal(python[spellings.indexOf('get_by_text("\\N{BULLET}")')], 'get_by_text("•")');
  });

  test('the regular expressions this package keeps compile in Python, with the same flags, and read back the same', () => {
    // Sources built from pieces of both syntaxes: every one JavaScript takes without the u flag is
    // either refused here, or compiles in Python to a pattern whose key reads back the same.
    const pieces = ['a', 'é', '😀', '.', '\\d', '\\w', '\\s', '\\b', '\\B', '\\/', '\\\\', '\\.', '\\1', '\\k', '\\c', '\\p',
      '\\x41', '\\u00e9', '\\0', '\\a', '\\Z', '[a-c]', '[^b]', '[\\w-]', '[\\d-z]', '[]', '[^]', '[[]', '[a--b]', '(', ')',
      '(?:', '(?=', '(?!', '(?<=', '(?<!', '(?<n>', '|', '*', '+', '?', '{2}', '{1,3}', '{,3}', '{', '}', ']', '^', '$', '"',
      "'"];
    const sources = new Set<string>();
    for (const a of pieces) {
      for (const b of pieces) {
        for (const c of ['', 'x', ')', '|y', '+', '{2}']) sources.add(a + b + c);
      }
    }
    for (const extra of ['(?<=a{2})b', '(?<=a{2,3})b', '(?<=[ab])c', '(?<=.)c', '(?<=(a|b))c', '(?<=(ab|c))d', '(?<=(?:)*)a',
      '(?<=\\b)a', '(?<=^)a', '(?<=(?=x)a)b', '(?<=a?)b', '(?<=a*)b', '(?<=😀)b', '(?<=\\x41\\u00e9)b', '[\\x41-\\x5a]',
      '[\\n-\\r]', '[z-a]', '[a-\\d]', '[-a]', '[a-]', '[\\b-c]', '[\\B]', '(?:a{2}){3}', 'a{2}{3}', 'a**', 'a*?', 'a??',
      'a+?+', 'x{99999}', 'x{100001}', 'x{5,2}', '(?i:a)', '(?i)a']) {
      sources.add(extra);
    }
    // Each as the RegExp's own source gives it ('/' escaped), which is what a key holds.
    const js = [...new Set([...sources].map((s) => {
      try {
        return new RegExp(s).source;
      } catch {
        return null;
      }
    }).filter((s): s is string => s !== null && s !== '(?:)'))];
    const kept = js.filter((s) => pyReadable(s));
    const keys = kept.map((s) => keyOf('getByText', new RegExp(s, 'is')));
    for (const key of keys) assert.ok(key.startsWith('get_by_text(re.compile('), key);
    const python = runPython<{ compiles: boolean[]; keys: (string | null)[] }>(PY_HEAD + `
import warnings
warnings.simplefilter('error')
args = json.load(sys.stdin)
compiles = []
for source in args['js']:
    try:
        re.compile(source, re.I | re.S)
        compiles.append(True)
    except Exception:
        compiles.append(False)
keys = []
for key in args['keys']:
    call = parse(key)
    keys.append(canonical(call) if call else None)
print(json.dumps({'compiles': compiles, 'keys': keys}, ensure_ascii=False))`, { js, keys });
    // Every source kept here compiles in Python, with no warning.
    js.forEach((source, i) => {
      if (pyReadable(source)) assert.ok(python.compiles[i], 'kept, but Python refuses: ' + JSON.stringify(source));
    });
    keys.forEach((key, i) => assert.equal(python.keys[i], key, kept[i]));
    // And most of what both read is kept: a refusal here is a call that is not healed.
    const both = js.filter((s, i) => python.compiles[i]);
    assert.ok(kept.length > both.length * 0.6, kept.length + ' kept of ' + both.length);
    for (const s of ['(?<=$)\\d+', '(?<=ab|cd)x', '(?<=a{2})b', '[\\w-]', 'a*?', '(?<=(?:)*)a', '\\x41\\u00e9']) {
      assert.ok(pyReadable(s), s);
    }
    for (const s of ['(?<name>x)', '(?<=a+)b', '(?<=a|bc)b', '\\k<n>', '\\cA', '\\p{L}', '[^]', '[]a]', '(a)\\1', '{,3}',
      '[[a]', '[a--b]', '(?i:a)', '\\a', '\\Z']) {
      assert.ok(!pyReadable(s), s);
    }
  });

  test('the messages are the Python package\'s', () => {
    const python = runPython<string[]>(`
import json
from mendwright import getby
call = getby.parse('get_by_role("heading", name="Cart", level=2)')
print(json.dumps([getby.role_mismatch(call), getby.through_message('get_by_role("a")', getby.HIDDEN),
                  getby.role_unknown_message('k'), getby.not_healed_message('page.x()', getby.TYPE_REASON),
                  getby.ROLE_UNREADABLE, getby.REJECTED, getby.STARTED_TO_MATCH, getby.EMPTY_REASON.format('name'),
                  getby.TYPE_REASON, getby.SURROGATE_REASON, getby.BIND_REASON, getby.LONG_REASON,
                  getby.no_fingerprint_message('k'), getby.NO_MODEL, getby.NOT_FOLLOWED]))`);
    const call = parse('get_by_role("heading", name="Cart", level=2)') as GetByCall;
    // The Python package's words, but for its names: getBy* where it says get_by_*.
    const ts = (text: string) => text.replace(/get_by_\*/g, 'getBy*');
    // But for NO_MODEL: since 0.11.0 the Python package can let a model heal a call
    // (heal_get_by='model'), and its sentence says so. This package heals a call from its
    // fingerprint only, so its sentence keeps the reason no model is asked, and offers none.
    const pythonNoModel = python.splice(13, 1)[0];
    const shared = 'asks no model to heal a get_by_* call: the call names its element by role and words, and those '
      + 'are what changed';
    assert.ok(pythonNoModel.includes(shared), pythonNoModel);
    assert.ok(NO_MODEL.startsWith('mendwright ' + ts(shared) + ', so a model could only guess. Change the call by hand.'));
    assert.ok(!/model'|healGetBy/.test(NO_MODEL), NO_MODEL);
    assert.deepEqual([roleMismatch(call), throughMessage('get_by_role("a")', 'matches 1 hidden element'),
      roleUnknownMessage('k'), notHealedMessage('page.x()', TYPE_REASON), ROLE_UNREADABLE, 'was rejected by the browser',
      'started to match something other than one visible element', emptyReason('name'), TYPE_REASON, SURROGATE_REASON,
      BIND_REASON, LONG_REASON, noFingerprintMessage('k'), NOT_FOLLOWED], python.map(ts));
  });
});
