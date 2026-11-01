"""get_by twins: a heal of the key of a heal.page.get_by_*() call (or, with
open(suggest_get_by=True), of any key) is saved with a get_by_* call that matched the healed
element, and nothing else, on the live page when the heal was made (client.get_by_twin).
Offline: the lxml FakePage of conftest, whose get_by_* match the way Playwright does, and
whose aria_snapshot() gives the element's role and name. No browser, network or model."""

import json

import lxml.html
import pytest

import mendwright
from mendwright import client, getby
from mendwright.coordinator import HealCoordinator
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.store import load_store

KEY = 'get_by_role("button", name="Sign in")'
HEAL = "//button[@id='login']"
PAGE = ("<html><body><main><form id='login-form'><label for='email'>Email</label>"
        "<input id='email' name='email' type='email'>"
        "<button id='login' type='submit' class='primary'>Sign in</button></form>"
        "<a href='/help'>Help</a></main></body></html>")
RENAMED = PAGE.replace('>Sign in<', '>Log in<')
TWIN = 'get_by_role("button", name="Log in", exact=True)'
URL = 'https://shop.test/login'


def _page(make_page, html=PAGE):
    return make_page(html, url=URL, fingerprints=True)


def _open(page, tmp_path, **kwargs):
    kwargs.setdefault('heal_get_by', True)
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=None, store_path=str(tmp_path / 'o.json'), **kwargs)


def _saved(tmp_path):
    return json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))['selectors']


def _row(tmp_path, key, xpath):
    return next(c for c in _saved(tmp_path)[key]['candidates'] if c['xpath'] == xpath)


