"""Shared fakes for the facade tests: an lxml-backed page with the Playwright sync shape.

FakePage is detected as a Playwright page by mendwright.open(). Its locators count real
matches in the HTML, answer describe() (evaluate_all) like a browser would, and record
every action instead of running it. No browser, no network, no clock. The fingerprint read
answers only on a FakePage(..., fingerprints=True), and so do the reads of a list's
fingerprint (mendwright.lists.LIST_JS, and LIST_PLACE_JS with where the list sits); the
read of a list's items for the live gate (ITEMS_JS) always answers. The page counts each
kind of read. Locators have nth() and all(). CSS is "#id" only,
and a selector needs its xpath= or css= prefix, unless the page is made with css=True, when
cssselect matches the rest and a selector without a prefix is read as mendwright reads it.

ShadowPage (Playwright-shaped) and ShadowDriver (Selenium-shaped) model open and closed shadow
roots, which lxml has none of (see the comment above ShadowDOM, near the end of this file).

Its get_by_role(), get_by_text(), get_by_label(), get_by_placeholder(), get_by_alt_text(),
get_by_title() and get_by_test_id() give FakeGetBy locators, which match the way Playwright
does, near enough for the tests: a role from role= or the element's tag, a name from
aria-label, aria-labelledby, a <label> or the text, matched as a case-insensitive part of
it, or whole with exact=True, or by a regular expression; elements hidden from the
accessibility tree (aria-hidden, inert, display: none) left out of get_by_role(), while
one with data-invisible stays in it and is not visible, as a zero-size element. Their
actions need one visible element, and raise a strict mode violation or a timeout, as
Playwright's do. Each call is recorded in page.get_by_calls, and page.reject = {method:
message} makes the browser reject that method's calls when they run.
"""

import inspect
import re

import lxml.html
import pytest

from mendwright.fingerprint import FINGERPRINT_JS, raw_of_element
from mendwright.lists import ITEMS_JS, LIST_JS, LIST_PLACE_JS, items_of_elements, raw_of_elements

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
        if el.get('data-invisible') is not None:    # in the accessibility tree, not visible
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
            self._page.list_reads += 1
            return raw_of_elements(self._elements()) if self._page.fingerprints else None
        if js == LIST_PLACE_JS:
            self._page.fingerprint_reads += 1
            self._page.place_reads += 1
            if not self._page.fingerprints:
                return None
            return raw_of_elements(self._elements(), place=True)
        if js == ITEMS_JS:
            self._page.items_reads += 1
            return items_of_elements(self._elements())
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
        if verb.startswith('get_by_'):     # a get_by_* call inside this locator's elements
            return lambda *args, **kwargs: FakeGetBy(self._page, verb, args, kwargs, scope=self)
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


# --- get_by_* -------------------------------------------------------------------------------

class FakeError(Exception):
    """Stands in for playwright's Error."""


class FakeTimeout(FakeError):
    """Stands in for playwright's TimeoutError."""


def _norm(text):
    return ' '.join((text or '').split())


def _tag(el):
    return el.tag.lower() if isinstance(el.tag, str) else ''


_INPUT_ROLES = {'button': 'button', 'submit': 'button', 'reset': 'button', 'image': 'button',
                'checkbox': 'checkbox', 'radio': 'radio', 'search': 'searchbox',
                'range': 'slider', 'number': 'spinbutton', 'text': 'textbox',
                'email': 'textbox', 'tel': 'textbox', 'url': 'textbox'}
_TAG_ROLES = {'button': 'button', 'textarea': 'textbox', 'select': 'combobox', 'nav': 'navigation',
              'li': 'listitem', 'ul': 'list', 'ol': 'list', 'main': 'main', 'dialog': 'dialog',
              'table': 'table', 'tr': 'row', 'option': 'option'}


def aria_role(el):
    """The element's ARIA role, from role= or its tag, as Playwright gives it."""
    explicit = (el.get('role') or '').split()
    if explicit:
        return explicit[0].lower()
    tag = _tag(el)
    if tag == 'a':
        return 'link' if el.get('href') is not None else None
    if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        return 'heading'
    if tag == 'img':
        return 'img' if el.get('alt') != '' else None
    if tag == 'input':
        return _INPUT_ROLES.get((el.get('type') or 'text').lower())
    return _TAG_ROLES.get(tag)


