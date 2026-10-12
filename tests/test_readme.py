"""README.md proves itself.

Every ```python block in README.md runs here exactly as written. Fake 'playwright' and
'selenium' modules are put in sys.modules for the duration of a block. Their pages are
lxml documents: a selector matches only what it really matches in the HTML, and fill(),
check() and click() change the document. https://example.com/signup is served from
SIGNUP_HTML, a redesign of the page in which the checkbox lost its id, so the README's
"//input[@id='terms']" matches nothing and has to be healed. auto_provider() is replaced
by a stub that answers with a fixed XPath. No browser, network or API key is needed.

The block marked readme-test: flow runs on a page that a OneTrust-style consent banner
and a newsletter modal both cover: it is given that page as `page`, and a click on an
element with data-closes="<id>" closes the element with that id, as the page's script
would. The pytest block runs through pytester with the plugin loaded, the way pytest
loads it for a user. The AttentionRequired text the README quotes is compared with what the code
raises today, and the links in README.md and docs/guide.md are checked.
"""

import json
import os
import re
import sys
import types
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.providers

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / 'README.md'
GUIDE = ROOT / 'docs' / 'guide.md'
SOURCES = ROOT / 'mendwright'

# The README has this many ```python blocks, and all of them run. Change it together with
# the README, so a new block cannot slip in untested.
EXPECTED_PYTHON_BLOCKS = 5
SKIP_MARK = '<!-- readme-test: skip -->'
ATTENTION_MARK = '<!-- readme-test: attention-pay -->'
FLOW_MARK = '<!-- readme-test: flow -->'

SIGNUP_URL = 'https://example.com/signup'
SIGNUP_HTML = """<!doctype html>
<html><head><title>Sign up</title></head>
<body>
  <form class="signup">
    <label for="email">Email</label>
    <input id="email" name="email" type="email">
    <div class="consent">
      <label><input type="checkbox" name="terms" id="consent-7f3a"> I accept the terms</label>
    </div>
    <button id="create-account" type="submit">Create account</button>
  </form>
</body></html>"""
# What the stub model proposes for each key. Any other key gets no proposal.
PROPOSALS = {
    'accept_terms': "//label[contains(normalize-space(.), 'I accept the terms')]"
                    "//input[@type='checkbox']",
}
# The signup page, with a consent banner and a newsletter modal on top of it.
FLOW_HTML = """<!doctype html>
<html><head><title>Sign up</title></head>
<body>
  <form class="signup">
    <label for="email">Email</label>
    <input id="email" name="email" type="email">
    <button id="create-account" type="submit">Create account</button>
  </form>
  <div id="newsletter" role="dialog" aria-label="Newsletter">
    <p>Get 10% off your first order.</p>
    <button id="newsletter-close" data-closes="newsletter" aria-label="Close">x</button>
  </div>
  <div id="onetrust-consent-sdk"><div id="onetrust-banner-sdk" role="region" aria-label="Cookie banner">
    <p>We use cookies.</p>
    <button id="onetrust-reject-all-handler" data-closes="onetrust-consent-sdk">Reject All</button>
    <button id="onetrust-accept-btn-handler" data-closes="onetrust-consent-sdk">Accept All</button>
  </div></div>
</body></html>"""
PAGES = {SIGNUP_URL: SIGNUP_HTML}
BLANK = '<!doctype html><html><head></head><body></body></html>'
_CSS_ID = re.compile(r'^#([A-Za-z_][\w-]*)$')


# --- README parsing -----------------------------------------------------------------------

_FENCE = re.compile(r'^```([\w-]*)[ \t]*\n(.*?)^```[ \t]*$', re.M | re.S)


def _blocks(text):
    """[(lang, code, marker, line)] for every fenced block. marker is the HTML comment on
    the nearest non-blank line above the fence, or ''."""
    out = []
    for m in _FENCE.finditer(text):
        before = [ln.strip() for ln in text[:m.start()].splitlines() if ln.strip()]
        marker = before[-1] if before and before[-1].startswith('<!--') else ''
        line = text.count('\n', 0, m.start()) + 1
        out.append((m.group(1), m.group(2), marker, line))
    return out


