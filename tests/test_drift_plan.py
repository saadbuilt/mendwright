"""What `mendwright check` decides before and right after it loads a page, as pure
functions of mendwright.drift: which site each key belongs to, the address of each page,
the pages whose path reads as an action, page patterns, and the answers that leave a page
unchecked. Offline: stores in tmp_path, no browser."""

import pytest

from mendwright import drift
from mendwright.drift import UsageError, action_word, page_pattern, plan_check
from mendwright.store import load_store


def _store(tmp_path, keys):
    """keys: {key: (lastPage, lastUrl, lastSucceededAt)}."""
    store = load_store(None, str(tmp_path / 'o.json'))
    for key, (page, url, when) in keys.items():
        store.add_candidate(key, '#' + key, source='code')
        if when:
            store.record_success(key, '#' + key, now=when)
        if page:
            store.set_last_page(key, page)
        if url:
            store.set_last_url(key, url)
    return store


def _pages(plan):
    return {(p.origin, p.page): p for p in plan.pages}


def test_keys_are_grouped_by_the_origin_they_load_from_and_their_page(tmp_path):
    store = _store(tmp_path, {
        'a': ('/signin', 'https://shop.example/signin', None),
        'b': ('/signin', 'https://login.example/signin', None),
        'c': ('/signin', 'https://shop.example/SignIn', '2026-10-11T00:00:00+00:00')})
    plan = plan_check(store, ['a', 'b', 'c'], origins={
        'https://shop.example': 'https://shop.example',
        'https://login.example': 'https://login.example'})
    pages = _pages(plan)
    assert sorted(pages) == [('https://login.example', '/signin'),
                             ('https://shop.example', '/signin')]
    shop = pages[('https://shop.example', '/signin')]
    assert shop.keys == ['a', 'c'] and shop.skip is None
    # The newest of the group's addresses.
    assert shop.address == 'https://shop.example/SignIn'
    assert pages[('https://login.example', '/signin')].address == 'https://login.example/signin'


def test_origin_names_the_sites_and_maps_them(tmp_path):
    store = _store(tmp_path, {
        'a': ('/signup', 'https://shop.example/signup', None),
        'b': ('/help', 'https://help.example/help', None)})
    plan = plan_check(store, ['a', 'b'], origins={'https://shop.example': 'https://shop.example'})
    assert [p.address for p in plan.pages] == ['https://shop.example/signup']
    assert plan.left_out == [('b', 'other-site')]
    assert drift.left_out_lines(plan.left_out) == [
        '1 key was found on a site no --origin names and was left out.']
    plan = plan_check(store, ['a'], origins={
        'https://shop.example': 'https://staging.shop.example'})
    assert [p.address for p in plan.pages] == ['https://staging.shop.example/signup']
    assert plan.sites == [{'recorded': ['https://shop.example'],
                           'loaded': 'https://staging.shop.example'}]


def test_base_url_loads_every_page_from_one_site(tmp_path):
    store = _store(tmp_path, {
        'a': ('/signup', 'http://127.0.0.1:8123/signup', None),
        'b': ('/signup', 'http://127.0.0.1:9000/signup', None),
        'c': ('/checkout', None, None)})                 # saved before 0.9.0
    plan = plan_check(store, ['a', 'b', 'c'], base_url='http://127.0.0.1:8000')
    assert {p.page: p.address for p in plan.pages} == {
        '/signup': 'http://127.0.0.1:8000/signup', '/checkout': 'http://127.0.0.1:8000/checkout'}
    assert plan.notes == []                              # one host, other ports
    store = _store(tmp_path / 'x', {
        'a': ('/signup', 'http://127.0.0.1:8123/signup', None),
        'b': ('/signin', 'https://login.example/signin', None)})
    plan = plan_check(store, ['a', 'b'], base_url='http://127.0.0.1:8000')
    assert plan.notes == [
        'mendwright check: pages recorded on 2 hosts (127.0.0.1, login.example) are all loaded '
        'from http://127.0.0.1:8000. If they are different sites, map each with --origin '
        'RECORDED=ACTUAL instead.']


