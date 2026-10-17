"""Lists (mendwright.lists): heal.locator_all() gives the elements of a list and records a
list fingerprint, and never heals a list whose selectors all match nothing (see
test_lists_emptied.py for why), and heal.page.locator(selector).all() and
heal.driver.find_elements() give what the real call gives, as in 0.7.0. Offline: the
lxml-backed FakePage from conftest (fingerprints=True answers the list fingerprint read), a
stub provider, and the fake Selenium driver of test_proxies.py."""

import asyncio
import json
import logging
import time

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, lists
from mendwright.store import load_store

from test_aio import AsyncFakeLocator, AsyncFakePage
from test_proxies import WebDriver, WebElement

PRODUCTS = [('Blue mug', '$12.00'), ('Red mug', '$13.00'), ('Green mug', '$14.00'),
            ('Tea pot', '$29.00')]


def card(cls, name, price, tag='div'):
    return ("<" + tag + " class='" + cls + "'><img src='/i/" + name.replace(' ', '-')
            + ".png' alt='" + name + "'><h3>" + name + "</h3><span class='price'>" + price
            + "</span><button type='button'>Add to cart</button></" + tag + ">")


def shop(cards, extra='', grid="id='grid' class='grid'"):
    return ("<html><body><header><nav><a href='/'>Home</a><a href='/shop'>Shop</a>"
            "<a href='/cart'>Cart</a></nav></header><main id='content'><h1>Shop</h1>"
            "<div " + grid + ">" + cards + "</div>" + extra + "</main>"
            "<footer><ul><li>About</li><li>Help</li></ul></footer></body></html>")


def grid(cls, products=PRODUCTS, tag='div'):
    return ''.join(card(cls, n, p, tag) for n, p in products)


V1 = shop(grid('product-card'))
V2 = shop(grid('product-tile'))                     # the card class changed
TILE = "//div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]"
CARDS = "//div[contains(@class,'product-card')]"
# What a lookup of the list 'cards', recorded with 4 cards, raises when none of its selectors
# matches.
MISSING = ("No matching element for: cards (tried 1 candidate(s)).\n  -> 'cards' matched 4 "
           'elements when its list fingerprint was recorded, and none of its selectors matches '
           'now. Either the list is empty at this point, or its selector broke: mendwright '
           "can't tell the two apart, since the items of an emptied list can show in another "
           "list, such as a cart's in Saved for later, so it never heals a list that matches "
           'nothing. If the selector broke, change it in your code.')


def _lfp(html=V1, xpath=CARDS):
    return lists.of_elements(lxml.html.fromstring(html).xpath(xpath))