if not README.exists():
    pytest.skip('README.md is not in this copy of the project', allow_module_level=True)
README_TEXT = README.read_text(encoding='utf-8')
BLOCKS = _blocks(README_TEXT)
PY_BLOCKS = [(code, line) for lang, code, marker, line in BLOCKS
             if lang == 'python' and marker != SKIP_MARK]
SKIPPED = [line for lang, code, marker, line in BLOCKS if lang == 'python' and marker == SKIP_MARK]


# --- the fake browser ------------------------------------------------------------------------

class Site:
    """The pages the fakes serve, and what the README code did to them."""

    def __init__(self, pages=None):
        self.pages = dict(PAGES if pages is None else pages)
        self.doc = lxml.html.document_fromstring(BLANK)
        self.url = 'about:blank'
        self.actions = []        # (verb, selector) for every action on an element
        self.unsupported = []    # selectors these fakes cannot run (the test fails on any)
        self.closed = False

    def load(self, url):
        if url not in self.pages:
            raise AssertionError('the README opens ' + url + ', which this test does not serve')
        self.url = url
        self.doc = lxml.html.document_fromstring(self.pages[url])

    def set_content(self, html):
        self.doc = lxml.html.document_fromstring(html)

    def query(self, kind, value):
        if kind == 'xpath':
            return [e for e in self.doc.xpath(value) if isinstance(getattr(e, 'tag', None), str)]
        m = _CSS_ID.match(value.strip())
        if kind == 'css' and m:
            return self.doc.xpath('//*[@id=$i]', i=m.group(1))
        self.unsupported.append((kind, value))
        raise ValueError('the README test fakes run XPath and #id CSS only, not ' + repr(value))

    def html(self):
        return '<!DOCTYPE html>\n' + lxml.html.tostring(self.doc, encoding='unicode')

    def element(self, xpath):
        found = self.doc.xpath(xpath)
        assert len(found) == 1, xpath
        return found[0]


def _hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def _is_checkbox(el):
    return el.tag == 'input' and (el.get('type') or '').lower() in ('checkbox', 'radio')


