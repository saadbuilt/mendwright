"""Lists (mendwright.lists): heal.locator_all() heals a list selector that matches nothing
any more, and heal.page.locator(selector).all() and heal.driver.find_elements() give what
the real call gives, as in 0.7.0. Offline: the lxml-backed FakePage from conftest
(fingerprints=True answers the list fingerprint read), a stub provider, and the fake
Selenium driver of test_proxies.py."""

import asyncio
import json
import re
import time

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


def test_new_products_in_the_same_container_are_left_to_the_model():
    # Where a group sits says nothing about what its items are: none of them is a product
    # the list had, so the list fingerprint does not take them.
    match = lists.find(_lfp(), shop(grid('product-tile', NEW_PRODUCTS)), 'div')
    assert match.xpath is None and match.score >= lists.THRESHOLD
    assert match.reason == ('the best group (score ' + format(match.score, '.2f') + ') shares '
                            'no id, test id, name, link, label or text with the items that '
                            'were recorded')
    assert match.groups[0].xpath == TILE                # the model is shown it


def _form(errors, cls='error'):
    """A signup form: two fields, each with a hint and, with errors, an error."""
    fields = ''
    for name, hint, error in zip(('email', 'password'), HINTS, errors or (None, None), strict=True):
        fields += ("<div class='field'><label for='" + name + "'>" + name.title() + "</label>"
                   "<input id='" + name + "' name='" + name + "'><span class='hint'>" + hint
                   + '</span>' + ("<span class='" + cls + "'>" + error + '</span>' if error else '')
                   + '</div>')
    return "<html><body><form id='signup'>" + fields + '<button>Sign up</button></form></body></html>'


HINTS = ('We never share your email', '8 characters or more')
ERRORS = ('Email is required', 'Password is required')


def test_an_emptied_error_list_is_not_healed_onto_the_hints_next_to_it():
    lfp = _lfp(_form(ERRORS), "//span[@class='error']")
    match = lists.find(lfp, _form(None), 'span')
    assert match.xpath is None and match.score >= lists.THRESHOLD   # alike, and in the form
    assert 'shares no id, test id, name, link, label or text' in match.reason
    # The same errors under a new class name are the list: their text says so.
    match = lists.find(lfp, _form(ERRORS, cls='field-error'), 'span')
    assert match.xpath == "//span[contains(concat(' ', normalize-space(@class), ' '), ' field-error ')]"


def test_two_empty_shapes_say_nothing():
    assert lists.shape_similarity({}, {}) is None
    assert lists.shape_similarity({'b': 1}, {}) == 0.0
    lfp = _lfp(_form(ERRORS), "//span[@class='error']")
    match = lists.find(lfp, _form(ERRORS, cls='field-error'), 'span')
    assert match.groups[0].shape is None                 # the shape check is left out


def _results(cls, names, attrs=''):
    cards = ''.join("<div class='" + cls + "'" + attrs + "><img src='/i/x.png' alt='" + n
                    + "'><h3>" + n + "</h3><span class='price'>$9.00</span></div>" for n in names)
    return ("<html><body><main id='search'><h1>Search</h1><div id='results'>" + cards
            + '</div></main></body></html>')


MUGS = ['Blue mug', 'Red mug', 'Green mug', 'Mug set']
POPULAR = ['Tea pot', 'Kettle', 'Coffee grinder', 'French press']


def test_a_search_with_no_results_is_not_healed_onto_its_recommendations():
    lfp = _lfp(_results('result-card', MUGS), "//div[@class='result-card']")
    match = lists.find(lfp, _results('popular-card', POPULAR), 'div')
    assert match.xpath is None and match.score >= lists.THRESHOLD
    assert 'shares no id, test id, name, link, label or text' in match.reason
    # One recorded item among them is not enough: the list held three that were recorded.
    match = lists.find(lfp, _results('popular-card', ['Blue mug'] + POPULAR), 'div')
    assert match.xpath is None
    assert ('has only 1 of the 3 recorded items among its elements (an element that shares an '
            'id, test id, name, link, label or text with one), and 2 are needed, so it may be '
            'another list in the same place') in match.reason
    # What every recorded item shares, such as one test id on each card, is not which items.
    tested = " data-testid='card'"
    lfp = _lfp(_results('result-card', MUGS, tested), "//div[@class='result-card']")
    match = lists.find(lfp, _results('popular-card', POPULAR, tested), 'div')
    assert match.xpath is None
    assert 'shares an id, test id, name, link, label or text only with every recorded item' in (
        match.reason)
    assert lists.find(lfp, _results('popular-card', MUGS, tested), 'div').reason == ''


