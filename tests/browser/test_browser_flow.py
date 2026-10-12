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

import pytest

import mendwright
from mendwright import AttentionRequired
from mw_engines import FAST_MS, POLL_MS, journal_events, page, saved_entry

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
