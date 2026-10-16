"""heal.drift() and `mendwright check` against real browsers.

heal.drift() runs on every engine of the suite (the tab fixture: Playwright, patchright,
camoufox and Selenium), on await heal.drift() for the async Playwright engines, and the
command runs in Playwright's Chromium, Firefox and WebKit against a local HTTP server on
127.0.0.1 (Site below), which logs every request. A store is recorded the way a run
records it: mendwright.open() on v1 of a page. Then the page changes and drift is
checked. Every page reports its click, input, keydown, submit, focus and change events
to the server (and to window.__events), so a test sees any action mendwright took.

No model is ever asked: providers are stand-ins or _NullProvider.
"""

import http.server
import json
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import mendwright
from mendwright import cli
from mendwright.facade import _NullProvider
from mw_engines import FAST_MS, POLL_MS

pytestmark = pytest.mark.browser

# Reports every click, input, keydown, submit, focus and change on an element of the page to
# window.__events and the data-events attribute of <html>, and, on a served page, to the
# server as GET /event?type=... (a GET, so the server's log of methods stays clean).
WATCH = """<script>
window.__events = [];
for (const type of ['click', 'input', 'keydown', 'submit', 'focus', 'change']) {
  document.addEventListener(type, (e) => {
    // The browser focusing the page itself on load is no action on an element.
    const el = e.target;
    if (!(el instanceof Element) || el === document.body || el === document.documentElement) return;
    window.__events.push(type);
    // In the DOM too: patchright and camoufox evaluate in a world of their own.
    document.documentElement.dataset.events = window.__events.join(' ');
    if (location.protocol.startsWith('http')) {
      fetch('/event?type=' + type + '&id=' + encodeURIComponent((e.target && e.target.id) || ''));
    }
  }, true);
}
</script>"""


def page(body, title='drift'):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + title
            + '</title>' + WATCH + '</head><body>' + body + '</body></html>')


SIGNUP_V1 = page("""<form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <label><input type="checkbox" id="accept-terms" name="terms">
    <span>I accept the terms and conditions</span></label>
  <label><input type="checkbox" id="news" class="opt"> Send me the newsletter</label>
  <button type="button" id="pay">Pay now</button>
  <button type="button" id="submit">Create account</button>
</form>""")

SIGNUP_V2 = page("""<form id="signup">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <fieldset class="legal"><label class="ds-checkbox">
    <input type="checkbox" name="agreement" data-testid="legal-consent">
    <span>I accept the terms and conditions</span></label></fieldset>
  <label><input type="checkbox" class="opt"> Send me the newsletter</label>
  <label><input type="checkbox" class="opt"> Send me the newsletter</label>
  <button type="button" id="pay-now">Pay now</button>
  <div id="submit">Create account</div>
</form>""")

SIGNUP_KEYS = [('email', '#email', 'input'), ('accept_terms', '#accept-terms', 'checkbox'),
               ('newsletter', '#news', 'checkbox'), ('pay', '#pay', 'button'),
               ('submit', '#submit', 'button')]
SIGNUP_VERDICTS = {'email': 'ok', 'accept_terms': 'would-heal', 'newsletter': 'refused',
                   'pay': 'refused', 'submit': 'wrong-role'}

BROWSERS = ('chromium', 'firefox', 'webkit')


# --- the local site ------------------------------------------------------------------------------

class Site:
    """An HTTP server on 127.0.0.1 in a thread. pages maps a path to html, or to a function
    of the request handler that returns (status, headers, body). requests logs (method,
    path with its query) of every request."""

    def __init__(self):
        self.pages = {}
        self.requests = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _log(self):
                site.requests.append((self.command, self.path))

            def do_GET(self):   # noqa: N802 (http.server's name)
                self._log()
                path = urlsplit(self.path).path
                answer = site.pages.get(path)
                if callable(answer):
                    status, headers, body = answer(self)
                elif isinstance(answer, str):
                    status, headers, body = 200, {'Content-Type': 'text/html; charset=utf-8'}, answer
                else:
                    status, headers, body = 404, {'Content-Type': 'text/html'}, '<p>Not found</p>'
                data = body.encode('utf-8')
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _refuse(self):
                self._log()
                self.send_response(405)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse   # noqa: N815

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method='GET'):
        return [urlsplit(p).path for m, p in self.requests if m == method]

    def events(self):
        return [parse_qs(urlsplit(p).query) for m, p in self.requests
                if urlsplit(p).path == '/event']