def _level(el):
    tag = _tag(el)
    if el.get('aria-level'):
        return int(el.get('aria-level'))
    return int(tag[1]) if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') else None


def _out_of_tree(el):
    """Hidden from the accessibility tree: aria-hidden, inert or not displayed. An element
    with data-invisible stays in it, as a zero-size one does in a browser, while
    is_visible() says it is not visible."""
    node = el
    while node is not None:
        style = (node.get('style') or '').replace(' ', '').lower()
        if ((node.get('aria-hidden') or '').lower() == 'true' or node.get('inert') is not None
                or 'display:none' in style or node.get('hidden') is not None):
            return True
        node = node.getparent()
    return False


def _by_id(doc, ident):
    found = doc.xpath('//*[@id=$i]', i=ident)
    return found[0] if found else None


def _label_text(doc, el):
    """The text of the <label> of a form field: its for= label, or the one around it."""
    if el.get('id'):
        for label in doc.iter('label'):
            if label.get('for') == el.get('id'):
                return _norm(label.text_content())
    node = el.getparent()
    while node is not None:
        if _tag(node) == 'label':
            return _norm(node.text_content())
        node = node.getparent()
    return None


def accessible_name(doc, el):
    """The element's accessible name, near enough to the browser's."""
    if el.get('aria-labelledby'):
        parts = [_by_id(doc, i) for i in el.get('aria-labelledby').split()]
        return _norm(' '.join(p.text_content() for p in parts if p is not None))
    if el.get('aria-label'):
        return _norm(el.get('aria-label'))
    tag = _tag(el)
    if tag in ('input', 'textarea', 'select'):
        if tag == 'input' and (el.get('type') or '').lower() in ('button', 'submit', 'reset'):
            return _norm(el.get('value'))
        return _label_text(doc, el) or _norm(el.get('placeholder') or el.get('title'))
    if tag == 'img':
        return _norm(el.get('alt'))
    return _norm(el.text_content()) or _norm(el.get('title'))


def text_matches(wanted, text, exact=False):
    """Playwright's text match: part of the text, case-insensitive; the whole text with
    exact=True; re.search for a regular expression."""
    if text is None:
        return False
    text = _norm(text)
    if isinstance(wanted, re.Pattern):
        return wanted.search(text) is not None
    if exact:
        return text == _norm(wanted)
    return _norm(wanted).lower() in text.lower()


def _fields(doc):
    return [el for el in doc.iter() if _tag(el) in ('input', 'textarea', 'select')]


def get_by_elements(doc, method, args, kwargs):
    """The elements of doc a get_by_* call matches."""
    exact = bool(kwargs.get('exact'))
    if method == 'get_by_role':
        role = args[0] if args else kwargs['role']
        name, level = kwargs.get('name'), kwargs.get('level')
        out = []
        for el in doc.iter():
            if not isinstance(el.tag, str) or aria_role(el) != role:
                continue
            if not kwargs.get('include_hidden') and _out_of_tree(el):
                continue
            if level is not None and _level(el) != level:
                continue
            if name is not None and not text_matches(name, accessible_name(doc, el), exact):
                continue
            out.append(el)
        return out
    value = args[0] if args else kwargs.get('text', kwargs.get('test_id'))
    if method == 'get_by_text':
        found = [el for el in doc.iter() if isinstance(el.tag, str)
                 and _tag(el) not in ('html', 'head', 'title', 'script', 'style')
                 and text_matches(value, el.text_content(), exact)]
        # The deepest elements that hold the text, as Playwright's text engine gives them.
        return [el for el in found
                if not any(other is not el and other in el.iterdescendants() for other in found)]
    if method == 'get_by_label':
        return [el for el in _fields(doc)
                if text_matches(value, _label_text(doc, el) or el.get('aria-label'), exact)]
    if method == 'get_by_test_id':
        return [el for el in doc.iter() if isinstance(el.tag, str) and el.get('data-testid') is not None
                and (value.search(el.get('data-testid')) if isinstance(value, re.Pattern)
                     else el.get('data-testid') == value)]
    attr = {'get_by_placeholder': 'placeholder', 'get_by_alt_text': 'alt',
            'get_by_title': 'title'}[method]
    return [el for el in doc.iter() if isinstance(el.tag, str) and el.get(attr) is not None
            and text_matches(value, el.get(attr), exact)]


