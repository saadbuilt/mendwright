"""describe(selector) and inventory(limit) on both drivers. describe's role and text must
agree with what the gate computes, and neither may leak input values."""

import lxml.html
import pytest

from mendwright import InvalidSelector, validate
from mendwright.drivers.base import (
    DESCRIBE_JS,
    INVENTORY_JS,
    OUTER_HTML_LIMIT,
    element_summary,
    inventory_rows,
    role_for,
)
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.validation import _role_of


# --- Playwright fakes -------------------------------------------------------------------

class Loc:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def evaluate_all(self, js):
        assert js == DESCRIBE_JS
        if self.selector in self.page.errors:
            raise self.page.errors[self.selector]
        els = self.page.elements.get(self.selector, [])
        if len(els) != 1:
            return None
        tag, html = els[0]
        return {'tag': tag, 'html': html}


class Page:
    url = 'https://example.test/'

    def __init__(self, elements=None, errors=None, inventory=None):
        self.elements = elements or {}
        self.errors = errors or {}
        self.inventory_raw = inventory
        self.evaluated = []

    def locator(self, selector):
        return Loc(self, selector)

    def frame_locator(self, selector):
        return self

    def content(self):
        return ''

    def evaluate(self, js, arg):
        self.evaluated.append((js, arg))
        if isinstance(self.inventory_raw, Exception):
            raise self.inventory_raw
        return self.inventory_raw


def _pw(tag, html, selector='css=#x'):
    return PlaywrightSyncDriver(Page({selector: [(tag, html)]}))


# --- describe ---------------------------------------------------------------------------

def test_describe_checkbox_and_button():
    d = _pw('input', '<input type="checkbox" id="x">')
    assert d.describe('#x') == {'tag': 'input', 'role': 'checkbox', 'text': '',
                                'outer_html': '<input type="checkbox" id="x">'}
    d = _pw('button', '<button id="x">  Pay\n   now </button>')
    out = d.describe('#x')
    assert out['role'] == 'button' and out['text'] == 'Pay now'


@pytest.mark.parametrize('tag, html', [
    ('a', '<a href="/docs">Docs</a>'),
    ('div', '<div role="checkbox" aria-checked="false">x</div>'),
    ('select', '<select id="c"><option>One</option></select>'),
    ('input', '<input type="submit" value="Go">'),
    ('input', '<input type="radio" name="r">'),
    ('input', '<input name="q">'),
    ('textarea', '<textarea>notes</textarea>'),
    ('td', '<td class="cell">1</td>'),
    ('option', '<option value="1">One</option>'),
    ('li', '<li role="link">Next</li>'),
])
def test_describe_role_is_the_gates_role(tag, html):
    expected = _role_of(lxml.html.fragment_fromstring(html))
    assert _pw(tag, html).describe('#x')['role'] == expected


def test_describe_handles_document_level_elements():
    assert _pw('body', '<body class="b"><p>x</p></body>').describe('#x')['role'] == 'body'
    assert _pw('html', '<html><body>x</body></html>').describe('#x')['role'] == 'html'


def test_describe_text_matches_what_the_gate_checks_for_destructive_keys():
    page_html = '<form><button id="pay">\n  Pay <b>now</b>\n</button></form>'
    btn = lxml.html.tostring(lxml.html.fromstring(page_html).xpath('//button')[0],
                             encoding='unicode', with_tail=False)
    out = _pw('button', btn, selector="xpath=//button[@id='pay']").describe("//button[@id='pay']")
    result = validate("//button[@id='pay']", page_html, expected_role=out['role'],
                      expected_text=[out['text']], destructive=True)
    assert result.ok, result.reason


def test_describe_drops_value_attributes_and_cuts_long_html():
    out = _pw('input', '<input id="x" value="hunter2" type="text">').describe('#x')
    assert 'hunter2' not in out['outer_html']
    long_html = '<div id="x">' + 'y' * 5000 + '</div>'
    out = _pw('div', long_html).describe('#x')
    assert len(out['outer_html']) == OUTER_HTML_LIMIT
    assert len(out['text']) == 200


def test_textarea_reports_no_text():
    assert _pw('textarea', '<textarea id="x">my notes</textarea>').describe('#x')['text'] == ''


def test_describe_is_none_unless_exactly_one_match():
    page = Page({'css=#none': [], 'css=#two': [('b', '<b>1</b>'), ('b', '<b>2</b>')]})
    d = PlaywrightSyncDriver(page)
    assert d.describe('#none') is None
    assert d.describe('#two') is None


