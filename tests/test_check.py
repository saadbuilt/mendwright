"""`mendwright check`, offline: cli.cmd_check() with a fake browser session.

The store is recorded the way a run records it, with mendwright.open() on conftest's
FakePage. FakeSession then plays the browser: each address it is asked for gives a
FakePage (or an error), an HTTP status and the address the page ended on, and it records
what it was asked for and whether it was closed. No browser, network or model; the real
browser runs in tests/browser/test_browser_drift.py.
"""

import json
import os
import sys

import pytest

import mendwright
from mendwright import cli

SHOP = 'https://shop.example'
V1 = ("<form><input id='email' name='email'>"
      "<label><input type='checkbox' id='accept-terms' name='terms'> I accept the terms</label>"
      "<button type='button' id='pay'>Pay now</button></form>")
V2 = ("<form><input id='email' name='email'>"
      "<label><input type='checkbox' name='terms' data-testid='legal-consent'> I accept the "
      "terms</label><button type='button' id='pay-now'>Pay now</button></form>")
ACCOUNT = "<main><a id='orders' href='/orders'>Orders</a></main>"


@pytest.fixture
def store_dir(tmp_path):
    return tmp_path / 'store'


def record(make_page, store_dir, pages=None, provider=None):
    """A run on v1 of the shop: 'email', 'accept_terms' and 'pay' on /signup, 'orders' on
    /account. Returns the store path."""
    path = str(store_dir / 'overrides.json')
    pages = pages or {SHOP + '/signup': (V1, [('email', '#email', 'input'),
                                              ('accept_terms', '#accept-terms', 'checkbox'),
                                              ('pay', '#pay', 'button')]),
                      SHOP + '/account': (ACCOUNT, [('orders', '#orders', 'link')])}
    for url, (html, keys) in pages.items():
        page = make_page(html, url=url, fingerprints=True)
        heal = mendwright.open(page, provider=provider or _null(), store_path=path)
        for key, selector, role in keys:
            heal.locator(key, selector, role=role, destructive=key == 'pay' or None,
                         timeout_ms=0, poll_ms=0)
    return path


def _null():
    from mendwright.facade import _NullProvider
    return _NullProvider()


class FakeVisit:
    def __init__(self, page, status, error, session):
        self.page, self.status, self.error, self.seconds = page, status, error, 0.0
        self._session = session

    def close(self):
        self._session.closed_visits += 1


class FakeSession:
    """site maps an address to (html, status, final address) or to an exception."""

    def __init__(self, site, make_page, options, interrupt_on=None, start_error=None,
                 raises=None):
        self.site, self.make_page, self.options = site, make_page, options
        self.visited, self.pages = [], []
        self.closed, self.closed_visits = False, 0
        self.interrupt_on, self.start_error = interrupt_on, start_error
        self.raises = raises or {}      # address -> what visit() raises, as the browser would

    def start(self):
        if self.start_error is not None:
            raise self.start_error

    def visit(self, url, wait_until='load'):
        self.visited.append(url)
        if url == self.interrupt_on:
            raise KeyboardInterrupt
        if url in self.raises:
            raise self.raises[url]
        answer = self.site.get(url, ('<p>Not found</p>', 404, url))
        if isinstance(answer, BaseException):
            return FakeVisit(None, None, answer, self)
        html, status, final = answer[:3]
        page = answer[3](html, final) if len(answer) > 3 else self.make_page(
            html, url=final, fingerprints=True)
        self.pages.append(page)
        return FakeVisit(page, status, None, self)

    def close(self):
        self.closed = True


class Browser:
    """The session factory cmd_check() takes, and every session it made."""

    def __init__(self, make_page, site, **session_kw):
        self.make_page, self.site, self.session_kw = make_page, site, session_kw
        self.sessions = []

    def __call__(self, **options):
        session = FakeSession(self.site, self.make_page, options, **self.session_kw)
        self.sessions.append(session)
        return session

    @property
    def visited(self):
        return [url for s in self.sessions for url in s.visited]

    @property
    def actions(self):
        return [a for s in self.sessions for page in s.pages for a in page.actions]


def shop(v=V2, account=ACCOUNT, **extra):
    site = {SHOP + '/signup': (v, 200, SHOP + '/signup'),
            SHOP + '/account': (account, 200, SHOP + '/account')}
    site.update(extra)
    return site


def run_check(argv, browser, capsys):
    args = cli.build_parser().parse_args(['check'] + argv)
    code = cli.cmd_check(args, session_factory=browser)
    out = capsys.readouterr()
    return code, out.out, out.err


