"""A healed key whose code uses CSS also gets a CSS selector for the healed element
(mendwright.css), checked on the captured page and the live one, and `mendwright fix`
writes that one where your code has CSS, including where only CSS runs."""

import json

import lxml.html
import pytest

import mendwright
from mendwright import cli
from mendwright.client import apply, css_twin
from mendwright.css import css_selectors, css_string

PAGE = """<html><body>
<form id="checkout"><label>Code <input name="coupon" id="coupon-code"></label>
  <button type="submit" class="btn primary">Apply</button></form>
<div class="row"><button class="btn">Share</button></div>
<div class="row"><button class="btn">Like</button></div>
<button data-testid="save-btn">Save</button>
<button id="a.b">Dotted</button>
<svg><g id="icon"></g></svg>
</body></html>"""


def _doc():
    return lxml.html.fromstring(PAGE)


@pytest.mark.parametrize('xpath, first', [
    ("//input[@name='coupon']", '#coupon-code'),
    ("//button[@data-testid='save-btn']", '[data-testid="save-btn"]'),
    ("//form//button", 'button[type="submit"]'),
    ("//button[.='Dotted']", '[id="a.b"]'),
])
def test_css_selectors_match_the_element_and_nothing_else(xpath, first):
    doc = _doc()
    el = doc.xpath(xpath)[0]
    found = css_selectors(el, doc)
    assert found and found[0] == first
    cssselect = pytest.importorskip('lxml.cssselect')
    for css in found:     # an independent CSS engine agrees with the XPath twins
        assert cssselect.CSSSelector(css)(doc) == [el], css


def test_an_element_only_its_text_tells_apart_gets_no_css():
    doc = _doc()
    share = doc.xpath("//button[.='Share']")[0]
    assert css_selectors(share, doc) == []


def test_nothing_inside_an_svg():
    doc = _doc()
    assert css_selectors(doc.xpath("//g")[0], doc) == []


def test_css_string_escapes_quotes_and_backslashes():
    assert css_string('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'


class _Store:
    def __init__(self, code):
        self.code = code
        self.added = []

    def candidates(self, key):
        return [{'xpath': self.code, 'source': 'code'}]

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath, kw.get('css')))


class _Live:
    """A live page that runs the twin's CSS through lxml.cssselect."""

    def __init__(self, html, extra=0):
        self.doc = lxml.html.fromstring(html)
        self.extra = extra

    def _find(self, selector):
        if selector.startswith(('/', '(')):
            return self.doc.xpath(selector)
        return pytest.importorskip('lxml.cssselect').CSSSelector(selector)(self.doc)

    def count(self, selector, frame_chain=()):
        found = self._find(selector)
        return len(found) + (self.extra if not selector.startswith(('/', '(')) else 0)

    def is_visible(self, selector, frame_chain=()):
        return True

    def describe(self, selector, frame_chain=()):
        found = self._find(selector)
        if len(found) != 1:
            return None
        return {'tag': found[0].tag, 'role': 'button',
                'outer_html': lxml.html.tostring(found[0], encoding='unicode', with_tail=False)}


META = {'expectedRole': 'button', 'expectedText': [], 'destructive': False}
PROPOSAL = {'xpath': "//button[normalize-space()='Save']", 'fallbacks': []}


def test_a_heal_for_css_code_is_saved_with_its_css_twin():
    store = _Store('#save')
    applied, _ = apply(store, 'save', PROPOSAL, PAGE, META, driver=_Live(PAGE))
    assert applied == PROPOSAL['xpath']
    assert store.added == [('save', PROPOSAL['xpath'], '[data-testid="save-btn"]')]


def test_a_heal_for_xpath_code_gets_no_css():
    store = _Store("//button[@id='save']")
    apply(store, 'save', PROPOSAL, PAGE, META, driver=_Live(PAGE))
    assert store.added == [('save', PROPOSAL['xpath'], None)]


def test_a_twin_that_matches_more_on_the_live_page_is_dropped():
    store = _Store('#save')
    doc = _doc()
    body = PROPOSAL['xpath']
    assert css_twin(store, 'save', doc, body, body, _Live(PAGE)) == '[data-testid="save-btn"]'
    assert css_twin(store, 'save', doc, body, body, _Live(PAGE, extra=1)) is None


def test_the_store_keeps_the_twin_across_runs(tmp_path, make_page, stub_provider):
    page = make_page(PAGE)
    store_path = tmp_path / 'o.json'
    heal = mendwright.open(page, provider=stub_provider(PROPOSAL['xpath']), store_path=str(store_path),
                           fingerprint=False)
    heal.locator('save', 'css=#save', role='button', timeout_ms=0, poll_ms=0)
    saved = json.loads(store_path.read_text(encoding='utf-8'))
    heals = [c for c in saved['selectors']['save']['candidates'] if c['source'] == 'heal']
    # The fake page runs only "#id" CSS, so the live check could not confirm the twin.
    assert heals and 'css' not in heals[0]


# --- mendwright fix ----------------------------------------------------------------------

T0, T1 = "2026-10-01T08:00:00+00:00", "2026-10-02T09:30:00+00:00"
NEW = "//button[normalize-space()='Save']"
TWIN = '[data-testid="save-btn"]'


def _project(root, key, old):
    folder = root / '.mendwright'
    folder.mkdir(parents=True)
    entry = {'expectedRole': 'button', 'candidates': [
        {'xpath': old, 'source': 'code', 'status': 'active', 'addedAt': T0, 'lastSucceededAt': T0},
        {'xpath': NEW, 'source': 'heal', 'status': 'active', 'addedAt': T1, 'lastSucceededAt': T1,
         'css': TWIN}]}
    (folder / 'overrides.json').write_text(json.dumps({'schema': 2, 'selectors': {key: entry}}),
                                           encoding='utf-8')
    events = [{'ts': T1, 'event': 'heal.applied', 'key': key, 'xpath': NEW, 'old': [old]},
              {'ts': T1, 'event': 'heal.canary', 'key': key, 'xpath': NEW, 'success': True}]
    (folder / 'journal.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events),
                                          encoding='utf-8')


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_fix_writes_the_css_twin_where_the_code_has_css(root, capsys):
    _project(root, 'save', '#save')
    (root / 'test_a.py').write_text('def test_a(page):\n    page.click("#save")\n', encoding='utf-8')
    assert cli.main(['fix', '--apply']) == 0
    assert (root / 'test_a.py').read_text(encoding='utf-8') == (
        'def test_a(page):\n    page.click(\'[data-testid="save-btn"]\')\n')
    assert 'now  ' + TWIN in capsys.readouterr().out


def test_fix_writes_css_into_by_css_selector(root, capsys):
    _project(root, 'css selector=#save', '#save')
    source = 'driver.find_element(By.CSS_SELECTOR, "#save").click()\n'
    (root / 'test_b.py').write_text(source, encoding='utf-8')
    assert cli.main(['fix', '--apply']) == 0
    assert "By.CSS_SELECTOR, '[data-testid=\"save-btn\"]')" in (root / 'test_b.py').read_text(
        encoding='utf-8')


def test_fix_keeps_xpath_for_xpath_code(root, capsys):
    _project(root, 'save', "//button[@id='save']")
    (root / 'test_c.py').write_text('page.click("//button[@id=\'save\']")\n', encoding='utf-8')
    assert cli.main(['fix', '--apply']) == 0
    assert NEW in (root / 'test_c.py').read_text(encoding='utf-8')
