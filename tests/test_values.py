"""expect_value=: what a read through a heal must return (mendwright.values). Offline: the
lxml-backed FakePage from conftest, a stub provider, and the fake Selenium driver of
test_proxies.py."""

import asyncio
import json
import re
from decimal import Decimal

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired
from mendwright.values import NAMES, expectation, expectations, parse_money, parse_number

from test_aio import AsyncFakeLocator, AsyncFakePage
from test_proxies import WebDriver, WebElement

# A product page before and after a redesign: the price lost its id, and a rating, a count
# and a delivery date sit next to it.
V1 = ("<main id='product'><h1>Blue mug</h1><span id='price'>$1,299.00</span>"
      "<span class='rating'>4.5</span><span class='reviews'>(128 reviews)</span>"
      "<span class='eta'>Oct 15, 2026</span><a class='seller' href='/s/acme'>Acme</a>"
      "<input id='qty' name='qty' value='2'></main>")
V2 = V1.replace("id='price'", "class='amount'")
RATING = "//span[@class='rating']"
AMOUNT = "//span[@class='amount']"


def _open(page, tmp_path, provider, **kwargs):
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'),
                           fingerprint=False, timeout_ms=0, **kwargs)


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


# --- parsing --------------------------------------------------------------------------------

@pytest.mark.parametrize('text, number', [
    ('1,299.00', '1299.00'), ('1.299,00', '1299.00'), ('1 299,00', '1299.00'),
    ('1 299,00', '1299.00'), ('1 299,00', '1299.00'), ("1'299.00", '1299.00'),
    ('1’299.50', '1299.50'), ('1,29,999.00', '129999.00'), ('1.299.000,5', '1299000.5'),
    ('1,299', '1299'), ('1.299', '1299'), ('0,299', '0.299'), ('1234.567', '1234.567'),
    ('12,5', '12.5'), ('-12', '-12'), ('+3.5', '3.5'), ('−12', '-12'),
    ('１２３', '123'), ('١٢٣', '123'), ('  42 ', '42'),
])
def test_numbers_as_locales_write_them(text, number):
    assert parse_number(text) == Decimal(number)


@pytest.mark.parametrize('text', ['12 34', '1 299.000,00', '1,2,3', 'abc', '12kg', '$5', '',
                                  '1.', '4.5 out of 5', None, 12])
def test_what_is_not_a_number(text):
    assert parse_number(text) is None


@pytest.mark.parametrize('text, amount', [
    ('$1,299.00', '1299.00'), ('1.299,00 €', '1299.00'), ('1 299,00 €', '1299.00'),
    ('129 kr', '129'), ('129 kr.', '129'), ('kr 129', '129'), ('129,-', '129'),
    ('129:-', '129'), ('12.–', '12'), ('129,- kr', '129'), ('CHF 12.50', '12.50'),
    ('USD 1299', '1299'), ('12 USD', '12'), ('$12.00 USD', '12.00'), ('US$ 12', '12'),
    ('R1,299.00', '1299.00'), ('Rs. 1,299', '1299'), ('₹1,29,999.00', '129999.00'),
    ('¥1,299', '1299'), ('£9.99', '9.99'), ('12 zł', '12'), ('1 290 Kč', '1290'),
    ('-$12', '-12'), ('$-12', '-12'), ('-12 €', '-12'), ('€0', '0'),
])
def test_money_as_locales_write_it(text, amount):
    assert parse_money(text) == Decimal(amount)


@pytest.mark.parametrize('text', ['1,299.00', '4.5', 'SKU 12345', '€', 'Free shipping',
                                  '$12.99 $15.99', '-$-12', '12 kg', '(128 reviews)', None])
def test_what_is_not_money(text):
    assert parse_money(text) is None


@pytest.mark.parametrize('name, good, bad', [
    ('money', ['$1,299.00', '1.299,00 €', '129 kr'], ['4.5', '1,299.00', 'Free']),
    ('number', ['1,299.00', '4.5', '-3'], ['$5', '12 kg', '(128 reviews)']),
    ('integer', ['1,299', '12', '-3'], ['12.00', '1.5', 'twelve']),
    ('percent', ['12%', '12.5 %', '-15%', '%20', '١٢٪'], ['12', '12 % off']),
    ('date', ['2026-10-15', '2026-10-15T14:30:00Z', '15/10/2026', '10/15/2026', '15.10.2026',
              'Oct 15, 2026', '15 October 2026', '15. Oktober 2026', '15 de octubre de 2026',
              '1er octobre 2026', 'Thu, 15 Oct 2026 14:30', 'Oct 15, 2026 at 2:30 PM',
              '2026年10月15日', 'October 2026', '15 Oct'],
     ['31/02/2026', '13/13/2026', 'In stock', '$12.00', '4.5', '2 days ago', 'Sold out']),
    ('email', ['ada@example.com', 'mailto:ada@example.com?subject=hi'], ['a@b', 'Contact']),
    ('url', ['https://shop.example/p/1', '/p/1', './p', '//cdn.example/x.png'],
     ['javascript:void(0)', '#', 'p/1', 'https:// spaced.example']),
])
def test_the_named_checks(name, good, bad):
    check = expectation(name)
    assert [check.passes(v) for v in good] == [True] * len(good)
    assert [check.passes(v) for v in bad] == [False] * len(bad)
    assert check.passes(None) is False