def _open(page, tmp_path, provider=None, **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'), **kwargs)


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


# --- the list fingerprint ---------------------------------------------------------------------

def test_a_list_fingerprint_holds_three_items_their_shape_and_container():
    lfp = _lfp()
    assert lists.valid(lfp)
    assert lfp['count'] == 4 and lfp['depth'] == 1
    assert [item['text'] for item in lfp['items']] == [
        'Blue mug$12.00Add to cart', 'Red mug$13.00Add to cart', 'Tea pot$29.00Add to cart']
    assert lfp['items'][0]['shape'] == {'button': 1, 'h3': 1, 'img': 1, 'span': 1, '>button': 1,
                                        '>h3': 1, '>img': 1, '>span': 1}
    assert lfp['container'] == {'tag': 'div', 'id': 'grid', 'classes': ['grid']}
    assert lists.item_role(lfp) == 'div'


def test_fewer_than_two_elements_are_no_list():
    doc = lxml.html.fromstring(V1)
    assert lists.of_elements(doc.xpath('//h1')) is None
    assert lists.from_raw({'count': 1, 'items': []}) is None
    assert not lists.valid({'v': 1, 'count': 4, 'items': []})
    assert not lists.valid(None)


def test_a_shape_skips_what_the_browser_does_not_render():
    el = lxml.html.fragment_fromstring(
        '<div><p>a</p><template><b>x</b></template><noscript><i>y</i></noscript><p>b</p></div>')
    assert lists.shape_of(el) == {'>p': 2, '>template': 1, '>noscript': 1, 'p': 2,
                                  'template': 1, 'noscript': 1}


# --- the store -------------------------------------------------------------------------------

def _store(tmp_path, code='.product-card'):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('cards', [code])
    store.set_meta('cards', role='div', list_fingerprint=_lfp())
    return store


def test_the_store_keeps_a_list_fingerprint(tmp_path):
    store = _store(tmp_path)
    again = load_store(None, str(tmp_path / 'o.json'))
    assert again.meta('cards')['listFingerprint'] == _lfp()
    with pytest.raises(ValueError, match='takes a list fingerprint from mendwright.lists'):
        store.set_meta('cards', list_fingerprint={'v': 1})


# --- Healer.locator_all() ---------------------------------------------------------------------

def test_a_grid_whose_card_class_changed_is_not_healed(tmp_path, make_page, stub_provider, caplog):
    page = make_page(V1, fingerprints=True)
    provider = stub_provider(TILE)                       # it would pick the new cards
    heal = _open(page, tmp_path, provider)
    cards = heal.locator_all('cards', CARDS)
    assert [c.inner_text() for c in cards] == [n + p + 'Add to cart' for n, p in PRODUCTS]
    assert heal.store.meta('cards')['expectedRole'] == 'div'
    page.set_html(V2)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        with pytest.raises(AttentionRequired) as info:
            heal.locator_all('cards', CARDS)
    assert str(info.value) == MISSING
    assert [r.getMessage() for r in caplog.records if 'did not heal' in r.getMessage()] == [
        "mendwright: did not heal 'cards': " + MISSING.split('  -> ')[1]]
    assert heal.heals == [] and heal.pending() == {} and provider.bundles == []
    assert [c for c in heal.store.candidates('cards') if c['source'] == 'heal'] == []
    # Nothing was looked for: the one heal event is the decline, which names why.
    assert [(e['event'], e['code'], e['reason']) for e in _journal(tmp_path)
            if e['event'].startswith('heal.')] == [
        ('heal.declined', 'list_empty', MISSING.split('  -> ')[1])]
    # Fixed in the code, the list is found and recorded for its new selector.
    cards = heal.locator_all('cards', TILE)
    assert len(cards) == 4 and cards[3].inner_text() == 'Tea pot$29.00Add to cart'
    assert heal.store.meta('cards')['listFingerprint']['items'][0]['classes'] == ['product-tile']


def test_a_list_never_seen_with_items_is_empty_at_once(tmp_path, make_page):
    page = make_page(V2, fingerprints=True)
    heal = _open(page, tmp_path, timeout_ms=60000)    # it would wait a minute if it waited
    assert heal.locator_all('cards', CARDS) == []
    page.set_html(shop(card('product-card', *PRODUCTS[0])))
    assert len(heal.locator_all('cards', CARDS)) == 1  # one item: nothing repeats in it
    page.set_html(V2)
    assert heal.locator_all('cards', CARDS) == []
    assert heal.store.meta('cards')['listFingerprint'] is None


def test_an_optional_list_is_never_healed(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path, timeout_ms=60000)
    heal.locator_all('cards', CARDS)
    page.set_html(V2)
    assert heal.locator_all('cards', CARDS, optional=True) == []
    assert heal.heals == []


@pytest.mark.parametrize('setting', ['dry run', 'destructive', 'another page', 'guard'])
def test_what_would_stop_a_heal_changes_nothing_for_a_list(tmp_path, make_page, setting):
    # No heal is tried, so the reason is the same with each of them.
    page = make_page(V1, url='https://shop.example/shop', fingerprints=True)
    heal = _open(page, tmp_path, dry_run=setting == 'dry run',
                 guard=(lambda: False) if setting == 'guard' else None)
    heal.locator_all('cards', CARDS)
    if setting == 'destructive':
        heal.store.set_meta('cards', destructive=True)
    if setting == 'another page':
        page.url = 'https://shop.example/cart'
    page.set_html(V2)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cards', CARDS)
    assert str(info.value) == MISSING and heal.heals == []


# --- heal.page and heal.driver: as in 0.7.0 ---------------------------------------------------

def _writes(monkeypatch):
    """The writes of the override file, by any store, from now on."""
    from mendwright.store import SelectorStore
    writes, real = [], SelectorStore._write

    def write(self, payload):
        writes.append(1)
        return real(self, payload)
    monkeypatch.setattr(SelectorStore, '_write', write)
    return writes


def test_heal_page_locator_all_is_the_real_one_and_saves_nothing(
        tmp_path, make_page, stub_provider, monkeypatch):
    # A script that waits for toasts to go away, or checks that a cart is empty, gets []
    # at once, as it did in 0.7.0, even for a selector that matched two or more before.
    writes = _writes(monkeypatch)
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(TILE)
    heal = _open(page, tmp_path, provider, timeout_ms=60000)   # it would wait a minute
    assert len(heal.page.locator('.product-card').all()) == 4
    assert len(heal.page.locator('h1').all()) == 1 and heal.page.locator('.nothing').all() == []
    polls = 0
    started = time.monotonic()
    while heal.page.locator('.product-card').all() and polls < 10:   # until the cards go
        polls += 1
        if polls == 3:
            page.set_html(V2)
    assert polls == 3 and time.monotonic() - started < 5
    assert heal.heals == [] and provider.bundles == [] and page.fingerprint_reads == 0
    assert writes == [] and not (tmp_path / 'o.json').exists()
    again = _open(page, tmp_path, provider, timeout_ms=60000)
    assert again.page.locator('.product-card').all() == []


def test_heal_page_locator_all_never_waits_for_a_list_heal_locator_all_recorded(
        tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(TILE)
    heal = _open(page, tmp_path, provider, timeout_ms=60000)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    started = time.monotonic()
    assert heal.page.locator('.product-card').all() == []    # no wait, no heal, no model
    assert time.monotonic() - started < 5
    assert heal.heals == [] and provider.bundles == []
    with pytest.raises(AttentionRequired, match='the list is empty at this point, or its '
                                                'selector broke'):
        heal.locator_all('.product-card', '.product-card', timeout_ms=0)
    assert len(heal.page.locator('.product-tile').all()) == 4


def _chaining(fake_page):
    """conftest's FakePage, with nth() locators that also chain, as Playwright's do."""
    loc = fake_page('<p></p>').locator('xpath=//p')

    class Nth(type(loc.nth(0))):
        def locator(self, selector):
            return self

        def frame_locator(self, selector):
            return self

    class Locator(type(loc)):
        def nth(self, index):
            return Nth(self._page, self._selector, index)

    class Page(fake_page):
        def locator(self, selector):
            return Locator(self, selector)
    return Page


def test_heal_page_all_is_guarded_with_dismiss(tmp_path, make_page):
    from mendwright.proxies import GuardedLocator
    page = _chaining(make_page)(V1, fingerprints=True, css=True)
    items = _open(page, tmp_path, dismiss=True).page.locator('.product-card').all()
    assert type(items[0]) is GuardedLocator
    assert repr(items[1]) == "<GuardedLocator \"page.locator('.product-card').all()[1]\">"


class ListElement(WebElement):
    @property
    def text(self):
        return ' '.join(self.el.text_content().split())


class ListDriver(WebDriver):
    """test_proxies' WebDriver with class CSS, and execute_script for the list reads."""

    def find_elements(self, by, value):
        if by == 'css selector' and value.startswith('.'):
            found = self.doc.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), $c)]",
                                   c=' ' + value[1:] + ' ')
            return [ListElement(self, e) for e in found]
        return [ListElement(self, e.el) for e in super().find_elements(by, value)]

    def execute_script(self, script, *args):
        if script.startswith('return (' + lists.LIST_JS):
            return lists.raw_of_elements([e.el for e in args[0]])
        return super().execute_script(script, *args)