def _args(path, *more):
    return ['--store', path, '--origin', SHOP, '--timeout-ms', '0'] + list(more)


# --- exit codes ---------------------------------------------------------------------------------

def test_a_redesign_exits_1_and_says_which_keys_broke(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    before = open(path, 'rb').read()
    browser = Browser(make_page, shop())
    code, out, err = run_check(_args(path), browser, capsys)
    assert code == 1
    assert sorted(browser.visited) == [SHOP + '/account', SHOP + '/signup']
    assert browser.actions == []
    assert 'ok             email' in out
    assert 'would heal     accept_terms' in out
    assert 'refused        pay' in out
    assert out.rstrip().endswith('Exit 1: 2 keys broke.')
    assert err == ('mendwright check: loading 2 pages from https://shop.example with Playwright '
                   'chromium (headless).\n')
    assert open(path, 'rb').read() == before
    assert sorted(os.listdir(store_dir)) == ['overrides.json']    # no journal, no lock file
    assert all(s.closed for s in browser.sessions)


def test_every_key_ok_exits_0(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    code, out, _err = run_check(_args(path), Browser(make_page, shop(v=V1)), capsys)
    assert code == 0
    assert out.rstrip().endswith('Exit 0: every checked key resolves.')
    assert '4 ok.' in out


def test_a_page_not_checked_exits_3_and_broken_wins_over_it(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    site = shop(v=V1, **{SHOP + '/account': ('<p>gone</p>', 404, SHOP + '/account')})
    code, out, _err = run_check(_args(path), Browser(make_page, site), capsys)
    assert code == 3
    assert ('Exit 3: 1 key was not checked (pass --allow-unchecked to accept that).'
            in out)
    assert 'The page answered HTTP 404, so its keys were not checked.' in out
    code, out, _err = run_check(_args(path, '--allow-unchecked'), Browser(make_page, site), capsys)
    assert code == 0
    site = shop(**{SHOP + '/account': ('<p>gone</p>', 404, SHOP + '/account')})
    code, out, _err = run_check(_args(path), Browser(make_page, site), capsys)
    assert code == 1


def test_a_redirect_still_checks_the_keys_found_on_the_page_it_went_to(make_page, store_dir,
                                                                       capsys):
    login = "<main><a id='orders' href='/orders'>Orders</a></main>"
    path = record(make_page, store_dir, pages={
        SHOP + '/login': (login, [('orders', '#orders', 'link')]),
        SHOP + '/account': (ACCOUNT + "<a id='profile' href='/p'>Profile</a>",
                            [('orders', '#orders', 'link'), ('profile', '#profile', 'link')])})
    site = {SHOP + '/account': (login, 200, SHOP + '/login?next=/account')}
    browser = Browser(make_page, site)
    code, out, _err = run_check(_args(path, '--format', 'json'), browser, capsys)
    keys = {k['key']: k for k in json.loads(out)['keys']}
    assert browser.visited == [SHOP + '/account']
    assert keys['orders']['verdict'] == 'ok'          # it was found on /login before
    assert (keys['profile']['verdict'], keys['profile']['detail']) == ('unreachable', 'redirect')
    assert code == 3


def test_an_empty_store_exits_3(tmp_path, make_page, capsys):
    path = tmp_path / 'o.json'
    path.write_text('{"schema": 2, "selectors": {}}', encoding='utf-8')
    browser = Browser(make_page, {})
    code, out, _err = run_check(['--store', str(path), '--origin', SHOP], browser, capsys)
    assert (code, out) == (3, 'The store has no keys yet, so nothing was checked.\n')
    code, _out, _err = run_check(['--store', str(path), '--origin', SHOP, '--allow-unchecked'],
                                 browser, capsys)
    assert code == 0 and browser.sessions == []


def test_what_check_cannot_run_without_exits_4(tmp_path, make_page, store_dir, capsys,
                                               monkeypatch):
    browser = Browser(make_page, {})
    code, _out, err = run_check(['--store', str(tmp_path / 'missing.json'), '--origin', SHOP],
                                browser, capsys)
    assert code == 4 and 'mendwright check: no store at' in err
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    code, _out, err = run_check(['--store', str(bad), '--origin', SHOP], browser, capsys)
    assert code == 4 and err.startswith('mendwright check: ') and 'is not valid JSON' in err
    path = record(make_page, store_dir)
    code, _out, err = run_check(_args(path, '--seed', str(tmp_path / 'no-seed.json')), browser,
                                capsys)
    assert code == 4 and 'seed file' in err and 'does not exist' in err
    state = tmp_path / 'state.json'
    state.write_text('cookies: session=SENTINEL-4242', encoding='utf-8')
    code, out, err = run_check(_args(path, '--storage-state', str(state)), browser, capsys)
    assert code == 4 and 'could not use the storage state file' in err
    assert 'SENTINEL' not in out + err and browser.sessions == []
    # No Playwright: the real session finds it missing.
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', None)
    args = cli.build_parser().parse_args(['check'] + _args(path))
    assert cli.cmd_check(args) == 4
    assert 'mendwright check needs Playwright to load pages' in capsys.readouterr().err


def test_a_browser_that_does_not_start_exits_4(make_page, store_dir, capsys):
    from mendwright.drift_session import SetupError
    path = record(make_page, store_dir)
    browser = Browser(make_page, shop(), start_error=SetupError("Playwright's webkit browser "
                                                               'is not installed.'))
    code, _out, err = run_check(_args(path), browser, capsys)
    assert code == 4 and "Playwright's webkit browser is not installed." in err
    assert all(s.closed for s in browser.sessions)


class _Refusing:
    """A Playwright browser whose new_context() (or the context's new_page()) raises."""

    def __init__(self, context_error=None, page_error=None):
        self.context_error, self.page_error, self.closed = context_error, page_error, []

    def new_context(self, **options):
        if self.context_error is not None:
            raise self.context_error
        browser = self

        class Context:
            def set_default_navigation_timeout(self, ms):
                pass

            def new_page(self):
                raise browser.page_error

            def close(self):
                browser.closed.append(True)
        return Context()


def test_a_storage_state_playwright_refuses_is_a_setup_error(tmp_path):
    from mendwright.drift_session import PlaywrightSession, SetupError
    session = PlaywrightSession(storage_state=str(tmp_path / 'state.json'))
    session._browser = _Refusing(context_error=RuntimeError(
        'Browser.new_context: Cookie should have a url or a domain/path pair\nCall log: ...'))
    with pytest.raises(SetupError) as info:
        session.visit('https://shop.example/signup')
    assert str(info.value) == (
        'could not use the storage state file ' + str(tmp_path / 'state.json') + ' (Browser.'
        'new_context: Cookie should have a url or a domain/path pair). Save one with '
        'context.storage_state(path=...).')
    session = PlaywrightSession()
    session._browser = _Refusing(page_error=RuntimeError('Target page, context or browser has '
                                                         'been closed'))
    with pytest.raises(SetupError) as info:
        session.visit('https://shop.example/signup')
    assert str(info.value).startswith('chromium could not open a page (Target page, context or '
                                      'browser has been closed).')
    assert session._browser.closed == [True]


@pytest.mark.parametrize('workers', ['1', '2'])
def test_a_browser_that_fails_while_pages_load_exits_4(make_page, store_dir, capsys, workers):
    from mendwright.drift_session import SetupError
    path = record(make_page, store_dir)
    refused = SetupError('could not use the storage state file s.json (Browser.new_context: '
                         'Cookie should have a url or a domain/path pair). Save one with '
                         'context.storage_state(path=...).')
    browser = Browser(make_page, shop(), raises={SHOP + '/signup': refused})
    code, out, err = run_check(_args(path, '--workers', workers), browser, capsys)
    assert code == 4 and out == ''
    assert err.splitlines()[-1] == 'mendwright check: ' + str(refused)
    assert all(s.closed for s in browser.sessions)


def test_any_other_error_while_pages_load_exits_4_with_one_line(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    crash = RuntimeError('Target crashed at https://shop.example/signup?token=SECRET\n'
                         'Call log')
    browser = Browser(make_page, shop(), raises={SHOP + '/signup': crash})
    code, out, err = run_check(_args(path), browser, capsys)
    assert code == 4 and out == ''
    assert err.splitlines()[-1] == ('mendwright check: stopped before it could report '
                                    '(RuntimeError: Target crashed at '
                                    'https://shop.example/signup).')
    assert all(s.closed for s in browser.sessions)


# --- usage errors -------------------------------------------------------------------------------

def test_the_site_must_be_named(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    browser = Browser(make_page, shop())
    code, _out, err = run_check(['--store', path], browser, capsys)
    assert code == 2 and browser.sessions == []
    assert err == ('mendwright check: name the site to load pages from: mendwright check loads '
                   'only sites you name, never one the store alone points to. The store has '
                   'pages on https://shop.example (4 keys). Pass --origin ORIGIN for each site '
                   'to check, or --base-url URL to load every page from one site.\n')


def test_a_store_saved_before_0_9_0_needs_base_url(make_page, tmp_path, capsys):
    path = tmp_path / 'o.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'email': {
        'expectedRole': 'input', 'lastPage': '/signup', 'pages': ['/signup'],
        'candidates': [{'xpath': '#email', 'source': 'code', 'status': 'active'}]}}}),
        encoding='utf-8')
    browser = Browser(make_page, shop(v=V1))
    code, _out, err = run_check(['--store', str(path)], browser, capsys)
    assert code == 2 and err.endswith('The store was saved before 0.9.0 and records only paths. '
                                      'Pass --base-url URL.\n')
    code, out, _err = run_check(['--store', str(path), '--base-url', SHOP, '--timeout-ms', '0'],
                                browser, capsys)
    assert code == 0 and browser.visited == [SHOP + '/signup']


