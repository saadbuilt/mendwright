"""Flow changes on real browsers: interruptions, the cookies= preset, optional steps and
the page guard, through the public facade with a stub provider (no model is called).

Each test runs on every engine (see conftest.py): Playwright Chromium, Firefox and WebKit,
patchright, camoufox, and Selenium with Chrome and with Firefox.

The signup story is the one from the issue that asked for this: a script that clicks a
cookie banner after signing up, on a site where the banner now opens first and covers
the form. The script registers the banner once with heal.interrupt() and keeps its old
step. The same script runs on the page where the banner comes after, and where it never
comes.
"""

import json
import time

import pytest

import mendwright
from mendwright import AttentionRequired
from mw_engines import FAST_MS, IFRAME_PAGE, POLL_MS, journal_events, page, saved_entry

pytestmark = pytest.mark.browser

_BANNER = """<div id="cookie-banner" role="dialog" aria-label="Cookie consent"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4)">
  <div style="position: absolute; left: 0; right: 0; bottom: 0; padding: 16px; background: #fff">
    <p>We use cookies to keep you signed in.</p>
    <button id="cookie-accept" type="button" onclick="consent('accepted')">Accept all</button>
  </div>
</div>"""

_SCRIPT = """<script>
function consent(choice) {
  document.getElementById('consent').textContent = choice;
  document.getElementById('cookie-banner').remove();
}
function showBanner() {
  document.body.insertAdjacentHTML('beforeend', %s);
}
</script>""" % json.dumps(_BANNER)


def _signup(banner):
    """The signup page. banner is 'before' (it opens with the page and covers the form),
    'after' (it opens when the account is created) or 'never'."""
    on_create = "document.getElementById('done').textContent = 'created';"
    if banner == 'after':
        on_create += ' showBanner();'
    body = ('<p>Consent: <span id="consent">none</span></p>'
            '<form id="signup"><label for="email">Email</label>'
            '<input id="email" name="email" type="email">'
            '<button id="create" type="button" onclick="' + on_create + '">Create account</button>'
            '</form><p id="done"></p>' + _SCRIPT)
    if banner == 'before':
        body += _BANNER
    return page(body, 'signup')


def _open(tab, provider, store_file, **kwargs):
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def _step(heal, key, selector, **kwargs):
    return heal.locator(key, selector, timeout_ms=FAST_MS, poll_ms=POLL_MS, **kwargs)


def _the_script(heal):
    """The user's script: the banner registered once, the old explicit step kept."""
    heal.interrupt('cookies', '#cookie-accept')
    _step(heal, 'email', '#email').fill('ada@example.com')
    _step(heal, 'create', '#create').click()
    return _step(heal, 'cookies', '#cookie-accept').click()


def _flow_events(store_file):
    return [(e['event'], e['key'], e.get('before') or e.get('reason'))
            for e in journal_events(store_file) if e['event'].startswith('flow.')]


def test_a_banner_that_now_opens_first_is_handled_and_the_old_step_skipped(tab, store_file,
                                                                            stub_provider):
    tab.load(_signup('before'))
    provider = stub_provider("//button[@id='create']")
    heal = _open(tab, provider, store_file)

    assert _the_script(heal) is None

    assert tab.value('#email') == 'ada@example.com'
    assert tab.text('#done') == 'created'
    assert tab.text('#consent') == 'accepted'
    assert tab.js("document.getElementById('cookie-banner') === null") is True
    assert _flow_events(store_file) == [('flow.interrupt', 'cookies', 'email'),
                                        ('flow.skip', 'cookies', 'interruption handled')]
    assert provider.bundles == []
    assert heal.heals == []


def test_the_same_script_when_the_banner_comes_after(tab, store_file, stub_provider):
    tab.load(_signup('after'))
    provider = stub_provider("//button[@id='create']")
    heal = _open(tab, provider, store_file)

    _the_script(heal)

    assert tab.value('#email') == 'ada@example.com'
    assert tab.text('#done') == 'created'
    assert tab.text('#consent') == 'accepted'            # clicked by the old step itself
    assert _flow_events(store_file) == []
    assert heal.handled_interruptions == [] and provider.bundles == []


