"""List heals on real browsers, on every engine (see conftest.py).

The list fingerprint a browser records (LIST_JS, one round trip) must be the one lxml
computes from the captured HTML, or a heal would score the page differently from how the
list was recorded. Then a product grid whose card class changed heals as a list, with no
provider, through heal.page.locator(selector).all() on Playwright and
heal.driver.find_elements() on Selenium, and through heal.locator_all() on both; and a list
that would heal onto one element is refused. So is another list in an emptied list's place
(recommendations where search results were, a form's hints where its errors were), and the
skeleton cards of a list that is still loading. A heal of one card never answers for the
list, and the reads of a healed list are checked.
"""

import json
import re

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
    with pytest.raises(AttentionRequired) as info:
        _all(tab, heal, '#results .result-card')
    assert ('shares no id, test id, name, link, label or text with the items that were '
            'recorded') in str(info.value)
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
    with pytest.raises(AttentionRequired, match='shares no id, test id, name, link, label or '
                                                'text'):
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
    if tab.family == SELENIUM:
        # find_elements() raises a NoSuchElementException while it cannot find the list,
        # so WebDriverWait goes on polling until the cards are there.
        from selenium.webdriver.support.ui import WebDriverWait
        cards = WebDriverWait(heal.driver, 10, poll_frequency=0.1).until(
            lambda d: d.find_elements('css selector', '.product-card'))
    else:
        with pytest.raises(AttentionRequired, match='so the list may still be loading'):
            heal.page.locator('.product-card').all()
        tab.target.wait_for_selector('.product-card')
        cards = heal.page.locator('.product-card').all()
    assert _texts(tab, cards) == NAMES
    assert heal.heals == []
    key = _key(tab, '.product-card')
    saved = saved_candidates(store_file, key)
    assert [x for x, c in saved.items() if c['source'] == 'heal'] == []
    events = [e for e in journal_events(store_file) if e.get('key') == key]
    assert any(e['event'] == 'heal.requested' and e.get('list') for e in events)  # and declined
    assert not any(e['event'] == 'heal.applied' for e in events)


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
    assert _texts(tab, _all(tab, heal, '.product-card')) == NAMES
    assert sorted(h.source for h in heal.heals) == ['fingerprint', 'model']
    saved = saved_candidates(store_file, _key(tab, '.product-card'))
    assert saved[TILE]['list'] is True and 'list' not in saved[provider.xpath]


def _read(tab, item):
    return item.text if tab.family == SELENIUM else item.inner_text()


def test_the_reads_of_a_healed_list_are_checked(tab, store_file):
    tab.load(SHOP_V1)
    key = _key(tab, '.product-card')
    heal = _open(tab, store_file, expect_values={key: 'money'})
    assert 'Blue mug' in _read(tab, _all(tab, heal, '.product-card')[0])   # no heal: unchecked
    assert len(heal.locator_all('cards', '.product-card', poll_ms=POLL_MS)) == 4
    tab.load(SHOP_V2)
    cards = heal.locator_all('cards', '.product-card', poll_ms=POLL_MS,
                             expect_value=re.compile('mug|pot'))
    assert 'Blue mug' in _read(tab, cards[0]) and 'Tea pot' in _read(tab, cards[3])
    assert heal.pending()['cards'] == TILE               # a read that passes confirms nothing
    items = _all(tab, heal, '.product-card')             # healed too, and checked for money
    with pytest.raises(AttentionRequired, match="'" + re.escape(key) + "' was healed to "
                       + re.escape(TILE) + ', and its ' + ('text' if tab.family == SELENIUM
                                                           else r'inner_text\(\)')):
        _read(tab, items[0])
    assert saved_candidates(store_file, key)[TILE]['status'] == 'quarantined'
