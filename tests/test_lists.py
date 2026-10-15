"""Lists (mendwright.lists): heal.locator_all(), heal.page.locator(selector).all() and
heal.driver.find_elements() heal a list selector that matches nothing any more. Offline: the
lxml-backed FakePage from conftest (fingerprints=True answers the list fingerprint read), a
stub provider, and the fake Selenium driver of test_proxies.py."""

import asyncio
import json

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, HealEvent, LocalModelProvider, lists
from mendwright.capture import build_list_bundle
from mendwright.client import apply_list
from mendwright.gate import list_live_reason
from mendwright.prompt import build_prompt
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
NEW_PRODUCTS = [('Lamp', '$40.00'), ('Chair', '$90.00'), ('Desk', '$250.00'), ('Sofa', '$900.00')]


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


# --- finding the list ---------------------------------------------------------------------------

def test_a_grid_whose_card_class_changed_is_found():
    match = lists.find(_lfp(), V2, 'div')
    assert match.xpath == TILE and match.css == 'div.product-tile'
    assert match.count == 4 and match.score > 0.9 and match.reason == ''


def test_new_products_in_the_same_container_are_found():
    match = lists.find(_lfp(), shop(grid('product-tile', NEW_PRODUCTS)), 'div')
    assert match.xpath == TILE and match.reason == ''


def test_new_products_in_a_new_container_are_left_to_the_model():
    page = shop(grid('product-tile', NEW_PRODUCTS), grid="class='catalog'")
    match = lists.find(_lfp(), page, 'div')
    assert match.xpath is None
    assert match.reason.startswith('the best group (score ')
    assert 'shares no id, test id, name, link, label or text with the items' in match.reason
    assert match.groups and match.groups[0].xpath == TILE


def test_a_look_alike_list_outside_the_recorded_container_is_no_rival():
    recent = "<section id='recent'><h2>Recently viewed</h2>" + grid('product-tile', PRODUCTS[:3]) + '</section>'
    match = lists.find(_lfp(), shop(grid('product-tile'), extra=recent), 'div')
    assert match.xpath == ("//div[@id='grid']/div[contains(concat(' ', normalize-space(@class), "
                           "' '), ' product-tile ')]")
    assert match.css == 'div#grid > div.product-tile'


def test_one_element_is_never_taken_for_a_list():
    one = shop(card('product-tile', *PRODUCTS[0]) + "<div class='banner'><p>Sale!</p></div>")
    match = lists.find(_lfp(), one, 'div')
    assert match.xpath is None
    assert 'not all shaped like the recorded items' in match.reason
    assert ("The only element like the recorded items is one <div> ('Blue mug$12.00Add to "
            "cart'); mendwright never heals a list onto one element") in match.reason

    alone = ("<html><body><main id='content'><section>" + card('product-tile', *PRODUCTS[0])
             + "</section></main></body></html>")
    match = lists.find(_lfp(), alone, 'div')
    assert match.xpath is None and match.groups == []
    assert match.reason.startswith('the page has no group of 2 or more div elements that '
                                   'repeat the same structure. The only element like')


def test_a_hidden_copy_is_neither_the_answer_nor_a_rival():
    page = shop(grid('product-tile'), extra="<div id='m' class='grid'>" + grid('product-tile')
                + '</div>', grid="id='d' class='grid'")
    lfp = _lfp(shop(grid('product-card'), grid="class='grid'"))
    assert lists.find(lfp, page, 'div').reason.startswith('two groups score alike')
    hidden = "//div[@id='m']/div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]"
    assert hidden in [g.xpath for g in lists.find(lfp, page, 'div').groups]
    match = lists.find(lfp, page, 'div', visible=lambda xpath: xpath != hidden)
    assert match.reason == '' and match.count == 4 and match.xpath != hidden


def test_items_wrapped_one_level_down_are_found():
    wrapped = ''.join("<div class='col'>" + card('product-tile', n, p) + '</div>' for n, p in PRODUCTS)
    match = lists.find(_lfp(), shop(wrapped), 'div')
    assert match.xpath == TILE and match.count == 4


def test_a_list_of_links_heals_onto_links():
    v1 = "<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' href='/b'>B</a></li><li><a class='m' href='/c'>C</a></li></ul></body></html>"
    lfp = _lfp(v1, "//a[@class='m']")
    assert lists.item_role(lfp) == 'link'
    match = lists.find(lfp, v1.replace("class='m'", "class='menu-link'"), 'link')
    assert match.xpath == "//a[contains(concat(' ', normalize-space(@class), ' '), ' menu-link ')]"


# --- the static gate --------------------------------------------------------------------------