def test_a_regex_searches_and_a_callable_decides():
    sku = expectation(re.compile(r'^SKU-\d+$'))
    assert sku.passes('SKU-123') and not sku.passes('Item SKU-123')
    assert expectation(re.compile(r'SKU-\d+')).passes('Item SKU-123')

    def is_sku(text):
        return text.startswith('SKU-')
    check = expectation(is_sku)
    assert check.passes('SKU-1') and not check.passes('x')
    assert check.shown == 'expect_value=is_sku' and check.refusal == 'was refused by is_sku()'
    boom = expectation(lambda text: 1 / 0)
    assert boom.passes('x') is False and boom.refusal == 'was refused by the callable'


def test_arguments_that_can_never_work_raise():
    assert expectation(None) is None
    assert set(NAMES) == {'money', 'number', 'integer', 'percent', 'date', 'email', 'url'}
    assert expectation(' Money ').spec == 'money'
    with pytest.raises(ValueError, match="expect_value= for 'price' takes 'money'"):
        expectation('price', 'price')
    with pytest.raises(TypeError, match='bytes pattern'):
        expectation(re.compile(rb'\d'))
    with pytest.raises(TypeError, match='got int'):
        expectation(5)
    with pytest.raises(TypeError, match='a dict of keys'):
        expectations(['money'])
    with pytest.raises(TypeError, match='keys as strings'):
        expectations({1: 'money'})
    assert expectations({'#price': 'money', '#x': None}).keys() == {'#price'}


# --- reads through a heal -------------------------------------------------------------------