def test_the_same_script_when_the_banner_never_comes(tab, store_file, stub_provider):
    tab.load(_signup('never'))
    provider = stub_provider("//button[@id='create']")
    heal = _open(tab, provider, store_file)

    assert _the_script(heal) is None

    assert tab.text('#done') == 'created'
    assert tab.text('#consent') == 'none'
    assert _flow_events(store_file) == [('flow.skip', 'cookies', 'interruption absent')]
    assert provider.bundles == []


_ONETRUST = page("""
<p>Consent: <span id="consent">none</span></p>
<form><label for="email">Email</label><input id="email" type="email"></form>
<div id="onetrust-consent-sdk">
  <div class="onetrust-pc-dark-filter" style="position: fixed; inset: 0; z-index: 9;
    background: rgba(0, 0, 0, 0.5)"></div>
  <div id="onetrust-banner-sdk" class="otFlat" role="region" aria-label="Cookie banner"
    style="position: fixed; left: 0; right: 0; bottom: 0; z-index: 10; background: #fff;
    padding: 16px">
    <div id="onetrust-policy-text">We use cookies to improve your experience.</div>
    <div id="onetrust-button-group">
      <button id="onetrust-pc-btn-handler">Cookie Settings</button>
      <button id="onetrust-reject-all-handler" onclick="choose('rejected')">Reject All</button>
      <button id="onetrust-accept-btn-handler" onclick="choose('accepted')">Accept All Cookies</button>
    </div>
  </div>
</div>
<script>
function choose(choice) {
  document.getElementById('consent').textContent = choice;
  document.getElementById('onetrust-consent-sdk').remove();
}
</script>""", 'onetrust')


@pytest.mark.parametrize('choice, outcome', [('reject', 'rejected'), ('accept', 'accepted')])
def test_the_cookies_preset_on_a_onetrust_banner(tab, store_file, stub_provider, choice, outcome):
    tab.load(_ONETRUST)
    heal = _open(tab, stub_provider(None), store_file, cookies=choice)

    _step(heal, 'email', '#email').fill('ada@example.com')

    assert tab.text('#consent') == outcome
    assert tab.value('#email') == 'ada@example.com'
    assert [e.key for e in heal.handled_interruptions] == ['cookie_banner']


_OAUTH = page("""
<div id="oauth-consent" class="consent-screen">
  <h1>Example App wants to read your email</h1>
  <p>Answer: <span id="answer">none</span></p>
  <button id="deny" type="button" onclick="answer('denied')">Deny</button>
  <button id="allow" type="button" onclick="answer('allowed')">Allow</button>
</div>
<script>
function answer(text) { document.getElementById('answer').textContent += ' ' + text; }
</script>""", 'oauth')


@pytest.mark.parametrize('choice', ['reject', 'accept'])
def test_the_cookies_preset_leaves_a_consent_screen_about_something_else_alone(
        tab, store_file, stub_provider, choice):
    tab.load(_OAUTH)
    heal = _open(tab, stub_provider(None), store_file, cookies=choice)

    _step(heal, 'deny', '#deny').click()

    assert tab.text('#answer') == 'none denied'       # never Allow, never a second Deny
    assert heal.handled_interruptions == []


def test_an_optional_step_that_is_not_there(tab, store_file, stub_provider):
    tab.load(_signup('never'))
    provider = stub_provider("//button[@id='create']")
    heal = _open(tab, provider, store_file)

    promo = _step(heal, 'promo_close', '#promo-close', optional=True, role='button')
    assert promo.present is False
    assert promo.click() is None
    assert provider.bundles == []

    there = _step(heal, 'create', '#create', optional=True)
    assert there.present is True
    there.click()
    assert tab.text('#done') == 'created'


_CONTINUE = page('<form><label for="email">Email</label><input id="email">'
                 '<button type="button" id="continue">Continue</button></form>', 'signup')
_DETAILS = page('<form><label for="phone">Phone</label><input id="phone">'
                '<button type="button" id="help">Help</button></form>', 'details')


