"""heal.page.get_by_*() with mendwright.open(page, heal_get_by='model'), offline: the lxml
FakePage of conftest and a StubProvider that stands in for the model. A model is asked for a
call only in that mode, only after the fingerprint did not heal it, and every check of a
heal still runs. No browser, network or model."""

import json

import pytest

import mendwright
from mendwright import AttentionRequired, facade, getby

# get_by_* calls are Playwright's API, and the error a call that stays broken raises,
# getby.GetByNotFound, is also Playwright's TimeoutError: CI's offline job has no Playwright.
pytest.importorskip('playwright.sync_api')

KEY = 'get_by_role("button", name="Sign in")'
HEAL = "//button[@id='login']"
PAGE = ("<html><body><main><form id='login-form'><label for='email'>Email</label>"
        "<input id='email' name='email' type='email'>"
        "<button id='login' type='submit' class='primary'>Sign in</button></form>"
        "<a href='/help'>Help</a></main></body></html>")
RENAMED = PAGE.replace('>Sign in<', '>Log in<')          # the fingerprint finds it
# The id and the words changed: the fingerprint has nothing strong to go on.
REDONE = PAGE.replace("<button id='login' type='submit' class='primary'>Sign in</button>",
                      "<button id='enter' type='submit'>Continue</button>"
                      "<button id='cancel' type='button'>Cancel</button>")
MODEL_HEAL = "//button[@id='enter']"
URL = 'https://shop.test/login'


def _page(make_page, html=PAGE):
    return make_page(html, url=URL, fingerprints=True)


def _open(page, tmp_path, provider, **kwargs):
    kwargs.setdefault('heal_get_by', 'model')
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'), **kwargs)


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _saved(tmp_path):
    path = tmp_path / 'o.json'
    return json.loads(path.read_text(encoding='utf-8'))['selectors'] if path.exists() else {}


def _sign_in(heal):
    return heal.page.get_by_role('button', name='Sign in').click()


def _matched_then(make_page, tmp_path, html, **kwargs):
    """A page whose Sign in button matched once with a healer opened with kwargs, then
    changed to html."""
    page = _page(make_page)
    _sign_in(_open(page, tmp_path, None, **kwargs))
    page.set_html(html)
    return page


# --- the option -------------------------------------------------------------------------------

def test_model_is_an_option_and_true_stays_fingerprint_only(tmp_path, make_page, stub_provider,
                                                            monkeypatch):
    page = _page(make_page)
    heal = _open(page, tmp_path, stub_provider(MODEL_HEAL))
    assert heal._heal_get_by == 'model' and heal.coordinator.get_by_model is True
    heal = _open(page, tmp_path, stub_provider(MODEL_HEAL), heal_get_by=True)
    assert heal._heal_get_by is True and heal.coordinator.get_by_model is False
    monkeypatch.setenv('MENDWRIGHT_HEAL_GET_BY', ' MODEL ')
    heal = _open(page, tmp_path, stub_provider(MODEL_HEAL), heal_get_by=None)
    assert heal._heal_get_by == 'model' and heal.coordinator.get_by_model is True
    monkeypatch.setenv('MENDWRIGHT_HEAL_GET_BY', '1')
    assert _open(page, tmp_path, None, heal_get_by=None).coordinator.get_by_model is False
    with pytest.raises(TypeError, match="heal_get_by='model'"):
        _open(page, tmp_path, None, heal_get_by='Model')


def test_with_true_a_model_is_never_asked(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE, heal_get_by=True)
    provider = stub_provider(MODEL_HEAL)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider, heal_get_by=True))
    assert str(caught.value).endswith(getby.NO_MODEL)
    assert provider.bundles == []


# --- what a model is asked, and when -------------------------------------------------------

def test_a_confident_fingerprint_heals_and_no_model_is_asked(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, RENAMED)
    provider = stub_provider(MODEL_HEAL)
    heal = _open(page, tmp_path, provider)
    _sign_in(heal)
    assert [(h.new, h.source) for h in heal.heals] == [(HEAL, 'fingerprint')]
    assert provider.bundles == []


