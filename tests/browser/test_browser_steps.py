"""Steps in any order (heal.step() and heal.run_steps()) on real browsers.

Each test runs on every engine (see conftest.py): Playwright Chromium, Firefox and WebKit,
patchright, camoufox, and Selenium with Chrome and with Firefox. The checkout is built
here: a shipping, a payment and a review page at /checkout/step-a, step-b, ... in the order
a test asks for, so the address says where a step is in the flow, not which step it is.
On Playwright the pages come from a route (tab.serve), on Selenium from files. Each page's
Continue button goes to the next one and carries what was filled in along in the query,
and the last page shows it, so a test can tell what the steps did. The steps are the
user's script, written once: on Playwright they get heal.page, on Selenium heal.driver.
No model is called.
"""

import logging

import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright.flow import DISMISS_KEY
from mendwright.steps import WAITING_STEP
from mw_engines import FAST_MS, POLL_MS, journal_events, page

pytestmark = pytest.mark.browser

# How long run_steps() waits for the next page in a flow that goes on, and in one that is
# expected to stop.
WAIT_MS = 5000
STOP_MS = 1200

CARD = '4242 4242 4242 4242'
DECLINED = '4000 0000 0000 0002'

# go(url, [[param, field id], ...]) opens url with the fields' values added to what the
# query carries already. The payment page sends a declined card back to itself.
_SCRIPT = """<script>
function go(url, fields) {
  var q = location.search.slice(1).split('&').filter(function (p) {
    return p && p.indexOf('declined=') !== 0; });
  (fields || []).forEach(function (f) {
    q.push(f[0] + '=' + encodeURIComponent(document.getElementById(f[1]).value)); });
  location.href = url + (q.length ? '?' + q.join('&') : '');
}
</script>"""


def _go(url, *fields):
    pairs = ', '.join("['" + p + "', '" + i + "']" for p, i in fields)
    return "go('" + url + "', [" + pairs + "])"


def _shipping(tab, nxt, me, field='ship-name'):
    return page('<h1>Shipping address</h1><form><label for="' + field + '">Full name</label>'
                '<input id="' + field + '" name="' + field + '">'
                '<button type="button" id="next" onclick="' + _go(tab.address(nxt), ('ship', field))
                + '">Continue</button></form>' + _SCRIPT, 'shipping')


def _payment(tab, nxt, me, field=None):
    pay = ("var c = document.getElementById('card-number').value; if (c === '" + DECLINED
           + "') { location.href = '" + tab.address(me) + "?declined=1'; } else { "
           + _go(tab.address(nxt), ('card', 'card-number')) + '; }')
    return page('<h1>Payment</h1><p id="error"></p><form><label for="card-number">Card number'
                '</label><input id="card-number" name="card-number">'
                '<button type="button" id="next" onclick="' + pay + '">Continue</button></form>'
                + _SCRIPT + "<script>if (location.search.indexOf('declined=') >= 0) "
                "document.getElementById('error').textContent = 'Your card was declined.';"
                '</script>', 'payment')


def _review(tab, nxt, me, field=None):
    return page('<h1>Review your order</h1><button type="button" id="place" onclick="'
                + _go(tab.address(nxt)) + '">Place order</button>' + _SCRIPT, 'review')


def _upsell(tab, nxt, me, field=None):
    return page('<h1>Protect your order</h1><p>Add insurance for $4.99.</p>'
                '<button type="button" id="skip-insurance" onclick="' + _go(tab.address(nxt))
                + '">Continue without insurance</button>' + _SCRIPT, 'upsell')


def _gift(tab, nxt, me, field=None):
    return page('<h1>Gift options</h1><label><input type="checkbox" id="wrap"> Gift wrap</label>'
                '<button type="button" id="gift-next" onclick="' + _go(tab.address(nxt))
                + '">Continue</button>' + _SCRIPT, 'gift')


