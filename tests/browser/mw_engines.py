"""Browser engines and test pages for the real-browser suite in tests/browser.

Every test here runs on each engine through the same small adapter, so a scenario is
written once. Nothing in this module imports playwright, selenium, patchright or camoufox
at import time: the default test run still collects tests/browser in jobs that have none
of them installed, and only deselects the tests afterwards (pyproject addopts).

Engines start lazily, one at a time, headless. An engine whose library or browser is not
on this machine skips its tests with the reason and the command that fixes it. Pages are
loaded with page.set_content() on Playwright and from a file:// page written to tmp_path
on Selenium, so nothing touches the network. tab.load() keeps one address for the whole
test (as a redesign of one page would); tab.load_at(html, path) gives a page its own
path, served from a route on Playwright and from its own file on Selenium. For a site of
several pages that link to each other, tab.serve(pages) serves them all without opening
one, tab.address(path) is where a page links to another, and tab.visit(path) opens one.
"""

import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PLAYWRIGHT = 'playwright'
SELENIUM = 'selenium'

# Engine id -> family. The ids are what pytest prints, for example test_x[pw-webkit].
ENGINES = {
    'pw-chromium': PLAYWRIGHT,
    'pw-firefox': PLAYWRIGHT,
    'pw-webkit': PLAYWRIGHT,
    'patchright': PLAYWRIGHT,
    'camoufox': PLAYWRIGHT,
    'sel-chrome': SELENIUM,
    'sel-firefox': SELENIUM,
}
ENGINE_IDS = tuple(ENGINES)

# How long a test waits on a selector it expects to be broken before mendwright heals.
FAST_MS = 300
POLL_MS = 50
# Playwright's own timeout for actions, so a failing action fails in seconds.
ACTION_TIMEOUT_MS = 5000


def family_of(engine_id):
    return ENGINES[engine_id]


# --- pages -------------------------------------------------------------------------------

def page(body, title='mendwright test'):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + title
            + '</title></head><body>' + body + '</body></html>')


# A signup form before and after a front-end redesign. The redesign drops the checkbox's
# hand-picked id, so the selector written against v1 stops matching, while the role and
# the label text stay the same. Same story as examples/demo.
SIGNUP_V1 = page("""
<form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <label for="password">Password</label><input id="password" name="password" type="password">
  <div class="consent">
    <label><input id="accept-terms" name="terms" type="checkbox">
      <span>I accept the terms and conditions</span></label>
  </div>
  <button type="button" id="next">Next</button>
</form>""", 'signup v1')

TERMS_V2 = """<fieldset class="legal"><ds-checkbox>
    <label class="ds-checkbox__control">
      <input type="checkbox" name="agreement" id="mui-generated-8842" data-testid="legal-consent">
      <span>I accept the terms and conditions</span></label>
  </ds-checkbox></fieldset>"""

SIGNUP_V2 = page("""
<form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <label for="password">Password</label><input id="password" name="password" type="password">
  """ + TERMS_V2 + """
  <button type="button" id="next">Next</button>
</form>""", 'signup v2')

STALE = "//input[@id='accept-terms']"
HEALED = "//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']"
V2_CHECKBOX = "[data-testid='legal-consent']"

# The v2 consent block inside an iframe, on a page that has no checkbox of its own.
IFRAME_PAGE = page(
    '<h1>Outer page</h1><p>No checkbox out here.</p>'
    '<iframe id="consent-frame" width="600" height="200" srcdoc="'
    + page(TERMS_V2, 'consent').replace('&', '&amp;').replace('"', '&quot;')
    + '"></iframe>', 'iframe')

# A visible and a display:none button. A proposal for the hidden one passes the static
# gate (one button in the captured HTML) and must fail the live gate.
HIDDEN_PAGE = page("""
<form>
  <button type="button" id="ghost" style="display:none">Continue</button>
  <button type="button" id="back">Back</button>
</form>""")

PAY_PAGE = page('<form><button type="button" id="pay-now">Pay now</button></form>')


def neutral_button_page(button_id):
    """One button labelled Continue, with the given id."""
    return page('<form><button type="button" id="' + button_id + '">Continue</button></form>')


