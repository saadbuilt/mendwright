"""heal.page.get_by_*() with mendwright.open(page, heal_get_by=True), offline: the lxml
FakePage of conftest, whose get_by_* match the way Playwright does, and a StubProvider that
would propose the right element, configured in every test that heals or declines a call, to
show it is never asked. No browser, network or model."""

import json
import logging

import pytest

import mendwright
from mendwright import AttentionRequired, InvalidSelector, facade, getby
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.facade import ActionProxy
from mendwright.flow import InterruptionLoop
from mendwright.proxies import GuardedLocator, HealingGetByLocator

KEY = 'get_by_role("button", name="Sign in")'
HEAL = "//button[@id='login']"
PAGE = ("<html><body><main><form id='login-form'><label for='email'>Email</label>"
        "<input id='email' name='email' type='email'>"
        "<button id='login' type='submit' class='primary'>Sign in</button></form>"
        "<a href='/help'>Help</a></main></body></html>")
RENAMED = PAGE.replace('>Sign in<', '>Log in<')          # the name changed, the id stayed
TWINS = PAGE.replace("<button id='login' type='submit' class='primary'>Sign in</button>",
                     "<button type='submit' class='primary'>Log in</button>"
                     "<button type='submit' class='primary'>Log in</button>")
URL = 'https://shop.test/login'

timeout_error = pytest.importorskip('playwright.sync_api').TimeoutError


def _page(make_page, html=PAGE):
    return make_page(html, url=URL, fingerprints=True)


def _open(page, tmp_path, provider, **kwargs):
    kwargs.setdefault('heal_get_by', True)
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'), **kwargs)


def _saved(tmp_path):
    path = tmp_path / 'o.json'
    return json.loads(path.read_text(encoding='utf-8'))['selectors'] if path.exists() else {}


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _sign_in(heal, **kwargs):
    return heal.page.get_by_role('button', name='Sign in').click(**kwargs)


def _healed(make_page, tmp_path, provider, **kwargs):
    """A page whose Sign in button matched once, then was renamed and healed."""
    page = _page(make_page)
    heal = _open(page, tmp_path, provider, **kwargs)
    _sign_in(heal)
    page.set_html(RENAMED)
    _sign_in(heal)
    return page, heal


@pytest.fixture
def clock(monkeypatch):
    """A fake clock for the lookups of get_by_* calls: a poll's sleep moves it on, and no
    test waits."""
    state = {'now': 0.0, 'slept': [], 'timeouts': []}
    real = facade.resolve_get_by

    def sleep(seconds):
        state['slept'].append(seconds)
        state['now'] += seconds

    def resolve(*args, **kwargs):
        state['timeouts'].append(kwargs['timeout_ms'])
        return real(*args, monotonic=lambda: state['now'], sleep=sleep, **kwargs)

    monkeypatch.setattr(facade, 'resolve_get_by', resolve)
    return state


class Counting(PlaywrightSyncDriver):
    """The sync driver, counting the page queries it makes."""

    trips = 0

    def _probe(self, *args, **kwargs):
        self.trips += 1
        return super()._probe(*args, **kwargs)


# --- off by default ----------------------------------------------------------------------

def test_off_by_default_get_by_is_the_real_locator(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), heal_get_by=None)
    button = heal.page.get_by_role('button', name='Sign in')
    assert type(button).__name__ == 'FakeGetBy'
    button.click()
    assert page.actions[-1][1] == 'click' and _saved(tmp_path) == {} and page.trips == 1
    with pytest.raises(TypeError):
        heal.page.get_by_role('button', nmae='Sign in')       # Playwright's own, as before
    guarded = _open(page, tmp_path, stub_provider(HEAL), dismiss=True, heal_get_by=False)
    go = guarded.page.get_by_role('button', name='Sign in')
    assert type(go) is GuardedLocator
    assert repr(go) == "<GuardedLocator \"page.get_by_role('button', name='Sign in')\">"


def test_the_environment_turns_it_on_and_the_argument_wins(tmp_path, make_page, stub_provider,
                                                          monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_HEAL_GET_BY', '1')
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), heal_get_by=None)
    assert type(heal.page.get_by_role('button', name='Sign in')) is HealingGetByLocator
    heal = _open(page, tmp_path, stub_provider(HEAL), heal_get_by=False)
    assert type(heal.page.get_by_role('button', name='Sign in')).__name__ == 'FakeGetBy'
    monkeypatch.setenv('MENDWRIGHT_HEAL_GET_BY', 'off')
    heal = _open(page, tmp_path, stub_provider(HEAL), heal_get_by=None)
    assert heal._heal_get_by is False
    assert facade.get_by_setting(True) is True


