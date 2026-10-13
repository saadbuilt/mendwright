"""The driver surface on real browsers: selector kinds, action verbs and their keyword
arguments, the iframe workaround, and what mendwright refuses (async objects, Playwright
selectors on Selenium). All through the public facade with a stub provider.

Tests marked engine_family run on one family only; see conftest.py.
"""

import time

import pytest

import mendwright
from mendwright import InvalidSelector
from mw_engines import (ACTIONS_PAGE, FAST_MS, FORM_CONTROLS, HEALED, IFRAME_PAGE, POLL_MS,
                        SAVE_PAGE, SIGNUP_V1, V2_CHECKBOX, journal_events, page,
                        saved_candidates)

pytestmark = pytest.mark.browser



def _open(target, provider, store_file, **kwargs):
    return mendwright.open(target, provider=provider, store_path=store_file, **kwargs)


@pytest.mark.parametrize('selector', ['#email', 'css=#email', "input[name='email']"])
def test_css_selectors_resolve_without_a_model(selector, tab, store_file, stub_provider):
    tab.load(SIGNUP_V1)
    provider = stub_provider(None)
    heal = _open(tab.target, provider, store_file)

    heal.locator('email', selector, role='input').fill('ada@example.test')

    assert tab.value('#email') == 'ada@example.test'
    assert provider.bundles == []


def test_fill_check_uncheck_and_select_option_through_the_proxy(tab, store_file, stub_provider):
    """Playwright verbs work the same on every engine. On Selenium the driver maps them
    to WebElement calls."""
    tab.load(FORM_CONTROLS)
    heal = _open(tab.target, stub_provider(None), store_file)
    name = heal.locator('name', "//input[@id='name']", role='input')
    news = heal.locator('news', "//input[@id='news']", role='checkbox')
    color = heal.locator('color', "//select[@id='color']")

    name.fill('Ada')
    assert tab.value('#name') == 'Ada'          # fill() replaces the old value
    news.check()
    news.check()                                 # checking twice keeps it checked
    assert tab.checked('#news') is True
    news.uncheck()
    assert tab.checked('#news') is False
    color.select_option('blue')
    assert tab.value('#color') == 'blue'
    color.select_option(label='Red')
    assert tab.value('#color') == 'red'


def test_the_iframe_workaround(tab, store_file, stub_provider):
    """Healing inside an iframe works when mendwright is opened on the frame: a Frame from
    page.frames on Playwright, or the driver after switch_to.frame on Selenium."""
    tab.load(IFRAME_PAGE)
    target = tab.enter_frame('#consent-frame')
    provider = stub_provider(HEALED)
    heal = _open(target, provider, store_file)

    proxy = heal.locator('frame_terms', "//input[@id='inner-terms']", role='checkbox',
                         timeout_ms=FAST_MS, poll_ms=POLL_MS)
    proxy.check(expect=lambda: tab.checked(V2_CHECKBOX))

    assert tab.checked(V2_CHECKBOX) is True
    assert len(provider.bundles) == 1
    assert 'legal-consent' in provider.bundles[0]['html']      # the frame's HTML was captured
    assert 'Outer page' not in provider.bundles[0]['html']
    assert provider.outcomes == [('frame_terms', HEALED, True)]


IN_FRAME = "//input[@data-testid='legal-consent']"
FRAME = 'iframe[id="consent-frame"]'


def _frame_checked(tab):
    return tab.js('document.querySelector("#consent-frame").contentDocument'
                  '.querySelector("[name=agreement]").checked')


def test_a_selector_that_matches_only_inside_an_iframe_is_followed_there(tab, store_file,
                                                                          stub_provider):
    """mendwright is opened on the page, and the key's selector matches only inside the
    iframe: it is looked up there from now on, with no model call."""
    tab.load(IFRAME_PAGE)
    provider = stub_provider(None)
    heal = _open(tab.target, provider, store_file)
    heal.locator('frame_terms', IN_FRAME, role='checkbox', timeout_ms=FAST_MS,
                 poll_ms=POLL_MS).check()

    assert _frame_checked(tab) is True
    assert provider.bundles == []
    assert [h.source for h in heal.heals] == ['frame']
    assert saved_candidates(store_file, 'frame_terms')['xpath=' + IN_FRAME]['frameXPath'] == FRAME
    applied = [e for e in journal_events(store_file) if e['event'] == 'heal.applied']
    assert applied[0]['source'] == 'frame' and applied[0]['frame'] == FRAME
    assert tab.js('window.frameElement === null') is True        # back on the page

    again = stub_provider(None)
    rerun = _open(tab.target, again, store_file)
    rerun.locator('frame_terms', IN_FRAME, role='checkbox', timeout_ms=FAST_MS,
                  poll_ms=POLL_MS).uncheck()
    assert _frame_checked(tab) is False
    assert rerun.heals == [] and again.bundles == []


