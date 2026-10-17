// The TypeScript port against the Python package it ports, run from this repository: the
// same text ratios, fingerprints, scores, XPaths, page paths, roles, prompt and cookie
// selectors for the same input.

import { strict as assert } from 'node:assert';
import { describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { ratio } from '../../src/difflib';
import { fromRaw, generatedId, hrefPath, sameText, score, stableClasses, textRatio } from '../../src/fingerprint';
import { pagePath, pageUrl } from '../../src/flow';
import { presetSelectors } from '../../src/interruptions';
import { buildPrompt, notHereReason, parseProposal } from '../../src/prompt';
import { inferRole, roleChoices } from '../../src/roles';
import { classify, hasPosition, isBrittle, normalize } from '../../src/selectors';
import { needsPython, runPython } from '../support/python';

const skip = needsPython();

function libIn(html: string) {
  const dom = new JSDOM(html, { url: 'https://shop.test/x', runScripts: 'outside-only' });
  const lib = dom.window.eval('(' + mendwrightLib.toString() + ')()') as ReturnType<typeof mendwrightLib>;
  return { dom, lib, doc: dom.window.document };
}

const TEXT_PAIRS: [string, string][] = [
  ['Save', 'Save'], ['Save', 'save changes'], ['Delete file 1', 'Delete file 2'], ['Log in', 'Login'],
  ['Accept the terms', 'Accept terms'], ['Continue to payment', 'Continue to shipping'], ['', 'x'],
  ['Subscribe to our newsletter', 'Subscribe to the newsletter'], ['abcabcabc', 'cbacbacba'],
  ['Email address', 'E-mail adress'], ['Проверить заказ', 'Проверить заказы'], ['naïve café', 'naive cafe'],
  ['emoji \u{1F600} here', 'emoji here'], ['aaaaaaaaaaaaaaaaaaaaaaaaaab', 'baaaaaaaaaaaaaaaaaaaaaaaaaa'],
  ['Remove item from cart', 'Remove from cart'], ['Next', 'Previous'], ['Sign up', 'Sign in'],
];

describe('parity with the Python package', { skip }, () => {
  test('difflib ratio and the text rules', () => {
    const py = runPython<{ ratio: number[]; lower: number[]; same: boolean[] }>(`
import json, sys, difflib
from mendwright.fingerprint import _ratio, _same_text
pairs = json.load(sys.stdin)
print(json.dumps({
  'ratio': [difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() for a, b in pairs],
  'lower': [_ratio(a, b) for a, b in pairs],
  'same': [_same_text(a, b, _ratio(a, b)) for a, b in pairs],
}))`, TEXT_PAIRS);
    TEXT_PAIRS.forEach(([a, b], i) => {
      assert.equal(ratio(a, b), py.ratio[i], 'ratio ' + JSON.stringify([a, b]));
      assert.equal(textRatio(a, b), py.lower[i], 'textRatio ' + JSON.stringify([a, b]));
      assert.equal(sameText(a, b, textRatio(a, b)), py.same[i], 'sameText ' + JSON.stringify([a, b]));
    });
  });

  test('class tokens, generated ids and link paths', () => {
    const classes = ['btn btn-primary active', 'css-1x2y3 Header_link__a1B2c nav-link', 'sc-bdVaJa jsx-123 card',
      'ng-touched ng-dirty field', 'x1n2onr6 save-button', 'is-open dropdown__menu', 'Button_root__uIoob big'];
    const ids = [':r3:', 'mui-generated-8842', 'ember123', 'x1n2onr6', 'lastName', 'email', 'user_42', 'step-3'];
    const hrefs = ['/cart/', 'https://shop.test/orders?id=3#top', '#', 'javascript:void(0)', 'cart.html', '//cdn.test/a/b/', '/'];
    const py = runPython<{ classes: string[][]; ids: boolean[]; hrefs: string[] }>(`
import json, sys
from mendwright.fingerprint import stable_classes, generated_id, _href_path
data = json.load(sys.stdin)
print(json.dumps({'classes': [stable_classes(c) for c in data['classes']], 'ids': [generated_id(i) for i in data['ids']],
                  'hrefs': [_href_path(h) for h in data['hrefs']]}))`, { classes, ids, hrefs });
    const { lib } = libIn('<html><body></body></html>');
    assert.deepEqual(classes.map(stableClasses), py.classes);
    assert.deepEqual(JSON.parse(JSON.stringify(classes.map((c) => lib.stableClasses(c)))), py.classes);
    assert.deepEqual(ids.map(generatedId), py.ids);
    assert.deepEqual(ids.map((i) => lib.generatedId(i)), py.ids);
    assert.deepEqual(hrefs.map(hrefPath), py.hrefs);
  });

  const PAGE = `<html><head><title>Shop</title></head><body>
    <header id="top"><nav><a href="/cart/" class="nav-link">Cart (2)</a><a href="/help">Help</a></nav></header>
    <main id="content">
      <form id="signup">
        <label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="email">
        <label>Password <input type="password" name="pw"></label>
        <label><input type="checkbox" id="terms" data-testid="terms"> I accept the terms</label>
        <button type="submit" class="btn btn-primary css-1x2y3">Create account</button>
        <button type="button" aria-label="Show password" id=":r1:"></button>
      </form>
      <section id="offers"><h2>Offers</h2>
        <ul><li><span>Canvas cap</span><button class="add">Add to cart</button></li>
            <li><span>Wool scarf</span><button class="add">Add to cart</button></li></ul>
      </section>
      <div style="display:none"><button>Create account</button></div>
      <input type="hidden" name="csrf" value="x">
    </main></body></html>`;

  test('fingerprints, scores and XPaths of a page', () => {
    const { doc, lib } = libIn(PAGE);
    const roles = ['button', 'input', 'checkbox', 'link'];
    const ours: Record<string, unknown[]> = {};
    for (const role of roles) {
      const els = Array.from(doc.getElementsByTagName('*')).filter((el) => lib.roleOf(el) === role);
      ours[role] = els.map((el) => ({ fp: fromRaw(lib.rawOf(el)), xpath: lib.xpathFor(el), hidden: lib.staticallyHidden(el) }));
    }
    const py = runPython<Record<string, { fp: unknown; xpath: string | null; hidden: boolean }[]>>(`
import json, sys
import lxml.html
from mendwright.fingerprint import of_element, xpath_for, statically_hidden
from mendwright.validation import _role_of
data = json.load(sys.stdin)
root = lxml.html.fromstring(data['html'])
out = {}
for role in data['roles']:
    out[role] = [{'fp': of_element(el), 'xpath': xpath_for(el), 'hidden': statically_hidden(el)}
                 for el in root.iter() if isinstance(el.tag, str) and _role_of(el) == role]
print(json.dumps(out))`, { html: PAGE, roles });
    assert.deepEqual(ours, py);

    // Every fingerprint scored against every element of its role, both ways.
    const pairs: [unknown, unknown][] = [];
    for (const role of roles) for (const a of py[role]) for (const b of py[role]) pairs.push([a.fp, b.fp]);
    const scores = runPython<[number, boolean][]>(`
import json, sys
from mendwright.fingerprint import score
print(json.dumps([[s.value, s.strong] for s in (score(a, b) for a, b in json.load(sys.stdin))]))`, pairs);
    pairs.forEach(([a, b], i) => {
      const s = score(a as never, b as never);
      assert.deepEqual([s.value, s.strong], scores[i], 'score ' + i);
    });
  });

  test('page paths and addresses', () => {
    const urls = ['https://shop.example/Orders/10482/?tab=2', 'https://shop.example/orders/507f1f77bcf86cd799439011',
      'http://localhost:3000/signup/', 'about:blank', 'data:text/html,x', 'https://shop.test/', 'file:///C:/x/v1.html',
      'https://user:pw@Shop.Test:443/a//b', 'https://shop.test:8443/reset/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.x',
      'https://shop.test/u/ada@example.com/edit', 'https://[::1]:8080/x', 'https://shop.test/step-3/2fa/v2',
      'https://shop.test/a/01HZX3KQ4M8N2P7R5T9V0W6Y1B', 'not a url', ''];
    const py = runPython<[string | null, string | null][]>(`
import json, sys
from mendwright.flow import page_path, page_url
print(json.dumps([[page_path(u), page_url(u)] for u in json.load(sys.stdin)]))`, urls);
    assert.deepEqual(urls.map((u) => [pagePath(u), pageUrl(u)]), py);
  });

  test('selector kinds, roles and positions', () => {
    const selectors = ['#save', '//button[@id="save"]', 'xpath=//a', 'css=#x', 'text=Save', "role=button[name='Save']",
      '//form >> text=Save', '(//input)[2]', './/a', 'id("main")//button', "//input[@type='checkbox']", 'select#size',
      "input[type='radio']", '//*[@role="switch"]', '/html[1]/body[1]/div[3]/input[1]', '//li[last()]',
      "//a[contains(., '[2]')]", 'my-widget.big', '//div[@role="textbox"]', 'textarea', '//x | //y'];
    const roles = ['button', 'textbox', 'switch', 'heading', 'h1', 'my-widget', 'listbox', 'a', 'div'];
    const py = runPython<{ kinds: string[]; normal: string[]; infer: string[]; choices: string[][]; position: boolean[];
                           brittle: boolean[] }>(`
import json, sys
from mendwright.selectors import classify, normalize
from mendwright.roles import infer_role, role_choices
from mendwright.fit import has_position
from mendwright.validation import _is_brittle
data = json.load(sys.stdin)
s, r = data['selectors'], data['roles']
print(json.dumps({'kinds': [classify(x) for x in s], 'normal': [normalize(x) for x in s], 'infer': [infer_role(x) for x in s],
                  'choices': [list(role_choices(x)) for x in r], 'position': [has_position(x) for x in s],
                  'brittle': [_is_brittle(x) for x in s]}))`, { selectors, roles });
    assert.deepEqual(selectors.map(classify), py.kinds);
    assert.deepEqual(selectors.map(normalize), py.normal);
    assert.deepEqual(selectors.map(inferRole), py.infer);
    assert.deepEqual(roles.map(roleChoices), py.choices);
    assert.deepEqual(selectors.map(hasPosition), py.position);
    assert.deepEqual(selectors.map(isBrittle), py.brittle);
  });

  test('the prompt and the reading of a reply', () => {
    const bundle = {
      key: 'terms', expectedRole: 'checkbox', expectedText: ['I accept', 'Akzeptieren ü'], intent: 'the terms box',
      destructive: false, candidates: ['#accept-terms', "//input[@id='terms']"],
      inventory: [{ tag: 'input', role: 'checkbox', id: 'terms', name: '', text: '', aria_label: 'Terms ✓' }],
      html: '<html><body><input type="checkbox" id="terms-v2"></body></html>', url: 'https://shop.test/signup',
      page: '/signup', lastPage: '/signup', stage: null, version: null,
      rejectedXpath: "//input[@id='x']", rejectReason: 'no match',
    };
    const replies = ['{"xpath": "//a", "fallbacks": ["//b", 3], "reasoning": "r"}', 'sure: {"xpath": null, "reason": "gone"} ok',
      'no json', '{"xpath": ""}', '[1, 2]'];
    const py = runPython<{ prompt: { system: string; user: string }; destructive: { system: string; user: string };
                           parsed: unknown[]; reasons: unknown[] }>(`
import json, sys
from mendwright.prompt import build_prompt, parse_proposal, not_here_reason
data = json.load(sys.stdin)
b = data['bundle']
print(json.dumps({'prompt': build_prompt(b), 'destructive': build_prompt(dict(b, destructive=True)),
                  'parsed': [parse_proposal(t) for t in data['replies']], 'reasons': [not_here_reason(t) for t in data['replies']]}))`,
    { bundle, replies });
    assert.deepEqual(buildPrompt(bundle), py.prompt);
    assert.deepEqual(buildPrompt({ ...bundle, destructive: true }), py.destructive);
    assert.deepEqual(replies.map(parseProposal), py.parsed);
    assert.deepEqual(replies.map(notHereReason), py.reasons);
  });

  test('the cookies preset selectors', () => {
    const py = runPython<{ reject: string[]; accept: string[] }>(`
import json
from mendwright.interruptions import preset_selectors
print(json.dumps({'reject': list(preset_selectors('reject')), 'accept': list(preset_selectors('accept'))}))`);
    assert.deepEqual(presetSelectors('reject'), py.reject);
    assert.deepEqual(presetSelectors('accept'), py.accept);
  });
});