def test_a_long_list_whose_class_changed_is_found():
    # The recorded items are the first, the middle and the last of 450: the last two are
    # far past the first members a group is looked through for.
    many = [('Mug ' + str(i), '$' + str(i) + '.00') for i in range(450)]
    lfp = _lfp(shop(grid('product-card', many)))
    match = lists.find(lfp, shop(grid('product-tile', many)), 'div')
    assert match.xpath == TILE and match.count == 450 and match.reason == ''


def _skeleton():
    return ("<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
            "<button type='button' disabled></button></div>")


def test_the_placeholders_of_a_list_that_is_loading_are_no_list():
    match = lists.find(_lfp(), shop(_skeleton() * 4), 'div')
    assert match.xpath is None and match.groups == []
    assert match.reason == ('the only groups of div elements look like placeholders (no text '
                            'where the recorded items had text, or marked aria-busy="true"), '
                            'so the list may still be loading')
    busy = shop(grid('product-tile'), grid="id='grid' class='grid' aria-busy='true'")
    assert lists.find(_lfp(), busy, 'div').reason.startswith('the only groups of div')
    # Placeholders after the cards, as an infinite scroll loads more, are no rival.
    more = shop(grid('product-tile') + _skeleton() * 4)
    assert lists.find(_lfp(), more, 'div').xpath == TILE


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
    # One that would tie with it, with every product the list had and a container of the
    # same tag and class, is left out too: it is not inside div#grid.
    recent = "<div id='recent' class='grid'>" + grid('product-tile') + '</div>'
    match = lists.find(_lfp(), shop(grid('product-tile'), extra=recent), 'div')
    assert match.reason == '' and match.count == 4 and match.runner_up == 0.0
    assert match.xpath.startswith("//div[@id='grid']/")


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