def test_the_model_heals_a_call_the_fingerprint_does_not(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE)
    provider = stub_provider(MODEL_HEAL)
    heal = _open(page, tmp_path, provider)
    _sign_in(heal)
    assert page.actions[-1][:2] == ('xpath=' + MODEL_HEAL, 'click')
    assert [(h.key, h.new, h.source) for h in heal.heals] == [(KEY, MODEL_HEAL, 'model')]
    bundle, = provider.bundles
    assert bundle['key'] == KEY and bundle['candidates'] == [KEY]
    assert bundle['expectedRole'] == 'button'
    assert bundle['intent'] == 'the button named "Sign in"'
    assert 'fingerprint' not in bundle                 # only a local model gets it
    # The intent is for this heal only: never saved with the key.
    assert 'intent' not in _saved(tmp_path)[KEY]
    applied = [e for e in _journal(tmp_path) if e['event'] == 'heal.applied']
    assert applied[-1]['provider'] == 'StubProvider' and 'source' not in applied[-1]


def test_a_call_that_never_matched_is_looked_for_from_its_role_and_name(tmp_path, make_page,
                                                                       stub_provider):
    page = _page(make_page)
    provider = stub_provider(HEAL)
    heal = _open(page, tmp_path, provider)
    heal.page.get_by_role('button', name='Sing in').click()         # a typo: never matched
    key = mendwright.get_by_key('get_by_role', 'button', name='Sing in')
    assert [(h.key, h.new, h.source) for h in heal.heals] == [(key, HEAL, 'model')]
    bundle, = provider.bundles
    assert bundle['expectedRole'] == 'button' and bundle['intent'] == 'the button named "Sing in"'
    assert _saved(tmp_path)[key]['expectedRole'] == 'button'


@pytest.mark.parametrize('call, args, kwargs', [
    ('get_by_label', ('E-mail',), {}),
    ('get_by_text', ('Sign on',), {}),
    ('get_by_test_id', ('login-button',), {}),
    ('get_by_role', ('tab',), {'name': 'Sign in'}),
    ('get_by_role', ('menuitem',), {'name': 'Sign in'}),
], ids=['label', 'text', 'test id', 'tab', 'menuitem'])
def test_a_call_that_never_matched_and_names_no_kind_of_element_is_not_asked_about(
        tmp_path, make_page, stub_provider, call, args, kwargs):
    provider = stub_provider(HEAL)
    heal = _open(_page(make_page), tmp_path, provider)
    with pytest.raises(getby.GetByNotFound) as caught:
        getattr(heal.page, call)(*args, **kwargs).click()
    key = mendwright.get_by_key(call, *args, **kwargs)
    assert str(caught.value).endswith(
        getby.role_unknown_model_message(key, getby.parse(key)))
    assert _journal(tmp_path)[-1]['code'] == 'role_unknown'
    assert provider.bundles == [] and heal.heals == []


def test_a_heading_call_gets_the_level_it_names(tmp_path, make_page, stub_provider):
    page = _page(make_page, "<html><body><h1>Shop</h1><h2 id='basket'>Basket</h2></body></html>")
    provider = stub_provider("//h2[@id='basket']")
    heal = _open(page, tmp_path, provider)
    assert heal.page.get_by_role('heading', name='Cart', level=2).inner_text() == 'Basket'
    assert provider.bundles[0]['expectedRole'] == 'h2'