def test_a_bad_heal_get_by_raises_type_error(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    with pytest.raises(TypeError) as caught:
        _open(page, tmp_path, stub_provider(HEAL), heal_get_by='yes')
    assert str(caught.value) == ("mendwright.open() takes heal_get_by=True, heal_get_by=False "
                                 "or heal_get_by=None, got heal_get_by='yes'.")
    assert page.queries == [] and page.trips == 0


def test_a_bad_environment_value_raises_value_error(tmp_path, make_page, stub_provider,
                                                   monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_HEAL_GET_BY', 'maybe')
    with pytest.raises(ValueError, match='MENDWRIGHT_HEAL_GET_BY must be 1 or 0'):
        _open(_page(make_page), tmp_path, stub_provider(HEAL), heal_get_by=None)


def test_selenium_takes_the_argument_and_ignores_it(tmp_path):
    from test_proxies import WebDriver
    heal = mendwright.open(WebDriver('<button id="go">Go</button>'), provider=None,
                           store_path=str(tmp_path / 'o.json'), heal_get_by=True)
    assert heal._heal_get_by is True
    assert not hasattr(heal.driver, 'get_by_role')


# --- the happy path ------------------------------------------------------------------------

def test_a_call_that_matches_acts_and_records_it(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider)
    button = heal.page.get_by_role('button', name='Sign in')
    assert repr(button) == "<HealingGetByLocator 'get_by_role(\"button\", name=\"Sign in\")'>"
    assert isinstance(button, type(page.get_by_role('button')))
    assert page.trips == 0                     # building it touches nothing
    button.click()
    assert page.actions[-1][:2] == ("get_by_role('button', name='Sign in')", 'click')
    entry = _saved(tmp_path)[KEY]
    assert entry['expectedRole'] == 'button' and entry['lastPage'] == '/login'
    assert entry['fingerprint']['attrs']['id'] == 'login'
    assert [(c['xpath'], c['source']) for c in entry['candidates']] == [(KEY, 'code')]
    assert entry['candidates'][0]['lastSucceededAt']
    assert heal.heals == [] and provider.bundles == []

    driver = heal._driver = Counting(page)
    heal.page.get_by_role('button', name='Sign in').click()
    assert driver.trips == 3          # count, is_visible and scroll, over the raw call
    heal.page.native.get_by_role('button', name='Sign in').click()
    assert driver.trips == 3


def test_a_renamed_button_heals_from_its_fingerprint(tmp_path, make_page, stub_provider, caplog):
    provider = stub_provider(HEAL)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        page, heal = _healed(make_page, tmp_path, provider)
    assert page.actions[-1][:2] == ('xpath=' + HEAL, 'click')
    assert [(h.key, h.old, h.new, h.source) for h in heal.heals] == [
        (KEY, (KEY,), HEAL, 'fingerprint')]
    events = [(e['event'], e.get('source')) for e in _journal(tmp_path)]
    assert events == [('heal.requested', None), ('heal.proposed', 'fingerprint'),
                      ('heal.applied', 'fingerprint')]
    assert _journal(tmp_path)[-1]['old'] == [KEY]
    assert any(r.getMessage().startswith(
        "mendwright: healed '" + KEY + "': " + KEY + ' -> ' + HEAL + ' (fingerprint, ')
        for r in caplog.records)
    assert provider.bundles == []
    assert {c['xpath'] for c in _saved(tmp_path)[KEY]['candidates']} == {HEAL, KEY}


def test_the_saved_heal_answers_while_the_call_matches_nothing(tmp_path, make_page,
                                                              stub_provider, clock):
    page, _first = _healed(make_page, tmp_path, stub_provider(HEAL))
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    _sign_in(heal)
    assert page.actions[-1][:2] == ('xpath=' + HEAL, 'click')
    assert clock['slept'] == [] and heal.heals == []


def test_the_call_wins_over_a_saved_heal_when_it_matches_again(tmp_path, make_page,
                                                              stub_provider):
    page, heal = _healed(make_page, tmp_path, stub_provider(HEAL))
    page.set_html(PAGE)
    _sign_in(heal)
    assert page.actions[-1][:2] == ("get_by_role('button', name='Sign in')", 'click')
    assert heal.store.last_success(KEY)['xpath'] == KEY


# --- refusals ------------------------------------------------------------------------------

def test_a_call_is_never_healed_by_a_model(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider)
    _sign_in(heal)
    page.set_html(TWINS)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(heal)
    message = str(caught.value)
    assert message.startswith('No matching element for: ' + KEY + ' (tried 1 candidate(s)).')
    assert "the fingerprint match for '" + KEY + "' was not confident" in message
    assert message.endswith(getby.NO_MODEL)
    assert isinstance(caught.value, timeout_error) and isinstance(caught.value, AttentionRequired)
    assert provider.bundles == [] and heal.heals == []


@pytest.mark.parametrize('name', ['Sing in', 'Log in'])
def test_a_call_with_no_fingerprint_is_not_healed(tmp_path, make_page, stub_provider, name):
    provider = stub_provider(HEAL)
    heal = _open(_page(make_page), tmp_path, provider)
    with pytest.raises(getby.GetByNotFound) as caught:
        heal.page.get_by_role('button', name=name).click()
    key = mendwright.get_by_key('get_by_role', 'button', name=name)
    assert str(caught.value).endswith(getby.no_fingerprint_message(key))
    assert [e for e in _journal(tmp_path) if e['event'] == 'heal.requested'] == []
    with pytest.raises(getby.GetByNotFound):
        heal.page.get_by_role('btn', name='Sign in').click()      # an unknown role: none
    assert provider.bundles == []


def test_a_call_that_matched_only_with_the_tier_off_is_not_healed(tmp_path, make_page,
                                                                  stub_provider):
    page = _page(make_page)
    _sign_in(_open(page, tmp_path, stub_provider(HEAL), fingerprint=False))
    page.set_html(RENAMED)
    provider = stub_provider(HEAL)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider))
    assert str(caught.value).endswith(getby.no_fingerprint_message(KEY))
    assert provider.bundles == []