_VISIBLE_ACTIONS = ACTIONS - {'set_input_files', 'focus'}


# Playwright's signatures (1.53), which the fake's get_by_* check as Playwright does.
def _role(role, *, checked=None, disabled=None, expanded=None, include_hidden=None, level=None,
          name=None, pressed=None, selected=None, exact=None):
    pass


def _text(text, *, exact=None):
    pass


def _test_id(test_id):
    pass


PLAYWRIGHT_SIGNATURES = {'get_by_role': inspect.signature(_role),
                         'get_by_test_id': inspect.signature(_test_id)}
for _method in ('get_by_text', 'get_by_label', 'get_by_placeholder', 'get_by_alt_text',
                'get_by_title'):
    PLAYWRIGHT_SIGNATURES[_method] = inspect.signature(_text)


class FakeGetBy(FakeLocator):
    """A locator from a FakePage get_by_* call (see the module docstring)."""

    def __init__(self, page, method, args, kwargs, scope=None):
        parts = [repr(a) for a in args] + [k + '=' + repr(v) for k, v in kwargs.items()]
        super().__init__(page, method + '(' + ', '.join(parts) + ')')
        self._call = (method, args, kwargs)
        self._scope = scope

    def _elements(self):
        method, args, kwargs = self._call
        self._page.trips += 1
        if method in self._page.reject:
            raise FakeError(self._page.reject[method])
        found = get_by_elements(self._page._doc, method, args, kwargs)
        if self._scope is not None:
            inside = self._scope._elements()
            found = [el for el in found if any(el in s.iterdescendants() for s in inside)]
        return found

    def _one(self, verb):
        els = self._elements()
        if len(els) > 1:
            raise FakeError('Locator.' + verb + ': Error: strict mode violation: '
                            + self._selector + ' resolved to ' + str(len(els)) + ' elements')
        if not els or (verb in _VISIBLE_ACTIONS and _hidden(els[0])):
            raise FakeTimeout('Locator.' + verb + ': Timeout 30000ms exceeded.')
        return els[0]

    def get_attribute(self, name, **kwargs):
        return self._one('get_attribute').get(name)

    def inner_text(self, **kwargs):
        return _norm(self._one('inner_text').text_content())

    def text_content(self, **kwargs):
        return self._one('text_content').text_content()

    def input_value(self, **kwargs):
        return self._one('input_value').get('value') or ''

    def is_hidden(self, **kwargs):
        return not self.is_visible()

    # What builds another locator: real fake locators, never healed.

    def locator(self, selector):
        return FakeChain(self._page, self._selector + ' >> ' + selector,
                         lambda: [e for e in self._page.query(selector)
                                  if any(e in s.iterdescendants() for s in self._elements())])

    def frame_locator(self, selector):
        return self._page

    def nth(self, index):
        return FakeChain(self._page, self._selector + '.nth(' + str(index) + ')',
                         lambda: self._elements()[index:index + 1] if index >= 0
                         else self._elements()[index:][:1])

    @property
    def first(self):
        return self.nth(0)

    @property
    def last(self):
        return self.nth(-1)

    def filter(self, has_text=None, **kwargs):
        return FakeChain(self._page, self._selector + '.filter()',
                         lambda: [e for e in self._elements()
                                  if has_text is None or has_text in e.text_content()])

    def and_(self, other):
        return FakeChain(self._page, self._selector + ' & ' + other._selector,
                         lambda: [e for e in self._elements()
                                  if any(e is o for o in other._elements())])

    def or_(self, other):
        return FakeChain(self._page, self._selector + ' | ' + other._selector,
                         lambda: self._elements() + [o for o in other._elements()
                                                     if not any(o is e for e in self._elements())])

    def __getattr__(self, verb):
        if verb.startswith('get_by_'):
            return lambda *args, **kwargs: FakeGetBy(self._page, verb, args, kwargs, scope=self)
        if verb not in ACTIONS:
            raise AttributeError("'FakeGetBy' object has no attribute " + repr(verb))

        def action(*args, **kwargs):
            self._one('focus' if kwargs.get('force') else verb)    # force=True: hidden is fine
            self._page.actions.append((self._selector, verb, args, kwargs))
            error = self._page.fail.get(verb)
            if error is not None:
                raise error
            return self._page.results.get(verb)
        return action