@pytest.mark.parametrize('html, role, heal_xpath, read', [
    ("<html><body><h1>Shop</h1><main><h2 id='cart'>Shopping cart</h2><p>2 items</p></main>"
     "</body></html>", ('heading',), "//h2[@id='cart']", 'Shopping cart'),
    ("<html><body><form><label for='c'>Your comments</label><textarea id='c' name='comments'>"
     "Great</textarea><button>Send</button></form></body></html>", ('textbox',),
     "//textarea[@id='c']", None),
    ("<html><body><h1>Steps</h1><ol id='steps'><li>One</li><li>Two</li></ol></body></html>",
     ('list',), "//ol[@id='steps']", 'One'),
], ids=['heading with no level onto an h2', 'textbox onto a textarea', 'list onto an ol'])
def test_a_call_that_never_matched_heals_onto_any_kind_its_role_can_mean(
        tmp_path, make_page, stub_provider, html, role, heal_xpath, read):
    # A heading with no level= is any of h1 to h6: the element the model picks says which.
    page = _page(make_page, html)
    provider = stub_provider(heal_xpath)
    heal = _open(page, tmp_path, provider)
    found = heal.page.get_by_role(*role, name='Your cart')
    if read is None:
        found.fill('hi')
    else:
        assert read in found.inner_text()
    key = mendwright.get_by_key('get_by_role', *role, name='Your cart')
    assert [(h.key, h.new) for h in heal.heals] == [(key, heal_xpath)]
    assert len(provider.bundles) == 1           # no re-prompt
    assert not [e for e in _journal(tmp_path) if e['event'] == 'heal.declined']


# --- every check of a heal still runs ---------------------------------------------------------

def test_a_destructive_call_key_is_never_sent_to_a_model(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE)
    provider = stub_provider(MODEL_HEAL)
    with pytest.raises(AttentionRequired, match='destructive'):
        _sign_in(_open(page, tmp_path, provider, destructive_keys={KEY}))
    assert provider.bundles == []
    assert _journal(tmp_path)[-1]['code'] == 'destructive'


def test_the_page_guard_stops_a_model_heal(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE)
    page.url = 'https://shop.test/checkout'
    provider = stub_provider(MODEL_HEAL)
    with pytest.raises(getby.GetByNotFound, match='was last found on /login'):
        _sign_in(_open(page, tmp_path, provider))
    assert provider.bundles == [] and _journal(tmp_path)[-1]['code'] == 'page_guard'


def test_disable_and_dry_run(tmp_path, make_page, stub_provider, monkeypatch):
    page = _matched_then(make_page, tmp_path, REDONE)
    provider = stub_provider(MODEL_HEAL)
    with pytest.raises(getby.GetByNotFound, match='dry-run mode is on'):
        _sign_in(_open(page, tmp_path, provider, dry_run=True))
    assert len(provider.bundles) == 1
    assert [c['xpath'] for c in _saved(tmp_path)[KEY]['candidates']] == [KEY]
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, provider))
    assert facade.DISABLED_REASON in str(caught.value)
    assert len(provider.bundles) == 1 and _journal(tmp_path)[-1]['code'] == 'disabled'


def test_no_provider_says_so(tmp_path, make_page):
    page = _matched_then(make_page, tmp_path, REDONE)
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(_open(page, tmp_path, None))
    assert "the fingerprint match for '" + KEY + "' was not confident" in str(caught.value)
    assert 'no model provider is configured to try instead' in str(caught.value)
    assert _journal(tmp_path)[-1]['code'] == 'no_provider'


def test_model_heals_of_calls_count_toward_the_rate_limit(tmp_path, make_page, stub_provider):
    page = _page(make_page, PAGE.replace('</main>', "<button id='go'>Go</button></main>"))
    heal = _open(page, tmp_path, None, rate_global_per_hour=1)
    _sign_in(heal)
    heal.page.get_by_role('button', name='Go').click()
    page.set_html(REDONE.replace('</main>', "<button id='onward'>Onward</button></main>"))
    provider = stub_provider(MODEL_HEAL)
    heal = _open(page, tmp_path, provider, rate_global_per_hour=1)
    _sign_in(heal)                                          # the one model heal this hour
    provider.xpath = "//button[@id='onward']"
    with pytest.raises(getby.GetByNotFound, match='rate/spend cap'):
        heal.page.get_by_role('button', name='Go').click()
    assert len(provider.bundles) == 1 and _journal(tmp_path)[-1]['code'] == 'rate_limit'


def test_a_fingerprint_heal_of_a_call_does_not_count_toward_it(tmp_path, make_page,
                                                               stub_provider):
    page = _matched_then(make_page, tmp_path, RENAMED)
    heal = _open(page, tmp_path, stub_provider(MODEL_HEAL), rate_global_per_hour=0)
    _sign_in(heal)
    assert [h.source for h in heal.heals] == ['fingerprint']