def test_a_read_that_heals_onto_the_wrong_number_is_refused(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    provider = stub_provider(RATING)                  # a model that picked the rating
    heal = _open(page, tmp_path, provider)
    assert heal.locator('price', '#price', role='span', expect_value='money').inner_text() \
        == '$1,299.00'                                # the selector in code: not checked
    page.set_html(V2)
    price = heal.locator('price', '#price', role='span', expect_value='money')
    with pytest.raises(AttentionRequired) as info:
        price.inner_text()
    message = str(info.value)
    assert "'price' was healed to " + RATING in message
    assert "its inner_text() there is '4.5', which is not money (expect_value='money')" in message
    assert 'quarantined' in message
    assert heal.store.status('price', RATING) == 'quarantined'
    assert heal.heals[-1].confirmed is False and heal.pending() == {}
    assert provider.outcomes == [('price', RATING, False)]
    canary = [e for e in _journal(tmp_path) if e['event'] == 'heal.canary']
    assert canary[-1]['success'] is False
    assert canary[-1]['reason'] == 'expect_value: inner_text() is not money'
    assert '4.5' not in json.dumps(_journal(tmp_path))     # no page value in the journal


def test_a_correct_heal_passes_and_is_confirmed(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    provider = stub_provider(AMOUNT)
    heal = _open(page, tmp_path, provider)
    heal.locator('price', '#price', role='span')
    page.set_html(V2.replace('$1,299.00', '1.299,00 €'))     # and the shop went to euros
    price = heal.locator('price', '#price', role='span', expect_value='money')
    assert price.inner_text() == '1.299,00 €'
    assert heal.heals[-1].confirmed is True and heal.pending() == {}
    assert provider.outcomes == [('price', AMOUNT, True)]
    assert price.inner_text() == '1.299,00 €'                # again: still checked, passes


def test_an_earlier_run_s_heal_is_checked_too(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(RATING))
    heal.locator('price', '#price', role='span')
    page.set_html(V2)
    heal.locator('price', '#price', role='span').inner_text()   # healed, never checked
    later = _open(page, tmp_path, stub_provider(None))           # the next run
    with pytest.raises(AttentionRequired, match="healed to //span\\[@class='rating'\\]"):
        later.locator('price', '#price', role='span', expect_value='money').inner_text()
    assert later.store.status('price', RATING) == 'quarantined'
    quarantine = [e for e in _journal(tmp_path) if e['event'] == 'heal.quarantine']
    assert quarantine[-1]['reason'] == 'expect_value: inner_text() is not money'


def test_an_unhealed_read_is_never_blocked(tmp_path, make_page, stub_provider):
    page = make_page(V1.replace('$1,299.00', 'Call for price'))
    heal = _open(page, tmp_path, stub_provider(None), expect_values={'price': 'money'})
    assert heal.locator('price', '#price', role='span').inner_text() == 'Call for price'
    assert heal.locator('qty', '#qty', expect_value='date').input_value() == '2'


def test_the_reads_it_checks(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider("//a[@class='seller']"))
    heal.locator('seller', '#seller-link', role='link')
    heal.coordinator.pending.clear()                 # as if confirmed earlier
    seller = heal.locator('seller', '#seller-link', role='link', expect_value='url')
    assert seller.get_attribute('href') == '/s/acme'
    with pytest.raises(AttentionRequired, match=r"get_attribute\('title'\) there is None"):
        seller.get_attribute('title')
    heal2 = _open(make_page(V1), tmp_path / 'b', stub_provider("//span[@class='reviews']"))
    with pytest.raises(AttentionRequired, match=r"text_content\(\) there is '\(128 reviews\)', "
                                                r"which is not a whole number"):
        heal2.locator('count', '#count', role='span', expect_value='integer').text_content()
    # The next lookup heals again, and the model's same pick is refused as quarantined.
    with pytest.raises(AttentionRequired, match='previously quarantined'):
        heal2.locator('count', '#count', role='span', expect_value='integer')


def test_open_expect_values_reach_heal_page_reads(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(RATING), expect_values={'#price': 'money'})
    assert heal.page.inner_text('#price') == '$1,299.00'
    page.set_html(V2)
    with pytest.raises(AttentionRequired, match="'#price' was healed to"):
        heal.page.inner_text('#price')
    assert heal.store.status('#price', RATING) == 'quarantined'


def test_heal_page_locator_reads_are_checked(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(RATING), expect_values={'#price': 'money'})
    heal.page.locator('#price').text_content()
    page.set_html(V2)
    with pytest.raises(AttentionRequired, match=r"its text_content\(\) there is '4.5'"):
        heal.page.locator('#price').text_content()


def test_heal_page_input_value_goes_through_heal_locator(tmp_path, make_page, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(None))
    assert heal.page.input_value('#qty') == '2'
    assert heal.store.candidates('#qty')[0]['xpath'] == '#qty'


def test_a_skipped_step_and_a_bad_argument(tmp_path, make_page, stub_provider):
    heal = _open(make_page(V1), tmp_path, stub_provider(None))
    with pytest.raises(ValueError, match="takes 'money'"):
        heal.locator('price', '#price', expect_value='cash')
    with pytest.raises(TypeError, match='expect_values= takes a dict'):
        _open(make_page(V1), tmp_path, stub_provider(None), expect_values='money')
    promo = heal.locator('promo', '#promo', optional=True, expect_value='money')
    assert promo.present is False


class _Element(WebElement):
    @property
    def text(self):
        return ' '.join(self.el.text_content().split())


class _Driver(WebDriver):
    def find_elements(self, by, value):
        return [_Element(self, e.el) for e in super().find_elements(by, value)]


def test_selenium_text_and_get_attribute_through_a_heal(tmp_path, stub_provider):
    web = _Driver("<form><button id='go' value='9'>Go</button><b id='n'>3</b></form>")
    heal = _open(web, tmp_path, stub_provider("//button[@value='9']"),
                 expect_values={'id=go': 'integer', 'id=n': 'integer'})
    assert heal.driver.find_element('id', 'n').get_attribute('id') == 'n'   # not a heal
    heal.driver.find_element('id', 'go')
    web.set_html("<form><button value='9'>Go</button></form>")
    healed = heal.driver.find_element('id', 'go')
    assert healed.get_attribute('value') == '9'       # an integer: passes, and confirms
    assert heal.heals[-1].confirmed is True
    with pytest.raises(AttentionRequired, match=r"'id=go' was healed to //button\[@value='9'\], "
                                                r"and its text there is 'Go'"):
        _ = healed.text
    assert heal.store.status('id=go', "//button[@value='9']") == 'quarantined'


# --- async ----------------------------------------------------------------------------------

class _Locator(AsyncFakeLocator):
    async def text_content(self, **kwargs):
        return self._loc.text_content()


class _Page(AsyncFakePage):
    def locator(self, selector):
        return _Locator(self, selector)


def test_async_reads_are_checked(tmp_path, make_page, stub_provider):
    async def main():
        sync = make_page(V1)
        heal = await mendwright.aio.open(_Page(sync), provider=stub_provider(RATING),
                                         store_path=str(tmp_path / 'o.json'), fingerprint=False,
                                         timeout_ms=0, expect_values={'#price': 'money'})
        assert await heal.locator('price', '#price', role='span',
                                  expect_value='money').inner_text() == '$1,299.00'
        assert await heal.page.text_content('#price') == '$1,299.00'
        await heal.locator('again', '#price', role='span')
        sync.set_html(V2)
        with pytest.raises(AttentionRequired, match="'price' was healed to"):
            await heal.locator('price', '#price', role='span', expect_value='money').inner_text()
        with pytest.raises(AttentionRequired, match="'#price' was healed to"):
            await heal.page.text_content('#price')
        box = await heal.locator('again', '#price', role='span', expect_value='money')
        with pytest.raises(AttentionRequired, match="'again' was healed to"):
            await box.inner_text()             # a proxy resolved first, then read
        return heal
    heal = asyncio.run(main())
    assert heal.store.status('price', RATING) == 'quarantined'
    assert heal.heals[0].confirmed is False