def test_tier_off_and_disable_say_so(tmp_path, make_page, stub_provider, monkeypatch):
    page = _page(make_page)
    _sign_in(_open(page, tmp_path, stub_provider(HEAL)))
    page.set_html(RENAMED)
    provider = stub_provider(HEAL)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider, fingerprint=False))
    assert str(caught.value).endswith(getby.tier_off_message(KEY))
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider))
    assert str(caught.value).endswith(facade.DISABLED_REASON)
    assert provider.bundles == []


def test_an_unknown_role_is_declined_with_the_call_sentence(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    heal._driver.describe = lambda selector, frame_chain=(): None     # the read fails
    _sign_in(heal)
    assert _saved(tmp_path)[KEY].get('expectedRole', 'unknown') == 'unknown'
    page.set_html(RENAMED)
    provider = stub_provider(HEAL)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider))
    assert str(caught.value).endswith(getby.role_unknown_message(KEY))
    assert provider.bundles == []


@pytest.mark.parametrize('before, after, call, role', [
    ("<button role='tab' id='settings'>Settings</button>",
     "<button id='settings'>Preferences</button>",
     ('tab',), {'name': 'Settings'}),
    ("<h2 id='cart'>Cart</h2>", "<h2 id='cart' aria-level='3'>Cart</h2>",
     ('heading',), {'name': 'Cart', 'level': 2}),
    ("<a id='help' href='/help'>Help</a>", "<a id='help'>Help</a>",
     ('link',), {'name': 'Help'}),
], ids=['no longer a tab', 'another level', 'a link without href'])
def test_a_get_by_role_heal_must_have_the_calls_aria_role(tmp_path, make_page, stub_provider,
                                                          before, after, call, role):
    page = _page(make_page, '<html><body><main>' + before + '</main></body></html>')
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider)
    heal.page.get_by_role(*call, **role).click()
    page.set_html('<html><body><main>' + after + '</main></body></html>')
    with pytest.raises(getby.GetByNotFound) as caught:
        heal.page.get_by_role(*call, **role).click()
    mismatch = getby.role_mismatch(getby.call_of('get_by_role', call, role)[0])
    assert mismatch + ')' in str(caught.value)
    assert [e['reason'] for e in _journal(tmp_path) if e['event'] == 'heal.rejected'] == [mismatch]
    assert provider.bundles == [] and heal.heals == []


@pytest.mark.parametrize('answer', [None, 'missing'])
def test_a_driver_that_cannot_check_the_aria_role_refuses_the_heal(tmp_path, make_page,
                                                                   stub_provider, answer):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    _sign_in(heal)
    page.set_html(RENAMED)
    heal.coordinator.driver.role_match = None if answer == 'missing' else (lambda *a: None)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(heal)
    assert getby.ROLE_UNREADABLE in str(caught.value)
    assert [c['xpath'] for c in heal.store.candidates(KEY)] == [KEY]


def test_several_matches_go_to_playwright_and_nothing_is_saved(tmp_path, make_page,
                                                             stub_provider, clock):
    page = _page(make_page, PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in'
                                                             ' later</button>'))
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    with pytest.raises(Exception, match='strict mode violation') as caught:
        _sign_in(heal)
    assert not isinstance(caught.value, AttentionRequired)
    assert not (tmp_path / 'o.json').exists() and _journal(tmp_path) == []
    assert clock['slept'] == [] and clock['timeouts'] == []


def test_one_hidden_match_goes_to_playwright(tmp_path, make_page, stub_provider):
    page = _page(make_page, PAGE.replace("class='primary'", "class='primary' data-invisible"))
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    with pytest.raises(Exception, match='Timeout 30000ms exceeded'):
        _sign_in(heal)
    assert not (tmp_path / 'o.json').exists()