class FakeChain(FakeGetBy):
    """A locator built from a FakeGetBy (.first, .filter(), .and_(), ...)."""

    def __init__(self, page, name, find):
        FakeLocator.__init__(self, page, name)
        self._find = find

    def _elements(self):
        return self._find()


class FakePage:
    """Playwright-shaped page over an lxml DOM. set_html() stands in for a navigation."""

    def __init__(self, html, url='https://example.test/', fingerprints=False, css=False):
        self.url = url
        self.fingerprints = fingerprints   # answer mendwright's fingerprint read
        self.css = css                     # match CSS beyond "#id" with cssselect
        self.fingerprint_reads = 0
        self.list_reads = 0       # LIST_JS
        self.place_reads = 0      # LIST_PLACE_JS
        self.items_reads = 0      # ITEMS_JS
        self.actions = []     # (selector, verb, args, kwargs) for every action run
        self.fail = {}        # verb -> exception the action raises
        self.results = {}     # verb -> value the action returns
        self.queries = []     # every selector looked up
        self.describes = 0    # describe() round trips (evaluate_all calls)
        self.get_by_calls = []   # (method, args, kwargs) of every get_by_* call
        self.reject = {}      # get_by_* method -> the error the browser gives for its calls
        self.trips = 0        # page queries of the get_by_* locators
        self.frames = []      # a Page has a list of frames, a Frame none (no iframes here)
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

    def _get_by(self, method, args, kwargs):
        PLAYWRIGHT_SIGNATURES[method].bind(*args, **kwargs)     # TypeError, as Playwright's
        self.get_by_calls.append((method, args, kwargs))
        return FakeGetBy(self, method, args, kwargs)


def _page_get_by(method):
    def get_by(self, *args, **kwargs):
        return self._get_by(method, args, kwargs)
    get_by.__name__ = method
    return get_by


for _method in ('get_by_role', 'get_by_text', 'get_by_label', 'get_by_placeholder',
                'get_by_alt_text', 'get_by_title', 'get_by_test_id'):
    setattr(FakePage, _method, _page_get_by(_method))


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
    'MENDWRIGHT_FINGERPRINT', 'MENDWRIGHT_HEAL_GET_BY',
    'MENDWRIGHT_PROVIDER', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY', 'MENDWRIGHT_AUTH_HEADER',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL',
    'OPENAI_API_KEY', 'OPENAI_MODEL', 'OPENAI_BASE_URL',
    'GEMINI_API_KEY', 'GEMINI_MODEL', 'OLLAMA_MODEL', 'OLLAMA_HOST',
    # The GitHub Actions run this suite may run in, which the action's script reads.
    'GITHUB_ACTIONS', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY',
    'GITHUB_SERVER_URL', 'GITHUB_API_URL', 'GITHUB_EVENT_NAME', 'GITHUB_EVENT_PATH',
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


# --- open shadow roots ----------------------------------------------------------------------
#
# lxml has no shadow roots, so ShadowDOM models them: write each one as declarative shadow DOM,
# <template shadowrootmode="open"> (or "closed") as a child of its host, and set_html() moves
# its content into an lxml document of its own. A query in one scope (the page, or a shadow
# root) then sees that scope's elements only, as querySelectorAll() does in a browser, and the
# page's own HTML (content(), page_source) has no shadow content, as in a browser.
# ShadowPage is Playwright-shaped and ShadowDriver Selenium-shaped, so mendwright.open() runs
# its real drivers on them; they answer the scripts of mendwright.drivers.base and
# mendwright.fingerprint (PLACE_JS, SHADOW_HTML_JS, FINGERPRINT_JS with its hosts, ...) as a
# browser does. CSS needs cssselect.

class _Scope:
    def __init__(self, root, host=None, mode='open'):
        self.root, self.host, self.mode = root, host, mode


def _css(css):
    try:
        from lxml.cssselect import CSSSelector
    except ImportError:
        pytest.skip('cssselect is not installed')
    return CSSSelector(css)


def _is_shadow_template(el):
    return el.tag == 'template' and el.get('shadowrootmode') in ('open', 'closed')


