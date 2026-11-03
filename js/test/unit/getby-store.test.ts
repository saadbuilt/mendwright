// One store shared with the Python package for heal.page.getBy*() calls: a fingerprint this
// package recorded under a call's key heals the same call in the Python package, a heal this
// package saved answers it there, and a fingerprint the Python package recorded heals the call
// here. The journal lines of a call's heal are those of any fingerprint heal, field for field,
// in both packages.
//
// The Python package runs its own heal.page.get_by_role() with open(page, heal_get_by=True), on an
// lxml page with the shape of a sync Playwright page, cut to what the lookup and the heal of one
// call use: it needs only lxml and mendwright.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import { open } from '../../src/healer';
import type { HealProvider } from '../../src/providers/base';
import { cleanEnv, journalOf, world, type World } from '../support/env';
import { needsPython, runPython } from '../support/python';

const KEY = 'get_by_role("button", name="Sign in")';
const HEAL = "//button[@id='login']";
const PAGE = "<html><body><main><form id='login-form'><label for='email'>Email</label>"
  + "<input id='email' name='email' type='email'>"
  + "<button id='login' type='submit' class='primary'>Sign in</button></form>"
  + "<a href='/help'>Help</a></main></body></html>";
const RENAMED = PAGE.replace('>Sign in<', '>Log in<');
const URL = 'https://shop.test/login';

class Refusing implements HealProvider {
  asked = 0;

  async propose(): Promise<null> {
    this.asked += 1;
    throw new Error('the model was asked');
  }
}

const PY_PAGE = `
import json, re, sys
import lxml.html
import mendwright
from mendwright.drivers.base import VISIBLE_MATCH_JS
from mendwright.fingerprint import FINGERPRINT_JS, raw_of_element

URL = 'https://shop.test/login'

def hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None or el.get('aria-hidden') == 'true':
            return True
        el = el.getparent()
    return False

def norm(text):
    return ' '.join((text or '').split())

def role_of(el):
    role = (el.get('role') or '').split()
    if role:
        return role[0]
    if el.tag == 'button':
        return 'button'
    if el.tag == 'a' and el.get('href') is not None:
        return 'link'
    if el.tag == 'input':
        kind = (el.get('type') or 'text').lower()
        return 'button' if kind in ('button', 'submit', 'reset') else kind if kind in ('checkbox', 'radio') else 'textbox'
    return None

def matches(wanted, text, exact):
    if isinstance(wanted, re.Pattern):
        return wanted.search(text) is not None
    return norm(text) == norm(wanted) if exact else norm(wanted).lower() in norm(text).lower()

class Locator:
    def __init__(self, page, find, name):
        self.page, self.find, self.name = page, find, name
    def count(self):
        return len(self.find())
    def is_visible(self, **kwargs):
        els = self.find()
        return len(els) == 1 and not hidden(els[0])
    def scroll_into_view_if_needed(self, **kwargs):
        pass
    def locator(self, selector):
        assert selector == 'visible=true'
        return Locator(self.page, lambda: [e for e in self.find() if not hidden(e)], self.name)
    def and_(self, other):
        return Locator(self.page, lambda: [e for e in self.find() if any(e is o for o in other.find())], self.name)
    def evaluate_all(self, js, arg=None):
        els = self.find()
        html = lambda el: lxml.html.tostring(el, encoding='unicode', with_tail=False)
        if js == VISIBLE_MATCH_JS:
            return {'tag': els[0].tag, 'visible': min(len(els), 2), 'html': html(els[0])} if els else None
        if len(els) != 1:
            return None
        if js == FINGERPRINT_JS:
            return raw_of_element(els[0])
        return {'tag': els[0].tag, 'html': html(els[0])}
    def click(self, **kwargs):
        if self.count() != 1:
            raise RuntimeError('Locator.click: strict mode violation or no element: ' + self.name)
        self.page.clicked.append(self.name)

class Page:
    def __init__(self, html):
        self.url, self.clicked = URL, []
        self.doc = lxml.html.document_fromstring(html)
    def locator(self, selector):
        body = selector[len('xpath='):] if selector.startswith('xpath=') else selector
        def find():
            try:
                return [e for e in self.doc.xpath(body) if isinstance(getattr(e, 'tag', None), str)]
            except Exception:
                return []
        return Locator(self, find, selector)
    def frame_locator(self, selector):
        return self
    def get_by_role(self, role, name=None, exact=None, level=None, **kwargs):
        def find():
            return [e for e in self.doc.iter() if isinstance(e.tag, str) and role_of(e) == role and not hidden(e)
                    and (name is None or matches(name, norm(e.get('aria-label') or e.text_content()), exact))]
        return Locator(self, find, 'get_by_role(' + repr(role) + ', name=' + repr(name) + ')')
    def content(self):
        return lxml.html.tostring(self.doc, encoding='unicode')
    def evaluate(self, js, arg=None):
        return []

class Refusing:
    def __init__(self):
        self.asked = 0
    def propose(self, bundle):
        self.asked += 1
        raise AssertionError('the model was asked')

args = json.load(sys.stdin)

def journal():
    import os
    path = os.path.join(os.path.dirname(args['store']), 'journal.jsonl')
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path, encoding='utf-8').read().splitlines()]
`;

beforeEach(cleanEnv);

function login(html = PAGE): World {
  return world(html, { url: URL });
}

/** A journal event without what differs from run to run. */
function steady(event: Record<string, unknown>): Record<string, unknown> {
  const { ts: _ts, run: _run, latency_ms: _latency, ...rest } = event;
  return rest;
}