def test_the_page_guard_across_two_urls(tab, store_file, stub_provider):
    tab.load_at(_CONTINUE, '/signup')
    provider = stub_provider("//button[@id='help']")
    heal = _open(tab, provider, store_file)
    _step(heal, 'continue', '#continue').click()
    assert saved_entry(store_file, 'continue')['lastPage'].endswith('signup' if tab.family ==
                                                                    'playwright' else 'signup.html')

    # The Continue step moved to another page. The browser is on the next page, where a
    # Help button would pass every check.
    tab.load_at(_DETAILS, '/signup/details')
    with pytest.raises(AttentionRequired) as caught:
        _step(heal, 'continue', '#continue')
    assert 'was last found on' in str(caught.value)
    assert 'That looks like a change in the flow, not a renamed element' in str(caught.value)
    assert 'pass heal_across_pages=True to mendwright.open()' in str(caught.value)
    assert provider.bundles == []
    assert [e['event'] for e in journal_events(store_file)] == ['flow.page_guard']

    # With the guard off, the same heal goes to the provider.
    across = _open(tab, provider, store_file, heal_across_pages=True)
    _step(across, 'continue', '#continue')
    assert len(provider.bundles) == 1
    assert across.heals[0].new == "//button[@id='help']"


_COVERED_ON_HOVER = page("""
<p>Consent: <span id="consent">none</span></p>
<button id="create" type="button" onmouseover="if (!window.shown) { window.shown = true; showBanner(); }"
  onclick="document.getElementById('done').textContent = 'created'">Create account</button>
<p id="done"></p>""" + _SCRIPT, 'hover')


@pytest.mark.engine_family('playwright')
def test_an_action_that_a_banner_intercepts_is_retried_once(tab, store_file, stub_provider):
    # The banner opens when the pointer reaches the button, after mendwright has looked,
    # so the click fails with "intercepts pointer events". mendwright handles the banner
    # and clicks once more.
    tab.load(_COVERED_ON_HOVER)
    heal = _open(tab, stub_provider(None), store_file)
    heal.interrupt('cookies', '#cookie-accept')

    _step(heal, 'create', '#create').click(timeout=1500)

    assert tab.text('#done') == 'created'
    assert tab.text('#consent') == 'accepted'
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('cookies', 'create')]


# --- every action of the script, not only the steps mendwright routes ------------------------
#
# A checkout page whose selectors still work, covered by a consent banner with Reject,
# Accept and a Close icon. The script never names the banner: open(cookies='reject') is
# all it has.

_CONSENT = """<div id="cc-wrap" class="cookie-consent" role="dialog" aria-label="Cookie consent"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4); display: DISPLAY">
  <div style="position: absolute; left: 0; right: 0; bottom: 0; padding: 16px; background: #fff">
    <p>We use cookies to measure traffic.</p>
    <button type="button" onclick="consent('rejected')">Reject all</button>
    <button type="button" onclick="consent('accepted')">Accept all</button>
    <button type="button" aria-label="Close" onclick="consent('closed')">&times;</button>
  </div>
</div>"""

_LATER = """<script>
const go = document.getElementById('continue');
go.disabled = true;
setTimeout(() => { document.getElementById('cc-wrap').style.display = 'block'; }, 500);
setTimeout(() => { go.disabled = false; }, 1200);
</script>"""


def _checkout(banner):
    """banner is 'now' (it covers the form from the start), 'later' (it opens after
    500 ms, while Continue stays disabled until 1200 ms) or 'never'."""
    body = ("""<p>Consent: <span id="consent">none</span>. Done: <span id="done"></span></p>
<form id="checkout"><label for="email">Email</label><input id="email" name="email" type="email">
  <label><input type="checkbox" id="terms"> I accept the terms</label>
  <a href="#help" id="help" onclick="done('help'); return false;">Help</a>
  <button type="button" id="continue" onclick="done('continued')">Continue</button></form>
<script>
function consent(choice) {
  document.getElementById('consent').textContent = choice;
  document.getElementById('cc-wrap').remove();
}
function done(what) { document.getElementById('done').textContent += what + ' '; }
</script>""")
    if banner == 'now':
        body += _CONSENT.replace('DISPLAY', 'block')
    elif banner == 'later':
        body += _CONSENT.replace('DISPLAY', 'none') + _LATER
    return page(body, 'checkout')


def _rejected_once(tab, heal):
    return (tab.text('#consent') == 'rejected'
            and [e.key for e in heal.handled_interruptions] == ['cookie_banner'])