def _describe(el):
    return {'tag': el.tag.lower(), 'html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}


def _playwright_target(selector):
    """('xpath' | 'css', body) for a selector string the Playwright driver passes on."""
    for kind in ('xpath', 'css'):
        if selector.startswith(kind + '='):
            return kind, selector[len(kind) + 1:]
    return 'engine', selector


class SyncLocator:
    """The parts of playwright.sync_api.Locator that mendwright and the README use."""

    def __init__(self, site, selector):
        self._site = site
        self._selector = selector

    def _all(self):
        return self._site.query(*_playwright_target(self._selector))

    def _one(self, verb):
        els = self._all()
        if len(els) != 1:
            raise RuntimeError('Locator.' + verb + ': ' + self._selector + ' matched '
                               + str(len(els)) + ' elements')
        self._site.actions.append((verb, self._selector))
        return els[0]

    def count(self):
        return len(self._all())

    def is_visible(self, **kwargs):
        els = self._all()
        return len(els) == 1 and not _hidden(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def evaluate_all(self, expression, arg=None):
        els = self._all()
        return _describe(els[0]) if len(els) == 1 else None

    def get_attribute(self, name, **kwargs):
        els = self._all()
        return els[0].get(name) if els else None

    def is_checked(self, **kwargs):
        return self._one('is_checked').get('checked') is not None

    def fill(self, value, **kwargs):
        self._one('fill').set('value', value)

    def check(self, **kwargs):
        self._one('check').set('checked', '')

    def click(self, **kwargs):
        el = self._one('click')
        closes = el.get('data-closes')
        if closes:
            self._site.element('//*[@id=' + repr(closes) + ']').drop_tree()
        if _is_checkbox(el):
            if el.get('checked') is None:
                el.set('checked', '')
            else:
                del el.attrib['checked']


class SyncPage:
    """A playwright.sync_api Page over an lxml document."""

    def __init__(self, site):
        self._site = site

    @property
    def url(self):
        return self._site.url

    def goto(self, url, **kwargs):
        self._site.load(url)

    def set_content(self, html, **kwargs):
        self._site.set_content(html)

    def locator(self, selector):
        return SyncLocator(self._site, selector)

    def frame_locator(self, selector):
        raise AssertionError('the README pages have no frames')

    def content(self):
        return self._site.html()

    def evaluate(self, expression, arg=None):
        return []      # mendwright's element inventory; optional, so an empty one is fine


class AsyncLocator:
    """playwright.async_api.Locator: the same queries and actions, awaited."""

    def __init__(self, site, selector):
        self._sync = SyncLocator(site, selector)

    async def count(self):
        return self._sync.count()

    async def is_visible(self, **kwargs):
        return self._sync.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate_all(self, expression, arg=None):
        return self._sync.evaluate_all(expression, arg)

    async def get_attribute(self, name, **kwargs):
        return self._sync.get_attribute(name)

    async def is_checked(self, **kwargs):
        return self._sync.is_checked()

    async def fill(self, value, **kwargs):
        self._sync.fill(value)

    async def check(self, **kwargs):
        self._sync.check()

    async def click(self, **kwargs):
        self._sync.click()


class AsyncPage:
    """A playwright.async_api Page: locator() is a plain call, the rest is awaited."""

    def __init__(self, site):
        self._site = site

    @property
    def url(self):
        return self._site.url

    async def goto(self, url, **kwargs):
        self._site.load(url)

    def locator(self, selector):
        return AsyncLocator(self._site, selector)

    def frame_locator(self, selector):
        raise AssertionError('the README pages have no frames')

    async def content(self):
        return self._site.html()

    async def evaluate(self, expression, arg=None):
        return []


def _playwright_modules(site):
    """Fake 'playwright', 'playwright.sync_api' and 'playwright.async_api' modules."""

    class Browser:
        def new_page(self, **kwargs):
            return SyncPage(site)

        def close(self):
            site.closed = True

    class BrowserType:
        def launch(self, **kwargs):
            return Browser()

    class AsyncBrowser:
        async def new_page(self, **kwargs):
            return AsyncPage(site)

        async def close(self):
            site.closed = True

    class AsyncBrowserType:
        async def launch(self, **kwargs):
            return AsyncBrowser()

    class Playwright:
        def __init__(self, browser_type):
            self.chromium = self.firefox = self.webkit = browser_type

    class SyncContext:
        def __enter__(self):
            return Playwright(BrowserType())

        def __exit__(self, *exc):
            site.closed = True
            return False

    class AsyncContext:
        async def __aenter__(self):
            return Playwright(AsyncBrowserType())

        async def __aexit__(self, *exc):
            site.closed = True
            return False

    sync_api = types.ModuleType('playwright.sync_api')
    sync_api.sync_playwright = SyncContext
    async_api = types.ModuleType('playwright.async_api')
    async_api.async_playwright = AsyncContext
    package = types.ModuleType('playwright')
    package.__path__ = []
    package.sync_api, package.async_api = sync_api, async_api
    return {'playwright': package, 'playwright.sync_api': sync_api,
            'playwright.async_api': async_api}


class WebElement:
    """selenium WebElement over an lxml element."""

    def __init__(self, site, el, selector):
        self._site = site
        self._el = el
        self._selector = selector

    @property
    def tag_name(self):
        return self._el.tag

    def _log(self, verb):
        self._site.actions.append((verb, self._selector))

    def is_displayed(self):
        return not _hidden(self._el)

    def is_selected(self):
        return self._el.get('checked') is not None

    def get_attribute(self, name):
        if name == 'outerHTML':
            return _describe(self._el)['html']
        return self._el.get(name)

    def click(self):
        self._log('click')
        if _is_checkbox(self._el):
            if self._el.get('checked') is None:
                self._el.set('checked', '')
            else:
                del self._el.attrib['checked']

    def clear(self):
        self._log('clear')
        self._el.set('value', '')

    def send_keys(self, *values):
        self._log('send_keys')
        self._el.set('value', (self._el.get('value') or '') + ''.join(map(str, values)))


def _selenium_modules(site):
    """Fake 'selenium' and 'selenium.webdriver' modules with a lxml-backed Chrome."""

    class SwitchTo:
        def frame(self, reference):
            raise AssertionError('the README pages have no frames')

        def default_content(self):
            pass

    class Timeouts:
        implicit_wait = 0

    class Chrome:
        def __init__(self, *args, **kwargs):
            self.switch_to = SwitchTo()
            self.timeouts = Timeouts()

        def implicitly_wait(self, seconds):
            self.timeouts.implicit_wait = seconds

        def get(self, url):
            site.load(url)

        @property
        def current_url(self):
            return site.url

        @property
        def page_source(self):
            return site.html()

        def find_elements(self, by, value):
            kind = {'xpath': 'xpath', 'css selector': 'css'}.get(by, by)
            return [WebElement(site, el, value) for el in site.query(kind, value)]

        def execute_script(self, script, *args):
            if 'outerHTML' in script and args and isinstance(args[0], WebElement):
                return _describe(args[0]._el)
            if 'scrollIntoView' in script:
                return None
            return []       # mendwright's element inventory

        def quit(self):
            site.closed = True

    webdriver = types.ModuleType('selenium.webdriver')
    webdriver.Chrome = webdriver.Firefox = Chrome
    package = types.ModuleType('selenium')
    package.__path__ = []
    package.webdriver = webdriver
    return {'selenium': package, 'selenium.webdriver': webdriver}


class StubProvider:
    """Answers with PROPOSALS[key], or nothing for any other key. Records each request."""

    def __init__(self):
        self.keys = []
        self.outcomes = []

    def propose(self, bundle):
        self.keys.append(bundle['key'])
        xpath = PROPOSALS.get(bundle['key'])
        return {'xpath': xpath, 'fallbacks': [], 'reasoning': 'stub'} if xpath else None

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, bool(success)))