describe('one store shared with the Python package for getBy* calls', { skip: needsPython() }, () => {
  test("a fingerprint this package recorded under a call's key heals the same call in the Python package", async () => {
    const w = login();
    await w.open({ healGetBy: true, provider: new Refusing() }).page.getByRole('button', { name: 'Sign in' }).click();
    // The store as this package wrote it, for the same heal here below.
    const here = login(PAGE);
    fs.copyFileSync(w.storePath, here.storePath);
    const py = runPython<{ clicked: string[]; heals: [string, string[], string, string][]; asked: number; rows: string[][];
      events: Record<string, unknown>[] }>(PY_PAGE + `
page = Page(args['renamed'])
provider = Refusing()
heal = mendwright.open(page, provider=provider, store_path=args['store'], heal_get_by=True, timeout_ms=0)
heal.coordinator.log = lambda message: None
heal.page.get_by_role('button', name='Sign in').click()
rows = [[c['xpath'], c['source']] for c in heal.store.candidates(args['key'])]
print(json.dumps({'clicked': page.clicked, 'heals': [[h.key, list(h.old), h.new, h.source] for h in heal.heals],
                  'asked': provider.asked, 'rows': rows, 'events': journal()}))`,
    { store: w.storePath, renamed: RENAMED, key: KEY });
    assert.deepEqual(py.heals, [[KEY, [KEY], HEAL, 'fingerprint']]);
    assert.deepEqual(py.clicked, ['xpath=' + HEAL]);
    assert.equal(py.asked, 0);
    assert.deepEqual(py.rows, [[KEY, 'code'], [HEAL, 'heal']]);
    // This package heals the same call from the same fingerprint, with the same journal lines.
    here.page.goto(RENAMED, URL);
    const heal = here.open({ healGetBy: true, provider: new Refusing() });
    await heal.page.getByRole('button', { name: 'Sign in' }).click();
    assert.deepEqual(heal.heals.map((h) => [h.key, h.old, h.new, h.source]), py.heals);
    assert.deepEqual(py.events.map((e) => e.event), ['heal.requested', 'heal.proposed', 'heal.applied']);
    assert.deepEqual(journalOf(here.dir).map(steady), py.events.map(steady));
  });

  test('a heal this package saved for a call answers it in the Python package, at once', async () => {
    const w = login();
    const heal = w.open({ healGetBy: true, provider: new Refusing() });
    await heal.page.getByRole('button', { name: 'Sign in' }).click();
    w.page.goto(RENAMED, URL);
    await heal.page.getByRole('button', { name: 'Sign in' }).click();
    heal.confirmAll(true);
    assert.deepEqual(heal.heals.map((h) => h.new), [HEAL]);
    const py = runPython<{ clicked: string[]; heals: number; asked: number; status: string }>(PY_PAGE + `
page = Page(args['renamed'])
provider = Refusing()
heal = mendwright.open(page, provider=provider, store_path=args['store'], heal_get_by=True, timeout_ms=0)
heal.page.get_by_role('button', name='Sign in').click()
print(json.dumps({'clicked': page.clicked, 'heals': len(heal.heals), 'asked': provider.asked,
                  'status': heal.store.status(args['key'], args['heal'])}))`,
    { store: w.storePath, renamed: RENAMED, key: KEY, heal: HEAL });
    assert.deepEqual(py, { clicked: ['xpath=' + HEAL], heals: 0, asked: 0, status: 'active' });
  });

  test("a fingerprint the Python package recorded heals the call here, with the Python package's journal lines", async () => {
    const w = login(RENAMED);
    const py = runPython<{ clicked: string[]; events: Record<string, unknown>[]; renamed: { heals: string[][];
      events: Record<string, unknown>[] } }>(PY_PAGE + `
page = Page(args['page'])
heal = mendwright.open(page, provider=Refusing(), store_path=args['store'], heal_get_by=True, timeout_ms=0)
heal.page.get_by_role('button', name='Sign in').click()
recorded = journal()
# The same heal, in the Python package, on a copy of the store, for its journal lines.
import os, shutil, tempfile
copy = os.path.join(tempfile.mkdtemp(), 'overrides.json')
shutil.copyfile(args['store'], copy)
again = mendwright.open(Page(args['renamed']), provider=Refusing(), store_path=copy, heal_get_by=True, timeout_ms=0)
again.coordinator.log = lambda message: None
again.page.get_by_role('button', name='Sign in').click()
lines = [json.loads(l) for l in open(os.path.join(os.path.dirname(copy), 'journal.jsonl'), encoding='utf-8').read().splitlines()]
print(json.dumps({'clicked': page.clicked, 'events': recorded,
                  'renamed': {'heals': [[h.key, h.new, h.source] for h in again.heals], 'events': lines}}))`,
    { store: w.storePath, page: PAGE, renamed: RENAMED });
    assert.deepEqual(py.clicked, ["get_by_role('button', name='Sign in')"]);
    assert.deepEqual(py.events, []);
    const entry = JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors[KEY];
    assert.equal(entry.fingerprint.attrs.id, 'login');
    assert.equal(entry.expectedRole, 'button');
    const model = new Refusing();
    const heal = open(w.page as never, { storePath: w.storePath, timeoutMs: 100, healGetBy: true, provider: model,
      log: () => undefined });
    await heal.page.getByRole('button', { name: 'Sign in' }).click();
    assert.deepEqual(heal.heals.map((h) => [h.key, h.new, h.source]), py.renamed.heals);
    assert.deepEqual(w.page.actions.at(-1), ['click', 'xpath=' + HEAL]);
    assert.equal(model.asked, 0);
    assert.deepEqual(py.renamed.events.map((e) => e.event), ['heal.requested', 'heal.proposed', 'heal.applied']);
    assert.deepEqual(journalOf(w.dir).map(steady), py.renamed.events.map(steady));
  });
});