def test_a_masked_address_needs_url(tmp_path):
    store = _store(tmp_path, {'note': ('/orders/*', 'https://shop.example/Orders/*/', None)})
    origins = {'https://shop.example': 'https://shop.example'}
    [page] = plan_check(store, ['note'], origins=origins).pages
    assert page.address is None and page.shown == 'https://shop.example/Orders/*/'
    assert page.skip == ('no-url', "the address of /orders/* has a part mendwright doesn't "
                         'save (a record id, a token, an email address or a long name), so '
                         "mendwright check can't load it. Pass --url "
                         "'/orders/*=https://shop.example/orders/<one that exists>'.")
    [page] = plan_check(store, ['note'], origins=origins,
                        urls={'/orders/*': 'https://shop.example/orders/1001?tab=2'}).pages
    assert page.address == 'https://shop.example/orders/1001?tab=2'
    assert page.shown == 'https://shop.example/orders/1001?...'


def test_a_path_with_an_email_address_is_not_built_from_last_page(tmp_path):
    store = _store(tmp_path, {'k': ('/users/ada@example.com', None, None)})
    [page] = plan_check(store, ['k'], base_url='https://shop.example').pages
    assert page.address is None and page.skip[0] == 'no-url'


def test_a_stale_address_and_an_old_store(tmp_path):
    store = _store(tmp_path, {
        'framed': ('/signup', 'https://shop.example/embed/consent', None),
        'old': ('/checkout', None, None)})
    origins = {'https://shop.example': 'https://shop.example'}
    pages = {p.page: p for p in plan_check(store, ['framed', 'old'], origins=origins).pages}
    assert pages['/signup'].skip == ('no-url', 'its saved address and its saved page /signup '
                                     'disagree: it was last found on a page mendwright saves no '
                                     'address for (a Playwright Frame, or a frame the Selenium '
                                     "driver was switched into). Pass --url "
                                     "'/signup=https://shop.example/signup'.")
    assert pages['/checkout'].skip == ('no-url', 'the store has only the path /checkout for it, '
                                       'because it was saved before 0.9.0. Pass --base-url URL, '
                                       'or run your tests once with 0.9.0.')
    # --url gives them an address; --base-url builds one from lastPage.
    pages = {p.page: p for p in plan_check(store, ['framed', 'old'], origins=origins, urls={
        '/signup': 'https://shop.example/signup'}).pages}
    assert pages['/signup'].address == 'https://shop.example/signup'
    pages = {p.page: p for p in plan_check(store, ['framed', 'old'],
                                           base_url='https://shop.example').pages}
    assert pages['/checkout'].address == 'https://shop.example/checkout'
    assert pages['/signup'].address == 'https://shop.example/signup'


def test_url_must_name_a_selected_page_on_a_loaded_site(tmp_path):
    store = _store(tmp_path, {'a': ('/signup', 'https://shop.example/signup', None),
                              'b': ('/orders/*', 'https://shop.example/orders/*', None),
                              'c': ('/x', 'https://shop.example/x', None)})
    origins = {'https://shop.example': 'https://shop.example'}
    with pytest.raises(UsageError) as info:
        plan_check(store, ['a', 'b', 'c'], origins=origins, urls={'/nope': 'https://shop.example/n'})
    assert str(info.value) == ('--url names the page /nope, but no selected key was last found '
                               'there. Pages of the selected keys: /orders/*, /signup and 1 '
                               'more.')
    with pytest.raises(UsageError) as info:
        plan_check(store, ['a'], origins=origins, urls={'/signup': 'https://other.example/x?t=1'})
    assert str(info.value) == ("--url https://other.example/x?... is on https://other.example, "
                               "which this check doesn't load. Add --origin "
                               'https://other.example.')


def test_a_key_with_no_page_is_left_out(tmp_path):
    store = _store(tmp_path, {'draft': (None, None, None),
                              'home': (None, 'https://shop.example/', None)})
    plan = plan_check(store, ['draft', 'home'], origins={
        'https://shop.example': 'https://shop.example'})
    assert plan.left_out == [('draft', 'no-page')]
    assert [(p.page, p.address) for p in plan.pages] == [('/', 'https://shop.example/')]
    assert drift.left_out_lines(plan.left_out) == [
        '1 key was never found on a page with an address (set_content(), about:blank or a '
        'data: URL) and was left out.']