# How a Selenium script reaches an element, and what the action did.
_SELENIUM_WAYS = {
    'find_element click': (lambda d, By: d.find_element(By.ID, 'email').click(),
                           lambda tab: tab.js('document.activeElement.id') == 'email'),
    'send_keys': (lambda d, By: d.find_element(By.CSS_SELECTOR, '#email').send_keys('ada@x.test'),
                  lambda tab: tab.value('#email') == 'ada@x.test'),
    'checkbox': (lambda d, By: d.find_element(By.XPATH, "//input[@id='terms']").click(),
                 lambda tab: tab.checked('#terms') is True),
    'find_elements': (lambda d, By: d.find_elements(By.CSS_SELECTOR, '#checkout button')[0].click(),
                      lambda tab: tab.text('#done') == 'continued '),
    'link text': (lambda d, By: d.find_element(By.LINK_TEXT, 'Help').click(),
                  lambda tab: tab.text('#done') == 'help '),
    'inside an element': (lambda d, By: d.find_element(By.ID, 'checkout')
                          .find_element(By.TAG_NAME, 'button').click(),
                          lambda tab: tab.text('#done') == 'continued '),
}


@pytest.mark.engine_family('selenium')
@pytest.mark.parametrize('way', list(_SELENIUM_WAYS))
def test_every_element_heal_driver_finds_rejects_the_banner_first(tab, store_file,
                                                                   stub_provider, way):
    from selenium.webdriver.common.by import By
    tab.load(_checkout('now'))
    heal = _open(tab, stub_provider(None), store_file, cookies='reject', timeout_ms=FAST_MS)
    act, did = _SELENIUM_WAYS[way]

    act(heal.driver, By)

    assert _rejected_once(tab, heal)
    assert did(tab)


@pytest.mark.engine_family('selenium')
def test_with_an_implicit_wait_and_no_banner_an_action_does_not_wait(tab, store_file,
                                                                      stub_provider):
    from selenium.webdriver.common.by import By
    tab.load(_checkout('never'))
    raw = tab.target
    raw.implicitly_wait(5)
    try:
        driver = _open(tab, stub_provider(None), store_file, cookies='reject').driver
        started = time.monotonic()
        driver.find_element(By.ID, 'email').send_keys('ada@x.test')
        driver.find_element(By.LINK_TEXT, 'Help').click()
        driver.find_elements(By.ID, 'continue')[0].click()
        elapsed = time.monotonic() - started
        kept = raw.timeouts.implicit_wait
    finally:
        raw.implicitly_wait(0)
    assert elapsed < 2.5            # one lookup that waited would take 5 s
    assert kept == 5
    assert tab.text('#done') == 'help continued '


@pytest.mark.engine_family('selenium')
def test_an_element_from_heal_driver_works_where_selenium_takes_a_web_element(
        tab, store_file, stub_provider):
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.remote.webelement import WebElement
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    tab.load(_checkout('never'))
    driver = _open(tab, stub_provider(None), store_file, cookies='reject').driver

    el = driver.find_element(By.ID, 'continue')
    raw = tab.target.find_element(By.ID, 'continue')
    assert isinstance(el, WebElement) and type(el) is not type(raw)
    assert el == raw and raw == el and not (el != raw) and hash(el) == hash(raw)
    assert el in {raw} and raw in {el}
    assert el != tab.target.find_element(By.ID, 'email')
    assert driver.execute_script('return arguments[0].id', el) == 'continue'
    assert WebDriverWait(driver, 5).until(EC.element_to_be_clickable(el)) is el
    ActionChains(driver).move_to_element(el).click().perform()
    assert tab.text('#done') == 'continued '
    assert 'HealingElement' in repr(el)


@pytest.mark.engine_family('selenium')
def test_an_element_found_inside_an_iframe_acts_there(tab, store_file, stub_provider):
    # The key is followed into the iframe, so find_element wraps a FramedElement.
    from selenium.webdriver.common.by import By
    end = IFRAME_PAGE.rindex('</body>')
    tab.load(IFRAME_PAGE[:end] + _CONSENT.replace('DISPLAY', 'block')
             + "<script>function consent(c) { document.getElementById('cc-wrap').remove(); }"
             + '</script>' + IFRAME_PAGE[end:])
    heal = _open(tab, stub_provider(None), store_file, cookies='reject', timeout_ms=FAST_MS)

    box = heal.driver.find_element(By.XPATH, "//input[@type='checkbox'][@data-testid='legal-consent']")
    box.click()

    assert tab.js('document.getElementById("cc-wrap") === null') is True
    assert tab.js('document.querySelector("#consent-frame").contentDocument'
                  '.querySelector("[name=agreement]").checked') is True
    assert tab.js('window.frameElement === null') is True        # back on the page
    assert [h.source for h in heal.heals] == ['frame']