_DONE = page("""<h1>Thank you</h1><p>Ship to <span id="ship"></span>, card <span id="card"></span>.</p>
<script>
var got = new URLSearchParams(location.search);
document.getElementById('ship').textContent = got.get('ship') || '';
document.getElementById('card').textContent = got.get('card') || '';
</script>""", 'done')

_PAGES = {'shipping': _shipping, 'payment': _payment, 'review': _review, 'upsell': _upsell,
          'gift': _gift}


def _path(i):
    return '/checkout/step-' + 'abcdefg'[i]


def _checkout(tab, *order, field='ship-name'):
    """Serve a checkout whose pages come in this order, and open its first page."""
    paths = [_path(i) for i in range(len(order))] + ['/checkout/done']
    pages = {'/checkout/done': _DONE}
    for i, name in enumerate(order):
        pages[paths[i]] = _PAGES[name](tab, paths[i + 1], paths[i], field)
    tab.serve(pages)
    tab.visit(paths[0])


# --- the user's script, written once -----------------------------------------------------

def _fill(tab, target, css, value):
    if tab.family == 'playwright':
        target.fill(css, value)
    else:
        element = target.find_element('css selector', css)
        element.clear()
        element.send_keys(value)


def _click(tab, target, css):
    if tab.family == 'playwright':
        target.click(css)
    else:
        target.find_element('css selector', css).click()


def _place_order(tab):
    """The review step's landmark: Playwright's text engine, or an XPath on Selenium."""
    return 'text=Place order' if tab.family == 'playwright' else (
        "//button[normalize-space()='Place order']")


def _steps(heal, tab, *, card=CARD, upsell=False):
    """The approved example: shipping, payment and review, and an optional upsell."""

    @heal.step('shipping', "//h1[normalize-space()='Shipping address']")
    def shipping(target):
        _fill(tab, target, '#ship-name', 'Ada Lovelace')
        _click(tab, target, '#next')

    @heal.step('payment', ["//h1[normalize-space()='Payment']", '#card-number'])
    def payment(target):
        _fill(tab, target, '#card-number', card)
        _click(tab, target, '#next')

    if upsell:
        @heal.step('upsell', "//h1[normalize-space()='Protect your order']", optional=True)
        def no_insurance(target):
            _click(tab, target, '#skip-insurance')

    @heal.step('review', _place_order(tab), last=True)
    def review(target):
        _click(tab, target, '#place')


def _open(tab, provider, store_file, **kwargs):
    kwargs.setdefault('timeout_ms', FAST_MS)
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def _run(heal, timeout_ms=WAIT_MS):
    return heal.run_steps(timeout_ms=timeout_ms, poll_ms=POLL_MS)


def _ship_key(tab):
    """The key a step's '#ship-name' lookup is saved under: heal.page's selector, or
    heal.driver's '<by>=<value>'."""
    return '#ship-name' if tab.family == 'playwright' else 'css selector=#ship-name'


def _step_events(store_file):
    return [(e['key'], e.get('position'), e.get('registered'), e['outcome'])
            for e in journal_events(store_file) if e['event'] == 'flow.step']


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == 'mendwright' and r.levelno == logging.WARNING]


# --- tests ------------------------------------------------------------------------------------------

def test_the_same_script_runs_the_checkout_in_either_order(tab, store_file, stub_provider,
                                                           caplog):
    provider = stub_provider(None)
    for order in (('shipping', 'payment', 'review'), ('payment', 'shipping', 'review')):
        caplog.clear()
        _checkout(tab, *order)
        heal = _open(tab, provider, store_file)
        _steps(heal, tab)

        assert _run(heal) == list(order)

        assert tab.text('#ship') == 'Ada Lovelace'
        assert tab.text('#card') == CARD
        assert heal.heals == []
    assert _warnings(caplog) == [
        "mendwright: ran 'payment' before 'shipping': the site changed the order of the steps"]
    assert _step_events(store_file)[3:] == [('payment', 1, 2, 'ran'), ('shipping', 2, 1, 'ran'),
                                            ('review', 3, 3, 'ran')]
    assert provider.bundles == []