def test_a_hidden_checkbox_is_used(tmp_path, make_page, stub_provider):
    terms = ("<html><body><label><input type='checkbox' id='terms'> I accept the terms"
             "</label></body></html>")
    page = _page(make_page, terms)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    heal.page.get_by_role('checkbox', name='I accept the terms').check()
    page.set_html(terms.replace("id='terms'", "id='terms' data-invisible"))
    again = _open(page, tmp_path, stub_provider(HEAL))
    again.page.get_by_role('checkbox', name='I accept the terms', include_hidden=False).check(
        force=True)
    key = 'get_by_role("checkbox", name="I accept the terms")'
    assert again.store.last_success(key)['xpath'] == key     # looked up, not passed through


def test_a_rejected_call_goes_to_playwright_at_once(tmp_path, make_page, stub_provider, clock):
    page = _page(make_page)
    page.reject['get_by_role'] = ('Locator.count: Error: "level" attribute is only supported '
                                  'for roles: "heading", "listitem", "row", "treeitem"')
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    with pytest.raises(Exception, match='"level" attribute is only supported') as caught:
        heal.page.get_by_role('button', name='Sign in', level=2).click()
    assert not isinstance(caught.value, (AttentionRequired, InvalidSelector))
    assert clock['slept'] == [] and not (tmp_path / 'o.json').exists()


def test_a_saved_heal_never_answers_a_call_that_matches_something(tmp_path, make_page,
                                                                  stub_provider):
    page, heal = _healed(make_page, tmp_path, stub_provider(HEAL))
    page.set_html(RENAMED.replace('</form>', "<button>Sign in</button><button>Sign in"
                                             '</button></form>'))
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal)
    assert heal.store.status(KEY, HEAL) == 'active'
    assert page.actions[-1][0] == 'xpath=' + HEAL      # the heal was not acted on again


def test_a_call_that_starts_to_match_several_during_the_wait_passes_through_at_once(
        tmp_path, make_page, stub_provider, clock):
    page = _page(make_page, RENAMED)
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    real = page._get_by

    def later(method, args, kwargs):
        if page.trips >= 2:       # after the first look and the first poll
            page.set_html(PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in'
                                                           '</button>'))
        return real(method, args, kwargs)

    page._get_by = later
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal)
    assert len(clock['slept']) <= 2


class FakeFrame:
    """A child frame of a FakePage, for frame_matches()."""

    def __init__(self, make_page, html, parent, name='payment'):
        self._inner = make_page(html)
        self.parent_frame, self.name, self.url = parent, name, 'https://pay.test/form'

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def frame_element(self):
        raise RuntimeError('no iframe element in the fake')


class MainFrame:
    parent_frame = None


def test_a_call_that_matches_in_an_iframe_is_not_healed_or_followed(tmp_path, make_page,
                                                                    stub_provider):
    page = _page(make_page, "<html><body><main><p>Pay below</p></main></body></html>")
    pay = "<html><body><button id='pay'>Pay</button></body></html>"
    page.frames = [MainFrame(), FakeFrame(make_page, pay, MainFrame())]
    key = 'get_by_role("button", name="Pay")'
    heal = _open(page, tmp_path, stub_provider("//button[@id='pay']"))
    heal.store.add_candidate(key, "//button[@id='pay']")       # a heal that matches only there
    with pytest.raises(getby.GetByNotFound) as caught:
        heal.page.get_by_role('button', name='Pay').click()
    assert str(caught.value).endswith(
        "-> '" + key + "' is inside an iframe: " + key + ' matches nothing in the page '
        "mendwright was opened on, but matches inside the iframe named 'payment'. mendwright "
        'did not look for it there, because mendwright does not follow a get_by_* call into an '
        'iframe, and did not ask a model to heal it. Open mendwright on that Frame instead: '
        "heal = mendwright.open(page.frame(name='payment'), heal_get_by=True).")
    assert not any(c.get('frameXPath') for c in heal.store.candidates(key))
    assert heal.heals == []


def test_destructive_call_keys_are_refused(tmp_path, make_page, stub_provider):
    pay = mendwright.get_by_key('get_by_role', 'button', name='Sign in')
    page = _page(make_page)
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider, destructive_keys={pay})
    _sign_in(heal)
    page.set_html(RENAMED)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(heal)
    assert "is marked as a destructive control" in str(caught.value)
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {
        pay: {'destructive': True, 'expectedRole': 'button', 'candidates': []}}}), encoding='utf-8')
    store = mendwright.load_store(str(seed), str(tmp_path / 'seeded.json'))
    seeded = mendwright.open(page, provider=provider, store=store, heal_get_by=True, timeout_ms=0)
    with pytest.raises(getby.GetByNotFound, match='is marked as a destructive control'):
        _sign_in(seeded)
    assert provider.bundles == []


# The call as Python writes it, in single quotes, as mendwright's own INFO lines do.
PY_KEY = "get_by_role('button', name='Sign in')"


