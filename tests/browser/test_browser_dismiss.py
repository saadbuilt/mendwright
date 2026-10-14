"""Popups mendwright was never told about (open(dismiss=True)) on real browsers.

Each test runs on every engine (see conftest.py): Playwright Chromium, Firefox and WebKit,
patchright, camoufox, and Selenium with Chrome and with Firefox, unless it is marked for
one family. The pages are built here: a signup form, and a popup over it that the script
never names. The page logs every key press and every button its popup's buttons run into
#log, so a test can tell what mendwright pressed and clicked. No model is called.
"""

import importlib
import json
import logging
import time

import pytest

import mendwright
from mendwright.dismiss import expect_control, expect_of, parse_probe, popup_of
from mendwright.flow import DISMISS_KEY, InterruptionLoop, is_interception_error
from mw_engines import FAST_MS, POLL_MS, page

pytestmark = pytest.mark.browser

_FORM = """<form id="signup"><label for="email">Email</label><input id="email" type="email">
  <button type="button" id="go" onclick="done('go')">Go</button></form>
<p>Done: <span id="done"></span> Log: <span id="log"></span></p>
<script>
function done(what) { document.getElementById('done').textContent += what + ' '; }
function log(what) { document.getElementById('log').textContent += what + ' '; }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') log('key:Escape'); });
</script>"""


def _modal(buttons, *, escape=False, extra=''):
    """A newsletter modal over the whole page: a dialog with an email field and buttons.
    It closes on Escape only with escape=True."""
    html = ('<div id="nl" class="nl" role="dialog" aria-modal="true" aria-label="Newsletter" '
            'style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">'
            '<div style="background: #fff; width: 320px; margin: 140px auto; padding: 16px">'
            '<p>Get 10% off your first order.</p>'
            '<input id="nl-email" type="email" placeholder="Your email"> '
            + ' '.join(buttons) + extra + '</div></div>')
    if escape:
        html += ("<script>document.addEventListener('keydown', (e) => { if (e.key === "
                 "'Escape') { const m = document.getElementById('nl'); if (m) m.remove(); } "
                 '});</script>')
    return html


_CLOSE = "document.getElementById('nl').remove()"
SIGN_UP = '<button type="button" id="nl-signup" onclick="log(\'signup\')">Sign up</button>'
NO_THANKS = ('<button type="button" id="nl-no" onclick="log(\'no-thanks\'); ' + _CLOSE
             + '">No thanks</button>')
SUBSCRIBE = '<button type="button" onclick="log(\'subscribe\')">Subscribe</button>'
YES = '<button type="button" onclick="log(\'yes\')">Yes, sign me up</button>'


def _page(*popup):
    return page(_FORM + ''.join(popup), 'popup')


def _open(tab, provider, store_file, **kwargs):
    kwargs.setdefault('timeout_ms', FAST_MS)
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def _step(heal, key, selector, **kwargs):
    return heal.locator(key, selector, timeout_ms=FAST_MS, poll_ms=POLL_MS, **kwargs)


def _log(tab):
    return tab.text('#log').split()


def _gone(tab, ident='nl'):
    return tab.js('document.getElementById(' + json.dumps(ident) + ') === null') is True


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == 'mendwright'
            and r.levelno == logging.WARNING and r.getMessage().startswith('mendwright: ')]