@pytest.mark.parametrize('before, after, call, kwargs, proposal', [
    ("<button role='tab' id='settings'>Settings</button>",
     "<button id='prefs'>Preferences</button>", ('tab',), {'name': 'Settings'},
     "//button[@id='prefs']"),
    ("<a id='help' href='/help'>Help</a>", "<a id='support'>Support</a>",
     ('link',), {'name': 'Help'}, "//a[@id='support']"),
], ids=['no longer a tab', 'a link without href'])
def test_a_model_pick_must_have_the_calls_aria_role(tmp_path, make_page, stub_provider,
                                                     before, after, call, kwargs, proposal):
    page = _page(make_page, '<html><body><main>' + before + '</main></body></html>')
    _open(page, tmp_path, None).page.get_by_role(*call, **kwargs).click()
    page.set_html('<html><body><main>' + after + '</main></body></html>')
    provider = stub_provider(proposal)
    heal = _open(page, tmp_path, provider)
    with pytest.raises(getby.GetByNotFound) as caught:
        heal.page.get_by_role(*call, **kwargs).click()
    mismatch = getby.role_mismatch(getby.call_of('get_by_role', call, kwargs)[0])
    assert mismatch in str(caught.value)
    assert len(provider.bundles) == 2                      # a proposal and one re-prompt
    assert provider.bundles[1]['rejectReason'] == mismatch
    assert heal.heals == [] and _journal(tmp_path)[-1]['code'] == 'gate'


def test_a_driver_that_cannot_check_the_aria_role_refuses_a_model_pick(tmp_path, make_page,
                                                                        stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE)
    heal = _open(page, tmp_path, stub_provider(MODEL_HEAL))
    heal.coordinator.driver.role_match = lambda *a: None
    with pytest.raises(getby.GetByNotFound) as caught:
        _sign_in(heal)
    assert getby.ROLE_UNREADABLE in str(caught.value) and heal.heals == []


def test_a_quarantined_model_heal_is_not_taken_again(tmp_path, make_page, stub_provider):
    page = _matched_then(make_page, tmp_path, REDONE)
    provider = stub_provider(MODEL_HEAL)
    heal = _open(page, tmp_path, provider)
    page.fail['click'] = RuntimeError('Locator.click: Element is not attached to the DOM')
    with pytest.raises(RuntimeError):
        _sign_in(heal)
    assert heal.store.status(KEY, MODEL_HEAL) == 'quarantined'
    page.fail.clear()
    provider.xpath = "(//button[@id='enter'])[1]"           # the same element, spelt again
    with pytest.raises(getby.GetByNotFound, match='previously quarantined'):
        _sign_in(heal)
    assert page.actions[-1][0] != 'xpath=' + provider.xpath


def test_several_matches_and_one_hidden_match_go_to_playwright_unasked(tmp_path, make_page,
                                                                       stub_provider):
    page = _matched_then(make_page, tmp_path, PAGE.replace(
        'Sign in</button>', "Sign in</button><button id='later'>Sign in later</button>"))
    provider = stub_provider(MODEL_HEAL)
    heal = _open(page, tmp_path, provider)
    with pytest.raises(Exception, match='strict mode violation'):
        _sign_in(heal)
    page.set_html(PAGE.replace("<button id='login'", "<button data-invisible id='login'"))
    with pytest.raises(Exception, match='Timeout 30000ms exceeded'):
        _sign_in(heal)
    assert provider.bundles == [] and heal.heals == []


def test_an_intent_from_a_seed_file_wins_over_the_calls_words(tmp_path, make_page,
                                                              stub_provider):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {KEY: {
        'expectedRole': 'button', 'intent': 'the button that sends the login form',
        'candidates': []}}}), encoding='utf-8')
    page = _matched_then(make_page, tmp_path, REDONE)
    provider = stub_provider(MODEL_HEAL)
    store = mendwright.load_store(str(seed), str(tmp_path / 'o.json'))
    _sign_in(mendwright.open(page, provider=provider, store=store, heal_get_by='model',
                             timeout_ms=0))
    assert provider.bundles[0]['intent'] == 'the button that sends the login form'
