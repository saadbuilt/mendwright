"""Lists on real browsers, on every engine (see conftest.py).

The list fingerprint a browser records (LIST_JS, one round trip) must be the one lxml
computes from the captured HTML. heal.locator_all() never heals a list whose selectors all
match nothing: a product grid whose card class changed raises, saying that either the list
is empty at this point or its selector broke, and so do an emptied list with another list in
its place (recommendations where search results were, a form's hints where its errors were)
and a list that is still loading once timeout_ms ran out. A heal of one card never answers
for the list, and the reads of a list's elements are never checked.
heal.page.locator(selector).all() and heal.driver.find_elements() give what the real call
gives, as in 0.7.0, and never heal.
"""

import json
import time
from pathlib import Path

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, lists
from mendwright.drivers import wrap
from mendwright.proxies import selenium_selector
from mw_engines import FAST_MS, POLL_MS, SELENIUM, journal_events, page, saved_candidates

pytestmark = pytest.mark.browser

PRODUCTS = [('Blue mug', '$12.00'), ('Red mug', '$13.00'), ('Green mug', '$14.00'),
            ('Tea pot', '$29.00')]


def card(cls, name, price):
    return ('<div class="' + cls + '" onclick="document.getElementById(\'out\').textContent = '
            "'" + name + "'\"><img alt='" + name + "' width='40' height='40'><h3>" + name
            + '</h3><span class="price">' + price + '</span><button type="button">Add to cart'
            '</button></div>')


def shop(cards, extra=''):
    return page('<header><nav><a href="/">Home</a><a href="/cart">Cart</a></nav></header>'
                '<main id="content"><h1>Shop</h1><div id="grid" class="grid">' + cards
                + '</div>' + extra + '<p id="out">idle</p></main>', 'shop')


def grid(cls):
    return ''.join(card(cls, n, p) for n, p in PRODUCTS)


SHOP_V1 = shop(grid('product-card'))
SHOP_V2 = shop(grid('product-tile'))
TILE = "//div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]"
NAMES = [n for n, _ in PRODUCTS]
EMPTY_OR_BROKEN = 'the list is empty at this point, or its selector broke'


def _open(tab, store_file, **kwargs):
    return mendwright.open(tab.target, provider=None, store_path=store_file,
                           timeout_ms=FAST_MS, **kwargs)


def _texts(tab, items):
    """The h3 of each card, read through the element mendwright returned."""
    if tab.family == SELENIUM:
        return [el.find_element('css selector', 'h3').text for el in items]
    return [item.locator('h3').inner_text() for item in items]


def _key(tab, css):
    """The key heal.page or heal.driver saves css under."""
    return 'css selector=' + css if tab.family == SELENIUM else css


def _all(tab, heal, css):
    """heal.locator_all() under the key and selector of heal.page.locator(css), or of
    heal.driver.find_element(By.CSS_SELECTOR, css)."""
    selector = selenium_selector('css selector', css) if tab.family == SELENIUM else css
    return heal.locator_all(_key(tab, css), selector, poll_ms=POLL_MS)


def _real(tab, heal, css):
    """heal.page.locator(css).all() or heal.driver.find_elements(By.CSS_SELECTOR, css)."""
    if tab.family == SELENIUM:
        return heal.driver.find_elements('css selector', css)
    return heal.page.locator(css).all()


def test_the_browser_records_what_lxml_computes_for_a_list(tab):
    tab.load(SHOP_V1)
    driver = wrap(tab.target)
    live = driver.list_fingerprint('.product-card')
    captured = lxml.html.fromstring(driver.outer_html()).xpath(
        "//div[contains(@class, 'product-card')]")
    assert live == lists.of_elements(captured)
    assert live['count'] == 4 and live['container']['id'] == 'grid'
    assert driver.list_fingerprint('h1') is None          # one element is no list