def test_a_one_page_checkout_runs_both_forms_then_places_the_order(tab, store_file,
                                                                    stub_provider):
    done = tab.address('/checkout/done')
    one = page('<h1>Shipping address</h1><label for="ship-name">Full name</label>'
               '<input id="ship-name"><h1>Payment</h1><label for="card-number">Card number'
               '</label><input id="card-number">'
               '<button type="button" id="place" onclick="'
               + _go(done, ('ship', 'ship-name'), ('card', 'card-number'))
               + '">Place order</button>' + _SCRIPT, 'one page')
    tab.serve({'/checkout': one, '/checkout/done': _DONE})
    tab.visit('/checkout')
    heal = _open(tab, stub_provider(None), store_file)

    @heal.step('review', _place_order(tab), last=True)    # registered first, still runs last
    def review(target):
        _click(tab, target, '#place')

    @heal.step('shipping', "//h1[normalize-space()='Shipping address']")
    def shipping(target):
        _fill(tab, target, '#ship-name', 'Ada Lovelace')

    @heal.step('payment', '#card-number')
    def payment(target):
        _fill(tab, target, '#card-number', CARD)

    assert _run(heal) == ['shipping', 'payment', 'review']
    assert (tab.text('#ship'), tab.text('#card')) == ('Ada Lovelace', CARD)


def test_a_declined_card_stops_the_flow_instead_of_paying_again(tab, store_file, stub_provider):
    _checkout(tab, 'shipping', 'payment', 'review')
    heal = _open(tab, stub_provider(None), store_file)
    _steps(heal, tab, card=DECLINED)

    with pytest.raises(AttentionRequired) as caught:
        _run(heal, STOP_MS)

    message = str(caught.value)
    assert message.startswith("mendwright: 'payment' is on the page again after it ran, and no "
                              'step that has not run showed on /')
    assert 'step-b' in message.split(' within ')[0]
    assert 'its submit probably failed' in message
    assert "Steps that ran: 'shipping', 'payment'." in message
    assert tab.text('#error') == 'Your card was declined.'
    assert [e[0] for e in _step_events(store_file)] == ['shipping', 'payment']


def test_a_step_nobody_registered_stops_the_flow_and_names_the_page(tab, store_file,
                                                                    stub_provider):
    _checkout(tab, 'shipping', 'gift', 'payment', 'review')
    heal = _open(tab, stub_provider(None), store_file)
    _steps(heal, tab)

    with pytest.raises(AttentionRequired) as caught:
        _run(heal, STOP_MS)

    message = str(caught.value)
    assert message.startswith('mendwright: no step showed on /')
    assert 'step-b within ' in message or 'step-b.html within ' in message
    assert ("mendwright looked for 'payment' (//h1[normalize-space()='Payment'], #card-number) "
            "and 'review' (" + _place_order(tab) + ')') in message
    assert "Steps that ran: 'shipping'." in message
    assert 'add it with heal.step()' in message
    assert tab.js("document.getElementById('wrap').checked") is False   # nothing guessed