def test_heal_driver_find_elements_is_the_real_one_and_saves_nothing(
        tmp_path, stub_provider, monkeypatch):
    writes = _writes(monkeypatch)
    web = CountingDriver(V1)
    provider = stub_provider(TILE)
    heal = _open(web, tmp_path, provider, timeout_ms=60000)    # it would wait a minute
    real = web.find_elements('css selector', '.product-card')
    web.calls.clear()
    found = heal.driver.find_elements('css selector', '.product-card')
    assert web.calls == ['findElements']                    # the real call and nothing else
    assert [e.native.el for e in found] == [e.el for e in real]
    assert [e.text[:7] for e in found] == ['Blue mu', 'Red mug', 'Green m', 'Tea pot']
    assert len(heal.driver.find_elements('xpath', '//h1')) == 1
    web.set_html(V2)
    started = time.monotonic()
    assert heal.driver.find_elements('css selector', '.product-card') == []
    assert time.monotonic() - started < 5
    assert heal.heals == [] and provider.bundles == []
    assert writes == [] and not (tmp_path / 'o.json').exists()


def test_webdriverwait_on_heal_driver_find_elements_works_as_before(tmp_path):
    ui = pytest.importorskip('selenium.webdriver.support.ui')
    web = ListDriver(V1)
    heal = _open(web, tmp_path, timeout_ms=60000)
    polls = []

    def gone(driver):
        polls.append(1)
        if len(polls) == 3:
            web.set_html(shop(''))                        # the cards go away
        return len(driver.find_elements('css selector', '.product-card')) == 0
    started = time.monotonic()
    assert ui.WebDriverWait(heal.driver, 5, poll_frequency=0.01).until(gone) is True
    assert ui.WebDriverWait(heal.driver, 5, poll_frequency=0.01).until_not(
        lambda d: d.find_elements('css selector', '.product-card')) == []
    polls.clear()

    def cards(driver):
        polls.append(1)
        if len(polls) == 3:
            web.set_html(V1)                              # the cards arrive
        return driver.find_elements('css selector', '.product-card')
    found = ui.WebDriverWait(heal.driver, 5, poll_frequency=0.01).until(cards)
    assert len(found) == 4 and len(polls) == 3 and heal.heals == []
    assert time.monotonic() - started < 5