def test_describe_bad_selector_raises_other_errors_give_none():
    with pytest.raises(InvalidSelector):
        PlaywrightSyncDriver(Page()).describe('//div[')

    class PwError(Exception):
        pass
    d = PlaywrightSyncDriver(Page(errors={'css=#x': PwError('Frame was detached')}))
    assert d.describe('#x') is None
    bad = PwError('Locator.evaluateAll: Unexpected token "!" while parsing css selector "#x!"')
    with pytest.raises(InvalidSelector):
        PlaywrightSyncDriver(Page(errors={'css=#x!': bad})).describe('#x!')


def test_element_summary_falls_back_when_html_does_not_parse():
    out = element_summary('button', '')
    assert out['role'] == 'button' and out['text'] == ''


# --- Selenium describe ------------------------------------------------------------------

class SelEl:
    def __init__(self, tag, html):
        self.tag, self.html = tag, html


class SelDriver:
    def __init__(self, found=None, raw_inventory=None):
        self.found = found or {}
        self.raw_inventory = raw_inventory
        self.switch_to = self
        self.scripts = []
        self.inside = False

    def find_elements(self, by, value):
        return list(self.found.get(('in' if self.inside else 'top', value), []))

    def frame(self, el):
        self.inside = True

    def default_content(self):
        self.inside = False

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if 'outerHTML' in script:
            el = args[0]
            return {'tag': el.tag, 'html': el.html}
        if isinstance(self.raw_inventory, Exception):
            raise self.raw_inventory
        return self.raw_inventory


def test_selenium_describe_top_level_and_in_a_frame():
    cb = SelEl('input', '<input type="checkbox" id="t" value="on">')
    drv = SelDriver(found={('top', '#t'): [cb], ('top', '#f'): [SelEl('iframe', '')],
                           ('in', '#t'): [cb], ('top', '//b'): [cb, cb]})
    d = SeleniumDriver(drv)
    assert d.describe('#t') == {'tag': 'input', 'role': 'checkbox', 'text': '',
                                'outer_html': '<input type="checkbox" id="t">'}
    assert d.describe('#t', frame_chain=('#f',))['role'] == 'checkbox'
    assert drv.inside is False
    assert d.describe('//b') is None
    assert d.describe('#missing') is None


def test_selenium_describe_rejects_engine_selectors():
    with pytest.raises(InvalidSelector):
        SeleniumDriver(SelDriver()).describe('text=Save')


# --- inventory --------------------------------------------------------------------------

RAW = [
    {'tag': 'button', 'role': '', 'type': 'submit', 'id': 'go', 'name': '', 'text': 'Go  on',
     'aria_label': ''},
    {'tag': 'input', 'role': '', 'type': 'checkbox', 'id': 'terms', 'name': 'terms', 'text': '',
     'aria_label': 'Accept', 'value': 'secret'},
    {'tag': 'div', 'role': 'combobox', 'type': '', 'id': '', 'name': '', 'text': '',
     'aria_label': ''},
    {'tag': 'a', 'role': '', 'type': '', 'id': '', 'name': '', 'text': 'x' * 300,
     'aria_label': ''},
    'not a dict',
]
KEYS = {'tag', 'role', 'id', 'name', 'text', 'aria_label'}


def test_inventory_rows_shape_roles_and_caps():
    rows = inventory_rows(RAW)
    assert [set(r) for r in rows] == [KEYS] * 4
    assert [r['role'] for r in rows] == ['button', 'checkbox', 'combobox', 'link']
    assert rows[0]['text'] == 'Go on'
    assert len(rows[3]['text']) == 80
    assert all('secret' not in str(r) for r in rows)
    assert inventory_rows(RAW, limit=2) == rows[:2]
    assert inventory_rows(None) == [] and inventory_rows({'a': 1}) == []


def test_role_for_matches_the_gate():
    assert role_for('input', '', 'radio') == 'radio'
    assert role_for('span', 'button') == 'button'
    assert role_for('select') == 'combobox'
    assert role_for('my-widget') == 'my-widget'


def test_playwright_inventory_is_one_evaluate_call():
    page = Page(inventory=RAW)
    rows = PlaywrightSyncDriver(page).inventory(limit=50)
    assert len(rows) == 4
    assert page.evaluated == [(INVENTORY_JS, 50)]


def test_selenium_inventory_is_one_execute_script_call():
    drv = SelDriver(raw_inventory=RAW)
    rows = SeleniumDriver(drv).inventory(limit=3)
    assert len(rows) == 3
    (script, args), = drv.scripts
    assert script.startswith('return (') and INVENTORY_JS in script and args == (3,)


def test_inventory_is_empty_on_any_error():
    assert PlaywrightSyncDriver(Page(inventory=RuntimeError('closed'))).inventory() == []
    assert SeleniumDriver(SelDriver(raw_inventory=RuntimeError('gone'))).inventory() == []
    assert PlaywrightSyncDriver(Page(inventory='nope')).inventory() == []


def test_inventory_script_never_reads_values():
    assert '.value' not in INVENTORY_JS
    assert "type === 'hidden'" in INVENTORY_JS