def test_an_optional_upsell_that_shows_only_sometimes(tab, store_file, stub_provider, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    provider = stub_provider(None)
    _checkout(tab, 'shipping', 'payment', 'upsell', 'review')
    heal = _open(tab, provider, store_file)
    _steps(heal, tab, upsell=True)
    assert _run(heal) == ['shipping', 'payment', 'upsell', 'review']

    caplog.clear()
    _checkout(tab, 'shipping', 'payment', 'review')
    heal = _open(tab, provider, store_file)
    _steps(heal, tab, upsell=True)
    assert _run(heal) == ['shipping', 'payment', 'review']
    assert tab.text('#card') == CARD
    assert _warnings(caplog) == []
    assert ("mendwright: step 'upsell' did not show, and it is optional (optional=True)"
            in [r.getMessage() for r in caplog.records])


def test_a_renamed_field_in_a_reordered_step_heals_on_its_new_page(tab, store_file,
                                                                   stub_provider, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    _checkout(tab, 'shipping', 'payment', 'review')
    first = _open(tab, stub_provider(None), store_file, fingerprint=False)
    _steps(first, tab)
    assert _run(first) == ['shipping', 'payment', 'review']

    # The site now asks for the card first, and its shipping form's field has a new id, on
    # the page where the payment form used to be.
    _checkout(tab, 'payment', 'shipping', 'review', field='full-name')
    provider = stub_provider("//input[@id='full-name']")

    # Outside run_steps() the page guard declines the lookup there, as before.
    tab.visit(_path(1))
    outside = _open(tab, provider, store_file, fingerprint=False)
    with pytest.raises(AttentionRequired, match='was last found on'):
        _fill(tab, outside.page if tab.family == 'playwright' else outside.driver,
              '#ship-name', 'Ada Lovelace')
    assert provider.bundles == []

    tab.visit(_path(0))
    heal = _open(tab, provider, store_file, fingerprint=False)
    _steps(heal, tab)
    assert _run(heal) == ['payment', 'shipping', 'review']

    assert (tab.text('#ship'), tab.text('#card')) == ('Ada Lovelace', CARD)
    assert [(h.key, h.new) for h in heal.heals] == [(_ship_key(tab), "//input[@id='full-name']")]
    assert len(provider.bundles) == 1
    scope = [r.getMessage() for r in caplog.records
             if 'the page guard lets' in r.getMessage()]
    assert len(scope) == 1
    assert ("it was found during step 'shipping' before (last on " in scope[0]
            and "'shipping' is on the page now" in scope[0])


_CONSENT_WALL = """<div id="consent-wall" role="dialog" aria-label="Cookie consent"
  style="position: fixed; inset: 0; z-index: 20; background: #fff; padding: 40px">
  <p>We use cookies.</p>
  <button type="button" id="cookie-reject" onclick="document.getElementById('consent-wall').remove();
    document.getElementById('shop').style.display = 'block'">Reject all</button>
</div>"""

_NEWSLETTER = """<div id="nl" role="dialog" aria-modal="true" aria-label="Newsletter"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">
  <div style="background: #fff; width: 320px; margin: 140px auto; padding: 16px">
    <p>Get 10% off your first order.</p>
    <button type="button" id="nl-subscribe" onclick="document.title = 'subscribed'">Subscribe</button>
    <button type="button" id="nl-no" onclick="document.getElementById('nl').remove()">No thanks</button>
  </div>
</div>"""


def test_a_consent_wall_and_a_popup_nobody_registered_during_the_flow(tab, store_file,
                                                                      stub_provider):
    # The first page hides the checkout behind a consent wall until it is answered, and the
    # payment page opens a newsletter popup over the card field.
    _checkout(tab, 'payment', 'shipping', 'review')
    paths = [_path(0), _path(1), _path(2)]
    payment = _payment(tab, paths[1], paths[0])
    payment = payment.replace('<body>', '<body>' + _CONSENT_WALL + '<div id="shop" '
                              'style="display: none">', 1).replace('</body>', '</div>'
                                                                   + _NEWSLETTER + '</body>', 1)
    tab.serve({paths[0]: payment})
    tab.visit(paths[0])
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    heal.interrupt('cookies', '#cookie-reject')
    _steps(heal, tab)

    assert _run(heal) == ['payment', 'shipping', 'review']

    assert (tab.text('#ship'), tab.text('#card')) == ('Ada Lovelace', CARD)
    events = heal.handled_interruptions
    assert [(e.key, e.before) for e in events][0] == ('cookies', WAITING_STEP)
    popups = [e for e in events if e.key == DISMISS_KEY]
    assert len(popups) == 1 and popups[0].label == 'No thanks'
    assert tab.js('document.title') != 'subscribed'
    assert heal.heals == []