def test_one_off_list_selectors_through_heal_page_and_heal_driver_write_nothing(
        tmp_path, make_page, monkeypatch):
    # A scraper that reads a table row by row, with a new selector for each row.
    writes = _writes(monkeypatch)
    rows = ''.join('<tr>' + ''.join('<td>r%dc%d</td>' % (i, j) for j in range(5)) + '</tr>'
                   for i in range(1, 21))
    table = '<html><body><table><tbody>' + rows + '</tbody></table></body></html>'
    page = _open(make_page(table, fingerprints=True), tmp_path).page
    driver = _open(ListDriver(table), tmp_path).driver
    for i in range(1, 21):
        assert len(page.locator('xpath=//table/tbody/tr[%d]/td' % i).all()) == 5
        assert len(driver.find_elements('xpath', '//table/tbody/tr[%d]/td' % i)) == 5
    assert writes == [] and not (tmp_path / 'o.json').exists()


def test_heal_locator_all_saves_a_new_list_in_one_write(tmp_path, make_page, monkeypatch):
    page = make_page(V1, fingerprints=True, url='https://shop.example/shop')
    heal = _open(page, tmp_path)
    writes = _writes(monkeypatch)
    assert len(heal.locator_all('cards', CARDS)) == 4
    # Its selector, its first success, the page and the list fingerprint with the role.
    assert len(writes) == 1
    saved = load_store(None, str(tmp_path / 'o.json'))
    meta = saved.meta('cards')
    assert meta['listFingerprint'] == _lfp() and meta['expectedRole'] == 'div'
    assert meta['lastPage'] == '/shop' and saved.candidates('cards')[0]['lastSucceededAt']
    assert len(heal.locator_all('cards', CARDS)) == 4 and len(writes) == 1   # nothing new


# --- async ----------------------------------------------------------------------------------

class _Locator(AsyncFakeLocator):
    def __init__(self, page, selector, sync=None):
        self._page = page
        self._loc = sync or page.sync.locator(selector)

    def nth(self, index):
        return _Locator(self._page, None, self._loc.nth(index))

    async def all(self):
        return [self.nth(i) for i in range(self._loc.count())]


class _Page(AsyncFakePage):
    def locator(self, selector):
        return _Locator(self, selector)