def test_a_call_key_in_another_spelling_still_marks_the_call(tmp_path, make_page,
                                                             stub_provider):
    page = _page(make_page)
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider, destructive_keys={PY_KEY})
    _sign_in(heal)
    page.set_html(RENAMED)
    with pytest.raises(getby.GetByNotFound, match='is marked as a destructive control'):
        _sign_in(heal)
    assert heal.heals == [] and page.actions[-1][0] != 'xpath=' + HEAL
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {
        PY_KEY: {'destructive': True, 'expectedRole': 'button', 'candidates': []}}}),
        encoding='utf-8')
    store = mendwright.load_store(str(seed), str(tmp_path / 'seeded.json'))
    assert store.meta(KEY)['destructive'] is True and store.is_locked_destructive(KEY)
    seeded = mendwright.open(page, provider=provider, store=store, heal_get_by=True, timeout_ms=0)
    with pytest.raises(getby.GetByNotFound, match='is marked as a destructive control'):
        _sign_in(seeded)
    assert provider.bundles == []


def test_expect_values_and_present_read_a_call_key_in_another_spelling(tmp_path, make_page,
                                                                       stub_provider):
    page = _page(make_page, _total_page('$12.00'))
    heal = _open(page, tmp_path, stub_provider(HEAL), expect_values={"get_by_test_id('total')":
                                                                     'money'})
    assert heal.page.get_by_test_id('total').inner_text() == '$12.00'
    page.set_html(_total_page('3 items', testid='order-total'))
    with pytest.raises(AttentionRequired) as caught:
        heal.page.get_by_test_id('total').inner_text()
    assert not isinstance(caught.value, timeout_error)
    assert [h.confirmed for h in heal.heals] == [False]

    page, healed = _healed(make_page, tmp_path / 'b', stub_provider(HEAL))
    assert healed.present(PY_KEY) is True          # the saved heal, while the call misses


def test_a_call_key_written_on_page_is_a_key_of_its_own_and_warned_about(
        tmp_path, make_page, stub_provider, caplog):
    on_page, total = 'page.' + PY_KEY, "page.get_by_test_id('total')"
    page = _page(make_page)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        heal = _open(page, tmp_path, stub_provider(HEAL), destructive_keys={on_page},
                     expect_values={total: 'money'})
    assert [r.getMessage() for r in caplog.records] == [
        getby.on_object_message('expect_values=', total, getby.parse("get_by_test_id('total')")),
        getby.on_object_message('destructive_keys=', on_page, getby.parse(KEY))]
    _sign_in(heal)
    page.set_html(RENAMED)
    _sign_in(heal)                                  # not marked: the key is its own
    assert [h.key for h in heal.heals] == [KEY]
    caplog.clear()
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {
        on_page: {'destructive': True, 'candidates': []}}}), encoding='utf-8')
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store = mendwright.load_store(str(seed), str(tmp_path / 'seeded.json'))
    assert [r.getMessage() for r in caplog.records] == [
        getby.on_object_message(str(seed), on_page, getby.parse(KEY))]
    assert store.is_locked_destructive(on_page) and not store.is_locked_destructive(KEY)


def test_two_spellings_of_one_call_key_are_refused(tmp_path, make_page, stub_provider):
    with pytest.raises(ValueError) as caught:
        _open(_page(make_page), tmp_path, stub_provider(HEAL),
              expect_values={KEY: 'money', PY_KEY: 'money'})
    assert str(caught.value) == getby.two_spellings_message('expect_values=', KEY, PY_KEY)
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {
        KEY: {'candidates': []}, PY_KEY: {'destructive': True, 'candidates': []}}}),
        encoding='utf-8')
    with pytest.raises(mendwright.store.StoreReadError) as caught:
        mendwright.load_store(str(seed), str(tmp_path / 'seeded.json'))
    assert str(caught.value) == getby.two_spellings_message(str(seed), KEY, PY_KEY)


def test_a_heal_saved_before_the_key_was_marked_is_quarantined_and_not_a_timeout_error(
        tmp_path, make_page, stub_provider):
    page, _first = _healed(make_page, tmp_path, stub_provider(HEAL))
    heal = _open(page, tmp_path, stub_provider(HEAL), destructive_keys={KEY})
    with pytest.raises(AttentionRequired) as caught:
        _sign_in(heal)
    assert not isinstance(caught.value, timeout_error)
    assert 'a destructive control never acts through a healed selector' in str(caught.value)
    assert heal.store.status(KEY, HEAL) == 'quarantined'