def test_heal_page_locator_all_never_heals_a_list_heal_locator_all_recorded(
        tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(TILE)
    heal = _open(page, tmp_path, provider, timeout_ms=60000)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    assert heal.page.locator('.product-card').all() == []    # no wait, no heal, no model
    assert heal.heals == [] and provider.bundles == []
    assert len(heal.locator_all('.product-card', '.product-card', timeout_ms=0)) == 4
    assert heal.page.locator('.product-card').all() == []    # not through the list heal
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
        if script.startswith('return (' + lists.ITEMS_JS):
            return [{'tag': e.el.tag, 'role': '', 'type': '', 'visible': True} for e in args[0]]
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
        return heal, [await c.inner_text() for c in cards], items
    heal, texts, items = asyncio.run(main())
    assert texts[0] == 'Blue mug$12.00Add to cart' and items == []
    assert [h.new for h in heal.heals] == [TILE]
    assert heal.pending() == {'cards': TILE}
    assert heal.store.candidates('.product-card') == []      # heal.page saved nothing


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


# --- review fixes ---------------------------------------------------------------------------

def test_a_list_heal_is_saved_marked_and_needs_a_store_that_marks_it(tmp_path):
    store = _store(tmp_path)
    assert apply_list(store, 'cards', TILE, V2, store.meta('cards'), driver=LiveDriver(V2))[1] == 'ok'
    again = load_store(None, str(tmp_path / 'o.json'))
    assert [c.get('list') for c in again.candidates('cards') if c['source'] == 'heal'] == [True]

    old = _store(tmp_path / 'old')
    real = old.add_candidate

    def add_candidate(key, xpath, *, source='heal', frame_xpath=None, css=None):
        return real(key, xpath, source=source, frame_xpath=frame_xpath, css=css)
    old.add_candidate = add_candidate                    # a store from before list heals
    applied, reason, _count = apply_list(old, 'cards', TILE, V2, old.meta('cards'),
                                         driver=LiveDriver(V2))
    assert applied is None and reason.startswith('the store cannot mark a list heal')
    assert [c for c in old.candidates('cards') if c['source'] == 'heal'] == []


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
        assert len(heal.locator_all(key, selector)) == 4
    assert heal.driver.find_element('css selector', '.product-card').text.startswith('Blue mug')
    found = heal.locator_all(key, selector)
    assert [e.native.text[:7] for e in found] == ['Blue mu', 'Red mug', 'Green m', 'Tea pot']
    assert sorted(h.source for h in heal.heals) == ['fingerprint', 'model']


def test_a_heal_page_shortcut_s_heal_never_answers_for_the_list(tmp_path, make_page, stub_provider):
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(None)
    heal = _open(page, tmp_path, provider)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    provider.xpath = "//div[@class='product-tile'][h3='Red mug']"
    heal.page.click('.product-card')                      # a heal of one card
    assert len(heal.locator_all('.product-card', '.product-card')) == 4


def test_a_list_that_is_still_loading_is_not_healed_onto_its_placeholders(tmp_path):
    web = ListDriver(V1)
    heal = _open(web, tmp_path)
    heal.locator_all('cards', '.product-card')
    web.set_html(shop(_skeleton() * 4))
    with pytest.raises(AttentionRequired, match='so the list may still be loading'):
        heal.locator_all('cards', '.product-card')
    assert heal.heals == []
    assert [c for c in heal.store.candidates('cards') if c['source'] == 'heal'] == []


def test_a_list_that_comes_while_mendwright_looks_for_it_is_not_healed(tmp_path, make_page):
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('cards', '.product-card')) == 4
    page.set_html(shop(''))                               # the grid is loading
    capture = heal.coordinator.html_provider

    def cards_arrive():
        page.set_html(V1)                                 # as the page is captured
        return capture()
    heal.coordinator.html_provider = cards_arrive
    assert len(heal.locator_all('cards', '.product-card')) == 4
    assert heal.heals == []
    assert [c for c in heal.store.candidates('cards') if c['source'] == 'heal'] == []
    store = _store(tmp_path / 'b', code=CARDS)
    applied, reason, _count = apply_list(store, 'cards', TILE, V2, store.meta('cards'),
                                         driver=LiveDriver(shop(grid('product-tile') + grid('product-card'))))
    assert applied is None and reason == ('the selector in your code, ' + CARDS + ', matches 4 '
                                          'elements on the live page now, so the list needs no '
                                          'heal')


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
def test_with_list_heals_off_a_list_that_matches_nothing_is_empty_at_once(
        tmp_path, make_page, monkeypatch, off):
    page = make_page(V1, fingerprints=True, css=True)
    first = _open(page, tmp_path)
    first.locator_all('cards', CARDS)
    page.set_html(V2)
    assert len(first.locator_all('cards', CARDS)) == 4    # healed and saved
    kwargs = {'fingerprint': False}
    if off == 'MENDWRIGHT_DISABLE=1':
        monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
        kwargs = {}
    heal = _open(page, tmp_path, timeout_ms=60000, **kwargs)   # it would wait a minute
    assert len(heal.locator_all('cards', CARDS)) == 4     # the saved list heal still answers
    page.set_html(shop(grid('product-row')))
    assert heal.locator_all('cards', CARDS) == []
    assert heal.heals == []

    web = ListDriver(V1)
    _open(web, tmp_path / 'driver').locator_all('cards', '.product-card')
    web.set_html(shop(''))
    driver = _open(web, tmp_path / 'driver', timeout_ms=60000, **kwargs)
    assert driver.locator_all('cards', '.product-card') == []


def test_async_list_heals_are_off_with_the_fingerprint_tier(tmp_path, make_page):
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


def test_a_guard_stops_a_list_heal(tmp_path, make_page):
    page = make_page(V1, fingerprints=True)
    allowed = [True]
    heal = _open(page, tmp_path, guard=lambda: allowed[0])
    heal.locator_all('cards', CARDS)
    page.set_html(V2)
    allowed[0] = False
    with pytest.raises(AttentionRequired, match='a transaction guard is currently blocking heals'):
        heal.locator_all('cards', CARDS)
    assert heal.heals == []
    allowed[0] = True
    assert len(heal.locator_all('cards', CARDS)) == 4