@pytest.mark.parametrize('argv, text', [
    (['--origin', 'shop.example'], "--origin takes scheme://host[:port] with http or https, such "
                                   "as https://shop.example, got 'shop.example'."),
    (['--base-url', 'https://shop.example/app'], "--base-url takes scheme://host[:port] with http "
                                                 'or https, such as https://shop.example, got '
                                                 "'https://shop.example/app'."),
    (['--origin', SHOP, '--url', '/signup'], "--url takes PAGE=URL, such as "
                                             "'/orders/*=https://shop.example/orders/1001', got "
                                             "'/signup'."),
    (['--origin', SHOP, '--url', '/x=https://shop.example/x'],
     '--url names the page /x, but no selected key was last found there. Pages of the selected '
     'keys: /account and /signup.'),
    (['--origin', SHOP, '--url', '/signup=https://other.example/signup'],
     "--url https://other.example/signup is on https://other.example, which this check doesn't "
     'load. Add --origin https://other.example.'),
    (['--origin', SHOP, '--key', 'pay*', '--key', 'nope*'], "--key 'nope*' matches no key in the "
                                                           'store.'),
    (['--origin', SHOP, '--skip-key', '*'], 'no key is left after --key, --skip-key, --page, '
                                            '--skip-page and --origin. The store has pages on '
                                            'https://shop.example (4 keys).'),
    (['--origin', 'https://other.example'], 'no key is left after --key, --skip-key, --page, '
                                            '--skip-page and --origin. The store has pages on '
                                            'https://shop.example (4 keys).'),
    (['--origin', SHOP, '--page', 'signup'], "--page takes a path that starts with / or *, such "
                                             "as /checkout/*, got 'signup'."),
    (['--origin', SHOP, '--viewport', '100x100'], '--viewport takes WIDTHxHEIGHT with each side '
                                                  "200 to 4000, such as 1280x720, got '100x100'."),
    (['--origin', SHOP, '--workers', '9'], '--workers takes 1 to 8, got 9.'),
    (['--origin', SHOP, '--timeout-ms', '-1'], '--timeout-ms takes 0 or more, got -1.'),
    (['--origin', SHOP, '--nav-timeout-ms', '10'], '--nav-timeout-ms takes 1000 or more, got 10.'),
])
def test_usage_errors_exit_2_before_anything_loads(argv, text, make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    browser = Browser(make_page, shop())
    code, out, err = run_check(['--store', path] + argv, browser, capsys)
    assert (code, err) == (2, 'mendwright check: ' + text + '\n')
    assert out == '' and browser.sessions == []


def test_a_bad_setting_is_a_usage_error(make_page, store_dir, capsys, monkeypatch):
    path = record(make_page, store_dir)
    monkeypatch.setenv('MENDWRIGHT_DRY_RUN', 'maybe')
    code, _out, err = run_check(_args(path), Browser(make_page, shop()), capsys)
    assert code == 2 and err.startswith('mendwright check: MENDWRIGHT_DRY_RUN must be 1 or 0')


def test_both_sites_is_argparse_s_usage_error(capsys):
    assert cli.main(['check', '--origin', SHOP, '--base-url', SHOP]) == 2
    assert 'not allowed with argument' in capsys.readouterr().err


def test_help_lists_check(capsys):
    assert cli.main(['--help']) == 0
    assert 'check' in capsys.readouterr().out
    assert cli.main(['check', '--help']) == 0
    assert '--origin' in capsys.readouterr().out


# --- policies ------------------------------------------------------------------------------------

def test_fail_on_strict_and_allow_unchecked_change_only_the_exit_code(make_page, store_dir,
                                                                     capsys):
    path = record(make_page, store_dir)
    site = shop(**{SHOP + '/account': ('<p>x</p>', 500, SHOP + '/account')})
    results = {}
    for flags in ([], ['--fail-on', 'unhealable'], ['--fail-on', 'unhealable', '--allow-unchecked'],
                  ['--strict', '--allow-unchecked']):
        code, out, _err = run_check(_args(path, '--format', 'json', *flags),
                                    Browser(make_page, site), capsys)
        data = json.loads(out)
        results[tuple(flags)] = (code, {k['key']: (k['verdict'], k['fails']) for k in data['keys']})
    verdicts = {flags: {k: v for k, (v, _f) in keys.items()} for flags, (_c, keys) in results.items()}
    assert len({json.dumps(v, sort_keys=True) for v in verdicts.values()}) == 1
    assert results[()][0] == 1
    assert results[('--fail-on', 'unhealable')] == (1, {
        'accept_terms': ('would-heal', False), 'email': ('ok', False), 'orders': ('unreachable', False),
        'pay': ('refused', True)})


def test_unhealable_exits_3_when_a_run_gets_past_every_key(make_page, store_dir, capsys):
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [
        ('email', '#email', 'input'), ('accept_terms', '#accept-terms', 'checkbox')])})
    code, out, _err = run_check(_args(path, '--fail-on', 'unhealable'),
                                Browser(make_page, shop()), capsys)
    assert code == 0 and out.rstrip().endswith('Exit 0: a run gets past every checked key alone.')
    code, out, _err = run_check(_args(path), Browser(make_page, shop()), capsys)
    assert code == 1