def _journal(tmp_path):
    return [json.loads(line) for line in
            (tmp_path / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]


def _sign_in(heal):
    return heal.page.get_by_role('button', name='Sign in').click()


def _heal_once(make_page, tmp_path, after, before=PAGE, **kwargs):
    page = _page(make_page, before)
    heal = _open(page, tmp_path, **kwargs)
    _sign_in(heal)
    page.set_html(after)
    _sign_in(heal)
    return page, heal


class Counting(PlaywrightSyncDriver):
    """The sync driver, counting the round trips a twin costs."""

    def __init__(self, page):
        super().__init__(page)
        self.calls = []

    def aria_name(self, selector, frame_chain=()):
        self.calls.append('aria_name')
        return super().aria_name(selector, frame_chain)

    def same_element(self, selector, other, frame_chain=()):
        self.calls.append(('same_element', selector))
        return super().same_element(selector, other, frame_chain)


# --- what is saved -----------------------------------------------------------------------------

def test_a_call_heal_is_saved_with_its_twin_and_the_journal_names_it(tmp_path, make_page):
    page, heal = _heal_once(make_page, tmp_path, RENAMED)
    assert [h.new for h in heal.heals] == [HEAL]
    assert _row(tmp_path, KEY, HEAL)['getBy'] == TWIN
    applied = [e for e in _journal(tmp_path) if e['event'] == 'heal.applied']
    assert applied[-1]['get_by'] == TWIN
    # It matched the healed element, and nothing else, when it was saved.
    assert page.get_by_role('button', name='Log in', exact=True).count() == 1


def test_a_twin_that_matches_several_elements_is_not_saved(tmp_path, make_page):
    twice = RENAMED.replace("<a href='/help'>", "<button type='button'>Log in</button><a href='/help'>")
    _page_, heal = _heal_once(make_page, tmp_path, twice)
    assert [h.new for h in heal.heals] == [HEAL]
    assert 'getBy' not in _row(tmp_path, KEY, HEAL)
    assert 'get_by' not in [e for e in _journal(tmp_path) if e['event'] == 'heal.applied'][-1]


def test_the_next_call_is_tried_when_the_first_matches_several(tmp_path, make_page):
    before = PAGE.replace("<button id='login'", "<button id='login' data-testid='sign-in'")
    twice = before.replace('>Sign in<', '>Log in<').replace(
        "<a href='/help'>", "<button type='button'>Log in</button><a href='/help'>")
    _page_, heal = _heal_once(make_page, tmp_path, twice, before)
    assert _row(tmp_path, KEY, heal.heals[-1].new)['getBy'] == 'get_by_test_id("sign-in")'


def test_a_twin_that_matches_another_element_is_not_saved(tmp_path, make_page):
    # The healed button sits in an aria-hidden region: get_by_role() leaves it out and finds
    # the other "Log in" button, one element, but not the healed one.
    after = ("<html><body><main><div aria-hidden='true'><button id='old' class='x'>Log in"
             "</button></div><button id='other'>Log in</button></main></body></html>")
    page = _page(make_page, after)
    driver = PlaywrightSyncDriver(page)
    doc = lxml.html.fromstring(after)
    assert client.get_by_twin('sign_in', doc, "//button[@id='old']", "//button[@id='old']",
                              driver) == ''
    assert driver.same_element('get_by_role("button", name="Log in", exact=True)',
                               "//button[@id='old']") is False


def test_the_live_check_decides(tmp_path):
    doc = lxml.html.fromstring("<main><button id='go' data-testid='go'>Go</button></main>")
    asked = []

    class Driver:
        def aria_name(self, selector, frame_chain=()):
            return {'role': 'button', 'name': 'Go', 'level': None}

        def same_element(self, selector, other, frame_chain=()):
            asked.append(selector)
            return selector.startswith('get_by_test_id')

    assert client.get_by_twin(KEY, doc, "//button[@id='go']", "//button[@id='go']",
                              Driver()) == 'get_by_test_id("go")'
    assert asked == ['get_by_role("button", name="Go", exact=True)', 'get_by_test_id("go")']
    # None (cannot tell) or anything but True is no twin.
    Driver.same_element = lambda self, selector, other, frame_chain=(): None
    assert client.get_by_twin(KEY, doc, "//button[@id='go']", "//button[@id='go']",
                              Driver()) == ''


def test_a_driver_without_same_element_saves_no_twin_and_still_heals(tmp_path, make_page):
    page = _page(make_page)
    heal = _open(page, tmp_path)
    _sign_in(heal)
    page.set_html(RENAMED)
    heal.coordinator.driver.same_element = None
    _sign_in(heal)
    assert [h.new for h in heal.heals] == [HEAL] and 'getBy' not in _row(tmp_path, KEY, HEAL)
    assert client.get_by_twin(KEY, lxml.html.fromstring(RENAMED), HEAL, HEAL, object()) is None


def test_without_aria_snapshot_a_label_or_test_id_twin_is_still_found(tmp_path, make_page):
    before = PAGE.replace("<button id='login'", "<button id='login' data-testid='sign-in'")
    page = _page(make_page, before)
    page.aria_snapshot = False                      # a Playwright before 1.49
    heal = _open(page, tmp_path)
    _sign_in(heal)
    page.set_html(before.replace('>Sign in<', '>Log in<'))
    _sign_in(heal)
    assert _row(tmp_path, KEY, heal.heals[-1].new)['getBy'] == 'get_by_test_id("sign-in")'
    assert page.aria_reads == 0


def test_a_field_gets_a_label_twin_and_the_calls_own_method_goes_first(tmp_path, make_page):
    before = ("<html><body><form><label for='e'>Email</label><input id='e' name='email' "
              "placeholder='you@example.com'></form></body></html>")
    after = before.replace('>Email<', '>E-mail address<').replace('you@example.com', 'name@site')
    page = _page(make_page, before)
    heal = _open(page, tmp_path)
    heal.page.get_by_placeholder('you@example.com').fill('ada@example.com')
    heal.page.get_by_label('Email').fill('ada@example.com')
    page.set_html(after)
    heal.page.get_by_placeholder('you@example.com').fill('ada@example.com')
    heal.page.get_by_label('Email').fill('ada@example.com')
    saved = _saved(tmp_path)
    assert [c.get('getBy') for c in saved['get_by_placeholder("you@example.com")']['candidates']
            if c['source'] == 'heal'] == ['get_by_placeholder("name@site", exact=True)']
    assert [c.get('getBy') for c in saved['get_by_label("Email")']['candidates']
            if c['source'] == 'heal'] == ['get_by_label("E-mail address", exact=True)']


def test_a_twin_costs_one_read_and_two_round_trips_per_call_tried(tmp_path, make_page):
    page = _page(make_page)
    heal = _open(page, tmp_path)
    _sign_in(heal)
    page.set_html(RENAMED)
    driver = heal.coordinator.driver = Counting(page)
    _sign_in(heal)
    assert driver.calls == ['aria_name', ('same_element', TWIN)]
    assert page.aria_reads == 1


def test_at_most_max_twins_calls_are_tried(tmp_path):
    doc = lxml.html.fromstring("<form><label for='q'>Search</label><input id='q' "
                               "data-testid='q'></form>")
    tried = []

    class Driver:
        def aria_name(self, selector, frame_chain=()):
            return {'role': 'textbox', 'name': 'Search', 'level': None}

        def same_element(self, selector, other, frame_chain=()):
            tried.append(selector)
            return False

    client.get_by_twin('get_by_placeholder("Find")', doc, "//input[@id='q']",
                       "//input[@id='q']", Driver())
    assert len(tried) == client.MAX_TWINS == 3


# --- which heals get one ---------------------------------------------------------------------

def test_other_keys_get_a_twin_only_with_suggest_get_by(tmp_path, make_page, monkeypatch):
    moved = PAGE.replace("id='login'", "id='signin'")
    page = _page(make_page)
    heal = _open(page, tmp_path)
    heal.locator('sign_in', '#login').click()
    page.set_html(moved)
    heal.locator('sign_in', '#login').click()
    assert [h.source for h in heal.heals] == ['fingerprint']
    assert [c.get('getBy') for c in _saved(tmp_path)['sign_in']['candidates']
            if c['source'] == 'heal'] == [None]

    for folder, kwargs in ((tmp_path / 'b', {'suggest_get_by': True}),
                           (tmp_path / 'c', {})):
        folder.mkdir()
        if not kwargs:
            monkeypatch.setenv('MENDWRIGHT_SUGGEST_GET_BY', '1')
        page = _page(make_page)
        heal = _open(page, folder, **kwargs)
        assert heal.coordinator.twins == 'all'
        heal.locator('sign_in', '#login').click()
        page.set_html(moved)
        heal.locator('sign_in', '#login').click()
        assert [c.get('getBy') for c in _saved(folder)['sign_in']['candidates']
                if c['source'] == 'heal'] == ['get_by_role("button", name="Sign in", exact=True)']
    with pytest.raises(TypeError, match='suggest_get_by=True'):
        _open(page, folder, suggest_get_by='yes')
    monkeypatch.setenv('MENDWRIGHT_SUGGEST_GET_BY', 'maybe')
    with pytest.raises(ValueError, match='MENDWRIGHT_SUGGEST_GET_BY must be 1 or 0'):
        _open(page, folder)


def test_no_twin_inside_an_iframe_or_a_shadow_root_and_none_in_a_preview(tmp_path):
    coordinator = HealCoordinator(load_store(None, str(tmp_path / 'o.json')),
                                  provider=object(), html_provider=str, inventory_provider=list,
                                  guard=lambda: True)
    assert coordinator.twins == 'calls'
    assert coordinator._wants_twin(KEY) and not coordinator._wants_twin('sign_in')
    coordinator.frame_xpath = '#pay'
    assert not coordinator._wants_twin(KEY)
    coordinator.frame_xpath, coordinator.shadow_hosts = None, ('my-app',)
    assert not coordinator._wants_twin(KEY)
    coordinator.shadow_hosts, coordinator.twins = None, None
    assert not coordinator._wants_twin(KEY)


# --- the store -------------------------------------------------------------------------------

def test_the_store_keeps_a_twin_merges_it_and_drops_it_with_empty_text(tmp_path):
    path = str(tmp_path / 'o.json')
    store = load_store(None, path)
    store.add_candidate(KEY, HEAL, source='heal', get_by=TWIN)
    assert _row(tmp_path, KEY, HEAL)['getBy'] == TWIN
    other = load_store(None, path)
    assert next(c for c in other.candidates(KEY) if c['xpath'] == HEAL)['getBy'] == TWIN
    # Another process writes the twin of a row this one already holds: it is taken in.
    store.add_candidate(KEY, "//button[@id='x']", source='heal')
    other.add_candidate(KEY, "//button[@id='x']", source='heal', get_by='get_by_test_id("x")')
    store.record_success(KEY, "//button[@id='x']")
    assert _row(tmp_path, KEY, "//button[@id='x']")['getBy'] == 'get_by_test_id("x")'
    # '' drops it; None leaves it.
    store.add_candidate(KEY, HEAL, source='heal', get_by=None)
    assert _row(tmp_path, KEY, HEAL)['getBy'] == TWIN
    store.add_candidate(KEY, HEAL, source='heal', get_by='')
    assert 'getBy' not in _row(tmp_path, KEY, HEAL)


def test_a_twin_is_one_more_field_on_the_row_and_nothing_else_changes(tmp_path):
    with_twin, without = tmp_path / 'a.json', tmp_path / 'b.json'
    for path, twin in ((with_twin, TWIN), (without, None)):
        store = load_store(None, str(path))
        store.set_code_candidates(KEY, [KEY], now='2026-11-01T00:00:00+00:00')
        store.add_candidate(KEY, HEAL, source='heal', get_by=twin,
                            now='2026-11-01T00:00:01+00:00')
    a = json.loads(with_twin.read_text(encoding='utf-8'))
    b = json.loads(without.read_text(encoding='utf-8'))
    rows = a['selectors'][KEY]['candidates']
    assert [c.pop('getBy', None) for c in rows] == [None, TWIN]
    assert a == b


def test_an_older_store_without_get_by_still_gets_the_heal(tmp_path):
    class Older:
        def __init__(self):
            self.rows = []

        def add_candidate(self, key, xpath, *, source='heal', frame_xpath=None, css=None,
                          shadow_hosts=None):
            self.rows.append((key, xpath, css))

    older = Older()
    assert client.add_heal(older, KEY, HEAL, css='button#login', frame_xpath='',
                           shadow_hosts=[], get_by=TWIN) is True
    assert older.rows == [(KEY, HEAL, 'button#login')]


def test_twin_text_shows_only_a_call_in_mendwrights_spelling():
    assert getby.twin_text(TWIN) == TWIN
    assert getby.twin_text("get_by_role('button', name='Log in', exact=True)") is None
    assert getby.twin_text('page.get_by_role("button")') is None
    assert getby.twin_text('<img src=x onerror=alert(1)>') is None
    assert getby.twin_text(None) is None and getby.twin_text(7) is None


def test_parse_aria_reads_the_first_line_of_a_snapshot():
    assert getby.parse_aria('- link "Sign in":\n  - /url: /x') == {
        'role': 'link', 'name': 'Sign in', 'level': None}
    assert getby.parse_aria('- heading "Cart \\"2\\"" [level=2]') == {
        'role': 'heading', 'name': 'Cart "2"', 'level': 2}
    assert getby.parse_aria("- 'button \"a: b\"'") == {'role': 'button', 'name': 'a: b',
                                                      'level': None}
    assert getby.parse_aria('- button') == {'role': 'button', 'name': None, 'level': None}
    assert getby.parse_aria('- button /Log.*/') == {'role': 'button', 'name': None,
                                                     'level': None}
    for bad in (None, '', 'button "x"', "- 'button", '- "x"'):
        assert getby.parse_aria(bad) is None


def test_twin_calls_leave_out_long_and_empty_text_and_the_call_itself():
    doc = lxml.html.fromstring("<main><button id='b'>Sign in</button></main>")
    el = doc.xpath('//button')[0]
    call = getby.parse('get_by_role("button", name="Sign in", exact=True)')
    assert getby.twin_calls(el, call, {'role': 'button', 'name': 'Sign in'}) == []
    assert getby.twin_calls(el, None, {'role': 'button', 'name': 'x' * 101}) == []
    assert getby.twin_calls(el, None, {'role': 'button', 'name': '  '}) == []
    assert [getby.canonical(c) for c in getby.twin_calls(
        el, None, {'role': 'button', 'name': 'Sign in'})] == [
        'get_by_role("button", name="Sign in", exact=True)']
