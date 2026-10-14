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

import pytest

import mendwright
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
    assert (event.key, event.before, event.how) == (DISMISS_KEY, 'go', '“No thanks”')
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
    assert [e.how for e in heal.handled_interruptions] == ['Escape']


def test_an_icon_close_button_is_clicked(tab, store_file, stub_provider):
    close = ('<button type="button" aria-label="Close" onclick="log(\'close\'); ' + _CLOSE
             + '">&times;</button>')
    tab.load(_page(_modal([SUBSCRIBE, close])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'go', '#go').click()

    assert _log(tab) == ['key:Escape', 'close'] and tab.text('#done') == 'go '
    assert [e.how for e in heal.handled_interruptions] == ['“Close”']


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
    assert 'Escape did not close it, and it has no close or decline button' in warning
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


def test_a_popup_inside_an_iframe_is_dismissed_there(tab, store_file, stub_provider):
    # The key is followed into the iframe, so the look runs in the iframe's document.
    inner = page('<label><input type="checkbox" data-testid="legal-consent"> I accept the '
                 'terms</label>' + _modal([SIGN_UP, NO_THANKS]).replace("log(", "void("),
                 'consent')
    tab.load(page('<h1>Outer page</h1><iframe id="consent-frame" width="600" height="400" '
                  'srcdoc="' + inner.replace('&', '&amp;').replace('"', '&quot;')
                  + '"></iframe>', 'iframe'))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)

    _step(heal, 'terms', "//input[@data-testid='legal-consent']", role='checkbox').check()

    frame = 'document.querySelector("#consent-frame").contentDocument'
    assert tab.js(frame + '.querySelector("[data-testid=legal-consent]").checked') is True
    assert tab.js(frame + '.getElementById("nl") === null') is True
    assert [(e.key, e.how) for e in heal.handled_interruptions] == [(DISMISS_KEY, '“No thanks”')]
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