class ShadowDOM:
    """A page with shadow roots (see above). Every element knows its scope (owner)."""

    def __init__(self, html):
        self.set_html(html)

    def set_html(self, html):
        self.document = _Scope(lxml.html.fromstring(html))
        self.shadows = {}       # host element -> its _Scope
        self.owner = {}         # element -> the _Scope it is in
        self.order = []         # every scope, the page first
        self._split(self.document)

    def _split(self, scope):
        self.order.append(scope)
        tops = [t for t in scope.root.iter('template') if _is_shadow_template(t)
                and not any(_is_shadow_template(a) for a in t.iterancestors())]
        hosts = []
        for tpl in tops:
            host = tpl.getparent()
            inner = (tpl.text or '') + ''.join(
                lxml.html.tostring(c, encoding='unicode') for c in tpl)
            host.remove(tpl)
            root = lxml.html.fromstring('<html><body>' + inner + '</body></html>')
            self.shadows[host] = _Scope(root, host, tpl.get('shadowrootmode'))
            hosts.append(host)
        for el in scope.root.iter():
            if isinstance(el.tag, str):
                self.owner[el] = scope
        for host in hosts:
            self._split(self.shadows[host])

    # what a scope holds

    def elements(self, scope):
        skip = ('html', 'head', 'body') if scope.host is not None else ()
        return [e for e in scope.root.iter() if isinstance(e.tag, str) and e.tag not in skip]

    def query(self, scope, css):
        found = set(_css(css)(scope.root))
        return [e for e in self.elements(scope) if e in found]

    def xpath(self, scope, expression):
        try:
            found = scope.root.xpath(expression)
        except Exception:
            return []
        return [e for e in found if isinstance(getattr(e, 'tag', None), str)]

    def inner_html(self, scope):
        body = scope.root.find('body')
        return (body.text or '') + ''.join(lxml.html.tostring(c, encoding='unicode') for c in body)

    def content(self):
        return lxml.html.tostring(self.document.root, encoding='unicode')

    def open_shadow(self, host):
        scope = self.shadows.get(host)
        return scope if scope is not None and scope.mode == 'open' else None

    def enter(self, hosts):
        """drivers.base.ENTER_JS: the scope the hosts lead to strictly, or None."""
        scope = self.document
        for sel in hosts:
            try:
                found = self.query(scope, sel)
            except Exception:
                return None
            if len(found) != 1 or self.open_shadow(found[0]) is None:
                return None
            scope = self.open_shadow(found[0])
        return scope

    # what the scripts read

    def host_facts(self, host):
        from mendwright.fingerprint import HOST_ATTRS
        same = [e for e in self.elements(self.owner[host]) if e.tag == host.tag]
        attrs, counts = {}, {'tag': len(same)}
        for name in HOST_ATTRS:
            if host.get(name) is not None:
                attrs[name] = host.get(name)
                counts[name] = sum(1 for e in same if e.get(name) == host.get(name))
        return {'tag': host.tag, 'attrs': attrs, 'counts': counts}

    def hosts_of(self, el):
        out, scope = [], self.owner[el]
        while scope.host is not None:
            out.insert(0, self.host_facts(scope.host))
            scope = self.owner[scope.host]
        return out

    def fingerprint_raw(self, el):
        raw = raw_of_element(el)
        raw['hosts'] = self.hosts_of(el)
        return raw

    def hidden(self, el):
        while el is not None:
            if _hidden(el):
                return True
            el = self.owner[el].host
        return False

    def composed(self, within=None):
        """What Playwright's CSS pierces to: every element of the page and of its open shadow
        roots, or of those inside the elements of within (their descendants, and the open
        shadow roots of any of them and of themselves)."""
        out = []

        def add_scope(scope):
            for e in self.elements(scope):
                out.append(e)
                if self.open_shadow(e) is not None:
                    add_scope(self.open_shadow(e))
        if within is None:
            add_scope(self.document)
            return out
        for top in within:
            for e in [top] + [d for d in top.iterdescendants() if isinstance(d.tag, str)]:
                if e is not top:
                    out.append(e)
                if self.open_shadow(e) is not None:
                    add_scope(self.open_shadow(e))
        return out

    def pierce(self, css, within=None):
        """Playwright's css= engine: what css matches in each scope, of what composed()
        gives."""
        hits = set()
        for scope in self.order:
            hits.update(self.query(scope, css))
        seen, out = set(), []
        for e in self.composed(within):
            if e in hits and e not in seen:
                seen.add(e)
                out.append(e)
        return out

    def roots(self, arg):
        """drivers.base.SHADOW_ROOTS_JS."""
        roots = []

        def walk(scope, chain):
            for el in self.elements(scope):
                inner = self.open_shadow(el)
                if inner is None:
                    continue
                here = chain + [self.host_facts(el)]
                if arg['css'] is not None:
                    n = len(self.query(inner, arg['css']))
                    if n:
                        roots.append({'hosts': here, 'count': n})
                else:
                    roots.append({'hosts': here, 'html': self.inner_html(inner)})
                walk(inner, here)
        walk(self.document, [])
        return {'roots': roots, 'cut': False}

    def inventory(self, scope, limit):
        rows = []
        for e in self.elements(scope):
            if e.tag in ('button', 'a', 'input', 'select', 'textarea') or e.get('role'):
                rows.append({'tag': e.tag, 'role': e.get('role') or '', 'type': e.get('type') or '',
                             'id': e.get('id') or '', 'name': e.get('name') or '',
                             'text': ' '.join(e.text_content().split())[:80],
                             'aria_label': e.get('aria-label') or ''})
        return rows[:limit]