TWO_BUTTONS = page("""
<form>
  <button type="button" id="a">Alpha</button>
  <button type="button" id="b">Beta</button>
</form>""")


def country_page(select_id):
    return page('<form><label for="' + select_id + '">Country</label>'
                '<select id="' + select_id + '"><option value="nl">NL</option>'
                '<option value="de">DE</option></select></form>')


FORM_CONTROLS = page("""
<form>
  <input id="name" type="text" value="old">
  <input id="news" type="checkbox">
  <select id="color"><option value="red">Red</option><option value="blue">Blue</option></select>
</form>""")

ACTIONS_PAGE = page("""
<form>
  <input id="name" type="text">
  <input id="news" type="checkbox">
  <select id="color"><option value="red">Red</option><option value="blue">Blue</option></select>
  <button type="button" id="go"
    onclick="document.getElementById('out').textContent='clicked'">Go</button>
  <input id="search" type="text" onkeydown="if (event.key === 'Enter')
    document.getElementById('out2').textContent = 'enter:' + this.value">
</form>
<p id="out">idle</p><p id="out2"></p>""")

SAVE_PAGE = page("""
<form id="editor">
  <button type="button" id="save-btn"
    onclick="document.getElementById('out').textContent='saved'">Save</button>
</form>
<p id="out">idle</p>""")


# --- tabs: one fresh page per test --------------------------------------------------------

def _js_string(text):
    return json.dumps(text)


class Tab:
    """A fresh page on one engine. target is what the test passes to mendwright.open()."""

    family = None

    def __init__(self, engine_id):
        self.engine_id = engine_id

    def checked(self, css):
        return self.js('document.querySelector(' + _js_string(css) + ').checked')

    def value(self, css):
        return self.js('document.querySelector(' + _js_string(css) + ').value')

    def text(self, css):
        return self.js('document.querySelector(' + _js_string(css) + ').textContent')


SITE = 'https://mendwright.test'


class PlaywrightTab(Tab):
    family = PLAYWRIGHT

    def __init__(self, engine_id, browser):
        super().__init__(engine_id)
        self.page = browser.new_page()
        self.page.set_default_timeout(ACTION_TIMEOUT_MS)
        self._ctx = self.page
        self._served = {}

    @property
    def target(self):
        return self.page

    def load(self, html):
        self._ctx = self.page
        self.page.set_content(html)

    def load_at(self, html, path):
        """Show html at SITE + path, served by a route (no network)."""
        if not self._served:
            self.page.route(SITE + '/**', self._serve)
        self._served[path] = html
        self._ctx = self.page
        self.page.goto(SITE + path)

    def serve(self, pages):
        """Serve each of pages ({path: html}) at SITE + path, from the route, without
        opening one. A path served again gets its new html."""
        if not self._served:
            self.page.route(SITE + '/**', self._serve)
        self._served.update(pages)

    def address(self, path):
        """Where a served page is, for a link from another one."""
        return SITE + path

    def visit(self, path):
        """Open a page that serve() serves."""
        self._ctx = self.page
        self.page.goto(SITE + path)

    def _serve(self, route):
        html = self._served.get(urlsplit(route.request.url).path)
        if html is None:
            route.fulfill(status=404, body='')
        else:
            route.fulfill(status=200, content_type='text/html', body=html)

    def js(self, expression):
        return self._ctx.evaluate('() => (' + expression + ')')

    def enter_frame(self, css):
        """The iframe workaround on Playwright: open() the Frame instead of the page.
        Later js() calls run inside that frame."""
        assert self.page.query_selector(css) is not None, css
        frame = self.page.frames[1]
        self._ctx = frame
        return frame

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