def test_a_grid_whose_card_class_changed_is_not_healed(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    assert _texts(tab, _all(tab, heal, '.product-card')) == NAMES

    tab.load(SHOP_V2)
    with pytest.raises(AttentionRequired) as info:
        _all(tab, heal, '.product-card')
    assert ("'" + _key(tab, '.product-card') + "' matched 4 elements when its list fingerprint "
            'was recorded, and none of its selectors matches now. Either ' + EMPTY_OR_BROKEN
            + ':') in str(info.value)
    key = _key(tab, '.product-card')
    assert heal.heals == []
    assert [x for x, c in saved_candidates(store_file, key).items() if c['source'] == 'heal'] == []
    # Nothing was looked for: the only heal events are the declines, which say why.
    assert {(e['event'], e['code']) for e in journal_events(store_file)
            if e['event'].startswith('heal.')} == {('heal.declined', 'list_empty')}

    # The next run raises again; fixed in the code, the list is found.
    again = _open(tab, store_file)
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        _all(tab, again, '.product-card')
    assert _texts(tab, _all(tab, again, '.product-tile')) == NAMES
    assert again.heals == []


def test_the_items_of_heal_locator_all_act_and_confirm_nothing(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    cards = heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)
    assert len(cards) == 4
    cards[2].click()
    assert tab.text('#out') == 'Green mug'
    cards[1].confirm(False)
    assert heal.pending() == {} and heal.confirm_all(True) == 0 and heal.heals == []


def test_a_list_never_seen_with_items_is_empty_at_once(tab, store_file):
    tab.load(SHOP_V2)
    heal = mendwright.open(tab.target, provider=None, store_path=store_file, timeout_ms=60000)
    assert _real(tab, heal, '.product-card') == []
    assert heal.locator_all('cards', '.product-card') == []


def test_heal_page_and_heal_driver_list_calls_are_the_real_ones(tab, store_file, stub_provider):
    # As in 0.7.0: no wait, no heal, no model and nothing saved, so a script that waits for
    # the cards to go away, or checks that a cart is empty, works as before.
    tab.load(SHOP_V1)
    provider = stub_provider(TILE)
    heal = mendwright.open(tab.target, provider=provider, store_path=store_file,
                           timeout_ms=60000)
    assert _texts(tab, _real(tab, heal, '.product-card')) == NAMES
    tab.load(SHOP_V2)
    started = time.monotonic()
    assert _real(tab, heal, '.product-card') == []
    assert time.monotonic() - started < 5
    assert heal.heals == [] and provider.bundles == []
    assert not Path(store_file).exists()                  # nothing saved
    # Not even for a key heal.locator_all() recorded as a list.
    tab.load(SHOP_V1)
    assert len(_all(tab, heal, '.product-card')) == 4
    tab.load(SHOP_V2)
    started = time.monotonic()
    assert _real(tab, heal, '.product-card') == []
    assert time.monotonic() - started < 5 and heal.heals == []


# --- what is not the list ------------------------------------------------------------------

def results(cls, names):
    cards = ''.join('<div class="' + cls + '"><img alt="' + n + '" width="40" height="40"><h3>'
                    + n + '</h3><span class="price">$9.00</span></div>' for n in names)
    return page('<main id="search"><h1>Search</h1><div id="results">' + cards + '</div></main>',
                'search')


MUGS = ['Blue mug', 'Red mug', 'Green mug', 'Mug set']
POPULAR = ['Tea pot', 'Kettle', 'Coffee grinder', 'French press']


def test_a_search_with_no_results_is_not_healed_onto_its_recommendations(tab, store_file):
    tab.load(results('result-card', MUGS))
    heal = _open(tab, store_file)
    assert len(_all(tab, heal, '#results .result-card')) == 4
    tab.load(results('popular-card', POPULAR))
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        _all(tab, heal, '#results .result-card')
    assert heal.heals == []
    key = _key(tab, '#results .result-card')
    assert [x for x, c in saved_candidates(store_file, key).items() if c['source'] == 'heal'] == []


def form(errors):
    fields = ''
    for name, hint, error in zip(('email', 'password'),
                                 ('We never share your email', '8 characters or more'),
                                 errors or (None, None), strict=True):
        fields += ('<div class="field"><label for="' + name + '">' + name + '</label><input id="'
                   + name + '" name="' + name + '"><span class="hint">' + hint + '</span>'
                   + ('<span class="error">' + error + '</span>' if error else '') + '</div>')
    return page('<form id="signup">' + fields + '<button type="button">Sign up</button></form>',
                'signup')


def test_an_emptied_error_list_is_not_healed_onto_the_hints(tab, store_file):
    tab.load(form(('Email is required', 'Password is required')))
    heal = _open(tab, store_file)
    assert len(_all(tab, heal, '.error')) == 2
    tab.load(form(None))                                  # the errors went away
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        _all(tab, heal, '.error')
    assert heal.heals == []


SKELETON = ('<div class="card-skeleton"><img width="40" height="40"><h3></h3><span></span>'
            '<button type="button" disabled></button></div>')
# The grid shows skeleton cards, and the real cards a moment later, as a slow API answers.
LOADING = shop(SKELETON * 4) + (
    '<script>setTimeout(() => { document.getElementById("grid").innerHTML = '
    + json.dumps(grid('product-card')) + '; }, 3000);</script>')


def test_a_list_that_is_still_loading_is_not_healed_onto_its_skeletons(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    assert len(_all(tab, heal, '.product-card')) == 4
    tab.load(LOADING)
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        _all(tab, heal, '.product-card')                  # timeout_ms ran out first
    if tab.family == SELENIUM:
        # find_elements() gives [] until the cards are there, so WebDriverWait polls.
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(heal.driver, 10, poll_frequency=0.1).until(
            lambda d: d.find_elements('css selector', '.product-card'))
    else:
        tab.target.wait_for_selector('.product-card')
    cards = _all(tab, heal, '.product-card')
    assert _texts(tab, cards) == NAMES
    # With a timeout_ms longer than the load, the lookup waits for the cards.
    tab.load(LOADING)
    selector = (selenium_selector('css selector', '.product-card') if tab.family == SELENIUM
                else '.product-card')
    cards = heal.locator_all(_key(tab, '.product-card'), selector, timeout_ms=10000,
                             poll_ms=POLL_MS)
    assert _texts(tab, cards) == NAMES
    assert heal.heals == []
    key = _key(tab, '.product-card')
    assert [x for x, c in saved_candidates(store_file, key).items() if c['source'] == 'heal'] == []
    # Nothing was looked for: the only heal events are the declines, which say why.
    assert {(e['event'], e['code']) for e in journal_events(store_file)
            if e['event'].startswith('heal.')} == {('heal.declined', 'list_empty')}


def test_a_heal_of_one_card_never_answers_for_the_list(tab, store_file, stub_provider):
    tab.load(SHOP_V1)
    provider = stub_provider(None)
    heal = mendwright.open(tab.target, provider=provider, store_path=store_file,
                           timeout_ms=FAST_MS)
    assert len(_all(tab, heal, '.product-card')) == 4
    tab.load(SHOP_V2)
    provider.xpath = "//div[contains(@class, 'product-tile')][h3='Red mug']"
    if tab.family == SELENIUM:
        heal.driver.find_element('css selector', '.product-card').click()
    else:
        heal.page.click('.product-card')
    assert tab.text('#out') == 'Red mug'                  # a heal of one card, by the model
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        _all(tab, heal, '.product-card')                  # not through the card's heal
    assert [h.source for h in heal.heals] == ['model'] and len(provider.bundles) == 1
    saved = saved_candidates(store_file, _key(tab, '.product-card'))
    assert 'list' not in saved[provider.xpath]


def _read(tab, item):
    return item.text if tab.family == SELENIUM else item.inner_text()


def test_the_reads_of_a_list_are_never_checked(tab, store_file):
    # No list is found through a heal, so its elements' reads have nothing to be checked for.
    tab.load(SHOP_V1)
    key = _key(tab, '.product-card')
    heal = _open(tab, store_file, expect_values={key: 'money'})
    assert 'Blue mug' in _read(tab, _all(tab, heal, '.product-card')[0])
    assert 'Blue mug' in _read(tab, _real(tab, heal, '.product-card')[0])
    tab.load(SHOP_V2)
    assert 'Blue mug' in _read(tab, heal.locator_all(key, TILE, poll_ms=POLL_MS)[0])
    assert heal.heals == []