def test_a_destructive_key_is_not_followed_into_an_iframe(tab, store_file, stub_provider):
    tab.load(IFRAME_PAGE)
    heal = _open(tab.target, stub_provider(None), store_file)
    with pytest.raises(mendwright.AttentionRequired) as info:
        heal.locator('frame_terms', IN_FRAME, role='checkbox', destructive=True,
                     timeout_ms=FAST_MS, poll_ms=POLL_MS)
    assert 'is marked as a destructive control' in str(info.value)
    assert heal.heals == []


@pytest.mark.parametrize('fingerprint', [False, True])
def test_a_key_last_found_in_an_iframe_heals_inside_it(fingerprint, tab, store_file,
                                                        stub_provider):
    """After a redesign inside the iframe, the heal captures and checks the iframe's
    document, and saves the new selector there: from the model, or from the fingerprint
    recorded inside the iframe with no model call."""
    tab.load(IFRAME_PAGE)
    first = _open(tab.target, stub_provider(None), store_file, fingerprint=fingerprint)
    first.locator('frame_terms', IN_FRAME, role='checkbox', timeout_ms=FAST_MS,
                  poll_ms=POLL_MS).check(expect=lambda: _frame_checked(tab))
    # Once the move into the iframe is confirmed, the next match records the fingerprint
    # of the element inside the iframe.
    first.locator('frame_terms', IN_FRAME, role='checkbox', timeout_ms=FAST_MS,
                  poll_ms=POLL_MS)

    tab.load(IFRAME_PAGE.replace('legal-consent', 'consent-box'))   # the redesign
    provider = stub_provider("//input[@name='agreement']")
    heal = _open(tab.target, provider, store_file, fingerprint=fingerprint)
    heal.locator('frame_terms', IN_FRAME, role='checkbox', timeout_ms=FAST_MS,
                 poll_ms=POLL_MS).check()

    assert _frame_checked(tab) is True
    [event] = heal.heals
    assert event.source == ('fingerprint' if fingerprint else 'model')
    saved = saved_candidates(store_file, 'frame_terms')[event.new]
    assert saved['frameXPath'] == FRAME
    if fingerprint:
        assert provider.bundles == []
    else:
        assert 'consent-box' in provider.bundles[0]['html']        # the iframe's HTML
        assert 'Outer page' not in provider.bundles[0]['html']
    assert tab.js('window.frameElement === null') is True


def _srcdoc(html):
    return html.replace('&', '&amp;').replace('"', '&quot;')


@pytest.mark.engine_family('selenium')
def test_a_healer_opened_inside_an_iframe_stays_there(tab, store_file, stub_provider):
    """The iframe workaround on Selenium, with an iframe inside that iframe: a miss does
    not look into the inner iframe (frame candidates run from the top document), and the
    driver stays switched into the outer iframe."""
    inner = page("<label><input type='checkbox' id='deep'> ok</label>", 'inner')
    outer = page('<p>outer</p><iframe id="inner" srcdoc="' + _srcdoc(inner) + '"></iframe>', 'outer')
    tab.load(page('<h1>Top</h1><iframe id="outer" srcdoc="' + _srcdoc(outer) + '"></iframe>'))
    target = tab.enter_frame('#outer')
    deadline = time.monotonic() + 10
    while not tab.js("!!(document.querySelector('#inner') && document.querySelector('#inner')"
                     ".contentDocument && document.querySelector('#inner').contentDocument"
                     ".querySelector('#deep'))"):
        assert time.monotonic() < deadline, 'the inner iframe did not load'
        time.sleep(0.1)
    heal = _open(target, stub_provider(None), store_file)
    with pytest.raises(mendwright.AttentionRequired):
        heal.locator('deep', '#deep', role='checkbox', timeout_ms=FAST_MS, poll_ms=POLL_MS)
    assert heal.heals == []
    assert tab.js('window.self !== window.top') is True            # still in the outer iframe
    assert tab.js("document.querySelector('#inner') !== null") is True


def test_a_stored_frame_candidate_acts_inside_the_frame_and_returns(tab, store_file,
                                                                  stub_provider):
    """A candidate stored with a frame selector (frameXPath). On Selenium the driver
    switches into the frame for each call and always switches back to the page."""
    tab.load(IFRAME_PAGE)
    heal = _open(tab.target, stub_provider(None), store_file)
    heal.store.set_meta('frame_terms', role='checkbox')
    heal.store.add_candidate('frame_terms', "//input[@data-testid='legal-consent']",
                             source='code', frame_xpath="//iframe[@id='consent-frame']")

    proxy = heal.locator('frame_terms')
    proxy.check()

    assert tab.js('document.querySelector("#consent-frame").contentDocument'
                  '.querySelector("[data-testid=legal-consent]").checked') is True
    read = 'is_checked' if tab.family == 'playwright' else 'is_selected'
    assert getattr(proxy, read)() is True          # reads go to the native object
    assert tab.js('window.frameElement === null') is True        # back on the page


