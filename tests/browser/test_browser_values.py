"""expect_value= on real browsers, on every engine (see conftest.py): a read through a heal
must return what the key's check accepts. A price that heals onto the rating next to it is
refused and quarantined; a correct heal passes and is confirmed; a read through the selector
in your code is never checked. The texts are the browser's own, with its non-breaking and
narrow spaces, so the locale-tolerant parsing runs on what a scraper really gets."""

from decimal import Decimal

import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright.values import parse_money
from mw_engines import FAST_MS, POLL_MS, SELENIUM, journal_events, page

pytestmark = pytest.mark.browser

PRODUCT_V1 = page("""
<main id="product"><h1>Blue mug</h1>
  <span id="price">1&#8239;299,00&nbsp;€</span>
  <span class="rating">4.5</span>
  <span class="reviews">(128 reviews)</span>
</main>""", 'product v1')
PRODUCT_V2 = PRODUCT_V1.replace('id="price"', 'class="amount"')
RATING = "//span[@class='rating']"
AMOUNT = "//span[@class='amount']"


def _open(tab, provider, store_file, **kwargs):
    return mendwright.open(tab.target, provider=provider, store_path=store_file,
                           fingerprint=False, timeout_ms=FAST_MS, **kwargs)


def _read(tab, proxy):
    """What a scraper reads from the element: inner_text() on Playwright, .text on Selenium."""
    return proxy.text if tab.family == SELENIUM else proxy.inner_text()


def test_a_price_read_that_heals_onto_a_wrong_number_is_refused(tab, store_file, stub_provider):
    tab.load(PRODUCT_V1)
    provider = stub_provider(RATING)            # a model that picked the rating
    heal = _open(tab, provider, store_file)
    price = heal.locator('price', '#price', role='span', expect_value='money', poll_ms=POLL_MS)
    assert parse_money(_read(tab, price)) == Decimal('1299.00')

    tab.load(PRODUCT_V2)
    price = heal.locator('price', '#price', role='span', expect_value='money', poll_ms=POLL_MS)
    with pytest.raises(AttentionRequired) as info:
        _read(tab, price)
    message = str(info.value)
    assert "'price' was healed to " + RATING in message
    assert "there is '4.5', which is not money (expect_value='money')" in message
    assert heal.store.status('price', RATING) == 'quarantined'
    assert provider.outcomes == [('price', RATING, False)]
    canary = [e for e in journal_events(store_file) if e['event'] == 'heal.canary']
    assert [(e['success'], e['reason']) for e in canary] == [
        (False, 'expect_value: ' + ('text' if tab.family == SELENIUM else 'inner_text()')
         + ' is not money')]

    # The next lookup heals again, and the quarantine keeps the rating out of it.
    with pytest.raises(AttentionRequired, match='previously quarantined'):
        heal.locator('price', '#price', role='span', poll_ms=POLL_MS)


def test_a_correct_heal_passes_and_is_confirmed(tab, store_file, stub_provider):
    tab.load(PRODUCT_V1)
    provider = stub_provider(AMOUNT)
    heal = _open(tab, provider, store_file)
    heal.locator('price', '#price', role='span')
    tab.load(PRODUCT_V2)
    price = heal.locator('price', '#price', role='span', expect_value='money', poll_ms=POLL_MS)
    text = _read(tab, price)
    assert ' ' in text      # the browser's own narrow space (Selenium's text turns the
                                 # non-breaking one into a space)
    assert parse_money(text) == Decimal('1299.00')
    assert heal.heals[0].confirmed is True and heal.pending() == {}
    assert provider.outcomes == [('price', AMOUNT, True)]


def test_an_unhealed_read_is_never_blocked(tab, store_file, stub_provider):
    tab.load(PRODUCT_V1.replace('1&#8239;299,00&nbsp;€', 'Call for price'))
    heal = _open(tab, stub_provider(None), store_file, expect_values={'price': 'money'})
    price = heal.locator('price', '#price', role='span')
    assert _read(tab, price) == 'Call for price'


@pytest.mark.engine_family('playwright')
def test_heal_page_reads_take_the_open_level_check(tab, store_file, stub_provider):
    tab.load(PRODUCT_V1)
    heal = _open(tab, stub_provider(RATING), store_file, expect_values={'#price': 'money'})
    shop = heal.page
    assert parse_money(shop.text_content('#price')) == Decimal('1299.00')
    tab.load(PRODUCT_V2)
    with pytest.raises(AttentionRequired, match=r"its text_content\(\) there is '4.5'"):
        shop.text_content('#price')
    assert heal.store.status('#price', RATING) == 'quarantined'


@pytest.mark.engine_family('selenium')
def test_heal_driver_reads_take_the_open_level_check(tab, store_file, stub_provider):
    from selenium.webdriver.common.by import By
    tab.load(PRODUCT_V1)
    heal = _open(tab, stub_provider(RATING), store_file,
                 expect_values={'css selector=#price': 'money'})
    driver = heal.driver
    assert parse_money(driver.find_element(By.CSS_SELECTOR, '#price').text) == Decimal('1299.00')
    tab.load(PRODUCT_V2)
    healed = driver.find_element(By.CSS_SELECTOR, '#price')
    with pytest.raises(AttentionRequired, match="its text there is '4.5'"):
        _ = healed.text
    assert heal.store.status('css selector=#price', RATING) == 'quarantined'