def test_dry_run_makes_a_heal_from_memory_unhealable(make_page, store_dir, capsys, monkeypatch):
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [
        ('accept_terms', '#accept-terms', 'checkbox')])})
    monkeypatch.setenv('MENDWRIGHT_DRY_RUN', '1')
    code, out, _err = run_check(_args(path, '--fail-on', 'unhealable'),
                                Browser(make_page, shop()), capsys)
    assert code == 1 and 'Dry run is on' in out


# --- formats --------------------------------------------------------------------------------------

def test_json_report(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    store = mendwright.load_store(None, path)
    store.add_candidate('draft', '#draft', source='code')      # never found on a page
    site = shop(**{SHOP + '/account': ('<p>Sign in</p>', 200,
                                       SHOP + '/login?next=/account&token=SECRET')})
    code, out, _err = run_check(_args(path, '--format', 'json'), Browser(make_page, site), capsys)
    data = json.loads(out)
    assert code == 1 and data['schema'] == 1 and data['exit_code'] == 1
    assert data['mendwright'] == mendwright.__version__ and data['browser'] == 'chromium'
    assert data['sites'] == [{'recorded': [SHOP], 'loaded': SHOP}]
    assert data['left_out'] == [{'key': 'draft', 'reason': 'no-page'}]
    assert data['counts'] == {'ok': 1, 'would-heal': 1, 'refused': 1, 'unreachable': 1}
    assert [k['key'] for k in data['keys']] == ['orders', 'accept_terms', 'email', 'pay']
    assert set(data['keys'][0]) == {
        'key', 'page', 'verdict', 'detail', 'counts_as', 'fails', 'role', 'live_role',
        'destructive', 'used', 'heal', 'provider', 'dry_run', 'confirmed', 'reason', 'probes'}
    assert set(data['pages'][0]) >= {'page', 'address', 'outcome', 'status', 'final_page',
                                     'seconds', 'notes', 'keys'}
    account = [p for p in data['pages'] if p['page'] == '/account'][0]
    assert (account['outcome'], account['final_page']) == ('unreachable', '/login')
    assert 'SECRET' not in out and 'token' not in out
    assert out == json.dumps(data, indent=2, sort_keys=True) + '\n'


def test_markdown_escapes_cells(make_page, store_dir, capsys):
    path = record(make_page, store_dir, pages={SHOP + '/signup': (
        "<form><a id='x' href='#'>a|b</a></form>", [('pipe|key', "//a[text()='a|b']", 'link')])})
    code, out, _err = run_check(_args(path, '--format', 'md'), Browser(make_page, shop(
        v="<form><a id='x' href='#'>a|b</a></form>")), capsys)
    assert code == 0 and out.startswith('## mendwright check\n')
    assert '| Page | Key | Result | Details |' in out
    assert '`pipe\\|key`' in out and "//a[text()='a\\|b']" in out
    assert out.rstrip().endswith('Exit 0: every checked key resolves.')
    from mendwright.drift import _md_code
    # As in mendwright fix and report (mendwright._md): any run of backticks, and control
    # and bidirectional characters shown as \uXXXX.
    assert _md_code('a`b') == '``a`b``' and _md_code('a``b') == '```a``b```'
    assert _md_code('pay‮now') == '`pay\\u202Enow`'


def test_markdown_keeps_the_spelling_of_a_call_that_starts_a_reason():
    from mendwright.drift import KeyDrift, SelectorProbe, _reason_cell
    call = 'get_by_role("button", name="Sign in")'

    def drift(reason, selector=call):
        return KeyDrift(key=call, page='/login', verdict='refused', detail=None,
                        counts_as='broken', role='button', live_role=None, destructive=False,
                        used=None, heal=None, provider=None, dry_run=False, confirmed=None,
                        reason=reason, probes=(SelectorProbe(selector, 'code', None, 2, None,
                                                             'matches 2 elements'),))
    assert _reason_cell(drift(call + ' matches 2 elements.')) == call + ' matches 2 elements.'
    assert _reason_cell(drift('button.go matches nothing.', 'button.go')).startswith('button.go')
    assert _reason_cell(drift('it matches only through //a.')) == 'It matches only through //a.'


def test_quiet_lists_only_what_is_not_ok(make_page, store_dir, capsys):
    path = record(make_page, store_dir)
    code, out, _err = run_check(_args(path, '--quiet'), Browser(make_page, shop()), capsys)
    assert code == 1 and ' email ' not in out and 'accept_terms' in out
    assert '/account' not in out


def test_out_writes_the_report_to_a_file(make_page, store_dir, tmp_path, capsys):
    path = record(make_page, store_dir)
    target = tmp_path / 'reports' / 'check.md'
    code, out, _err = run_check(_args(path, '--format', 'md', '--out', str(target)),
                                Browser(make_page, shop()), capsys)
    assert code == 1 and out == 'Wrote ' + str(target) + '\n'
    assert target.read_text(encoding='utf-8').startswith('## mendwright check')


# --- privacy and the tripwire ------------------------------------------------------------------

def test_nothing_secret_is_printed(make_page, store_dir, tmp_path, capsys, monkeypatch):
    path = record(make_page, store_dir)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'cookies': [{'name': 'session', 'value': 'SENTINEL-4242'}],
                                 'origins': []}), encoding='utf-8')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-SECRET-KEY-VALUE')
    site = shop(**{
        SHOP + '/signup': ConnectionError('net::ERR_FAILED at https://x.example/y?token=SECRET '
                                          'key sk-ant-SECRET-KEY-VALUE'),
        SHOP + '/account': ('<p>login</p>', 200, SHOP + '/login?token=SECRET')})
    out_file = tmp_path / 'r.txt'
    browser = Browser(make_page, site)
    code, out, err = run_check(_args(path, '--storage-state', str(state), '--out', str(out_file)),
                               browser, capsys)
    report = out_file.read_text(encoding='utf-8')
    everything = out + err + report
    assert 'SENTINEL' not in everything and 'SECRET' not in everything
    assert 'token' not in everything and '?' not in report.split('Store:')[1].split('\n')[2]
    assert 'at https://x.example/y key ***' in ' '.join(report.split())
    assert browser.sessions[0].options['storage_state'] == str(state)