def test_the_page_guard_applies_to_call_keys(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    _sign_in(heal)
    page.url = 'https://shop.test/checkout'
    page.set_html(RENAMED)
    with pytest.raises(getby.GetByNotFound, match="was last found on /login, and this page is /checkout"):
        _sign_in(heal)
    assert [e['event'] for e in _journal(tmp_path)] == ['flow.page_guard', 'heal.declined']
    assert _journal(tmp_path)[-1]['code'] == 'page_guard'


def test_dry_run_checks_and_applies_nothing(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    _sign_in(_open(page, tmp_path, stub_provider(HEAL)))
    page.set_html(RENAMED)
    provider = stub_provider(HEAL)
    with pytest.raises(getby.GetByNotFound, match='dry-run mode is on'):
        _sign_in(_open(page, tmp_path, provider, dry_run=True))
    assert [e['event'] for e in _journal(tmp_path)] == ['heal.requested', 'heal.proposed',
                                                         'heal.declined']
    assert _journal(tmp_path)[-1]['code'] == 'dry_run'
    assert [c['xpath'] for c in _saved(tmp_path)[KEY]['candidates']] == [KEY]
    assert provider.bundles == []


def test_chains_and_positions_are_not_healed(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    call = heal.page.get_by_role('button', name='Sign in')
    form = heal.page.locator('#login-form')
    built = [call.first, call.nth(0), call.last, call.filter(has_text='Sign'),
             call.and_(heal.page.get_by_role('button')), call.or_(page.get_by_text('Help')),
             call.locator('xpath=.'), call.get_by_text('Sign in'),
             form.get_by_role('button', name='Sign in'),
             heal.page.frame_locator('#f').get_by_role('button', name='Sign in'),
             heal.page.native.get_by_role('button', name='Sign in')]
    for locator in built:
        assert type(locator).__name__ in ('FakeGetBy', 'FakeChain'), locator
    built[0].click()
    assert not (tmp_path / 'o.json').exists()
    guarded = _open(page, tmp_path, stub_provider(HEAL), dismiss=True)
    go = guarded.page.get_by_role('button', name='Sign in')
    assert [repr(x) for x in (go.first, go.nth(1), go.filter(has_text='S'))] == [
        "<GuardedLocator \"page.get_by_role('button', name='Sign in').first\">",
        "<GuardedLocator \"page.get_by_role('button', name='Sign in').nth(1)\">",
        "<GuardedLocator \"page.get_by_role('button', name='Sign in').filter(has_text='S')\">"]
    assert [repr(x) for x in go.all()] == [
        "<GuardedLocator \"page.get_by_role('button', name='Sign in').all()[0]\">"]


def test_calls_that_are_never_keys_say_so_once(tmp_path, make_page, stub_provider, caplog):
    page = _page(make_page, "<html><body><label><input type='checkbox' checked> Terms</label>"
                            '</body></html>')
    heal = _open(page, tmp_path, stub_provider(HEAL))
    with caplog.at_level(logging.INFO, logger='mendwright'):
        for _ in range(2):
            box = heal.page.get_by_role('checkbox', name='Terms', checked=True)
            assert type(box).__name__ == 'FakeGetBy'
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["mendwright: page.get_by_role('checkbox', name='Terms', checked=True) is "
                     'not healed: checked= picks the element by its state, which a saved selector '
                     'cannot keep. It goes to Playwright as written.']
    assert not (tmp_path / 'o.json').exists()


def test_reserved_keys_and_selectors(tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    call = 'get_by_role("button", name="Go")'
    for entry, run in (('heal.locator()', lambda: heal.locator(call, '#go')),
                       ('heal.locator_all()', lambda: heal.locator_all(call, '#go')),
                       ('heal.interrupt()', lambda: heal.interrupt(call, '#go'))):
        with pytest.raises(ValueError) as caught:
            run()
        assert str(caught.value) == (
            "mendwright: '" + call + "' is how mendwright names a heal.page.get_by_role() call, "
            'so ' + entry + ' cannot take it as a key. Pick another key.')
    for run in (lambda: heal.locator('go', call), lambda: heal.locator_all('go', call),
                lambda: heal.interrupt('go', call), lambda: heal.step('go', call, lambda p: None)):
        with pytest.raises(InvalidSelector) as caught:
            run()
        assert str(caught.value) == (
            "mendwright: the selector for 'go' is not usable: '" + call + "' is a get_by_* "
            'call, not a selector. Call heal.page.get_by_role(...) on mendwright.open(page, '
            'heal_get_by=True), or pass an XPath, CSS or Playwright selector.')
    assert type(heal.page.locator(call)).__name__ == 'FakeLocator'     # to the page as written
    assert page.trips == 0 and not (tmp_path / 'o.json').exists()


def test_is_visible_never_answers_from_a_heal(tmp_path, make_page, stub_provider):
    page, heal = _healed(make_page, tmp_path, stub_provider(HEAL))
    before = (tmp_path / 'o.json').read_bytes()
    assert heal.page.get_by_role('button', name='Sign in').is_visible() is False
    assert page.locator('xpath=' + HEAL).is_visible() is True    # the renamed button shows
    assert (tmp_path / 'o.json').read_bytes() == before


def test_present_follows_the_call_first(tmp_path, make_page, stub_provider):
    page, heal = _healed(make_page, tmp_path, stub_provider(HEAL))
    assert heal.present(KEY) is True                  # the saved heal, while the call misses
    page.set_html(PAGE)
    assert heal.present(KEY) is True
    page.set_html(PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in</button>'))
    assert heal.present(KEY) is False                 # several: no heal answers
    page.set_html('<html><body></body></html>')
    assert heal.present(KEY) is False


# --- with the rest of mendwright ------------------------------------------------------------

BANNER = "<div id='banner' role='dialog'><p>We use cookies</p><button id='reject'>Reject</button></div>"
REJECT = "//button[@id='reject']"


class Banner:
    """Makes a FakePage's banner go away when its Reject button is clicked: it is removed,
    and the rest of the page, hidden from the accessibility tree behind it, shows again."""

    def __init__(self, page):
        self.page = page
        real = page.locator

        def locator(selector):
            loc = real(selector)
            if selector == 'xpath=' + REJECT:
                act = loc.__getattr__('click')

                def click(*args, **kwargs):
                    out = act(*args, **kwargs)
                    self.close()
                    return out
                loc.click = click
            return loc
        page.locator = locator

    def close(self):
        doc = self.page._doc
        for el in doc.xpath("//*[@id='banner']"):
            el.getparent().remove(el)
        for el in doc.xpath('//*[@aria-hidden]'):
            del el.attrib['aria-hidden']

    def show(self, hide_page=True):
        main = self.page._doc.xpath('//main')[0]
        if hide_page:
            main.set('aria-hidden', 'true')
        import lxml.html
        main.addprevious(lxml.html.fragment_fromstring(BANNER))


def _with_banner(make_page, hide_page=True):
    page = _page(make_page)
    banner = Banner(page)
    banner.show(hide_page)
    return page, banner


def test_interruptions_are_handled_before_a_call_and_logged_before_its_key(
        tmp_path, make_page, stub_provider):
    page, _banner = _with_banner(make_page, hide_page=False)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    heal.interrupt('cookies', REJECT)
    _sign_in(heal)
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('cookies', KEY)]
    assert [a[1] for a in page.actions] == ['click', 'click']
    assert [e['before'] for e in _journal(tmp_path) if e['event'] == 'flow.interrupt'] == [KEY]


def test_a_banner_that_hides_the_page_is_handled_before_the_wait(tmp_path, make_page,
                                                                 stub_provider, clock):
    page, _banner = _with_banner(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=10000)
    heal.interrupt('cookies', REJECT)
    _sign_in(heal)
    assert clock['slept'] == []
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('cookies', KEY)]
    assert page.actions[-1][:2] == ("get_by_role('button', name='Sign in')", 'click')


def test_a_miss_handles_interruptions_again(tmp_path, make_page, stub_provider, clock):
    page, banner = _with_banner(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=1000)
    heal.interrupt('cookies', REJECT)
    real = page._get_by
    shown = []

    def comes_back(method, args, kwargs):
        if not shown and not page._doc.xpath("//*[@id='banner']"):
            shown.append(1)
            banner.show()            # it opens again once the first one closed
        return real(method, args, kwargs)

    page._get_by = comes_back
    _sign_in(heal)
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('cookies', KEY)] * 2
    assert page.actions[-1][:2] == ("get_by_role('button', name='Sign in')", 'click')


def test_dismiss_checks_a_calls_action_and_a_pass_through(tmp_path, make_page, stub_provider,
                                                         monkeypatch):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), dismiss=True)
    looked = []
    monkeypatch.setattr(heal, '_dismiss_before',
                        lambda key, native, verb, *rest: looked.append((key, verb)) or False)
    _sign_in(heal)
    page.set_html(PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in</button>'))
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal)
    assert looked == [(KEY, 'click'), (KEY, 'click')]