# How a Playwright script reaches an element that mendwright does not route, and what
# the action did.
_PLAYWRIGHT_WAYS = {
    'get_by_role': (lambda p: p.get_by_role('button', name='Continue').click(),
                    lambda tab: tab.text('#done') == 'continued '),
    'get_by_label': (lambda p: p.get_by_label('I accept the terms').check(),
                     lambda tab: tab.checked('#terms') is True),
    'chained': (lambda p: p.locator('#checkout').locator('#help').click(),
                lambda tab: tab.text('#done') == 'help '),
    'first': (lambda p: p.locator('#checkout button').first.click(),
              lambda tab: tab.text('#done') == 'continued '),
    'with options': (lambda p: p.locator('button', has_text='Continue').click(),
                     lambda tab: tab.text('#done') == 'continued '),
    'fill': (lambda p: p.get_by_label('Email').fill('ada@x.test'),
             lambda tab: tab.value('#email') == 'ada@x.test'),
    'the page itself': (lambda p: p.native.get_by_role('link', name='Help').click(),
                        lambda tab: tab.text('#done') == 'help '),
}


@pytest.mark.engine_family('playwright')
@pytest.mark.parametrize('way', list(_PLAYWRIGHT_WAYS))
def test_a_playwright_action_mendwright_does_not_route_rejects_the_banner_first(
        tab, store_file, stub_provider, way):
    tab.load(_checkout('now'))
    heal = _open(tab, stub_provider(None), store_file, cookies='reject')
    act, did = _PLAYWRIGHT_WAYS[way]

    act(heal.page)

    assert _rejected_once(tab, heal)
    assert did(tab)
    assert [e.before for e in heal.handled_interruptions] == ['<page action>']


@pytest.mark.engine_family('playwright')
@pytest.mark.parametrize('routed', [True, False])
def test_a_banner_that_opens_while_an_action_waits_is_handled_at_once(
        tab, store_file, stub_provider, routed):
    tab.load(_checkout('later'))
    heal = _open(tab, stub_provider(None), store_file, cookies='reject')
    started = time.monotonic()
    if routed:
        heal.page.click('#continue', timeout=30000)
    else:
        heal.page.get_by_role('button', name='Continue').click(timeout=30000)
    elapsed = time.monotonic() - started

    assert elapsed < 6              # without the handler: Playwright's whole 30 s timeout
    assert tab.text('#done') == 'continued '
    assert _rejected_once(tab, heal)
    assert [e.before for e in heal.handled_interruptions] == [
        '#continue' if routed else '<page action>']


# Usercentrics draws its banner inside a shadow root, which only the preset's CSS selector
# reaches.
_USERCENTRICS = """<div id="usercentrics-root" style="position: fixed; inset: 0; z-index: 10"></div>
<script>
const root = document.getElementById('usercentrics-root').attachShadow({mode: 'open'});
root.innerHTML = '<div style="position: absolute; inset: 0; background: rgba(0, 0, 0, 0.4)">'
  + '<p>We use cookies.</p>'
  + '<button data-testid="uc-deny-all-button">Deny</button>'
  + '<button data-testid="uc-accept-all-button">Accept All</button></div>';
for (const [id, choice] of [['uc-deny-all-button', 'rejected'], ['uc-accept-all-button', 'accepted']]) {
  root.querySelector('[data-testid="' + id + '"]').onclick = () => {
    document.getElementById('consent').textContent = choice;
    document.getElementById('usercentrics-root').remove();
  };
}
</script>"""


@pytest.mark.engine_family('playwright')
def test_a_banner_in_a_shadow_root_is_handled_before_an_action_mendwright_does_not_route(
        tab, store_file, stub_provider):
    tab.load(_checkout('never').replace('</body>', _USERCENTRICS + '</body>'))
    heal = _open(tab, stub_provider(None), store_file, cookies='reject')

    heal.page.get_by_role('button', name='Continue').click()

    assert tab.text('#done') == 'continued '
    assert _rejected_once(tab, heal)