def test_a_configured_provider_is_named_and_never_called(make_page, store_dir, capsys,
                                                         monkeypatch):
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1 + (
        "<label><input type='checkbox' id='news' class='opt'> News</label>"),
        [('news', '#news', 'checkbox')])})
    calls = []

    class Provider:
        model = 'claude-x'

        def propose(self, bundle):
            calls.append(bundle)

        def report_outcome(self, *args):
            calls.append(args)
    Provider.__name__ = 'AnthropicProvider'
    import mendwright.providers
    monkeypatch.setattr(mendwright.providers, 'auto_provider', lambda: Provider())
    v2 = V2 + ("<label><input type='checkbox' class='opt'> News</label>"
               "<label><input type='checkbox' class='opt'> News</label>")
    code, out, _err = run_check(_args(path, '--format', 'json'), Browser(make_page, shop(v=v2)),
                                capsys)
    data = json.loads(out)
    [news] = data['keys']
    assert (news['verdict'], news['provider']) == ('needs-model', 'AnthropicProvider')
    assert 'A run would ask AnthropicProvider, model claude-x. mendwright check asks no model.' \
        in news['reason']
    assert data['provider'] == {'name': 'AnthropicProvider', 'model': 'claude-x'}
    assert calls == []


def test_the_tripwire_raises_on_any_call():
    wire = cli._Tripwire('AnthropicProvider', 'claude-x')
    with pytest.raises(RuntimeError, match='a model call was attempted'):
        wire.propose({})
    with pytest.raises(RuntimeError, match='a model call was attempted'):
        wire.report_outcome('k', '//a', True)