def _total_page(text, testid='total'):
    return ("<html><body><main><span id='total' data-testid='" + testid + "' class='amount'>"
            + text + '</span></main></body></html>')


def test_expect_values_check_a_read_through_a_call_heal(tmp_path, make_page, stub_provider):
    total = mendwright.get_by_key('get_by_test_id', 'total')
    page = _page(make_page, _total_page('$12.00'))
    heal = _open(page, tmp_path, stub_provider(HEAL), expect_values={total: 'money'})
    assert heal.page.get_by_test_id('total').inner_text() == '$12.00'
    page.set_html(_total_page('$12.00', testid='order-total'))
    assert heal.page.get_by_test_id('total').inner_text() == '$12.00'
    assert [h.confirmed for h in heal.heals] == [True]

    page.set_html(_total_page('$12.00'))
    other = _open(page, tmp_path / 'b', stub_provider(HEAL), expect_values={total: 'money'})
    assert other.page.get_by_test_id('total').inner_text() == '$12.00'
    page.set_html(_total_page('3 items', testid='order-total'))
    with pytest.raises(AttentionRequired) as caught:
        other.page.get_by_test_id('total').inner_text()
    assert not isinstance(caught.value, timeout_error)
    assert [h.confirmed for h in other.heals] == [False]


def test_a_failed_action_quarantines_the_heal_and_the_next_lookup_avoids_it(
        tmp_path, make_page, stub_provider):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL))
    _sign_in(heal)
    page.set_html(RENAMED)
    page.fail['click'] = RuntimeError('Locator.click: Element is not attached to the DOM')
    with pytest.raises(RuntimeError):
        _sign_in(heal)
    assert heal.store.status(KEY, HEAL) == 'quarantined'
    page.fail.clear()
    with pytest.raises(getby.GetByNotFound, match='previously quarantined'):
        _sign_in(heal)