class SeleniumTab(Tab):
    family = SELENIUM

    def __init__(self, engine_id, driver, workdir):
        super().__init__(engine_id)
        self.driver = driver
        self._workdir = Path(workdir)
        self._loads = 0
        self._reset()

    @property
    def target(self):
        return self.driver

    def _reset(self):
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        self.driver.get('about:blank')

    def load(self, html):
        # One file for every load: a page that changes keeps its address, as on a real
        # site. The query only makes the browser read the file again.
        self._loads += 1
        path = self._workdir / 'page.html'
        path.write_text(html, encoding='utf-8')
        self.driver.switch_to.default_content()
        self.driver.get(path.as_uri() + '?load=' + str(self._loads))

    def load_at(self, html, path):
        """Show html from its own file, site/<path>.html, so its address differs."""
        self._loads += 1
        target = self._workdir.joinpath('site', *path.strip('/').split('/'))
        target = target.with_name(target.name + '.html')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding='utf-8')
        self.driver.switch_to.default_content()
        self.driver.get(target.as_uri() + '?load=' + str(self._loads))

    def _site_file(self, path):
        target = self._workdir.joinpath('site', *path.strip('/').split('/'))
        return target.with_name(target.name + '.html')

    def serve(self, pages):
        """Write each of pages ({path: html}) to its own file, site/<path>.html, without
        opening one. A path served again gets its new html."""
        for path, html in pages.items():
            target = self._site_file(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html, encoding='utf-8')

    def address(self, path):
        """Where a served page is, for a link from another one: its file:// URL."""
        return self._site_file(path).as_uri()

    def visit(self, path):
        """Open a page that serve() wrote."""
        self._loads += 1
        self.driver.switch_to.default_content()
        self.driver.get(self.address(path) + '?load=' + str(self._loads))

    def js(self, expression):
        return self.driver.execute_script('return (' + expression + ');')

    def enter_frame(self, css):
        """The iframe workaround on Selenium: switch into the frame, then open() the
        driver. Later js() calls run inside that frame."""
        self.driver.switch_to.frame(self.driver.find_element('css selector', css))
        return self.driver

    def close(self):
        try:
            self._reset()
        except Exception:
            pass


# --- engines -------------------------------------------------------------------------------

class Engine:
    """One browser, started once per test session and shared by its tests."""

    family = None

    def __init__(self, engine_id):
        self.id = engine_id

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def new_tab(self, workdir):
        raise NotImplementedError