def test_async_lists(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        assert len(await heal.locator_all('cards', CARDS)) == 4
        assert len(await heal.page.locator('.product-card').all()) == 4
        sync.set_html(V2)
        with pytest.raises(AttentionRequired) as info:
            await heal.locator_all('cards', CARDS)
        items = await heal.page.locator('.product-card').all()
        cards = await heal.locator_all('cards', '.product-tile')     # fixed in the code
        return heal, info.value, [await c.inner_text() for c in cards], items
    heal, error, texts, items = asyncio.run(main())
    assert str(error) == MISSING
    assert texts[0] == 'Blue mug$12.00Add to cart' and items == []
    assert heal.heals == [] and heal.pending() == {}
    assert heal.store.candidates('.product-card') == []      # heal.page saved nothing


def test_a_list_of_links_keeps_its_role(tmp_path, make_page):
    v1 = ("<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' "
          "href='/b'>B</a></li><li><a class='m' href='/c'>C</a></li></ul></body></html>")
    page = make_page(v1, fingerprints=True)
    heal = _open(page, tmp_path)
    links = heal.locator_all('menu', "//a[@class='m']", role='link')
    assert [link.get_attribute('href') for link in links] == ['/a', '/b', '/c']
    assert heal.store.meta('menu')['expectedRole'] == 'link'
    page.set_html(v1.replace("class='m'", "class='menu-link'"))
    with pytest.raises(AttentionRequired, match="'menu' matched 3 elements"):
        heal.locator_all('menu', "//a[@class='m']", role='link')


def test_a_saved_list_heal_needs_no_list_fingerprint_read_again(tmp_path, make_page):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cards', CARDS)
    reads = page.fingerprint_reads
    heal.locator_all('cards', CARDS)
    assert page.fingerprint_reads == reads      # once per Healer and selector


# --- review fixes ---------------------------------------------------------------------------

@pytest.mark.parametrize('order', ['find_element first', 'find_elements first'])
def test_a_heal_of_one_card_never_answers_for_the_list(tmp_path, stub_provider, order):
    # heal.locator_all() under the key and selector of driver.find_element().
    from mendwright.proxies import selenium_selector
    key, selector = 'css selector=.product-card', selenium_selector('css selector', '.product-card')
    web = ListDriver(V1)
    provider = stub_provider(None)
    heal = _open(web, tmp_path, provider)
    assert len(heal.locator_all(key, selector)) == 4
    web.set_html(V2)
    provider.xpath = "//div[@class='product-tile'][h3='Blue mug']"
    if order == 'find_elements first':
        with pytest.raises(AttentionRequired, match='its selector broke'):
            heal.locator_all(key, selector)
    assert heal.driver.find_element('css selector', '.product-card').text.startswith('Blue mug')
    with pytest.raises(AttentionRequired, match='its selector broke'):
        heal.locator_all(key, selector)                   # not through the card's heal
    assert [h.source for h in heal.heals] == ['model'] and len(provider.bundles) == 1


def test_a_heal_page_shortcut_s_heal_never_answers_for_the_list(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(None)
    heal = _open(page, tmp_path, provider)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    provider.xpath = "//div[@class='product-tile'][h3='Red mug']"
    heal.page.click('.product-card')                      # a heal of one card
    with pytest.raises(AttentionRequired, match='its selector broke'):
        heal.locator_all('.product-card', '.product-card')


def _skeleton():
    return ("<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
            "<button type='button' disabled></button></div>")


def test_a_list_that_is_still_loading_is_not_healed_onto_its_placeholders(tmp_path):
    web = ListDriver(V1)
    heal = _open(web, tmp_path)
    heal.locator_all('cards', '.product-card')
    web.set_html(shop(_skeleton() * 4))
    with pytest.raises(AttentionRequired, match='the list is empty at this point, or its '
                                                'selector broke'):
        heal.locator_all('cards', '.product-card')
    assert heal.heals == []
    assert [c for c in heal.store.candidates('cards') if c['source'] == 'heal'] == []


def _arriving(page, after):
    """page, whose cards arrive on the third lookup of a selector."""
    query, looks = page.query, []

    def arriving(selector):
        looks.append(selector)
        if len(looks) == 3:
            page.set_html(after)
        return query(selector)
    page.query = arriving
    return looks


def test_a_list_that_comes_while_mendwright_waits_for_it_is_used(tmp_path, make_page):
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path, timeout_ms=5000)
    assert len(heal.locator_all('cards', '.product-card', poll_ms=1)) == 4
    page.set_html(shop(''))                               # the grid is loading
    looks = _arriving(page, V1)
    assert len(heal.locator_all('cards', '.product-card', poll_ms=1)) == 4
    assert len(looks) >= 3 and heal.heals == []


class CountingDriver(ListDriver):
    """ListDriver that counts what reaches the browser."""

    def __init__(self, html):
        self.calls = []
        super().__init__(html)

    @property
    def current_url(self):
        self.calls.append('url')
        return self._url

    @current_url.setter
    def current_url(self, value):
        self._url = value

    def find_elements(self, by, value):
        self.calls.append('findElements')
        return super().find_elements(by, value)

    def execute_script(self, script, *args):
        self.calls.append('script')
        return super().execute_script(script, *args)


@pytest.mark.parametrize('off', ['fingerprint=False', 'MENDWRIGHT_DISABLE=1'])
def test_with_the_fingerprint_tier_off_a_list_that_matches_nothing_is_empty_at_once(
        tmp_path, make_page, monkeypatch, off):
    page = make_page(V1, fingerprints=True, css=True)
    _open(page, tmp_path).locator_all('cards', CARDS)       # recorded with 4 cards
    kwargs = {'fingerprint': False}
    if off == 'MENDWRIGHT_DISABLE=1':
        monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
        kwargs = {}
    heal = _open(page, tmp_path, timeout_ms=60000, **kwargs)   # it would wait a minute
    assert len(heal.locator_all('cards', CARDS)) == 4
    page.set_html(V2)
    started = time.monotonic()
    assert heal.locator_all('cards', CARDS) == []
    assert time.monotonic() - started < 5 and heal.heals == []

    web = ListDriver(V1)
    _open(web, tmp_path / 'driver').locator_all('cards', '.product-card')
    web.set_html(shop(''))
    driver = _open(web, tmp_path / 'driver', timeout_ms=60000, **kwargs)
    assert driver.locator_all('cards', '.product-card') == []


def test_async_a_list_is_empty_at_once_with_the_fingerprint_tier_off(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        first = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                          timeout_ms=0)
        await first.locator_all('cards', CARDS)
        sync.set_html(V2)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=60000, fingerprint=False)
        empty = await heal.locator_all('cards', CARDS)
        return heal, empty
    heal, empty = asyncio.run(main())
    assert empty == [] and heal.heals == []