# --- cookie banners -------------------------------------------------------------------------------

BANNER = ("<div id='onetrust-banner-sdk'><p>We use cookies.</p>"
          "<button id='onetrust-reject-all-handler'>Reject all</button>"
          "<button id='onetrust-accept-btn-handler'>Accept all</button></div>")


def _banner_page(make_page):
    def make(html, url):
        page = make_page(html, url=url, fingerprints=True)
        page.add_locator_handler_calls = []
        page.add_locator_handler = lambda *a, **kw: page.add_locator_handler_calls.append(a)
        real = page.query

        def query(selector):
            if page.actions and BANNER in html:         # the click closed the banner
                page.set_html(html.replace(BANNER, ''))
                page.actions_seen = True
            return real(selector)
        page.query = query
        return page
    return make


def test_cookies_reject_closes_the_banner_with_one_click(make_page, store_dir, capsys, caplog,
                                                         monkeypatch):
    from mendwright.facade import Healer
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [('email', '#email', 'input')])})
    before = open(path, 'rb').read()
    hidden = BANNER + "<form style='display:none'><input id='email'></form>"
    site = {SHOP + '/signup': (hidden.replace("style='display:none'", ''), 200, SHOP + '/signup',
                               _banner_page(make_page))}
    browser = Browser(make_page, site)
    # open(cookies=...) registers a Playwright overlay handler through Healer._watch: check
    # never passes cookies= to open(), so nothing is registered.
    watched = []
    monkeypatch.setattr(Healer, '_watch', lambda self, item: watched.append(item.key))
    with caplog.at_level('WARNING', logger='mendwright'):
        code, out, _err = run_check(_args(path, '--cookies', 'reject'), browser, capsys)
    assert code == 0, out
    [click] = browser.actions
    assert click[1] == 'click' and 'onetrust-reject-all-handler' in click[0]
    assert "handled interruption 'cookie_banner' before '<check>'" in caplog.text
    assert 'Closed the cookie banner with the cookies="reject" preset.' in out
    assert watched == []
    assert browser.sessions[0].pages[0].add_locator_handler_calls == []
    # The handled banner is journaled nowhere: no journal.jsonl next to the store.
    assert sorted(os.listdir(store_dir)) == ['overrides.json']
    assert open(path, 'rb').read() == before