@pytest.fixture
def site():
    served = Site()
    yield served
    served.close()


def _threaded(fn):
    """fn() in a thread of its own, which gets its own sync Playwright."""
    box = {}

    def target():
        try:
            box['value'] = fn()
        except BaseException as exc:
            box['error'] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(180)
    if 'error' in box:
        raise box['error']
    return box.get('value')


def _need(browser):
    sync_api = pytest.importorskip('playwright.sync_api')

    def check():
        with sync_api.sync_playwright() as p:
            exe = getattr(p, browser).executable_path
            return bool(exe) and os.path.exists(exe)
    if not _threaded(check):
        pytest.skip("Playwright's " + browser + ' browser is not installed. Run: python -m '
                    'playwright install ' + browser)


def record(site, store, path, keys, browser='chromium', cookies=None):
    """A first run: open the page at site + path in a real browser and look each key up,
    so the store holds its selector, role, page, address and fingerprint."""
    from playwright.sync_api import sync_playwright

    def work():
        with sync_playwright() as p:
            launched = getattr(p, browser).launch(headless=True)
            try:
                context = launched.new_context()
                if cookies:
                    context.add_cookies(cookies)
                tab = context.new_page()
                tab.goto(site.origin + path)
                heal = mendwright.open(tab, provider=_NullProvider(), store_path=store)
                for key, selector, role in keys:
                    heal.locator(key, selector, role=role, timeout_ms=5000, poll_ms=POLL_MS,
                                 destructive=True if key == 'pay' else None)
            finally:
                launched.close()
    _threaded(work)


def run_check(capsys, *argv):
    code = _threaded(lambda: cli.main(['check'] + list(argv)))
    out = capsys.readouterr()
    return code, out.out, out.err


def _json(out):
    data = json.loads(out)
    return data, {k['key']: k for k in data['keys']}


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / 'store' / 'overrides.json')


def _unchanged(store):
    path = Path(store)
    return path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(path.parent))


# --- mendwright check ----------------------------------------------------------------------------

@pytest.mark.parametrize('browser', BROWSERS)
def test_check_a_signup_redesign(browser, site, store, capsys, monkeypatch):
    _need(browser)
    site.pages['/signup'] = SIGNUP_V1
    record(site, store, '/signup', SIGNUP_KEYS, browser)
    before = _unchanged(store)
    site.pages['/signup'] = SIGNUP_V2
    site.requests.clear()
    code, out, err = run_check(capsys, '--store', store, '--base-url', site.origin, '--browser',
                               browser, '--timeout-ms', str(FAST_MS), '--format', 'json')
    data, keys = _json(out)
    assert code == 1 and data['exit_code'] == 1, err
    assert {k: v['verdict'] for k, v in keys.items()} == SIGNUP_VERDICTS
    assert keys['accept_terms']['heal']['selector'] == "//input[@data-testid='legal-consent']"
    assert 'no model provider is configured' in keys['newsletter']['reason']
    assert _unchanged(store) == before
    assert site.events() == [], site.events()
    assert [p for p in site.paths('GET') if p != '/favicon.ico'] == ['/signup']
    assert [m for m, _p in site.requests if m != 'GET'] == []
    # A provider in the environment: a run would ask it, check asks nothing.
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_MODEL', 'qwen-test')
    monkeypatch.setenv('OLLAMA_HOST', 'http://127.0.0.1:9')     # nothing listens there
    code, out, _err = run_check(capsys, '--store', store, '--base-url', site.origin, '--browser',
                                browser, '--timeout-ms', str(FAST_MS), '--format', 'json')
    data, keys = _json(out)
    assert keys['newsletter']['verdict'] == 'needs-model'
    assert data['provider']['model'] == 'qwen-test'
    assert _unchanged(store) == before