def test_a_list_heal_of_this_run_is_not_recorded_until_confirmed(tmp_path, make_page):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cards', CARDS)
    recorded = heal.store.meta('cards')['listFingerprint']
    page.set_html(V2)
    heal.locator_all('cards', CARDS)                      # heals
    heal.locator_all('cards', CARDS)                      # through the heal, still pending
    assert heal.pending() == {'cards': TILE}
    assert heal.store.meta('cards')['listFingerprint'] == recorded


def test_the_reads_of_a_list_found_through_a_heal_are_checked(tmp_path, make_page):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    assert heal.locator_all('cards', CARDS, expect_value='money')[0].inner_text() == (
        'Blue mug$12.00Add to cart')                      # the code's selector: never checked
    page.set_html(V2)
    cards = heal.locator_all('cards', CARDS, expect_value=re.compile('mug|pot'))
    assert [c.inner_text()[:7] for c in cards] == ['Blue mu', 'Red mug', 'Green m', 'Tea pot']
    assert heal.pending() == {'cards': TILE}              # a read that passes confirms nothing
    cards = heal.locator_all('cards', CARDS, expect_value='money')
    with pytest.raises(AttentionRequired, match="'cards' was healed to " + re.escape(TILE)
                       + r", and its inner_text\(\) there is 'Blue mug\$12.00Add to cart', "
                       'which is not money'):
        cards[0].inner_text()
    assert heal.store.status('cards', TILE) == 'quarantined' and heal.pending() == {}
    canary = [e for e in _journal(tmp_path) if e['event'] == 'heal.canary']
    assert [(e['success'], e['reason']) for e in canary] == [
        (False, 'expect_value: inner_text() is not money')]


def test_open_expect_values_check_the_reads_of_heal_locator_all_lists(tmp_path, make_page):
    page = make_page(V1, fingerprints=True, css=True)
    heal = _open(page, tmp_path, expect_values={'.product-card': 'money'})
    assert heal.locator_all('.product-card', '.product-card')[0].inner_text().startswith('Blue')
    page.set_html(V2)
    items = heal.locator_all('.product-card', '.product-card')
    with pytest.raises(AttentionRequired, match="'.product-card' was healed to"):
        items[1].inner_text()
    assert heal.store.status('.product-card', TILE) == 'quarantined'
    # heal.page's Locator.all() is never healed, so its reads are never checked.
    page.set_html(V1)
    assert heal.page.locator('.product-card').all()[0].inner_text().startswith('Blue')


def test_async_lists_optional_reads_and_item_actions(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        sync.set_html(V2)
        assert await heal.locator_all('cards', CARDS, optional=True) == []
        assert heal.heals == []
        cards = await heal.locator_all('cards', CARDS, expect_value=re.compile('mug|pot'))
        assert await cards[0].inner_text() == 'Blue mug$12.00Add to cart'
        await cards[1].click(expect=lambda: True)         # an item's action confirms nothing
        pending = heal.pending()
        cards = await heal.locator_all('cards', CARDS, expect_value='money')
        with pytest.raises(AttentionRequired, match="'cards' was healed to"):
            await cards[2].inner_text()
        return heal, sync, pending
    heal, sync, pending = asyncio.run(main())
    assert pending == {'cards': TILE} and sync.actions[-1][1] == 'click'
    assert heal.store.status('cards', TILE) == 'quarantined' and heal.pending() == {}


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


def test_async_a_list_that_comes_while_mendwright_looks_for_it_is_not_healed(tmp_path, make_page):
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        sync.set_html(shop(''))                           # the grid is loading
        capture = heal.coordinator.html_provider

        def cards_arrive():
            sync.set_html(V1)                             # as the page is captured
            return capture()
        heal.coordinator.html_provider = cards_arrive
        return heal, await heal.locator_all('cards', CARDS)
    heal, cards = asyncio.run(main())
    assert len(cards) == 4 and heal.heals == []


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


def test_async_a_run_function_s_list_lookups_and_checked_reads_skip_the_lock(tmp_path, make_page):
    # The step that runs the function holds the healer's lock while it waits for it, so the
    # function's list lookups, and the checked reads of a list found through a heal, go ahead
    # without the lock, as its other calls do. Taking it would wait for that step forever.
    async def main():
        sync = make_page(V1, fingerprints=True, css=True)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cards', CARDS)
        seen = []

        async def relogin(page):
            cards = await heal.locator_all('cards', CARDS, expect_value=re.compile('mug|pot'))
            seen.append([await card.inner_text() for card in cards])   # through the heal
            seen.append(len(await page.locator('.product-tile').all()))
            sync.set_html(V2)                             # signed in again: the popup is gone

        heal.interrupt('relogin', '#relogin', run=relogin)
        sync.set_html(BEHIND_RELOGIN)
        await asyncio.wait_for(heal.locator('heading', 'xpath=//h1').click(), 5)
        return heal, seen, sync.actions
    heal, seen, actions = asyncio.run(main())
    assert seen == [[n + p + 'Add to cart' for n, p in PRODUCTS], 4]
    assert actions[-1][1] == 'click'
    assert [(h.key, h.new) for h in heal.heals] == [('cards', TILE)]
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('relogin', 'heading')]