def test_the_reads_of_a_list_s_elements_are_never_checked(tmp_path, make_page):
    # No list is found through a heal, so there is nothing to check its reads against, and
    # heal.locator_all() takes no expect_value=.
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path, expect_values={'.product-card': 'money'})
    items = heal.locator_all('.product-card', '.product-card')
    assert items[0].inner_text() == 'Blue mug$12.00Add to cart'       # not money, and no error
    with pytest.raises(TypeError, match='expect_value'):
        heal.locator_all('.product-card', '.product-card', expect_value='money')
    assert heal.page.locator('.product-card').all()[0].inner_text().startswith('Blue')


def test_async_lists_optional_and_item_actions(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        sync.set_html(V2)
        assert await heal.locator_all('cards', CARDS, optional=True) == []
        cards = await heal.locator_all('cards', '.product-tile')
        assert await cards[0].inner_text() == 'Blue mug$12.00Add to cart'
        await cards[1].click(expect=lambda: True)         # an item's action confirms nothing
        cards[2].confirm(False)                           # nor does its confirm()
        with pytest.raises(TypeError, match='expect_value'):
            heal.locator_all('cards', CARDS, expect_value='money')
        return heal, sync
    heal, sync = asyncio.run(main())
    assert sync.actions[-1][1] == 'click' and heal.heals == [] and heal.pending() == {}


def test_async_heal_page_locator_all_is_the_real_one_and_saves_nothing(
        tmp_path, make_page, stub_provider):
    provider = stub_provider(TILE)

    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         provider=provider, timeout_ms=60000)
        assert len(await heal.locator_all('.product-card', '.product-card')) == 4
        assert len(await heal.page.locator('.product-card').all()) == 4
        sync.set_html(V2)
        started = time.monotonic()
        items = await heal.page.locator('.product-card').all()
        return heal, items, time.monotonic() - started
    heal, items, seconds = asyncio.run(main())
    assert items == [] and seconds < 5                      # no wait, no heal, no model
    assert heal.heals == [] and provider.bundles == []


def test_async_a_list_that_comes_while_mendwright_waits_for_it_is_used(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=5000)
        await heal.locator_all('cards', CARDS, poll_ms=1)
        sync.set_html(shop(''))                           # the grid is loading
        looks = _arriving(sync, V1)
        return heal, looks, await heal.locator_all('cards', CARDS, poll_ms=1)
    heal, looks, cards = asyncio.run(main())
    assert len(cards) == 4 and len(looks) >= 3 and heal.heals == []


# --- lists and the run= functions of heal.interrupt() -----------------------------------------

RELOGIN = ("<div id='relogin' role='dialog' aria-label='Session expired'><p>Your session expired. "
           "Sign in again.</p><input id='user'><button id='signin' type='button'>Sign in</button>"
           "</div>")
BEHIND_RELOGIN = shop(grid('product-tile'), RELOGIN)    # the card class changed, under a popup


