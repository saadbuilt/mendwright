"""Fingerprint healing on real browsers, on every engine (see conftest.py).

The fingerprint a browser records (FINGERPRINT_JS, one round trip) must be the one lxml
computes from the captured HTML, or a heal would score the page differently from how the
element was recorded. Then a redesign heals from the fingerprint alone, with no provider.
"""

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, HealEvent
from mendwright.drivers import wrap
from mendwright.fingerprint import of_element
from mw_engines import (FAST_MS, POLL_MS, SIGNUP_V1, SIGNUP_V2, STALE, V2_CHECKBOX,
                        journal_events, page, saved_entry)

pytestmark = pytest.mark.browser

FINGERPRINTED = "//input[@data-testid='legal-consent']"


def _open(tab, provider, store_file, **kwargs):
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


@pytest.mark.parametrize('xpath', [STALE, "//input[@id='email']", "//button[@id='next']",
                                   "//input[@id='password']"])
def test_the_browser_records_what_lxml_computes(tab, xpath):
    tab.load(SIGNUP_V1)
    driver = wrap(tab.target)
    live = driver.fingerprint(xpath)
    captured = lxml.html.fromstring(driver.outer_html()).xpath(xpath)[0]
    assert live == of_element(captured)
    assert live['tag'] in ('input', 'button')


def test_a_redesign_heals_from_the_fingerprint_with_no_model(tab, store_file, stub_provider):
    tab.load(SIGNUP_V1)
    provider = stub_provider(None)
    heal = _open(tab, provider, store_file)
    heal.locator('accept_terms', STALE, role='checkbox').check()
    assert saved_entry(store_file, 'accept_terms')['fingerprint']['label'] == \
        'I accept the terms and conditions'

    tab.load(SIGNUP_V2)
    box = heal.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                       poll_ms=POLL_MS)
    box.check(expect=lambda: tab.checked(V2_CHECKBOX))

    assert tab.checked(V2_CHECKBOX) is True
    assert provider.bundles == [] and provider.outcomes == []
    assert heal.heals == [HealEvent('accept_terms', (STALE,), FINGERPRINTED, True, 'fingerprint')]
    events = [(e['event'], e.get('source')) for e in journal_events(store_file)]
    assert events == [('heal.requested', None), ('heal.proposed', 'fingerprint'),
                      ('heal.applied', 'fingerprint'), ('heal.canary', None)]


def test_a_hidden_copy_does_not_stop_the_heal(tab, store_file, stub_provider):
    # A stylesheet hides the second button, which lxml cannot see. The live check drops it
    # as a rival, so the visible button heals.
    v1 = page("<form id='buy'><button type='button' id='add'>Add to cart</button></form>")
    v2 = page("<style>.sticky { display: none }</style>"
              "<form id='buy'><button type='button' onclick=\"document.getElementById('out')"
              ".textContent = 'added'\">Add to cart</button></form>"
              "<div class='sticky' data-testid='sticky-bar'><button type='button'>Add to cart"
              "</button></div><p id='out'>idle</p>")
    tab.load(v1)
    heal = _open(tab, stub_provider(None), store_file)
    heal.locator('add', '#add')
    tab.load(v2)
    heal.locator('add', '#add', timeout_ms=FAST_MS, poll_ms=POLL_MS).click()
    assert heal.heals[0].source == 'fingerprint'
    assert heal.heals[0].new == "//form[@id='buy']//button"
    assert tab.text('#out') == 'added'


def test_look_alikes_are_left_to_the_model(tab, store_file, stub_provider):
    v1 = page("<form><label><input type='checkbox' id='a'> Email me offers</label></form>")
    v2 = page("<form><label><input type='checkbox' name='x'> Email me offers</label>"
              "<label><input type='checkbox' name='y'> Email me offers</label></form>")
    tab.load(v1)
    heal = _open(tab, None, store_file)       # no provider at all: only the tier can heal
    heal.locator('offers', '#a')
    tab.load(v2)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('offers', '#a', timeout_ms=FAST_MS, poll_ms=POLL_MS)
    assert "the fingerprint match for 'offers' was not confident: two elements score alike" \
        in str(info.value)
    assert heal.heals == []