def _late_banner_page(make_page):
    """A consent script that shows its wall a moment after the page loaded: the banner
    appears once the key was looked for, and the form shows once a button closed it."""
    hidden = "<form style='display:none'><input id='email'></form>"
    shown = "<form><input id='email'></form>"

    def make(html, url):
        page = make_page(hidden, url=url, fingerprints=True)
        real, state = page.query, {'banner': False}

        def query(selector):
            if page.actions:
                page.set_html(shown)
            elif not state['banner'] and selector.endswith('#email'):
                state['banner'] = True
                page.set_html(BANNER + hidden)
            return real(selector)
        page.query = query
        return page
    return make


def test_cookies_reject_closes_a_banner_shown_after_the_page_loaded(make_page, store_dir,
                                                                    capsys):
    # A run misses the key, handles the banner and looks again (Healer._resolve_step), so
    # it finds the key with no heal. check does the same.
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [('email', '#email', 'input')])})
    site = {SHOP + '/signup': ('', 200, SHOP + '/signup', _late_banner_page(make_page))}
    browser = Browser(make_page, site)
    code, out, _err = run_check(_args(path, '--cookies', 'reject'), browser, capsys)
    assert code == 0, out
    assert 'ok             email' in out
    [click] = browser.actions
    assert click[1] == 'click' and 'onetrust-reject-all-handler' in click[0]
    assert 'Closed the cookie banner with the cookies="reject" preset.' in out