def test_check_text_report_and_its_cost(site, store, capsys):
    _need('chromium')
    site.pages['/signup'] = SIGNUP_V1
    record(site, store, '/signup', SIGNUP_KEYS)
    site.pages['/signup'] = SIGNUP_V2
    code, out, err = run_check(capsys, '--store', store, '--origin', site.origin,
                               '--timeout-ms', str(FAST_MS))
    assert code == 1
    assert err.startswith('mendwright check: loading 1 page from ' + site.origin
                          + ' with Playwright chromium (headless).')
    assert out.startswith('mendwright check: 5 keys on 1 page, Playwright chromium (headless), ')
    assert site.origin + '/signup  (HTTP 200, ' in out
    assert out.rstrip().endswith('Exit 1: 4 keys broke.')


def test_check_a_redirect_to_login_and_a_404(site, store, capsys):
    _need('chromium')
    site.pages['/account'] = page("<a id='orders' href='/orders'>Orders</a>")
    site.pages['/help'] = page("<a id='contact' href='/contact'>Contact</a>")
    record(site, store, '/account', [('orders', '#orders', 'link')])
    record(site, store, '/help', [('contact', '#contact', 'link')])
    site.pages['/account'] = lambda handler: (302, {'Location': '/login?next=/account'}, '')
    site.pages['/login'] = page("<form><input type='password' id='pw'></form>")
    del site.pages['/help']
    code, out, _err = run_check(capsys, '--store', store, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS), '--format', 'json')
    data, keys = _json(out)
    assert code == 3
    assert (keys['orders']['verdict'], keys['orders']['detail']) == ('unreachable', 'redirect')
    assert keys['orders']['reason'].startswith('the page went to /login instead')
    assert (keys['contact']['verdict'], keys['contact']['detail']) == ('unreachable', 'http-4xx')
    assert 'HTTP 404' in keys['contact']['reason']


def test_check_with_a_storage_state(site, store, tmp_path, capsys):
    _need('chromium')

    def account(handler):
        signed_in = 'session=ok' in (handler.headers.get('Cookie') or '')
        body = (page("<main><a id='orders' href='/orders'>Orders</a></main>") if signed_in
                else page("<form><input id='user'><input type='password' id='pw'>"
                          "<button>Sign in</button></form>"))
        return 200, {'Content-Type': 'text/html'}, body
    site.pages['/account'] = account
    cookie = {'name': 'session', 'value': 'ok', 'domain': '127.0.0.1', 'path': '/',
              'expires': -1, 'httpOnly': False, 'secure': False, 'sameSite': 'Lax'}
    record(site, store, '/account', [('orders', '#orders', 'link')], cookies=[cookie])
    code, out, _err = run_check(capsys, '--store', store, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS), '--format', 'json')
    _data, keys = _json(out)
    assert code == 3 and keys['orders']['detail'] == 'sign-in'
    state = tmp_path / 'check-auth.json'
    state.write_text(json.dumps({'cookies': [cookie], 'origins': []}), encoding='utf-8')
    before = (state.read_bytes(), state.stat().st_mtime_ns)
    code, out, err = run_check(capsys, '--store', store, '--origin', site.origin,
                               '--timeout-ms', str(FAST_MS), '--storage-state', str(state))
    assert code == 0, out + err
    assert (state.read_bytes(), state.stat().st_mtime_ns) == before
    assert 'session' not in out + err


CONSENT_WALL = """
<div id="didomi-notice" role="dialog" style="position:fixed;inset:0;background:#fff">
  <p>We use cookies.</p>
  <button id="didomi-notice-disagree-button"
    onclick="document.getElementById('didomi-notice').remove();
             document.getElementById('signup').style.display='block'">Disagree</button>
  <button id="didomi-notice-agree-button"
    onclick="document.getElementById('didomi-notice').remove();
             document.getElementById('signup').style.display='block'">Agree</button>
</div>
<form id="signup" style="display:none"><input id="email" name="email" type="email"></form>"""