@pytest.mark.parametrize('path, word', [
    ('/account/sign-out', 'sign out'), ('/logout', 'logout'), ('/cart/empty', 'empty'),
    ('/settings/cancel-subscription', 'cancel'), ('/newsletter/opt-out', 'opt out'),
    ('/account/close-account', 'close'),
    ('/products/close-up-lens', 'close'),       # a known false refusal: --load-page lifts it
    ('/order-confirmation', None), ('/blog/clearance-sale', None), ('/signup', None),
    ('/Account/Log_Out', 'log out'), ('/', None)])
def test_action_words(path, word):
    assert action_word(path) == word


def test_an_action_page_is_not_loaded_without_load_page(tmp_path):
    store = _store(tmp_path, {'bye': ('/account/sign-out', 'https://shop.example/account/sign-out',
                                      None)})
    origins = {'https://shop.example': 'https://shop.example'}
    [page] = plan_check(store, ['bye'], origins=origins).pages
    assert page.address is None
    assert page.skip == ('action-page', "the path /account/sign-out reads as an action (sign "
                         'out), and loading it could do that, such as end the session your runs '
                         "use. Pass --load-page '/account/sign-out' to check it anyway.")
    [page] = plan_check(store, ['bye'], origins=origins, load_pages=['/account/*']).pages
    assert page.address == 'https://shop.example/account/sign-out'
    # A --url value is the user's own: never refused.
    [page] = plan_check(store, ['bye'], origins=origins, urls={
        '/account/sign-out': 'https://shop.example/account/sign-out'}).pages
    assert page.address == 'https://shop.example/account/sign-out'


@pytest.mark.parametrize('text, pattern', [
    ('/Checkout/*', '/checkout/*'), ('/orders/10482', '/orders/*'), ('/', '/'),
    ('*/payment', '*/payment'), ('*', '*'), ('//signup//', '/signup')])
def test_page_patterns(text, pattern):
    assert page_pattern(text) == pattern


def test_page_patterns_match_across_segments_and_the_root():
    assert drift.page_matches('/checkout/*', '/checkout/payment/card')
    assert drift.page_matches('*/payment', '/checkout/payment')
    assert drift.page_matches('/', '/') and not drift.page_matches('/', '/signup')
    assert not drift.page_matches('/signup', None)


@pytest.mark.parametrize('text', ['checkout', 'https://shop.example/x', '', 5])
def test_a_page_pattern_must_be_a_path(text):
    with pytest.raises(UsageError) as info:
        page_pattern(text, '--skip-page')
    assert str(info.value) == ('--skip-page takes a path that starts with / or *, such as '
                               '/checkout/*, got ' + repr(text) + '.')


@pytest.mark.parametrize('text, origin', [
    ('https://Shop.Example', 'https://shop.example'), ('https://shop.example/', 'https://shop.example'),
    ('http://127.0.0.1:8000', 'http://127.0.0.1:8000'), ('https://shop.example:443', 'https://shop.example'),
    ('http://[::1]:3000', 'http://[::1]:3000')])
def test_origins(text, origin):
    assert drift.parse_origin(text) == origin


@pytest.mark.parametrize('text', ['shop.example', 'ftp://shop.example', 'https://shop.example/a',
                                  'https://shop.example?x=1', 'https://shop.example#x',
                                  'https://u:p@shop.example', 'https://shop.example:99999', ''])
def test_a_malformed_origin_is_a_usage_error(text):
    with pytest.raises(UsageError) as info:
        drift.parse_origin(text, '--base-url')
    assert str(info.value) == ('--base-url takes scheme://host[:port] with http or https, such '
                               'as https://shop.example, got ' + repr(text) + '.')


@pytest.mark.parametrize('value', ['/orders/*', 'orders=https://x.example/a',
                                   '/a=/relative', '/a=ftp://x.example/a',
                                   '/a=https://u:p@x.example/a', '/a=https://x.example/a#frag'])
def test_a_malformed_url_is_a_usage_error(value):
    with pytest.raises(UsageError) as info:
        drift.parse_url(value)
    assert str(info.value) == ("--url takes PAGE=URL, such as "
                               "'/orders/*=https://shop.example/orders/1001', got "
                               + repr(value) + '.')


def test_url_values():
    assert drift.parse_url('/Orders/10482=https://shop.example/orders/1001?tab=2') == (
        '/orders/*', 'https://shop.example/orders/1001?tab=2')


# --- answers that leave a page unchecked --------------------------------------------------