def test_the_static_gate():
    assert lists.validate(TILE, V2, 'div').ok
    assert lists.validate(TILE, V2, 'div').count == 4
    assert lists.validate("//div[@id='grid']/div[2]", V2, 'div').reason == lists.POSITIONAL
    assert lists.validate('//h1', V2, 'h1').reason == ('it matches 1 element, and a list heals '
                                                      'only onto 2 or more')
    assert lists.validate(TILE, V2, 'article').reason == 'role mismatch: got div want article'
    assert lists.validate('//div', V2, 'div').reason == 'its elements are inside one another'
    assert lists.validate('//p', V2, 'p').reason == 'no match'


class LiveDriver:
    """count() and list_items() over a page, for the live gate."""

    def __init__(self, html, hidden=False):
        self.doc = lxml.html.fromstring(html)
        self.hidden = hidden

    def _els(self, selector):
        body = selector[len('xpath='):] if selector.startswith('xpath=') else selector
        if body.startswith(('div.', 'div#')):
            from lxml.cssselect import CSSSelector
            return CSSSelector(body)(self.doc)
        return [e for e in self.doc.xpath(body) if hasattr(e, 'tag')]

    def count(self, selector, frame_chain=()):
        return len(self._els(selector))

    def list_items(self, selector, frame_chain=()):
        return [{'tag': e.tag, 'role': e.tag, 'visible': not self.hidden} for e in self._els(selector)]


def test_the_live_gate():
    meta = {'expectedRole': 'div'}
    assert list_live_reason(LiveDriver(V2), TILE, meta) == ''
    assert list_live_reason(None, TILE, meta) == ''
    assert list_live_reason(LiveDriver(V2, hidden=True), TILE, meta) == (
        'none of its 4 elements is visible on the live page')
    assert list_live_reason(LiveDriver(V2), '//h1', meta) == (
        'it matches 1 element on the live page, and a list heals only onto 2 or more')
    assert list_live_reason(LiveDriver(V2), TILE, {'expectedRole': 'li'}).startswith(
        'live element role mismatch: got div want li')


def _store(tmp_path, code='.product-card'):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('cards', [code])
    store.set_meta('cards', role='div', list_fingerprint=_lfp())
    return store


def test_apply_list_saves_the_heal_and_its_css(tmp_path):
    store = _store(tmp_path)
    applied, reason, count = apply_list(store, 'cards', TILE, V2, store.meta('cards'),
                                        driver=LiveDriver(V2), css='div.product-tile')
    assert (applied, reason, count) == (TILE, 'ok', 4)
    heal = [c for c in store.candidates('cards') if c['source'] == 'heal'][0]
    assert heal['xpath'] == TILE and heal['css'] == 'div.product-tile'
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))['selectors']['cards']
    assert lists.valid(saved['listFingerprint'])


def test_apply_list_refuses_what_a_quarantine_holds(tmp_path):
    store = _store(tmp_path, code=CARDS)
    store.add_candidate('cards', TILE, source='heal')
    store.quarantine('cards', TILE)
    other = "//div[@id='grid']/div"
    assert apply_list(store, 'cards', TILE, V2, store.meta('cards'))[1] == 'previously quarantined'
    applied, reason, _count = apply_list(store, 'cards', other, V2, store.meta('cards'))
    assert applied is None and reason.startswith('previously quarantined (it takes elements')
    assert apply_list(store, 'cards', '//h1', V2, store.meta('cards'))[1].startswith('it matches 1')


def test_the_store_keeps_a_list_fingerprint(tmp_path):
    store = _store(tmp_path)
    again = load_store(None, str(tmp_path / 'o.json'))
    assert again.meta('cards')['listFingerprint'] == _lfp()
    with pytest.raises(ValueError, match='takes a list fingerprint from mendwright.lists'):
        store.set_meta('cards', list_fingerprint={'v': 1})


# --- Healer.locator_all() ---------------------------------------------------------------------

