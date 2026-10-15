"""List heals on real browsers, on every engine (see conftest.py).

The list fingerprint a browser records (LIST_JS, one round trip) must be the one lxml
computes from the captured HTML, or a heal would score the page differently from how the
list was recorded. Then a product grid whose card class changed heals as a list, with no
provider, through heal.page.locator(selector).all() on Playwright and
heal.driver.find_elements() on Selenium, and through heal.locator_all() on both; and a list
that would heal onto one element is refused.
"""

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, lists
from mendwright.drivers import wrap
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


def _open(tab, store_file, **kwargs):
    return mendwright.open(tab.target, provider=None, store_path=store_file,
                           timeout_ms=FAST_MS, **kwargs)


def _texts(tab, items):
    """The h3 of each card, read through the element mendwright returned."""
    if tab.family == SELENIUM:
        return [el.find_element('css selector', 'h3').text for el in items]
    return [item.locator('h3').inner_text() for item in items]


def _all(tab, heal, css):
    """The zero-change list lookup: heal.page.locator(css).all() or
    heal.driver.find_elements(By.CSS_SELECTOR, css)."""
    if tab.family == SELENIUM:
        return heal.driver.find_elements('css selector', css)
    return heal.page.locator(css).all()


def _key(tab, css):
    return 'css selector=' + css if tab.family == SELENIUM else css


def test_the_browser_records_what_lxml_computes_for_a_list(tab):
    tab.load(SHOP_V1)
    driver = wrap(tab.target)
    live = driver.list_fingerprint('.product-card')
    captured = lxml.html.fromstring(driver.outer_html()).xpath(
        "//div[contains(@class, 'product-card')]")
    assert live == lists.of_elements(captured)
    assert live['count'] == 4 and live['container']['id'] == 'grid'
    assert driver.list_fingerprint('h1') is None          # one element is no list
    items = driver.list_items('.product-card')
    assert [i['role'] for i in items] == ['div'] * 4 and all(i['visible'] for i in items)


def test_list_items_tells_hidden_items_apart(tab):
    tab.load(shop(card('product-card', *PRODUCTS[0])
                  + card('product-card hidden', *PRODUCTS[1]).replace('<div ', '<div style="display:none" ', 1)))
    items = wrap(tab.target).list_items('.product-card')
    assert [i['visible'] for i in items] == [True, False]


def test_a_grid_whose_card_class_changed_heals_as_a_list(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    assert _texts(tab, _all(tab, heal, '.product-card')) == NAMES

    tab.load(SHOP_V2)
    cards = _all(tab, heal, '.product-card')
    assert _texts(tab, cards) == NAMES
    key = _key(tab, '.product-card')
    assert [(h.key, h.new, h.source) for h in heal.heals] == [(key, TILE, 'fingerprint')]
    saved = saved_candidates(store_file, key)
    assert saved[TILE]['css'] == 'div.product-tile'      # the code's selector is CSS
    applied = [e for e in journal_events(store_file) if e['event'] == 'heal.applied']
    assert [(e['list'], e['items']) for e in applied] == [(True, 4)]

    # The next run finds the saved heal, with no heal and nothing asked.
    again = _open(tab, store_file)
    assert _texts(tab, _all(tab, again, '.product-card')) == NAMES
    assert again.heals == []


def test_heal_locator_all_heals_and_its_items_act(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    assert len(heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)) == 4
    tab.load(SHOP_V2)
    cards = heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)
    assert len(cards) == 4
    cards[2].click()
    assert tab.text('#out') == 'Green mug'
    assert heal.pending() == {'cards': TILE}            # an item's action confirms nothing
    heal.confirm_all(True)
    assert heal.heals[0].confirmed is True


def test_a_list_selector_that_would_heal_to_one_element_is_refused(tab, store_file):
    tab.load(SHOP_V1)
    heal = _open(tab, store_file)
    heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)
    tab.load(shop(card('product-tile', *PRODUCTS[0]) + '<div class="banner"><p>Sale!</p></div>'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)
    message = str(info.value)
    assert "the list fingerprint match for 'cards' was not confident" in message
    assert ("The only element like the recorded items is one <div> ('Blue mug$12.00Add to "
            "cart'); mendwright never heals a list onto one element") in message
    assert heal.heals == []


def test_a_list_never_seen_with_items_is_empty_at_once(tab, store_file):
    tab.load(SHOP_V2)
    heal = mendwright.open(tab.target, provider=None, store_path=store_file, timeout_ms=60000)
    assert _all(tab, heal, '.product-card') == []
    assert heal.locator_all('cards', '.product-card') == []