def test_the_calls_own_element_reports_nothing_to_the_canary(tmp_path, make_page, stub_provider):
    page, heal = _healed(make_page, tmp_path, stub_provider(HEAL))
    assert heal.pending() == {KEY: HEAL}
    page.set_html(PAGE)
    page.fail['click'] = RuntimeError('Locator.click: Element is not attached to the DOM')
    with pytest.raises(RuntimeError):
        _sign_in(heal)
    assert heal.pending() == {KEY: HEAL} and heal.store.status(KEY, HEAL) == 'active'


def test_timeout_and_expect_kwargs(tmp_path, make_page, stub_provider, clock):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(HEAL), timeout_ms=7000)
    _sign_in(heal, timeout=5000)
    _sign_in(heal, timeout=0)
    assert clock['timeouts'] == [5000, 7000]
    assert [a[3] for a in page.actions] == [{'timeout': 5000}, {'timeout': 0}]
    checked = []
    page.set_html(PAGE.replace('Sign in</button>', 'Sign in</button><button>Sign in</button>'))
    page.fail['click'] = None
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal, expect=lambda: checked.append(1) or True)
    assert checked == []


def test_run_function_errors_and_interruption_loops_are_raised_as_they_are(
        tmp_path, make_page, stub_provider):
    page, _banner = _with_banner(make_page)

    def relogin(_page):
        raise AttentionRequired('the sign-in form was not there')

    heal = _open(page, tmp_path, stub_provider(HEAL))
    heal.interrupt('session', REJECT, run=relogin)
    with pytest.raises(AttentionRequired) as caught:
        _sign_in(heal)
    assert type(caught.value) is AttentionRequired and str(caught.value) == (
        'the sign-in form was not there')
    # A banner whose Reject button closes nothing: handled 3 times, then the step stops.
    stays = _page(make_page, PAGE.replace('<main>', BANNER + "<main aria-hidden='true'>"))
    looped = _open(stays, tmp_path / 'b', stub_provider(HEAL))
    looped.interrupt('cookies', REJECT)
    with pytest.raises(InterruptionLoop) as caught:
        _sign_in(looped)
    assert not isinstance(caught.value, timeout_error)


def test_an_optional_step_in_try_except_timeout_error_still_works(tmp_path, make_page,
                                                                   stub_provider, clock):
    heal = _open(_page(make_page), tmp_path, stub_provider(HEAL))
    try:
        heal.page.get_by_role('button', name='Maybe later').click(timeout=3000)
    except timeout_error:
        skipped = True
    assert skipped and clock['timeouts'] == [3000]


def test_an_action_proxy_comes_back_for_each_lookup(tmp_path, make_page, stub_provider):
    heal = _open(_page(make_page), tmp_path, stub_provider(HEAL))
    proxy = heal._get_by_locator(getby.parse(KEY), heal.page.native.get_by_role('button'))
    assert isinstance(proxy, ActionProxy) and proxy.key == KEY and proxy._canary is False


# --- with mendwright check: the address a call was found at ---------------------------------

def test_a_call_saves_its_address_and_one_handed_to_playwright_saves_none(make_page, tmp_path,
                                                                         stub_provider):
    page = make_page(PAGE, url='https://u:p@Shop.Test/Login/10482/?next=1#top', fingerprints=True)
    heal = _open(page, tmp_path, stub_provider(None))
    _sign_in(heal)
    assert _saved(tmp_path)[KEY]['lastUrl'] == 'https://shop.test/Login/*/'
    page.set_html(RENAMED)
    page.url = 'https://shop.test:8443/login/10482/'          # a test app on another port
    _sign_in(heal)                                            # healed, and found there
    assert heal.heals and heal.store.last_url(KEY) == 'https://shop.test:8443/login/*/'
    # Two Sign in buttons: Playwright runs the call as written, and nothing is saved.
    page.set_html(PAGE.replace('</form>', "<button type='button'>Sign in</button></form>"))
    page.url = 'https://shop.test:9000/login/10482/'
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal)
    assert heal.store.last_url(KEY) == 'https://shop.test:8443/login/*/'