def test_a_banner_that_keeps_coming_back_leaves_the_page_unchecked(make_page, store_dir,
                                                                   capsys):
    hidden = "<form style='display:none'><input id='email'></form>"

    def make(html, url):
        page = make_page(BANNER + hidden, url=url, fingerprints=True)
        real, seen = page.query, {'clicks': 0}

        def query(selector):
            if len(page.actions) > seen['clicks']:          # a click closes it for now
                seen['clicks'] = len(page.actions)
                page.set_html(hidden)
            elif selector.endswith('#email'):               # and the page shows it again
                page.set_html(BANNER + hidden)
            return real(selector)
        page.query = query
        return page
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [('email', '#email', 'input')])})
    browser = Browser(make_page, {SHOP + '/signup': ('', 200, SHOP + '/signup', make)})
    code, out, _err = run_check(_args(path, '--cookies', 'reject', '--format', 'json'), browser,
                                capsys)
    assert code == 3, out
    [key] = json.loads(out)['keys']
    assert key['verdict'] == 'unreachable' and key['detail'] == 'interruption-loop'
    assert len(browser.actions) == 3


def test_without_cookies_a_banner_is_noted_and_left_alone(make_page, store_dir, capsys):
    path = record(make_page, store_dir, pages={SHOP + '/signup': (V1, [
        ('email', '#email', 'input'), ('accept_terms', '#accept-terms', 'checkbox')])})
    site = {SHOP + '/signup': (BANNER + V1.replace("id='accept-terms'", "id='x'"), 200,
                               SHOP + '/signup')}
    browser = Browser(make_page, site)
    code, out, _err = run_check(_args(path, '--fingerprint'), browser, capsys)
    assert browser.actions == []
    assert ('A cookie banner is on this page (the cookies= preset found one). mendwright check '
            'clicked nothing. If the banner hides the page, pass --cookies reject.') in out


# --- workers and Ctrl+C ----------------------------------------------------------------------------

def _many_pages(make_page, store_dir, count=5):
    pages = {SHOP + '/p' + str(n): (V1, [('email' + str(n), '#email', 'input')])
             for n in range(count)}
    return record(make_page, store_dir, pages=pages), {
        url: (V1, 200, url) for url in pages}


def test_workers_give_the_same_report(make_page, store_dir, capsys):
    path, site = _many_pages(make_page, store_dir)
    _code, one, _err = run_check(_args(path, '--format', 'json'), Browser(make_page, site), capsys)
    browser = Browser(make_page, site)
    _code, three, _err = run_check(_args(path, '--format', 'json', '--workers', '3'), browser,
                                   capsys)
    strip = lambda text: {k: v for k, v in json.loads(text).items() if k != 'seconds'}  # noqa: E731
    for data in (json.loads(one), json.loads(three)):
        for page in data['pages']:
            page.pop('seconds')
    assert strip(one)['pages'] == strip(three)['pages'] and strip(one)['keys'] == strip(three)['keys']
    assert len(browser.sessions) == 3 and all(s.closed for s in browser.sessions)


def test_the_guide_quotes_what_check_says():
    from pathlib import Path

    from mendwright import drift
    from mendwright.flow import handled_message
    path = Path(__file__).resolve().parents[1] / 'docs' / 'guide.md'
    if not path.is_file():
        pytest.skip('docs/ is not in this copy of the project (the sdist leaves it out)')
    guide = path.read_text(encoding='utf-8')
    for word in drift.ACTION_WORDS:
        assert '`' + word + '`' in guide, word
    for pair in drift.ACTION_PAIRS:
        assert '`' + ' '.join(pair) + '`' in guide, pair
    assert handled_message('cookie_banner', '<check>') in guide
    for verdict in drift.VERDICTS:
        assert '| `' + drift.LABELS[verdict] + '` |' in guide, verdict
    for code in ('0', '1', '2', '3', '4', '130'):
        assert '| ' + code + ' |' in guide


@pytest.mark.parametrize('workers', ['1', '3'])
def test_ctrl_c_closes_every_browser_and_exits_130(workers, make_page, store_dir, capsys):
    path, site = _many_pages(make_page, store_dir)
    browser = Browser(make_page, site, interrupt_on=SHOP + '/p2')
    code, _out, err = run_check(_args(path, '--workers', workers), browser, capsys)
    assert code == 130 and 'interrupted' in err
    assert browser.sessions and all(s.closed for s in browser.sessions)