def test_a_grid_whose_card_class_changed_heals_as_a_list(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    provider = stub_provider(None)
    heal = _open(page, tmp_path, provider)
    cards = heal.locator_all('cards', CARDS)
    assert [c.inner_text() for c in cards] == [n + p + 'Add to cart' for n, p in PRODUCTS]
    assert heal.store.meta('cards')['expectedRole'] == 'div'
    page.set_html(V2)
    cards = heal.locator_all('cards', CARDS)
    assert len(cards) == 4 and cards[3].inner_text() == 'Tea pot$29.00Add to cart'
    assert heal.heals == [HealEvent('cards', (CARDS,), TILE, None, 'fingerprint')]
    assert heal.pending() == {'cards': TILE} and provider.bundles == []
    applied = [e for e in _journal(tmp_path) if e['event'] == 'heal.applied'][0]
    assert applied['list'] is True and applied['items'] == 4 and applied['source'] == 'fingerprint'
    cards[0].click()
    assert page.actions[-1][1] == 'click'
    assert heal.pending() == {'cards': TILE}           # an item's action confirms nothing
    assert len(heal.locator_all('cards', CARDS)) == 4   # the saved heal, no new heal
    assert len(heal.heals) == 1


def test_a_list_that_would_heal_to_one_element_is_refused(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)                       # no provider: the fingerprint only
    heal.locator_all('cards', CARDS)
    page.set_html(shop(card('product-tile', *PRODUCTS[0]) + "<div class='banner'><p>Sale!</p></div>"))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cards', CARDS)
    message = str(info.value)
    assert message.startswith('No matching element for: cards')
    assert "the list fingerprint match for 'cards' was not confident" in message
    assert 'mendwright never heals a list onto one element' in message
    assert 'no model provider is configured' in message
    assert heal.heals == []


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


def test_a_model_picks_one_of_the_groups(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    provider = stub_provider("//div[@id='nope']")       # not a listed group
    heal = _open(page, tmp_path, provider)
    heal.locator_all('cards', CARDS)
    page.set_html(shop(grid('product-tile', NEW_PRODUCTS), grid="class='catalog'"))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cards', CARDS)
    assert ('so the model was asked: no group the model picked for \'cards\' passed '
            "mendwright's list gate (last reason: it is not the xpath of one of the groups "
            'listed; copy one of them exactly)') in str(info.value)
    bundle = provider.bundles[0]
    assert bundle['list'] is True and bundle['count'] == 4 and bundle['expectedRole'] == 'div'
    assert 'html' not in bundle and 'inventory' not in bundle and 'fingerprint' not in bundle
    assert bundle['candidates'] == [CARDS]
    assert bundle['groups'][0] == {
        'xpath': TILE, 'count': 4, 'container': '<div class="catalog">',
        'items': ['<div class="product-tile">Lamp$40.00Add to cart</div>',
                  '<div class="product-tile">Chair$90.00Add to cart</div>']}
    assert provider.bundles[1]['rejectedXpath'] == "//div[@id='nope']"

    provider.xpath = TILE
    cards = heal.locator_all('cards', CARDS)
    assert len(cards) == 4 and heal.heals[-1] == HealEvent('cards', (CARDS,), TILE, None, 'model')


def test_a_model_s_pick_of_one_element_is_refused(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path, stub_provider(None))
    heal.locator_all('cards', CARDS)
    page.set_html(shop(grid('product-tile', NEW_PRODUCTS), grid="class='catalog'"))
    real = heal.coordinator.try_heal_list
    shown = {}

    def capture(key, meta, broken, context, row=None):
        shown['groups'] = lists.find(meta['listFingerprint'], page.content(), 'div').groups
        return real(key, meta, broken, context, row)
    heal.coordinator.try_heal_list = capture
    heal.coordinator.provider.xpath = '//h1'
    with pytest.raises(AttentionRequired, match='not the xpath of one of the groups listed'):
        heal.locator_all('cards', CARDS)
    assert all(len(g.members) >= 2 for g in shown['groups'])


def test_what_stops_a_list_heal(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path, stub_provider(None), dry_run=True)
    heal.locator_all('cards', CARDS)
    page.set_html(V2)
    with pytest.raises(AttentionRequired, match="dry-run mode is on, so the list fingerprint match"):
        heal.locator_all('cards', CARDS)
    heal.store.set_meta('cards', destructive=True)
    with pytest.raises(AttentionRequired, match="'cards' is marked as a destructive control"):
        heal.locator_all('cards', CARDS)


def test_the_page_guard_stops_a_list_heal(tmp_path, make_page):
    page = make_page(V1, url='https://shop.example/shop', fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cards', CARDS)
    page.url = 'https://shop.example/cart'
    page.set_html(V2)
    with pytest.raises(AttentionRequired, match="'cards' was last found on /shop, and this page "
                                                'is /cart'):
        heal.locator_all('cards', CARDS)


def test_the_local_model_declines_a_list():
    provider = LocalModelProvider(model='m', base_url='http://127.0.0.1:9')
    assert provider.propose({'list': True, 'key': 'cards', 'groups': []}) is None
    assert provider.last_error.startswith('the local model picks one element, and this is a '
                                          'list heal')


def test_the_list_prompt():
    bundle, shown = build_list_bundle(
        'cards', {'expectedRole': 'div', 'listFingerprint': _lfp(), 'intent': 'the products'},
        [{'xpath': CARDS}], [{'xpath': TILE, 'count': 4, 'container': '<div id="grid">',
                              'items': ['<div>Blue mug https://cdn.example/x.png</div>']}],
        {'url': 'https://shop.example/shop', 'page': '/shop', 'lastPage': '/shop'})
    assert shown == {TILE: TILE}
    assert bundle['groups'][0]['items'] == ['<div>Blue mug [URL]</div>']
    prompt = build_prompt(bundle)
    assert prompt['system'].startswith('You repair ONE broken web-automation selector for a LIST')
    assert "Each item's role must be 'div'." in prompt['system']
    assert prompt['user'] == (
        'key: cards\nitemRole: div\nintent: the products\npage: /shop\nlast found on page: /shop'
        '\nitems when it last matched: 4\nbroken candidates:\n' + CARDS +
        '\nrepeated groups (redacted):\n[1] ' + TILE + '\n    4 items in <div id="grid">'
        '\n    item: <div>Blue mug [URL]</div>')


# --- heal.page and heal.driver ----------------------------------------------------------------

def test_heal_page_locator_all_heals(tmp_path, make_page):
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.page.locator('.product-card').all()) == 4
    page.set_html(V2)
    items = heal.page.locator('.product-card').all()
    assert [i.inner_text() for i in items][:1] == ['Blue mug$12.00Add to cart']
    assert heal.heals[0].key == '.product-card' and heal.heals[0].new == TILE
    heal_row = [c for c in heal.store.candidates('.product-card') if c['source'] == 'heal'][0]
    assert 'css' in heal_row                            # the code's selector is CSS


def test_heal_page_all_with_one_or_no_match_stays_as_it_was(tmp_path, make_page):
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path, timeout_ms=60000)
    assert len(heal.page.locator('h1').all()) == 1
    assert heal.page.locator('.nothing').all() == []
    assert heal.store.candidates('h1') == [] and heal.store.candidates('.nothing') == []


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
        if script.startswith('return (' + lists.ITEMS_JS):
            return [{'tag': e.el.tag, 'role': '', 'type': '', 'visible': True} for e in args[0]]
        return super().execute_script(script, *args)


def test_heal_driver_find_elements_heals(tmp_path):
    web = ListDriver(V1)
    heal = _open(web, tmp_path)
    assert len(heal.driver.find_elements('css selector', '.product-card')) == 4
    web.set_html(V2)
    found = heal.driver.find_elements('css selector', '.product-card')
    assert [e.text for e in found][:1] == ['Blue mug$12.00Add to cart']
    assert heal.heals[0].key == 'css selector=.product-card' and heal.heals[0].new == TILE
    assert heal.driver.find_elements('css selector', '.nothing') == []
    assert heal.store.candidates('css selector=.nothing') == []


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


def test_async_lists_heal(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        assert len(await heal.locator_all('cards', CARDS)) == 4
        assert len(await heal.page.locator('.product-card').all()) == 4
        sync.set_html(V2)
        cards = await heal.locator_all('cards', CARDS)
        items = await heal.page.locator('.product-card').all()
        return heal, [await c.inner_text() for c in cards], len(items)
    heal, texts, count = asyncio.run(main())
    assert texts[0] == 'Blue mug$12.00Add to cart' and count == 4
    assert [h.new for h in heal.heals] == [TILE, TILE]
    assert heal.pending() == {'cards': TILE, '.product-card': TILE}


def test_heal_driver_find_elements_with_one_match_saves_nothing(tmp_path):
    web = ListDriver(V1)
    heal = _open(web, tmp_path, timeout_ms=60000)
    assert len(heal.driver.find_elements('xpath', '//h1')) == 1
    assert heal.driver.find_elements('xpath', '//h2') == []
    assert heal.store.candidates('xpath=//h1') == [] and heal.store.candidates('xpath=//h2') == []


def test_a_list_of_links_with_role_heals(tmp_path, make_page):
    v1 = ("<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' "
          "href='/b'>B</a></li><li><a class='m' href='/c'>C</a></li></ul></body></html>")
    page = make_page(v1, fingerprints=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('menu', "//a[@class='m']", role='link')) == 3
    page.set_html(v1.replace("class='m'", "class='menu-link'"))
    links = heal.locator_all('menu', "//a[@class='m']", role='link')
    assert [link.get_attribute('href') for link in links] == ['/a', '/b', '/c']
    assert heal.store.meta('menu')['expectedRole'] == 'link'


def test_a_saved_list_heal_needs_no_list_fingerprint_read_again(tmp_path, make_page):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cards', CARDS)
    reads = page.fingerprint_reads
    heal.locator_all('cards', CARDS)
    assert page.fingerprint_reads == reads      # once per Healer and selector