def test_check_a_consent_wall(site, store, capsys):
    _need('chromium')
    site.pages['/signup'] = page("<form id='signup'><input id='email' name='email' "
                                 "type='email'></form>")
    record(site, store, '/signup', [('email', '#email', 'input')])
    site.pages['/signup'] = page(CONSENT_WALL)
    site.requests.clear()
    code, out, _err = run_check(capsys, '--store', store, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS))
    assert code == 1 and 'A cookie banner is on this page' in out
    assert site.events() == []
    site.requests.clear()
    code, out, err = run_check(capsys, '--store', store, '--origin', site.origin,
                               '--timeout-ms', '3000', '--cookies', 'reject')
    assert code == 0, out + err
    assert 'Closed the cookie banner with the cookies="reject" preset.' in out
    clicks = [e for e in site.events() if e['type'] == ['click']]
    assert clicks == [{'type': ['click'], 'id': ['didomi-notice-disagree-button']}]


LATE_WALL = ("""<form id="signup" style="display:none"><input id="email" name="email"
  type="email"></form>
<script>setTimeout(function () {
  var wall = document.createElement('div');
  wall.innerHTML = %s;
  document.body.appendChild(wall.firstElementChild);
}, 400);</script>""" % json.dumps(CONSENT_WALL.split('<form')[0].strip()))


def test_check_a_consent_wall_shown_after_load(site, store, capsys):
    # Consent scripts often show their wall a moment after `load`. A run misses the key,
    # handles the banner and looks again; check does the same, and saves nothing.
    _need('chromium')
    site.pages['/signup'] = page("<form id='signup'><input id='email' name='email' "
                                 "type='email'></form>")
    record(site, store, '/signup', [('email', '#email', 'input')])
    site.pages['/signup'] = page(LATE_WALL)
    before = _unchanged(store)
    site.requests.clear()
    code, out, err = run_check(capsys, '--store', store, '--origin', site.origin,
                               '--timeout-ms', '1500', '--cookies', 'reject')
    assert code == 0, out + err
    assert 'Closed the cookie banner with the cookies="reject" preset.' in out
    clicks = [e for e in site.events() if e['type'] == ['click']]
    assert clicks == [{'type': ['click'], 'id': ['didomi-notice-disagree-button']}]
    assert _unchanged(store) == before                  # no journal, no lock file, no write


FRAMED = page('<h1>Sign up</h1><iframe id="consent-frame" width="600" height="200" srcdoc="'
              + page('<label><input type="checkbox" id="accept-terms" name="terms"> I accept '
                     'the terms and conditions</label>').replace('&', '&amp;').replace('"', '&quot;')
              + '"></iframe>')


def test_check_iframes(site, store, capsys):
    _need('chromium')
    site.pages['/signup'] = SIGNUP_V1
    record(site, store, '/signup', [('accept_terms', '#accept-terms', 'checkbox')])
    site.pages['/signup'] = FRAMED
    before = _unchanged(store)
    code, out, _err = run_check(capsys, '--store', store, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS), '--format', 'json')
    _data, keys = _json(out)
    assert code == 1
    assert (keys['accept_terms']['verdict'], keys['accept_terms']['detail']) == (
        'would-heal', 'frame')
    assert keys['accept_terms']['heal']['frame'] == 'iframe[id="consent-frame"]'
    assert _unchanged(store) == before
    # A key recorded inside the iframe (a first run follows it there): it resolves there.
    other = str(Path(store).parent.parent / 'framed' / 'overrides.json')
    record(site, other, '/signup', [('accept_terms', '#accept-terms', 'checkbox')])
    code, out, _err = run_check(capsys, '--store', other, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS), '--format', 'json')
    _data, keys = _json(out)
    assert keys['accept_terms']['counts_as'] == 'ok' and code == 0


def test_check_never_loads_an_action_page(site, store, capsys):
    _need('chromium')
    site.pages['/account/sign-out'] = page("<a id='again' href='/'>Sign in again</a>")
    record(site, store, '/account/sign-out', [('again', '#again', 'link')])
    site.requests.clear()
    code, out, _err = run_check(capsys, '--store', store, '--origin', site.origin,
                                '--timeout-ms', str(FAST_MS), '--format', 'json')
    _data, keys = _json(out)
    assert code == 3 and keys['again']['detail'] == 'action-page'
    assert site.requests == []