def test_an_attention_required_from_a_run_function_is_not_taken_for_a_list_miss(
        tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(TILE)
    heal = _open(page, tmp_path, provider)
    heal.locator_all('cards', CARDS)
    error, calls = AttentionRequired('mendwright: the sign-in form changed'), []

    def relogin(p):
        calls.append(p)
        raise error

    heal.interrupt('relogin', '#relogin', run=relogin)
    page.set_html(BEHIND_RELOGIN)
    with pytest.raises(AttentionRequired) as caught:
        heal.locator_all('cards', CARDS)
    assert caught.value is error and calls == [heal.page]
    assert heal.heals == [] and provider.bundles == []      # the list was not healed


def test_a_selenium_list_lookup_raises_what_a_run_function_raised_as_it_is(tmp_path):
    web = ListDriver(V1)
    heal = _open(web, tmp_path)
    assert len(heal.locator_all('cards', '.product-card')) == 4
    error, calls = AttentionRequired('mendwright: the sign-in form changed'), []

    def relogin(driver):
        calls.append(driver)
        raise error

    heal.interrupt('relogin', '#relogin', run=relogin)
    web.set_html(BEHIND_RELOGIN)
    with pytest.raises(AttentionRequired) as caught:
        heal.locator_all('cards', '.product-card')
    assert caught.value is error and calls == [heal.driver] and heal.heals == []


def test_async_an_attention_required_from_a_run_function_is_not_taken_for_a_list_miss(
        tmp_path, make_page):
    error = AttentionRequired('mendwright: the sign-in form changed')

    async def relogin(page):
        raise error

    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        heal.interrupt('relogin', '#relogin', run=relogin)
        sync.set_html(BEHIND_RELOGIN)
        with pytest.raises(AttentionRequired) as info:
            await heal.locator_all('cards', CARDS)
        return heal, info.value
    heal, caught = asyncio.run(main())
    assert caught is error
    assert heal.heals == []


def test_async_a_run_function_s_list_lookups_skip_the_lock(tmp_path, make_page):
    # The step that runs the function holds the healer's lock while it waits for it, so the
    # function's list lookups go ahead without the lock, as its other calls do. Taking it
    # would wait for that step forever.
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        seen = []

        async def relogin(page):
            cards = await heal.locator_all('cards', '.product-tile')
            seen.append([await card.inner_text() for card in cards])
            seen.append(len(await page.locator('.product-tile').all()))
            sync.set_html(V2)                             # signed in again: the popup is gone

        heal.interrupt('relogin', '#relogin', run=relogin)
        sync.set_html(BEHIND_RELOGIN)
        await asyncio.wait_for(heal.locator('heading', 'xpath=//h1').click(), 5)
        return heal, seen, sync.actions
    heal, seen, actions = asyncio.run(main())
    assert seen == [[n + p + 'Add to cart' for n, p in PRODUCTS], 4]
    assert actions[-1][1] == 'click' and heal.heals == []
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('relogin', 'heading')]


# --- a list and a heal of one element under one key ------------------------------------------

FIRST = "//div[contains(@class,'product-tile')][h3='Blue mug']"


def _canaries(tmp_path):
    return sorted((e['xpath'], e['success']) for e in _journal(tmp_path)
                  if e['event'] == 'heal.canary')


def test_a_list_never_takes_the_heal_of_one_element_of_its_key_and_its_elements_report_nothing(
        tmp_path, make_page, stub_provider):
    # heal.locator_all('.product-card', ...) and heal.page.inner_text('.product-card') share
    # a key, and the first card healed, through the model.
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(FIRST)
    heal = _open(page, tmp_path, provider)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    assert heal.page.inner_text('.product-card').startswith('Blue mug')   # the first card's
    with pytest.raises(AttentionRequired, match='its selector broke'):
        heal.locator_all('.product-card', '.product-card')
    cards = heal.locator_all('.product-card', '.product-tile')     # fixed in the code
    assert len(cards) == 4
    cards[1].confirm(True)                                 # no heal of the list to report
    cards[0].confirm(False)                                # and the card's heal is not its
    assert heal.pending() == {'.product-card': FIRST}
    assert heal.store.status('.product-card', FIRST) == 'active'
    assert [(h.new, h.confirmed) for h in heal.heals] == [(FIRST, None)]
    assert heal.confirm_all(True) == 1
    assert _canaries(tmp_path) == [(FIRST, True)] and heal.pending() == {}
    assert provider.outcomes == [('.product-card', FIRST, True)]