@pytest.mark.engine_family('playwright')
@pytest.mark.parametrize('selector', ["role=button[name='Save']", 'text=Save',
                                      '#editor >> text=Save'])
def test_playwright_engine_selectors(selector, tab, store_file, stub_provider):
    tab.load(SAVE_PAGE)
    provider = stub_provider(None)
    heal = _open(tab.target, provider, store_file)

    heal.locator('save', selector).click()

    assert tab.text('#out') == 'saved'
    assert provider.bundles == []
    assert heal.store.meta('save')['expectedRole'] == 'button'   # read from the page


@pytest.mark.engine_family('selenium')
def test_a_playwright_selector_on_selenium_fails_at_once(tab, store_file, stub_provider):
    tab.load(SAVE_PAGE)
    provider = stub_provider("//button[@id='save-btn']")
    heal = _open(tab.target, provider, store_file)

    started = time.monotonic()
    with pytest.raises(InvalidSelector) as info:
        heal.locator('save', "role=button[name='Save']", timeout_ms=10000)

    assert time.monotonic() - started < 5            # not after the 10 s timeout
    assert 'is a Playwright selector, and Selenium cannot run it' in str(info.value)
    assert provider.bundles == []          # not a broken selector, so no model call


@pytest.mark.engine_family('playwright')
def test_keyword_arguments_pass_through_to_playwright(tab, store_file, stub_provider):
    tab.load(ACTIONS_PAGE)
    heal = _open(tab.target, stub_provider(None), store_file)

    heal.locator('go', '#go', role='button').click(timeout=2000, delay=5)
    assert tab.text('#out') == 'clicked'
    heal.locator('name', '#name', role='input').fill('Ada', timeout=1000)
    assert tab.value('#name') == 'Ada'
    heal.locator('news', '#news', role='checkbox').check(force=True, timeout=1000)
    assert tab.checked('#news') is True
    heal.locator('color', '#color').select_option(value='blue', timeout=1000)
    assert tab.value('#color') == 'blue'
    search = heal.locator('search', '#search', role='input')
    search.fill('q')
    search.press('Enter', delay=5, timeout=1000)
    assert tab.text('#out2') == 'enter:q'
    with pytest.raises(TypeError):
        heal.locator('go', '#go', role='button').click(not_an_option=True)


@pytest.mark.engine_family('selenium')
def test_playwright_options_are_accepted_and_ignored_on_selenium(tab, store_file,
                                                                stub_provider):
    """Code written for Playwright passes timeout=, force= and delay=. On Selenium the
    mapped verbs accept them and ignore them."""
    tab.load(ACTIONS_PAGE)
    heal = _open(tab.target, stub_provider(None), store_file)

    heal.locator('name', '#name', role='input').fill('Ada', timeout=1000)
    assert tab.value('#name') == 'Ada'
    heal.locator('news', '#news', role='checkbox').check(force=True, timeout=1000)
    assert tab.checked('#news') is True
    heal.locator('color', '#color').select_option(value='blue', timeout=1000)
    assert tab.value('#color') == 'blue'
    search = heal.locator('search', '#search', role='input')
    search.fill('q')
    search.press('Enter', delay=5, timeout=1000)
    assert tab.text('#out2') == 'enter:q'
    heal.locator('go', '#go', role='button').dblclick(timeout=1000)
    assert tab.text('#out') == 'clicked'


@pytest.mark.engine_family('selenium')
def test_click_accepts_a_playwright_timeout_on_selenium(tab, store_file, stub_provider):
    tab.load(ACTIONS_PAGE)
    heal = _open(tab.target, stub_provider(None), store_file)

    heal.locator('go', '#go', role='button').click(timeout=2000)

    assert tab.text('#out') == 'clicked'


@pytest.mark.engine_family('playwright')
def test_a_locator_is_rejected_with_a_hint(tab, store_file, stub_provider):
    tab.load(SIGNUP_V1)
    with pytest.raises(TypeError) as info:
        mendwright.open(tab.page.locator('form'), provider=stub_provider(None),
                        store_path=store_file)
    assert 'locator.page' in str(info.value)


@pytest.mark.engine_family('playwright')
def test_async_objects_are_sent_to_mendwright_aio(engine, tmp_path, stub_provider):
    store = str(tmp_path / 'store' / 'overrides.json')

    def try_open(async_page):
        found = {}
        for label, obj in (('Page', async_page), ('Frame', async_page.main_frame),
                           ('Locator', async_page.locator('button'))):
            try:
                mendwright.open(obj, provider=stub_provider(None), store_path=store)
            except TypeError as exc:
                found[label] = str(exc)
            else:
                found[label] = None
        return found

    messages = engine.with_async_page(try_open)

    for label, message in messages.items():
        assert message is not None, label + ' was accepted'
        assert 'mendwright.open() takes the sync Playwright API' in message, message
        assert 'mendwright.aio.open' in message, message