# --- heal.drift() on every engine ------------------------------------------------------------------

def _drift_signup(tab, store, provider=None):
    tab.serve({'/signup': SIGNUP_V1})
    tab.visit('/signup')
    heal = mendwright.open(tab.target, provider=_NullProvider(), store_path=store)
    for key, selector, role in SIGNUP_KEYS:
        heal.locator(key, selector, role=role, timeout_ms=5000, poll_ms=POLL_MS,
                     destructive=True if key == 'pay' else None)
    tab.serve({'/signup': SIGNUP_V2})
    tab.visit('/signup')


def test_heal_drift_on_every_engine(tab, store):
    _drift_signup(tab, store)
    before = _unchanged(store)
    heal = mendwright.open(tab.target, provider=_NullProvider(), store_path=store)
    report = heal.drift(timeout_ms=FAST_MS, poll_ms=POLL_MS)
    assert {k.key: k.verdict for k in report.keys} == SIGNUP_VERDICTS
    assert not tab.js('document.documentElement.dataset.events || ""')
    assert _unchanged(store) == before
    assert heal.heals == [] and not report.ok


@pytest.mark.engine_family('playwright')
def test_await_heal_drift_gives_the_same_verdicts(engine, tab, store):
    if not hasattr(engine, 'with_async_page'):
        pytest.skip('this engine has no async page')
    _drift_signup(tab, store)
    before = _unchanged(store)
    import mendwright.aio

    async def check(apage):
        async def serve(route):
            await route.fulfill(status=200, content_type='text/html', body=SIGNUP_V2)
        await apage.route('https://mendwright.test/**', serve)
        await apage.goto('https://mendwright.test/signup')
        heal = await mendwright.aio.open(apage, provider=_NullProvider(), store_path=store)
        report = await heal.drift(timeout_ms=FAST_MS, poll_ms=POLL_MS)
        return report, await apage.evaluate('document.documentElement.dataset.events || ""')

    report, events = engine.with_async_page(check)
    assert {k.key: k.verdict for k in report.keys} == SIGNUP_VERDICTS
    assert not events and _unchanged(store) == before


BANNER_AND_FRAME = page(
    '<div id="onetrust-banner-sdk" style="position:fixed;bottom:0;left:0;right:0">'
    '<p>We use cookies.</p><button id="onetrust-reject-all-handler">Reject all</button></div>'
    '<input id="name" name="name">'
    '<iframe id="consent-frame" width="600" height="200" srcdoc="'
    + page('<label><input type="checkbox" id="inner-terms"> I accept the terms</label>')
    .replace('&', '&amp;').replace('"', '&quot;') + '"></iframe>')


@pytest.mark.engine_family('playwright')
def test_heal_drift_runs_no_overlay_handler(tab, store):
    tab.serve({'/signup': BANNER_AND_FRAME.replace('id="onetrust-banner-sdk"',
                                                   'id="onetrust-banner-sdk" hidden')})
    tab.visit('/signup')
    first = mendwright.open(tab.target, provider=_NullProvider(), store_path=store)
    first.locator('name', '#name', role='input', timeout_ms=5000, poll_ms=POLL_MS)
    first.locator('terms', '#inner-terms', role='checkbox', timeout_ms=5000, poll_ms=POLL_MS)
    # The iframe's checkbox and the name field are renamed, and the banner shows.
    tab.serve({'/signup': BANNER_AND_FRAME.replace('inner-terms', 'terms-v2')
               .replace('id="name"', 'id="full-name"')})
    tab.visit('/signup')
    calls = []
    tab.page.add_locator_handler(tab.page.locator('#onetrust-banner-sdk'),
                                 lambda *a: calls.append('script'), no_wait_after=True)
    heal = mendwright.open(tab.target, provider=_NullProvider(), store_path=store,
                           cookies='reject')
    report = heal.drift(timeout_ms=FAST_MS, poll_ms=POLL_MS)
    assert {k.key for k in report.keys} == {'name', 'terms'}
    assert calls == [] and heal.handled_interruptions == []
    assert not tab.js('document.documentElement.dataset.events || ""')
    assert tab.js("document.getElementById('onetrust-banner-sdk') !== null")