@pytest.mark.parametrize('status, detail, text', [
    (401, 'http-4xx', 'the page answered HTTP 401, so its keys were not checked. It probably '
                      'needs a sign-in: pass --storage-state FILE.'),
    (403, 'http-4xx', 'the page answered HTTP 403, so its keys were not checked. It probably '
                      'needs a sign-in: pass --storage-state FILE.'),
    (404, 'http-4xx', 'the page answered HTTP 404, so its keys were not checked. If the page '
                      'moved, the keys that were on it need a new home in your script. If its '
                      'address needs a record that exists, pass --url.'),
    (410, 'http-4xx', 'the page answered HTTP 410, so its keys were not checked. If the page '
                      'moved, the keys that were on it need a new home in your script. If its '
                      'address needs a record that exists, pass --url.'),
    (500, 'http-5xx', 'the page answered HTTP 500, so its keys were not checked: the site had '
                      'an error.'),
    (503, 'http-5xx', 'the page answered HTTP 503, so its keys were not checked: the site had '
                      'an error.'),
    (418, 'http-4xx', 'the page answered HTTP 418, so its keys were not checked.')])
def test_http_errors(status, detail, text):
    assert drift.status_failure(status) == (detail, text)


@pytest.mark.parametrize('status', [200, 204, 301, 399, None])
def test_other_statuses_are_fine(status):
    assert drift.status_failure(status) is None


def test_redirects():
    assert drift.redirect_failure('https://shop.example/account', 'https://shop.example/account/',
                                  '/account') is None
    assert drift.redirect_failure('https://shop.example/account',
                                  'https://shop.example/login?next=/account&token=SECRET',
                                  '/account') == (
        'redirect', 'the page went to /login instead, so its keys were not checked. It probably '
                    'needs a sign-in or the steps before it: pass --storage-state FILE, or leave '
                    "the page out with --skip-page '/account'.")
    _detail, text = drift.redirect_failure('https://shop.example/account',
                                           'https://login.example/login?token=SECRET', '/account')
    assert text.startswith('the page went to /login (on https://login.example) instead')
    assert 'SECRET' not in text and '?' not in text


def test_navigation_errors_are_cut_and_scrubbed(monkeypatch):
    class TimeoutError(Exception):
        pass
    assert drift.nav_failure(TimeoutError('Timeout 30000ms exceeded.'), 30000) == (
        'nav-timeout', 'the page did not load within 30000 ms (--nav-timeout-ms).')
    assert drift.nav_failure(Exception('Download is starting'), 1000) == (
        'download', 'the address started a download instead of showing a page.')
    from mendwright.cli import _scrub
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-secret-value')
    error = ConnectionError('net::ERR_CONNECTION_REFUSED at https://x.example/y?token=SECRET '
                            'with sk-ant-secret-value\nsecond line')
    detail, text = drift.nav_failure(error, 30000, _scrub)
    assert detail == 'nav-error'
    assert text == ('the page did not load (ConnectionError: net::ERR_CONNECTION_REFUSED at '
                    'https://x.example/y with ***).')


class Page:
    """A driver whose url, password fields and their visibility a test sets."""

    def __init__(self, url, passwords=0, visible=True):
        self.address, self.passwords, self.visible = url, passwords, visible

    def url(self):
        return self.address

    def count(self, selector, frame_chain=()):
        return self.passwords

    def is_visible(self, selector, frame_chain=()):
        return self.visible


def test_the_gate_sees_a_late_redirect_and_a_sign_in_page():
    gate = drift.page_gate(Page('https://shop.example/account'), 'https://shop.example/account',
                           '/account')
    assert gate(True) is None
    moved = Page('https://shop.example/account')
    gate = drift.page_gate(moved, 'https://shop.example/account', '/account')
    moved.address = 'https://shop.example/login'
    assert gate(False)[0] == 'redirect'
    gate = drift.page_gate(Page('https://shop.example/account', passwords=1),
                           'https://shop.example/account', '/account')
    assert gate(True) == ('sign-in', drift.SIGN_IN_TEXT)
    assert gate(False) is None                          # one key resolved: not a sign-in page
    gate = drift.page_gate(Page('https://shop.example/account', passwords=1, visible=False),
                           'https://shop.example/account', '/account')
    assert gate(True) is None
