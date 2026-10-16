"""Shared fakes for the facade tests: an lxml-backed page with the Playwright sync shape.

FakePage is detected as a Playwright page by mendwright.open(). Its locators count real
matches in the HTML, answer describe() (evaluate_all) like a browser would, and record
every action instead of running it. No browser, no network, no clock. The fingerprint read
answers only on a FakePage(..., fingerprints=True), and so does the read of a list's
fingerprint (mendwright.lists.LIST_JS). Locators have nth() and all(). CSS is "#id" only,
and a selector needs its xpath= or css= prefix, unless the page is made with css=True, when
cssselect matches the rest and a selector without a prefix is read as mendwright reads it.
"""

import re

import lxml.html
import pytest

from mendwright.fingerprint import FINGERPRINT_JS, raw_of_element
from mendwright.lists import LIST_JS, raw_of_elements

# tests/test_pytest_plugin.py runs pytest inside pytest through the pytester fixture.
pytest_plugins = ['pytester']

# Playwright Locator verbs the fake accepts. Anything else is an AttributeError, as on a
# real Locator.
ACTIONS = frozenset({
    'click', 'dblclick', 'fill', 'type', 'press', 'press_sequentially', 'check', 'uncheck',
    'set_checked', 'select_option', 'hover', 'tap', 'clear', 'focus', 'set_input_files',
    'drag_to', 'blur',
})

_CSS_ID = re.compile(r'^#([\w-]+)$')


def _hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def _elements(self):
        return self._page.query(self._selector)

    def count(self):
        return len(self._elements())

    def is_visible(self):
        els = self._elements()
        return len(els) == 1 and not _hidden(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def nth(self, index):
        return FakeNth(self._page, self._selector, index)

    def all(self):
        return [self.nth(i) for i in range(self.count())]

    def evaluate_all(self, js):
        if js == LIST_JS:
            self._page.fingerprint_reads += 1
            return raw_of_elements(self._elements()) if self._page.fingerprints else None
        if js == FINGERPRINT_JS:
            # The fingerprint read. Off unless the page was made with fingerprints=True, so
            # tests written before the fingerprint tier still see the model heal.
            self._page.fingerprint_reads += 1
            els = self._elements()
            if not self._page.fingerprints or len(els) != 1:
                return None
            return raw_of_element(els[0])
        self._page.describes += 1
        els = self._elements()
        if len(els) != 1:
            return None
        el = els[0]
        return {'tag': el.tag.lower(),
                'html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}

    def get_attribute(self, name, **kwargs):
        els = self._elements()
        return els[0].get(name) if els else None

    def inner_text(self, **kwargs):
        els = self._elements()
        return ' '.join(els[0].text_content().split()) if els else ''

    def text_content(self, **kwargs):
        els = self._elements()
        return els[0].text_content() if els else None

    def input_value(self, **kwargs):
        els = self._elements()
        return (els[0].get('value') or '') if els else ''

    def __getattr__(self, verb):
        if verb not in ACTIONS:
            raise AttributeError("'FakeLocator' object has no attribute " + repr(verb))

        def action(*args, **kwargs):
            self._page.actions.append((self._selector, verb, args, kwargs))
            error = self._page.fail.get(verb)
            if error is not None:
                raise error
            return self._page.results.get(verb)
        return action


class FakeNth(FakeLocator):
    """locator.nth(index): the index-th match of the selector, or none."""

    def __init__(self, page, selector, index):
        super().__init__(page, selector)
        self._index = index

    def _elements(self):
        els = self._page.query(self._selector)
        return els[self._index:self._index + 1]


class FakePage:
    """Playwright-shaped page over an lxml DOM. set_html() stands in for a navigation."""

    def __init__(self, html, url='https://example.test/', fingerprints=False, css=False):
        self.url = url
        self.fingerprints = fingerprints   # answer mendwright's fingerprint read
        self.css = css                     # match CSS beyond "#id" with cssselect
        self.fingerprint_reads = 0
        self.actions = []     # (selector, verb, args, kwargs) for every action run
        self.fail = {}        # verb -> exception the action raises
        self.results = {}     # verb -> value the action returns
        self.queries = []     # every selector looked up
        self.describes = 0    # describe() round trips (evaluate_all calls)
        self.set_html(html)

    def set_html(self, html):
        self._doc = lxml.html.fromstring(html)

    def query(self, selector):
        self.queries.append(selector)
        if selector.startswith('xpath='):
            try:
                found = self._doc.xpath(selector[len('xpath='):])
            except Exception:
                return []
            return [e for e in found if hasattr(e, 'tag')]
        if self.css and not selector.startswith(('xpath=', 'css=')):
            from mendwright.selectors import XPATH, classify
            selector = ('xpath=' if classify(selector) == XPATH else 'css=') + selector
        if selector.startswith('xpath='):
            try:
                found = self._doc.xpath(selector[len('xpath='):])
            except Exception:
                return []
            return [e for e in found if hasattr(e, 'tag')]
        if selector.startswith('css='):
            css = selector[len('css='):].strip()
            m = _CSS_ID.match(css)
            if m:
                return self._doc.xpath('//*[@id=$i]', i=m.group(1))
            if not self.css:
                return []
            try:
                from lxml.cssselect import CSSSelector
            except ImportError:     # lxml's own extra: a test that needs CSS can't run without it
                pytest.skip('cssselect is not installed')
            try:
                return CSSSelector(css)(self._doc)
            except Exception:
                return []
        return []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def frame_locator(self, selector):
        return self

    def content(self):
        return lxml.html.tostring(self._doc, encoding='unicode')

    def evaluate(self, js, arg=None):
        return []


class StubProvider:
    """Proposes a fixed xpath (or nothing) and records every call. No network."""

    def __init__(self, xpath=None, fallbacks=(), last_error=None):
        self.xpath = xpath
        self.fallbacks = list(fallbacks)
        self.last_error = last_error
        self.bundles = []
        self.outcomes = []

    def propose(self, bundle):
        self.bundles.append(bundle)
        if self.xpath is None:
            return None
        return {'xpath': self.xpath, 'fallbacks': list(self.fallbacks), 'reasoning': 'stub'}

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, bool(success)))


@pytest.fixture
def make_page():
    return FakePage


@pytest.fixture
def stub_provider():
    return StubProvider


# Variables that change what open() and the providers do. A developer's shell often has
# some of them set (a key, or a *_BASE_URL for a proxy), so every test starts without them.
_ENV_SETTINGS = (
    'MENDWRIGHT_STORE', 'MENDWRIGHT_TIMEOUT_MS', 'MENDWRIGHT_DRY_RUN', 'MENDWRIGHT_DISABLE',
    'MENDWRIGHT_FINGERPRINT',
    'MENDWRIGHT_PROVIDER', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY', 'MENDWRIGHT_AUTH_HEADER',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL',
    'OPENAI_API_KEY', 'OPENAI_MODEL', 'OPENAI_BASE_URL',
    'GEMINI_API_KEY', 'GEMINI_MODEL', 'OLLAMA_MODEL', 'OLLAMA_HOST',
)


@pytest.fixture(autouse=True)
def _no_mendwright_env(monkeypatch):
    """Facade and provider settings come from environment variables; keep the developer's
    shell out."""
    for name in _ENV_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_rate_limiters():
    """open() shares one rate limiter per store file for the whole process. Tests must
    not spend each other's budget."""
    from mendwright import facade
    facade._LIMITERS.clear()
    yield
    facade._LIMITERS.clear()