def _installed(name):
    """True if the top-level package can be found. Never imports it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _require(module):
    """Import a module or skip the test with the install hint."""
    package = module.split('.')[0]
    if not _installed(package):
        pytest.skip(package + ' is not installed. Install it to run these tests: '
                    'pip install ' + package)
    return pytest.importorskip(module)


def _run_in_thread(coro_fn, timeout=120):
    """Run an async function on its own event loop in a worker thread and return its
    result. The sync Playwright API keeps a loop of its own on the main thread, so async
    code must never share that thread."""
    box = {}

    def target():
        try:
            box['value'] = asyncio.run(coro_fn())
        except BaseException as exc:  # handed back to the caller below
            box['error'] = exc

    worker = threading.Thread(target=target, name='mendwright-async-probe', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError('the async browser did not finish within ' + str(timeout) + 's')
    if 'error' in box:
        raise box['error']
    return box['value']


class PlaywrightEngine(Engine):
    """Playwright or patchright: library module, browser type and how to install it."""

    family = PLAYWRIGHT

    def __init__(self, engine_id, library, browser_type):
        super().__init__(engine_id)
        self.library = library
        self.browser_type = browser_type
        self._pw = None
        self._browser = None

    def start(self):
        sync_api = _require(self.library + '.sync_api')
        self._pw = sync_api.sync_playwright().start()
        try:
            launcher = getattr(self._pw, self.browser_type)
            exe = launcher.executable_path
            if not exe or not os.path.exists(exe):
                pytest.skip(self.library + "'s " + self.browser_type + ' browser is not installed '
                            '(' + str(exe) + ' is missing). Run: python -m ' + self.library
                            + ' install ' + self.browser_type)
            self._browser = launcher.launch(headless=True)
        except BaseException:
            self.stop()
            raise

    def stop(self):
        for close in (getattr(self._browser, 'close', None), getattr(self._pw, 'stop', None)):
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        self._browser = self._pw = None

    def new_tab(self, workdir):
        return PlaywrightTab(self.id, self._browser)

    def with_async_page(self, fn):
        """Call fn(page) with an async_api Page of the same engine, from a worker thread,
        and return what fn returns."""
        async_api = importlib.import_module(self.library + '.async_api')

        async def main():
            async with async_api.async_playwright() as p:
                browser = await getattr(p, self.browser_type).launch(headless=True)
                try:
                    page_ = await browser.new_page()
                    await page_.set_content('<button type="button">Go</button>')
                    return fn(page_)
                finally:
                    await browser.close()
        return _run_in_thread(main)


def _import_camoufox_offline():
    """Import camoufox without letting browserforge reach the network.

    browserforge, which camoufox imports, downloads its model files from GitHub when they
    are missing or older than five weeks, and it does that while being imported. On
    Windows an overwrite keeps the file's old creation date, so once the files are five
    weeks old the download repeats on every import. These tests only need the files to be
    on disk: when they are, the refresh is turned off for the import; when they are not,
    the camoufox tests are skipped."""
    if 'camoufox.sync_api' in sys.modules:
        return
    try:
        download = importlib.import_module('browserforge.download')
        paths = list(download._get_all_paths(headers=True, fingerprints=True))
        original = download.Download
    except Exception as exc:
        pytest.skip('camoufox: could not check browserforge model files without a download '
                    '(' + type(exc).__name__ + ': ' + str(exc) + '). Run camoufox once '
                    'outside the tests, then rerun.')
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        pytest.skip("camoufox needs browserforge's model files, and " + missing[0] + ' is '
                    'missing. Run python -m browserforge update once to download them, '
                    'then rerun.')

    def no_download(*args, **kwargs):
        return None   # the files are on disk; keep using them

    download.Download = no_download
    try:
        importlib.import_module('camoufox.sync_api')
        importlib.import_module('camoufox.async_api')
    finally:
        download.Download = original


class CamoufoxEngine(Engine):
    family = PLAYWRIGHT

    def __init__(self, engine_id):
        super().__init__(engine_id)
        self._cm = None
        self._browser = None

    def _options(self):
        from camoufox import DefaultAddons
        # The default uBlock Origin addon is downloaded on first launch; leave it out.
        return {'headless': True, 'exclude_addons': [DefaultAddons.UBO]}

    def start(self):
        if not _installed('camoufox'):
            pytest.skip('camoufox is not installed. Install it to run these tests: '
                        'pip install camoufox')
        _require('playwright.sync_api')
        _import_camoufox_offline()
        from camoufox.pkgman import camoufox_path, launch_path
        try:
            camoufox_path(download_if_missing=False)
            exe = launch_path()
        except Exception as exc:
            # Never 'camoufox fetch' from a test: that downloads a whole browser.
            pytest.skip("camoufox's browser is not installed or is out of date ("
                        + type(exc).__name__ + ': ' + str(exc).splitlines()[0] + '). '
                        'Run: python -m camoufox fetch')
        if not os.path.exists(exe):
            pytest.skip("camoufox's browser is missing at " + exe + '. '
                        'Run: python -m camoufox fetch')
        from camoufox.sync_api import Camoufox
        self._cm = Camoufox(**self._options())
        try:
            self._browser = self._cm.__enter__()
        except BaseException:
            self._cm = None
            raise

    def stop(self):
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        self._cm = self._browser = None

    def new_tab(self, workdir):
        return PlaywrightTab(self.id, self._browser)

    def with_async_page(self, fn):
        from camoufox.async_api import AsyncCamoufox
        options = self._options()

        async def main():
            async with AsyncCamoufox(**options) as browser:
                page_ = await browser.new_page()
                await page_.set_content('<button type="button">Go</button>')
                return fn(page_)
        return _run_in_thread(main)


def _first_existing(*candidates):
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _program_dirs():
    return [os.environ.get(name) for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]


def find_chrome():
    on_path = [shutil.which(n) for n in ('google-chrome', 'google-chrome-stable', 'chrome',
                                         'chromium', 'chromium-browser')]
    windows = [os.path.join(d, 'Google', 'Chrome', 'Application', 'chrome.exe')
               for d in _program_dirs() if d]
    mac = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    return _first_existing(*on_path, *windows, *mac)


def find_firefox():
    on_path = [shutil.which('firefox')]
    windows = [os.path.join(d, 'Mozilla Firefox', 'firefox.exe') for d in _program_dirs() if d]
    mac = ['/Applications/Firefox.app/Contents/MacOS/firefox']
    return _first_existing(*on_path, *windows, *mac)


_DRIVER_FILES = ('chromedriver', 'chromedriver.exe', 'geckodriver', 'geckodriver.exe')


@contextlib.contextmanager
def _drivers_off_path():
    """PATH without the folders that hold a chromedriver or geckodriver, for the block."""
    old = os.environ.get('PATH', '')
    kept = [d for d in old.split(os.pathsep)
            if not any(os.path.isfile(os.path.join(d, f)) for f in _DRIVER_FILES)]
    os.environ['PATH'] = os.pathsep.join(kept)
    try:
        yield
    finally:
        os.environ['PATH'] = old


class SeleniumEngine(Engine):
    """Selenium with an installed browser. The browser binary is passed explicitly, so
    Selenium Manager only ever fetches a driver, never a browser."""

    family = SELENIUM

    def __init__(self, engine_id, browser):
        super().__init__(engine_id)
        self.browser = browser
        self._driver = None

    def start(self):
        webdriver = _require('selenium.webdriver')
        exceptions = importlib.import_module('selenium.common.exceptions')
        if self.browser == 'chrome':
            binary = find_chrome()
            label = 'Google Chrome'
        else:
            binary = find_firefox()
            label = 'Firefox'
        if binary is None:
            pytest.skip(label + ' is not installed here, so the Selenium + ' + label + ' tests '
                        'cannot run. Install ' + label + ' to run them.')
        if self.browser == 'chrome':
            options = webdriver.ChromeOptions()
            for arg in ('--headless=new', '--window-size=1280,900', '--disable-gpu',
                        '--disable-dev-shm-usage', '--no-first-run'):
                options.add_argument(arg)
            if sys.platform.startswith('linux'):
                # CI containers often cannot give Chrome its sandbox. Playwright turns the
                # sandbox off by default for the same reason.
                options.add_argument('--no-sandbox')
            make = webdriver.Chrome
        else:
            options = webdriver.FirefoxOptions()
            options.add_argument('-headless')
            make = webdriver.Firefox
        options.binary_location = binary
        try:
            try:
                self._driver = make(options=options)
            except exceptions.SessionNotCreatedException as exc:
                if 'only supports' not in str(exc):
                    raise
                # A driver on PATH that does not match the browser. SeleniumBase's pytest
                # plugin, for one, puts its own drivers first on PATH. Let Selenium Manager
                # pick a matching driver instead.
                with _drivers_off_path():
                    self._driver = make(options=options)
        except exceptions.WebDriverException as exc:
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            pytest.skip('could not start ' + label + ' through Selenium (' + first + '). '
                        'Selenium Manager fetches a matching driver on first use, so check '
                        'the network for that, or put a driver that matches the browser on PATH.')

    def stop(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
        self._driver = None

    def new_tab(self, workdir):
        return SeleniumTab(self.id, self._driver, workdir)


def make_engine(engine_id):
    if engine_id == 'patchright':
        return PlaywrightEngine(engine_id, 'patchright', 'chromium')
    if engine_id == 'camoufox':
        return CamoufoxEngine(engine_id)
    if engine_id.startswith('pw-'):
        return PlaywrightEngine(engine_id, 'playwright', engine_id[len('pw-'):])
    if engine_id.startswith('sel-'):
        return SeleniumEngine(engine_id, engine_id[len('sel-'):])
    raise ValueError('unknown engine ' + repr(engine_id))


# --- reading what mendwright saved ---------------------------------------------------------

def saved_entry(store_file, key):
    """The key's entry in the override file, as written to disk."""
    data = json.loads(Path(store_file).read_text(encoding='utf-8'))
    return data['selectors'][key]


def saved_candidates(store_file, key):
    """{selector: candidate dict} for the key, as written to disk."""
    return {c['xpath']: c for c in saved_entry(store_file, key)['candidates']}


def journal_events(store_file):
    """The journal lines next to the store file, parsed."""
    path = Path(store_file).parent / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]