# --- a list heal and a heal of one element under one key -------------------------------------

FIRST = "//div[contains(@class,'product-tile')][h3='Blue mug']"


def _both(tmp_path, make_page, stub_provider):
    """A healer whose key '.product-card' has, in this run, a list heal (heal.locator_all())
    and a heal of the first card (a heal.page shortcut, through the model)."""
    page = make_page(V1, fingerprints=True, css=True)
    provider = stub_provider(FIRST)
    heal = _open(page, tmp_path, provider)
    assert len(heal.locator_all('.product-card', '.product-card')) == 4
    page.set_html(V2)
    cards = heal.locator_all('.product-card', '.product-card')     # the list heal
    assert heal.page.inner_text('.product-card').startswith('Blue mug')   # the first card's
    assert [(h.new, h.source) for h in heal.heals] == [(TILE, 'fingerprint'), (FIRST, 'model')]
    return heal, cards, provider


def _canaries(tmp_path):
    return sorted((e['xpath'], e['success']) for e in _journal(tmp_path)
                  if e['event'] == 'heal.canary')


def test_a_list_heal_and_a_heal_of_one_element_of_one_key_are_confirmed_each_on_its_own(
        tmp_path, make_page, stub_provider):
    heal, _cards, provider = _both(tmp_path, make_page, stub_provider)
    assert heal.pending() == {'.product-card': FIRST}     # one key: the element's heal shows
    assert heal.confirm_all(True) == 2
    assert [(h.new, h.confirmed) for h in heal.heals] == [(TILE, True), (FIRST, True)]
    assert _canaries(tmp_path) == sorted([(TILE, True), (FIRST, True)])
    assert heal.pending() == {} and heal.confirm_all(True) == 0
    assert provider.outcomes == [('.product-card', FIRST, True)]   # the model's own heal only


def test_a_list_element_s_confirm_and_failed_read_are_the_list_heal_s(
        tmp_path, make_page, stub_provider):
    heal, cards, _provider = _both(tmp_path, make_page, stub_provider)
    cards[1].confirm(True)                                 # an element of the list says so
    assert [(h.new, h.confirmed) for h in heal.heals] == [(TILE, True), (FIRST, None)]
    assert heal.pending() == {'.product-card': FIRST}
    heal.confirm_all(False)                               # the test failed: the element's heal
    assert heal.store.status('.product-card', FIRST) == 'quarantined'
    assert heal.store.status('.product-card', TILE) == 'active'

    heal, _cards, _provider = _both(tmp_path / 'b', make_page, stub_provider)
    cards = heal.locator_all('.product-card', '.product-card', expect_value='money')
    with pytest.raises(AttentionRequired, match="'.product-card' was healed to " + re.escape(TILE)):
        cards[0].inner_text()
    assert heal.store.status('.product-card', TILE) == 'quarantined'
    assert heal.store.status('.product-card', FIRST) == 'active'
    assert heal.pending() == {'.product-card': FIRST}