class ShadowLocator:
    """A Playwright Locator on a ShadowPage: each step pierces open shadow roots, inside the
    elements of the step before it, as Playwright's CSS does. An xpath= step runs on the page
    only. Actions need exactly one element (Playwright's strict mode)."""

    def __init__(self, page, steps, pick=None):
        from mendwright.shadow import split_text
        self._page = page
        self._steps = [part for step in steps for part in split_text(step)]   # ' >> ' chains
        self._pick = pick

    def _elements(self):
        dom, els = self._page.dom, None
        for step in self._steps:
            if step.startswith('xpath='):
                found = dom.xpath(dom.document, step[len('xpath='):]) if els is None else []
            else:
                css = step[len('css='):] if step.startswith('css=') else step
                try:
                    found = dom.pierce(css, els)
                except Exception:
                    found = []
            els = found
        els = els or []
        return els[self._pick:self._pick + 1] if self._pick is not None else els

    def locator(self, selector):
        return ShadowLocator(self._page, self._steps + [selector])

    def count(self):
        return len(self._elements())

    def is_visible(self):
        els = self._elements()
        if len(els) > 1:
            raise FakeError('strict mode violation: ' + ' >> '.join(self._steps))
        return bool(els) and not self._page.dom.hidden(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def nth(self, index):
        return ShadowLocator(self._page, self._steps, index)

    def evaluate_all(self, js, arg=None):
        from mendwright.drivers.base import PLACE_JS
        dom, els = self._page.dom, self._elements()
        self._page.scripts.append(js)
        if js == PLACE_JS:
            root = dom.enter(arg)
            return {'n': len(els), 'inside': sum(1 for e in els if dom.owner[e] is root)}
        if js == FINGERPRINT_JS:
            return dom.fingerprint_raw(els[0]) if len(els) == 1 else None
        if js == ITEMS_JS:
            return items_of_elements(els)
        if js in (LIST_JS, LIST_PLACE_JS):
            if len(els) < 2:
                return None
            raw = raw_of_elements(els, place=js == LIST_PLACE_JS)
            raw['shadow'] = dom.owner[els[0]].host is not None
            return raw
        if len(els) != 1:
            return None
        return {'tag': els[0].tag, 'html': lxml.html.tostring(els[0], encoding='unicode',
                                                             with_tail=False)}

    def __getattr__(self, verb):
        if verb not in ACTIONS:
            raise AttributeError("'ShadowLocator' object has no attribute " + repr(verb))

        def action(*args, **kwargs):
            els = self._elements()
            if len(els) != 1:
                raise FakeError('Locator.' + verb + ': strict mode violation or timeout: '
                                + str(len(els)) + ' elements')
            self._page.actions.append((els[0], verb, args, kwargs))
            return None
        return action


class ShadowPage:
    """A Playwright-shaped page over a ShadowDOM (see above): set_html() stands in for a
    navigation, actions records (element, verb, args, kwargs), scripts every script run."""

    def __init__(self, html, url='https://example.test/'):
        self.url = url
        self.dom = ShadowDOM(html)
        self.actions = []
        self.scripts = []
        self.frames = []

    def set_html(self, html):
        self.dom.set_html(html)

    def locator(self, selector):
        return ShadowLocator(self, [selector])

    def frame_locator(self, selector):
        return self

    def content(self):
        return self.dom.content()

    def evaluate(self, js, arg=None):
        from mendwright.drivers.base import (INVENTORY_JS, SHADOW_HTML_JS, SHADOW_INVENTORY_JS,
                                             SHADOW_ROOTS_JS)
        self.scripts.append(js)
        dom = self.dom
        if js == SHADOW_HTML_JS:
            scope = dom.enter(arg)
            return dom.inner_html(scope) if scope is not None else None
        if js == SHADOW_INVENTORY_JS:
            scope = dom.enter(arg['hosts'])
            return dom.inventory(scope, arg['limit']) if scope is not None else None
        if js == SHADOW_ROOTS_JS:
            return dom.roots(arg)
        if js == INVENTORY_JS:
            return dom.inventory(dom.document, arg)
        return None


class ShadowElement:
    """A Selenium WebElement on a ShadowDriver."""

    def __init__(self, driver, el):
        self._driver, self.el = driver, el

    def __eq__(self, other):
        return isinstance(other, ShadowElement) and other.el is self.el

    def __hash__(self):
        return id(self.el)

    @property
    def tag_name(self):
        return self.el.tag

    def is_displayed(self):
        return not self._driver.dom.hidden(self.el)

    def get_attribute(self, name):
        if name == 'outerHTML':
            return lxml.html.tostring(self.el, encoding='unicode', with_tail=False)
        return self.el.get(name)

    def _act(self, verb, *args):
        self._driver.actions.append((self.el, verb, args))

    def click(self):
        self._act('click')

    def clear(self):
        self._act('clear')

    def send_keys(self, *args):
        self._act('send_keys', *args)


class _ShadowSwitch:
    def default_content(self):
        pass

    def frame(self, element):
        raise LookupError('no frames here')


class _NoWait:
    implicit_wait = 0


class ShadowDriver:
    """A Selenium-shaped WebDriver over a ShadowDOM (see above): find_elements() looks at the
    page only, as Selenium's does, and execute_script() answers mendwright's scripts."""

    def __init__(self, html, url='https://example.test/'):
        self.current_url = url
        self.dom = ShadowDOM(html)
        self.switch_to = _ShadowSwitch()
        self.timeouts = _NoWait()
        self.actions = []
        self.scripts = []
        self._wrapped = {}

    def set_html(self, html):
        self.dom.set_html(html)
        self._wrapped = {}

    def implicitly_wait(self, seconds):
        pass

    def _wrap(self, els):
        return [self._wrapped.setdefault(e, ShadowElement(self, e)) for e in els]

    @property
    def page_source(self):
        return self.dom.content()

    def find_elements(self, by, value):
        dom = self.dom
        if by == 'xpath':
            return self._wrap(dom.xpath(dom.document, value))
        return self._wrap(dom.query(dom.document, value))

    def execute_script(self, script, *args):
        from mendwright.drivers import selenium as sel
        self.scripts.append(script)
        dom = self.dom
        if script == sel._SHADOW_FIND:
            scope = dom.enter(args[0])
            return self._wrap(dom.query(scope, args[1])) if scope is not None else []
        if script == sel._SHADOW_HTML:
            scope = dom.enter(args[0])
            return dom.inner_html(scope) if scope is not None else None
        if script == sel._SHADOW_INVENTORY:
            scope = dom.enter(args[0]['hosts'])
            return dom.inventory(scope, args[0]['limit']) if scope is not None else None
        if script == sel._SHADOW_ROOTS:
            return dom.roots(args[0])
        if script == 'return (' + FINGERPRINT_JS + ')([arguments[0]]);':
            return dom.fingerprint_raw(args[0].el)
        if script.startswith('return {tag: arguments[0].tagName'):
            return {'tag': args[0].el.tag, 'html': args[0].get_attribute('outerHTML')}
        if script == 'return window.self === window.top;':
            return True
        if script == 'return window.self !== window.top;':
            return False
        return None


@pytest.fixture
def make_shadow_page():
    return ShadowPage


@pytest.fixture
def make_shadow_driver():
    return ShadowDriver