def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('ANTHROPIC_', 'OPENAI_', 'GEMINI_', 'OLLAMA_', 'MENDWRIGHT_')):
            monkeypatch.delenv(name)
    monkeypatch.setenv('MENDWRIGHT_TIMEOUT_MS', '0')    # a missing element fails at once


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The fakes installed, the provider stubbed, and the cwd a fresh folder (the README
    code writes ./.mendwright/)."""
    site = Site()
    stub = StubProvider()
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mendwright.providers, 'auto_provider', lambda *a, **k: stub)
    for name, module in {**_playwright_modules(site), **_selenium_modules(site)}.items():
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(site=site, stub=stub, store=tmp_path / '.mendwright')


def _journal(folder):
    path = Path(folder) / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _check_the_heal(store_dir):
    """The heal for accept_terms is in the override file and the journal."""
    store = json.loads((Path(store_dir) / 'overrides.json').read_text(encoding='utf-8'))
    terms = store['selectors']['accept_terms']
    assert terms['expectedRole'] == 'checkbox'
    heals = [c['xpath'] for c in terms['candidates'] if c['source'] == 'heal']
    assert heals == [PROPOSALS['accept_terms']]
    events = [(e['event'], e.get('key')) for e in _journal(store_dir)]
    assert ('heal.applied', 'accept_terms') in events
    return events


# --- the tests ---------------------------------------------------------------------------------

def test_the_readme_has_the_python_blocks_this_file_runs():
    assert SKIPPED == []
    assert len(PY_BLOCKS) == EXPECTED_PYTHON_BLOCKS


FLOW_BLOCKS = [(code, line) for lang, code, marker, line in BLOCKS
               if lang == 'python' and marker == FLOW_MARK]
SCRIPT_BLOCKS = [b for b in PY_BLOCKS if 'def test_' not in b[0] and b not in FLOW_BLOCKS]


@pytest.mark.parametrize('code, line', SCRIPT_BLOCKS, ids=['line' + str(b[1]) for b in SCRIPT_BLOCKS])
def test_readme_python_block_runs_as_written(world, code, line):
    namespace = {'__name__': '__readme__'}
    exec(compile(code, 'README.md:' + str(line), 'exec'), namespace)

    site = world.site
    assert site.unsupported == []
    # The checkbox selector was broken, so it was healed with one model call, and the
    # step ran on the element the heal found.
    assert world.stub.keys == ['accept_terms']
    assert site.element("//input[@type='checkbox']").get('checked') is not None
    if '.fill(' in code:
        assert site.element("//input[@id='email']").get('value') == 'ada@example.com'
    if 'create_account' in code:
        assert ('click', 'css=#create-account') in site.actions
    _check_the_heal(world.store)


def test_the_readme_flow_block_runs_as_written(world):
    assert len(FLOW_BLOCKS) == 1, 'README.md should mark the flow example with ' + FLOW_MARK
    code, line = FLOW_BLOCKS[0]
    site = world.site
    site.pages[SIGNUP_URL] = FLOW_HTML
    site.load(SIGNUP_URL)
    namespace = {'__name__': '__readme__', 'mendwright': mendwright, 'page': SyncPage(site)}
    exec(compile(code, 'README.md:' + str(line), 'exec'), namespace)

    # The banner was rejected and the modal closed before the first step, then the
    # steps ran. No model call was needed.
    clicks = [selector for verb, selector in site.actions if verb == 'click']
    assert len(clicks) == 3
    assert site.doc.xpath("//*[@id='onetrust-consent-sdk'] | //*[@id='newsletter']") == []
    assert clicks[1:] == ['css=#newsletter-close', 'css=#create-account']
    assert site.element("//input[@id='email']").get('value') == 'ada@example.com'
    assert world.stub.keys == []
    heal = namespace['heal']
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookie_banner', 'email'), ('newsletter', 'email')]
    events = [(e['event'], e.get('key')) for e in _journal(world.store)]
    assert events == [('flow.interrupt', 'cookie_banner'), ('flow.interrupt', 'newsletter')]


PYTEST_BLOCKS = [b for b in PY_BLOCKS if 'def test_' in b[0]]
PLUGIN = ('-p', 'no:mendwright', '-p', 'mendwright.pytest_plugin', '-p', 'no:cacheprovider',
          '-W', 'ignore::pytest.PytestAssertRewriteWarning')
INNER_CONFTEST = '''
import importlib.util

import pytest

import mendwright.providers

_spec = importlib.util.spec_from_file_location('_readme_fakes', {path!r})
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


@pytest.fixture
def page():
    return _fakes.SyncPage(_fakes.Site())


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    stub = _fakes.StubProvider()
    monkeypatch.setattr(mendwright.providers, 'auto_provider', lambda *a, **k: stub)
'''


@pytest.mark.parametrize('code, line', PYTEST_BLOCKS, ids=['line' + str(b[1]) for b in PYTEST_BLOCKS])
def test_readme_pytest_block_passes_with_the_plugin(pytester, monkeypatch, code, line):
    _clean_env(monkeypatch)
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    pytester.makeconftest(INNER_CONFTEST.format(path=str(Path(__file__).resolve())))
    pytester.makepyfile(test_readme_block=code)

    result = pytester.runpytest_inprocess(*PLUGIN)

    result.assert_outcomes(passed=1)
    # The heal fixture saved the heal, and the passing test confirmed it.
    events = _check_the_heal(pytester.path / '.mendwright')
    assert events[-1] == ('heal.canary', 'accept_terms')
    canary = [e for e in _journal(pytester.path / '.mendwright') if e['event'] == 'heal.canary']
    assert [e['success'] for e in canary] == [True]


def test_readme_quotes_the_attention_required_text_the_code_raises(world):
    quoted = [code for lang, code, marker, line in BLOCKS if marker == ATTENTION_MARK]
    assert len(quoted) == 1, 'README.md should mark the quoted error with ' + ATTENTION_MARK

    world.site.set_content(SIGNUP_HTML)       # has no #pay, so the selector misses
    heal = mendwright.open(SyncPage(world.site))
    with pytest.raises(mendwright.AttentionRequired) as caught:
        heal.locator('pay', '#pay', destructive=True).click()
    error = caught.value
    raised = type(error).__module__ + '.' + type(error).__qualname__ + ': ' + str(error)

    assert raised == quoted[0].rstrip('\n')
    assert world.stub.keys == []              # refused before any model call


_ENV_NAME = re.compile(r'\b(?:MENDWRIGHT|ANTHROPIC|OPENAI|GEMINI|OLLAMA|BEDROCK|LITELLM|AWS)_[A-Z_]+\b')
_PYTEST_OPTION = re.compile(r'--mendwright-[a-z-]+')


def _source_text():
    parts = [p.read_text(encoding='utf-8') for p in sorted(SOURCES.rglob('*.py'))]
    gateway = ROOT / 'examples' / 'heal_gateway.mjs'
    if gateway.exists():
        parts.append(gateway.read_text(encoding='utf-8'))
    return '\n'.join(parts)


def _docs():
    docs = [README]
    if GUIDE.exists():
        docs.append(GUIDE)
    return docs


def test_every_environment_variable_the_docs_name_is_read_by_the_code():
    source = _source_text()
    browser_suite = ROOT / 'tests' / 'browser'
    if browser_suite.exists():
        source += '\n'.join(p.read_text(encoding='utf-8') for p in browser_suite.glob('*.py'))
    for doc in _docs():
        names = set(_ENV_NAME.findall(doc.read_text(encoding='utf-8')))
        assert names, doc
        missing = sorted(n for n in names if n not in source)
        assert missing == [], doc.name + ' names variables mendwright never reads: ' + str(missing)


def test_every_pytest_option_the_docs_name_exists():
    plugin = (SOURCES / 'pytest_plugin.py').read_text(encoding='utf-8')
    for doc in _docs():
        options = set(_PYTEST_OPTION.findall(doc.read_text(encoding='utf-8')))
        missing = sorted(o for o in options if o not in plugin)
        assert missing == [], doc.name + ' names pytest options the plugin lacks: ' + str(missing)


# --- links ---------------------------------------------------------------------------------------

_MD_LINK = re.compile(r'\]\(([^)\s]+)\)')
_HTML_URL = re.compile(r'(?:src|srcset|href)="([^"]+)"')
_REPO_URL = re.compile(r'^https://(?:github\.com/saadbuilt/mendwright/(?:blob|tree)/main/'
                       r'|raw\.githubusercontent\.com/saadbuilt/mendwright/main/)(.+)$')
# Produced from a terminal recording of `mendwright demo`, outside this test suite.
_RECORDED = set()  # assets produced by another step; none outstanding


def _targets(text):
    return _MD_LINK.findall(text) + _HTML_URL.findall(text)


def test_readme_links_are_absolute_so_they_work_on_pypi():
    relative = [t for t in _targets(README_TEXT) if not t.startswith(('https://', '#'))]
    assert relative == []


def test_readme_links_into_the_repo_point_at_files_on_main():
    if not (ROOT / 'docs').is_dir():
        pytest.skip('docs/ is not in this copy of the project (the sdist leaves it out)')
    paths = []
    for target in _targets(README_TEXT):
        if 'saadbuilt/mendwright' in target and '/actions/' not in target:
            m = _REPO_URL.match(target.split('#')[0])
            assert m, 'link into the repo that is not on the main branch: ' + target
            paths.append(m.group(1))
    assert paths
    missing = [p for p in paths if not (ROOT / p).exists() and p not in _RECORDED]
    assert missing == []


def test_guide_relative_links_point_at_existing_files():
    if not GUIDE.exists():
        pytest.skip('docs/guide.md is not in this copy of the project (the sdist leaves it out)')
    text = re.sub(r'```.*?```', '', GUIDE.read_text(encoding='utf-8'), flags=re.S)
    checked = 0
    for target in _targets(text):
        if target.startswith(('http://', 'https://', 'mailto:', '#')):
            continue
        path = (GUIDE.parent / target.split('#')[0]).resolve()
        assert path.exists(), 'docs/guide.md links to a missing file: ' + target
        checked += 1
    assert checked