def test_a_newsletter_modal_is_dismissed_with_no_thanks_never_sign_up(tab, store_file,
                                                                     stub_provider, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_page(_modal([SIGN_UP, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'go', '#go').click()

    assert tab.text('#done') == 'go '
    assert _log(tab) == ['key:Escape', 'no-thanks']           # never Sign up
    assert _gone(tab)
    [event] = heal.handled_interruptions
    assert (event.key, event.before, event.how, event.label) == (DISMISS_KEY, 'go', 'decline',
                                                                 'No thanks')
    assert event.popup.startswith('<div id="nl" role="dialog" class="nl"> ("Get 10% off '
                                  'your first order.')
    assert [w for w in _warnings(caplog) if w.startswith(
        "mendwright: dismissed a popup in the way of 'go': " + event.popup
        + ' with “No thanks”, after Escape did not close it')]


def test_a_modal_that_closes_on_escape_is_dismissed_by_escape_alone(tab, store_file,
                                                                    stub_provider):
    tab.load(_page(_modal([SUBSCRIBE], escape=True)))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'email', '#email').fill('ada@example.com')

    assert tab.value('#email') == 'ada@example.com'
    assert _log(tab) == ['key:Escape'] and _gone(tab)
    assert [e.how for e in heal.handled_interruptions] == ['escape']


def test_an_icon_close_button_is_clicked(tab, store_file, stub_provider):
    close = ('<button type="button" aria-label="Close" onclick="log(\'close\'); ' + _CLOSE
             + '">&times;</button>')
    tab.load(_page(_modal([SUBSCRIBE, close])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'go', '#go').click()

    assert _log(tab) == ['key:Escape', 'close'] and tab.text('#done') == 'go '
    assert [(e.how, e.label) for e in heal.handled_interruptions] == [('close', 'Close')]


def test_a_modal_with_only_subscribe_and_yes_is_left_alone(tab, store_file, stub_provider,
                                                           caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_page(_modal([SUBSCRIBE, YES])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    with pytest.raises(Exception) as caught:
        _step(heal, 'go', '#go').click(timeout=1500)    # Selenium ignores the timeout

    assert is_interception_error(caught.value)           # the browser's own error
    assert not isinstance(caught.value, mendwright.AttentionRequired)
    assert _log(tab) == ['key:Escape'] and not _gone(tab)
    assert tab.text('#done') == '' and heal.handled_interruptions == []
    [warning] = _warnings(caplog)
    assert warning.startswith("mendwright: could not dismiss a popup in the way of 'go': "
                              '<div id="nl" role="dialog" class="nl"> ("Get 10% off your '
                              'first order.')
    assert ('mendwright pressed Escape, which did not close it, and it has no close or decline '
            'button') in warning
    assert 'heal.interrupt(' in warning and 'action="fill"' in warning


def test_a_no_thanks_link_that_goes_somewhere_is_not_clicked(tab, store_file, stub_provider):
    link = '<a id="nl-no" href="/no-thanks" onclick="log(\'link\'); return false;">No thanks</a>'
    tab.load(_page(_modal([SUBSCRIBE, link])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    with pytest.raises(Exception) as caught:
        _step(heal, 'go', '#go').click(timeout=1500)

    assert is_interception_error(caught.value)
    assert 'link' not in _log(tab) and not _gone(tab)


def test_the_element_inside_the_modal_is_used_and_nothing_is_dismissed(tab, store_file,
                                                                       stub_provider):
    tab.load(_page(_modal([SIGN_UP, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'nl_email', '#nl-email').fill('ada@example.com')
    _step(heal, 'nl_email', '#nl-email').click()

    assert tab.value('#nl-email') == 'ada@example.com'
    assert _log(tab) == [] and not _gone(tab)
    assert heal.handled_interruptions == []


def test_a_small_sticky_header_over_the_top_edge_is_left_alone(tab, store_file,
                                                               stub_provider):
    header = ('<header id="top" style="position: sticky; top: 0; height: 40px; z-index: 5; '
              'background: #eee">Shop <button type="button" onclick="log(\'header\')">Close'
              '</button></header>')
    form = ('<button type="button" id="go" onclick="done(\'go\')" style="position: absolute; '
            'top: 40px; left: 200px; height: 40px">Go</button>')
    tab.load(page(header + form + _FORM.replace('id="go"', 'id="other"'), 'header'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'go', '#go').click()

    assert tab.text('#done') == 'go '
    assert _log(tab) == [] and heal.handled_interruptions == []


def test_a_consent_banner_is_declined_never_accepted(tab, store_file, stub_provider):
    banner = """<div id="cmp" role="dialog" aria-label="Privacy choices"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4)">
  <div style="position: absolute; left: 0; right: 0; bottom: 0; padding: 16px; background: #fff">
    <p>We and our partners use cookies to measure traffic.</p>
    <button type="button" onclick="log('accept'); this.closest('#cmp').remove()">Accept all</button>
    <button type="button" onclick="log('settings')">Settings</button>
    <button type="button" onclick="log('reject'); this.closest('#cmp').remove()">Reject all</button>
  </div></div>"""
    tab.load(_page(banner))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)   # no cookies=

    _step(heal, 'go', '#go').click()

    assert _log(tab) == ['key:Escape', 'reject'] and _gone(tab, 'cmp')
    assert tab.text('#done') == 'go '


def test_with_dismiss_off_the_modal_stops_the_click_as_before(tab, store_file, stub_provider):
    tab.load(_page(_modal([SIGN_UP, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file)        # dismiss=False, the default

    with pytest.raises(Exception) as caught:
        _step(heal, 'go', '#go').click(timeout=1500)

    assert is_interception_error(caught.value)
    assert _log(tab) == [] and not _gone(tab)
    assert heal.handled_interruptions == []


def test_a_popup_that_comes_back_each_time_stops_the_step(tab, store_file, stub_provider):
    again = ('<button type="button" id="nl-no" onclick="log(\'no-thanks\'); ' + _CLOSE
             + '; document.body.insertAdjacentHTML(\'beforeend\', window.popupHtml)">'
             'No thanks</button>')
    popup = _modal([SIGN_UP, again])
    tab.load(_page(popup, '<script>window.popupHtml = ' + json.dumps(popup) + ';</script>'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    with pytest.raises(InterruptionLoop, match="3 interruptions and popups were handled for "
                                               "'go' and a popup is still in the way"):
        _step(heal, 'go', '#go').click()

    assert _log(tab).count('no-thanks') == 3 and 'signup' not in _log(tab)
    assert [e.key for e in heal.handled_interruptions] == [DISMISS_KEY] * 3
    assert tab.text('#done') == ''


def _framed():
    """A page whose terms checkbox is inside an iframe, under a newsletter modal there."""
    inner = page('<label><input type="checkbox" data-testid="legal-consent"> I accept the '
                 'terms</label>' + _modal([SIGN_UP, NO_THANKS]).replace("log(", "void("),
                 'consent')
    return page('<h1>Outer page</h1><iframe id="consent-frame" width="600" height="400" '
                'srcdoc="' + inner.replace('&', '&amp;').replace('"', '&quot;')
                + '"></iframe>', 'iframe')


TERMS = "//input[@data-testid='legal-consent']"


def test_a_popup_inside_an_iframe_is_dismissed_there(tab, store_file, stub_provider):
    # The key is followed into the iframe, so the look runs in the iframe's document.
    tab.load(_framed())
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'terms', TERMS, role='checkbox').check()

    frame = 'document.querySelector("#consent-frame").contentDocument'
    assert tab.js(frame + '.querySelector("[data-testid=legal-consent]").checked') is True
    assert tab.js(frame + '.getElementById("nl") === null') is True
    assert [(e.key, e.how, e.label) for e in heal.handled_interruptions] == [
        (DISMISS_KEY, 'decline', 'No thanks')]
    assert [h.source for h in heal.heals] == ['frame']


@pytest.mark.engine_family('selenium')
def test_selenium_find_element_and_click(tab, store_file, stub_provider):
    from selenium.webdriver.common.by import By
    tab.load(_page(_modal([SIGN_UP, NO_THANKS])))
    driver = _open(tab, stub_provider(None), store_file, dismiss=True).driver

    driver.find_element(By.ID, 'go').click()
    driver.find_element(By.ID, 'email').send_keys('ada@example.com')

    assert tab.text('#done') == 'go ' and tab.value('#email') == 'ada@example.com'
    assert _log(tab) == ['key:Escape', 'no-thanks']


@pytest.mark.engine_family('selenium')
def test_with_an_implicit_wait_looking_and_dismissing_do_not_wait(tab, store_file,
                                                                  stub_provider):
    from selenium.webdriver.common.by import By
    raw = tab.target
    raw.implicitly_wait(5)
    try:
        tab.load(_page())                                      # nothing in the way
        driver = _open(tab, stub_provider(None), store_file, dismiss=True).driver
        started = time.monotonic()
        driver.find_element(By.ID, 'go').click()
        driver.find_elements(By.ID, 'email')[0].send_keys('ada@x.test')
        plain = time.monotonic() - started

        tab.load(_framed())                                    # a popup inside an iframe
        heal = _open(tab, stub_provider(None), store_file, dismiss=True)
        started = time.monotonic()
        _step(heal, 'terms', TERMS, role='checkbox').check()
        framed = time.monotonic() - started
        kept = raw.timeouts.implicit_wait
    finally:
        raw.implicitly_wait(0)
    assert plain < 2.5 and framed < 4.5    # one lookup that waited would take 5 s
    assert kept == 5
    assert [e.label for e in heal.handled_interruptions] == ['No thanks']


@pytest.mark.engine_family('playwright')
def test_playwright_get_by_role_with_dismiss_and_expect(tab, store_file, stub_provider):
    # expect() from the page's own library: patchright pages are not playwright's.
    library = type(tab.target).__module__.split('.')[0]
    expect = importlib.import_module(library + '.sync_api').expect
    tab.load(_page(_modal([SIGN_UP, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    go = heal.page.get_by_role('button', name='Go')
    go.click(timeout=5000)
    expect(go).to_have_text('Go')
    heal.page.locator('#signup').locator('#email').fill('ada@example.com')

    assert tab.text('#done') == 'go ' and tab.value('#email') == 'ada@example.com'
    assert _log(tab) == ['key:Escape', 'no-thanks']
    assert [e.before for e in heal.handled_interruptions] == [
        "page.get_by_role('button', name='Go')"]
    assert heal.store.candidates("page.get_by_role('button', name='Go')") == []


def _left_alone(tab, heal, selector='#go', key='go'):
    """Click selector's element with a popup over it that mendwright must leave: the
    browser's own error, and nothing recorded."""
    with pytest.raises(Exception) as caught:
        _step(heal, key, selector).click(timeout=1500)     # Selenium ignores the timeout
    assert is_interception_error(caught.value)
    assert not isinstance(caught.value, mendwright.AttentionRequired)
    assert heal.handled_interruptions == []


# --- where Escape goes ----------------------------------------------------------------------

def test_escape_is_not_pressed_while_a_field_the_script_filled_has_the_focus(tab, store_file,
                                                                            stub_provider):
    # Escape goes to the focused element: in a search field it clears what was typed.
    tab.load(_page('<input id="q" type="search">'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'q', '#q').fill('red shoes')                  # the focus stays in #q
    tab.js("document.body.insertAdjacentHTML('beforeend', "
           + json.dumps(_modal([SIGN_UP, NO_THANKS])) + ')')

    _step(heal, 'go', '#go').click()

    assert _log(tab) == ['no-thanks'] and tab.text('#done') == 'go '
    assert tab.value('#q') == 'red shoes'
    assert [(e.how, e.label) for e in heal.handled_interruptions] == [('decline', 'No thanks')]


_CHECKOUT = """<div id="checkout" role="dialog" aria-label="Checkout" style="position: fixed;
  left: 200px; top: 60px; width: 400px; height: 300px; z-index: 5; background: #fff">
  <input id="card" value="4242 4242">
  <button type="button" id="pay" onclick="done('pay')">Place order</button></div>
<div id="chat" style="position: fixed; left: 0; top: 0; width: 100%; height: 60%; z-index: 20;
  background: rgba(0, 0, 0, 0.3)"><p>Chat with us!</p>
  <button type="button" onclick="log('chat')">Chat</button></div>
<script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') {
  document.getElementById('checkout').style.display = 'none'; log('checkout-closed'); } });
</script>"""


def test_escape_is_not_pressed_while_the_focus_is_in_the_dialog_the_script_uses(
        tab, store_file, stub_provider, caplog):
    # The page's own checkout dialog closes on Escape; a chat layer covers it.
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_page(_CHECKOUT))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    tab.js("document.getElementById('card').focus() || true")

    _left_alone(tab, heal, '#pay', 'pay')

    assert _log(tab) == [] and tab.js("document.getElementById('checkout').style.display") == ''
    [warning] = [w for w in _warnings(caplog) if 'popup' in w]
    assert warning.startswith("mendwright: could not dismiss a popup in the way of 'pay': "
                              '<div id="chat"> ("Chat with us! Chat")')
    assert 'mendwright did not press Escape' in warning


def test_escape_that_hides_the_element_is_not_counted_as_a_dismissal(tab, store_file,
                                                                     stub_provider, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_page(_CHECKOUT))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)   # the focus is on body

    _left_alone(tab, heal, '#pay', 'pay')

    assert _log(tab) == ['key:Escape', 'checkout-closed']
    assert tab.text('#done') == ''
    warnings = [w for w in _warnings(caplog) if 'popup' in w]
    assert len(warnings) == 1 and warnings[0].startswith(
        "mendwright: Pressed Escape on a popup in the way of 'pay': <div id=\"chat\">")
    assert 'it is hidden or outside the window' in warnings[0]


def _framed_escape_only():
    """A page whose own dialog closes on Escape and holds an iframe, inside which a
    newsletter modal that only Escape closes covers the terms checkbox."""
    inner = page('<label><input type="checkbox" data-testid="legal-consent"> I accept the '
                 'terms</label>' + _modal([SUBSCRIBE.replace('log(', 'void(')], escape=True),
                 'consent')
    return page('<div id="outer" role="dialog" aria-label="Checkout"><h1>Checkout</h1>'
                '<iframe id="consent-frame" width="600" height="400" srcdoc="'
                + inner.replace('&', '&amp;').replace('"', '&quot;') + '"></iframe></div>'
                '<p>Log: <span id="log"></span></p><script>document.addEventListener("keydown", '
                '(e) => { if (e.key === "Escape") { document.getElementById("outer").style'
                '.display = "none"; document.getElementById("log").textContent += "outer-escape '
                '"; } });</script>', 'iframe')


def test_in_an_iframe_escape_is_never_pressed(tab, store_file, stub_provider, caplog):
    # The key would go to the page around the iframe, which mendwright never looked at, and
    # close its dialog. A popup in the iframe that only Escape closes stays.
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_framed_escape_only())
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    with pytest.raises(Exception) as caught:
        _step(heal, 'terms', TERMS, role='checkbox').check(timeout=1500)

    assert is_interception_error(caught.value)
    assert _log(tab) == [] and tab.js('document.getElementById("outer").style.display') == ''
    frame = 'document.querySelector("#consent-frame").contentDocument'
    assert tab.js(frame + '.getElementById("nl") !== null') is True
    assert heal.handled_interruptions == []
    [warning] = [w for w in _warnings(caplog) if w.startswith('mendwright: could not dismiss')]
    assert 'mendwright did not press Escape' in warning


@pytest.mark.engine_family('selenium')
def test_selenium_inside_a_frame_the_script_switched_into(tab, store_file, stub_provider):
    from selenium.webdriver.common.by import By
    tab.load(_framed())
    raw = tab.target
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    raw.switch_to.frame(raw.find_element(By.ID, 'consent-frame'))
    try:
        heal.driver.find_element(By.XPATH, TERMS).click()
        checked = raw.execute_script(
            'return document.querySelector("[data-testid=legal-consent]").checked')
        gone = raw.execute_script('return document.getElementById("nl") === null')
    finally:
        raw.switch_to.default_content()
    assert checked is True and gone is True
    assert [(e.how, e.label) for e in heal.handled_interruptions] == [('decline', 'No thanks')]


# --- buttons that are never clicked -----------------------------------------------------------

@pytest.mark.parametrize('buttons, clicked', [
    # A button inside a link that goes somewhere: the click follows the link.
    (['<a href="#/upgrade-plan" onclick="log(\'link\')"><button type="button">No thanks'
      '</button></a>'], 'link'),
    # A button in a label: the click checks the label's box.
    (['<label><input type="checkbox" id="mkt" onchange="log(\'checked\')"> <span role="button" '
      'onclick="log(\'span\')">No thanks</span></label>'], 'checked'),
    # A button with no type in a form submits it, email and all.
    (['<form onsubmit="log(\'submitted\'); return false"><input name="email" '
      'value="me@x.test"> <button>No thanks</button></form>'], 'submitted'),
    # What it shows is Keep me updated; the No thanks is hidden.
    (['<button type="button" onclick="log(\'keep\')"><span style="display: none">No thanks'
      '</span>Keep me updated</button>'], 'keep'),
    # A question: No is no decline button.
    (['<p>Keep the items in your basket?</p>',
      '<button type="button" onclick="log(\'no\')">No</button>',
      '<button type="button" onclick="log(\'yes\')">Yes</button>'], 'no'),
    (['<button type="button" onclick="log(\'trial\')">No-risk trial</button>'], 'trial'),
    (['<button type="button" onclick="log(\'go-on\')">Continue without delay</button>'],
     'go-on'),
    (['<button type="button" onclick="log(\'thanks\')">Count me in, thanks</button>'],
     'thanks'),
    # What it shows is Thanks, an answer, not a decline: the No is hidden.
    (['<button type="button" onclick="log(\'shown\')"><span style="display: none">No </span>'
      'Thanks</button>'], 'shown'),
])
def test_buttons_mendwright_never_clicks(tab, store_file, stub_provider, buttons, clicked):
    tab.load(_page(_modal(buttons)))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    url = tab.js('location.href')

    _left_alone(tab, heal)

    assert _log(tab) == ['key:Escape'] and not _gone(tab)
    assert clicked not in _log(tab) and tab.js('location.href') == url
    if clicked == 'checked':
        assert tab.checked('#mkt') is False


def test_a_no_thanks_in_a_form_with_method_dialog_is_clicked(tab, store_file, stub_provider):
    # Such a form only closes its dialog: nothing is sent.
    form = ('<form method="dialog"><button onclick="log(\'no-thanks\'); ' + _CLOSE
            + '">No thanks</button></form>')
    tab.load(_page(_modal([form])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'no-thanks'] and _gone(tab)
    assert tab.text('#done') == 'go '


def test_skip_the_line_for_money_is_never_clicked_and_no_thanks_is(tab, store_file,
                                                                  stub_provider):
    skip = '<button type="button" onclick="log(\'skip-the-line\')">Skip the line - $9.99</button>'
    tab.load(_page(_modal([skip, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'no-thanks'] and tab.text('#done') == 'go '


def test_a_disabled_no_thanks_is_passed_over(tab, store_file, stub_provider):
    disabled = '<button type="button" disabled onclick="log(\'disabled\')">No thanks</button>'
    aria = ('<button type="button" aria-disabled="true" onclick="log(\'aria-disabled\')">'
            'No thanks</button>')
    not_now = ('<button type="button" onclick="log(\'not-now\'); ' + _CLOSE + '">Not now'
               '</button>')
    tab.load(_page(_modal([disabled, aria, not_now])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'not-now'] and tab.text('#done') == 'go '


def test_with_cookies_accept_an_unknown_consent_banner_is_left_alone(tab, store_file,
                                                                    stub_provider, caplog):
    # Declining it would turn the consent down, the opposite of cookies="accept".
    caplog.set_level(logging.INFO, logger='mendwright')
    banner = """<div id="pop" role="dialog" aria-label="Privacy" style="position: fixed; inset: 0;
  z-index: 10; background: #fff">
  <p>We use cookies to improve your experience.</p>
  <button type="button" onclick="log('reject'); this.closest('#pop').remove()">Reject all</button>
  <button type="button" onclick="log('accept'); this.closest('#pop').remove()">Accept all</button>
  </div>"""
    tab.load(_page(banner))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True, cookies='accept')

    _left_alone(tab, heal)

    assert _log(tab) == [] and not _gone(tab, 'pop')
    assert [w for w in _warnings(caplog) if "left a popup in the way of 'go' alone: <div "
            'id="pop" role="dialog"> ("We use cookies to improve your experience.' in w]


# --- what is a popup and what is not ----------------------------------------------------------

def test_a_small_toast_in_a_full_window_layer_is_not_a_popup(tab, store_file, stub_provider):
    # Toast libraries put toasts in a full-window layer with pointer-events: none.
    toast = """<div id="toaster" style="position: fixed; inset: 0; pointer-events: none;
  z-index: 50"><div id="toast" role="status" style="position: absolute; pointer-events: auto;
  width: 220px; height: 60px; background: #333; color: #fff">Item added
  <button type="button" onclick="log('toast-dismiss')">Dismiss</button></div></div>
<script>const r = document.getElementById('go').getBoundingClientRect();
const t = document.getElementById('toast');
t.style.left = (r.left + r.width / 2 - 30) + 'px'; t.style.top = (r.top + r.height / 2 - 30) + 'px';
</script>"""
    tab.load(_page(toast))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _left_alone(tab, heal)
    assert _log(tab) == []


def test_an_element_in_the_popup_under_a_part_of_the_popup_is_left_alone(tab, store_file,
                                                                         stub_provider):
    # A floating label over a field of the popup the script is filling in.
    field = ('<div style="position: relative"><input id="nl-code" placeholder="Code">'
             '<span style="position: absolute; left: 0; top: 0; width: 100%; height: 100%; '
             'background: #eef">Your code</span></div>')
    tab.load(_page(_modal([SIGN_UP, NO_THANKS], extra=field)))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _left_alone(tab, heal, '#nl-code', 'nl_code')
    assert _log(tab) == [] and not _gone(tab)


def test_a_small_fixed_header_over_the_middle_of_the_element_is_left_alone(tab, store_file,
                                                                           stub_provider):
    header = ('<header id="top" style="position: fixed; top: 0; left: 0; right: 0; height: '
              '60px; z-index: 5; background: #eee">Shop <button type="button" '
              'onclick="log(\'header\')">Close</button></header>')
    go = ('<button type="button" id="go" onclick="done(\'go\')" style="position: fixed; top: '
          '15px; left: 300px; height: 30px; z-index: 1">Go</button>')
    tab.load(page(header + go + _FORM.replace('id="go"', 'id="other"'), 'header'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _left_alone(tab, heal)
    assert _log(tab) == [] and tab.text('#done') == ''


@pytest.mark.parametrize('height, popup', [('70%', True), ('15%', False)])
def test_a_plain_fixed_layer_is_a_popup_when_it_covers_a_quarter_of_the_window(
        tab, store_file, stub_provider, height, popup):
    # An app-install banner: no dialog role, only position: fixed.
    layer = ('<div id="app" style="position: fixed; left: 0; right: 0; top: 0; height: '
             + height + '; z-index: 10; background: #fff"><p>Get our app</p>'
             '<button type="button" onclick="log(\'not-now\'); this.parentNode.remove()">'
             'Not now</button></div>')
    tab.load(_page(layer))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    if popup:
        _step(heal, 'go', '#go').click()
        assert _log(tab) == ['key:Escape', 'not-now'] and _gone(tab, 'app')
        assert tab.text('#done') == 'go '
    else:
        _left_alone(tab, heal)
        assert _log(tab) == [] and not _gone(tab, 'app')


def test_a_modal_dialog_element_is_a_popup(tab, store_file, stub_provider):
    dialog = ('<dialog id="rate"><p>Rate us</p><button type="button" onclick="log(\'close\'); '
              'this.closest(\'dialog\').close()">Close</button></dialog>'
              '<script>document.getElementById("rate").showModal();</script>')
    tab.load(_page(dialog))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert tab.js('document.getElementById("rate").open') is False
    assert tab.text('#done') == 'go '
    [event] = heal.handled_interruptions
    assert event.key == DISMISS_KEY and event.popup.startswith('<dialog id="rate">')


_SHADOW_BANNER = """<cmp-banner></cmp-banner><script>
customElements.define('cmp-banner', class extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<div role="dialog" aria-label="Privacy" style="position: fixed; inset: 0; '
      + 'z-index: 10; background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p>'
      + '<button type="button" id="a">Accept all</button> '
      + '<button type="button" id="r">Reject all</button></div>';
    root.getElementById('a').onclick = () => { log('accept'); this.remove(); };
    root.getElementById('r').onclick = () => { log('reject'); this.remove(); };
  }
});
</script>"""


# A dialog of the page whose buttons are drawn by a component, in its own open shadow root.
_SHADOW_BUTTONS = """<div id="cmp" role="dialog" aria-modal="true" style="position: fixed; inset: 0;
  z-index: 10; background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p><cmp-buttons></cmp-buttons></div>
<script>
customElements.define('cmp-buttons', class extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<button type="button" id="a">Accept all</button> '
      + '<button type="button" id="r">Reject all</button>';
    root.getElementById('a').onclick = () => { log('accept'); document.getElementById('cmp').remove(); };
    root.getElementById('r').onclick = () => { log('reject'); document.getElementById('cmp').remove(); };
  }
});
</script>"""


@pytest.mark.parametrize('banner', [_SHADOW_BANNER, _SHADOW_BUTTONS], ids=['popup', 'buttons'])
def test_a_banner_in_an_open_shadow_root_is_declined(tab, store_file, stub_provider, banner):
    tab.load(_page(banner))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'reject'] and tab.text('#done') == 'go '
    assert [(e.how, e.label) for e in heal.handled_interruptions] == [('decline', 'Reject all')]


def test_a_dialog_that_is_neither_modal_nor_fixed_is_a_popup(tab, store_file, stub_provider):
    # A survey placed over the form with position: absolute, and only role="dialog".
    survey = ('<div id="survey" role="dialog" style="position: absolute; left: 0; top: 0; '
              'width: 600px; height: 200px; z-index: 10; background: #fff"><p>How did we do?</p>'
              '<button type="button" onclick="log(\'not-now\'); this.parentNode.remove()">Not now'
              '</button></div>')
    tab.load(_page(survey))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'not-now'] and tab.text('#done') == 'go '
    [event] = heal.handled_interruptions
    assert event.popup == '<div id="survey" role="dialog"> ("How did we do? Not now")'


# --- registered interruptions first -------------------------------------------------------------

def test_a_registered_banner_that_is_also_a_popup_is_handled_its_registered_way(
        tab, store_file, stub_provider):
    banner = """<div id="cmp" role="dialog" style="position: fixed; inset: 0; z-index: 10;
  background: rgba(0, 0, 0, 0.4)"><p>We and our partners use cookies.</p>
  <button type="button" id="accept" onclick="log('accept'); this.closest('#cmp').remove()">Accept all</button>
  <button type="button" onclick="log('reject'); this.closest('#cmp').remove()">Reject all</button></div>"""
    tab.load(_page(banner))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    heal.interrupt('consent', '#accept')

    _step(heal, 'go', '#go').click()

    assert _log(tab) == ['accept'] and tab.text('#done') == 'go '
    assert [e.key for e in heal.handled_interruptions] == ['consent']


# --- the scripts' own checks -------------------------------------------------------------------

def test_the_scripts_refuse_a_control_or_root_that_is_not_the_one_chosen(tab, store_file,
                                                                         stub_provider):
    # Between the list of buttons and the click the popup may re-render: the click script
    # checks that button number N is still the one chosen, the root script the root's id.
    tab.load(_page(_modal([SUBSCRIBE, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    driver = heal._driver
    native = driver.handle('#go')
    popup = popup_of(parse_probe(driver.popup_probe(native)))
    root = driver.popup_root(native, expect_of(popup))
    controls = driver.popup_controls(native, root)
    texts = [c['text'] for c in controls]
    subscribe, no_thanks = texts.index('Subscribe'), texts.index('No thanks')

    assert driver.popup_click(native, root, expect_control(subscribe, controls[no_thanks]),
                              1000) is False
    assert driver.popup_root(native, dict(expect_of(popup), id='other')) is None
    assert _log(tab) == [] and not _gone(tab)
    assert driver.popup_click(native, root, expect_control(no_thanks, controls[no_thanks]),
                              1000) is True
    assert _log(tab) == ['no-thanks'] and _gone(tab)


# --- timing ------------------------------------------------------------------------------------

def _closes_after(ms):
    return ('<button type="button" id="nl-no" onclick="log(\'no-thanks\'); setTimeout(() => '
            + _CLOSE + ', ' + str(ms) + ')">No thanks</button>')


def test_a_popup_that_closes_a_moment_after_the_click_is_dismissed(tab, store_file,
                                                                   stub_provider):
    # mendwright looks again for up to the step's timeout_ms (300 ms here) after each step.
    tab.load(_page(_modal([SIGN_UP, _closes_after(100)])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click()
    assert _log(tab) == ['key:Escape', 'no-thanks'] and tab.text('#done') == 'go '
    assert [(e.how, e.label) for e in heal.handled_interruptions] == [('decline', 'No thanks')]


@pytest.mark.engine_family('playwright')
def test_a_popup_that_closes_after_mendwright_stopped_looking_is_not_counted(
        tab, store_file, stub_provider, caplog):
    # Longer than the time mendwright looks again for: it reports that it could not dismiss
    # the popup, and Playwright's click, which waits, goes through once the popup is gone.
    caplog.set_level(logging.INFO, logger='mendwright')
    tab.load(_page(_modal([SIGN_UP, _closes_after(1200)])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    _step(heal, 'go', '#go').click(timeout=5000)
    assert tab.text('#done') == 'go ' and _gone(tab)
    assert heal.handled_interruptions == []
    assert [w for w in _warnings(caplog) if w.startswith(
        "mendwright: could not dismiss a popup in the way of 'go'")]


@pytest.mark.engine_family('playwright')
@pytest.mark.parametrize('how', ['locator', 'get_by_role'])
def test_an_element_below_the_window_gets_the_look_before_the_action(tab, store_file,
                                                                    stub_provider, how):
    # It is scrolled into view first, so the popup is dismissed before the click, not after
    # the click waited out its timeout on it.
    low = _FORM.replace('<form id="signup">', '<div style="height: 2500px"></div>'
                        '<form id="signup">')
    tab.load(page(low + _modal([SIGN_UP, NO_THANKS]), 'popup'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    started = time.monotonic()
    if how == 'locator':
        _step(heal, 'go', '#go').click(timeout=8000)
    else:
        heal.page.get_by_role('button', name='Go', exact=True).click(timeout=8000)
    took = time.monotonic() - started
    assert tab.text('#done') == 'go ' and _log(tab) == ['key:Escape', 'no-thanks']
    assert took < 6      # waiting on the popup would take the click's whole 8 s
