"""heal.locator_all(key, selector, nonempty=True): a list that always has items at that point.
When none of the key's selectors matches, mendwright waits, looks for the list from its list
fingerprint (lists.find) and saves what it finds as a proposed list heal for a person to
check, and raises. It never gives or acts on a list it found: heal.locator_all() gives only
what the key's own selectors match. Offline: conftest's lxml-backed FakePage, the fake
Selenium driver of test_proxies.py, no browser and no model (WrongPicker, the stub model of
test_lists_emptied.py that takes the other list, is never asked)."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, lists
from mendwright.aio import SyncBridge
from mendwright.client import list_gate
from mendwright.drivers.base import (driver_list_fingerprint, driver_list_items, driver_places,
                                     item_rows)
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.gate import list_live_reason
from mendwright.store import edit_list_heal, load_store

from test_lists import CARDS, PRODUCTS, TILE, V1, V2, ListDriver, _Page, card, grid, shop
from test_lists_emptied import cart, todomvc


class PlacedListDriver(ListDriver):
    """ListDriver that answers LIST_PLACE_JS and ITEMS_JS too."""

    def execute_script(self, script, *args):
        if script.startswith('return (' + lists.LIST_PLACE_JS):
            return lists.raw_of_elements([e.el for e in args[0]], place=True)
        if script.startswith('return (' + lists.ITEMS_JS):
            return lists.items_of_elements([e.el for e in args[0]])
        return super().execute_script(script, *args)


# --- the drivers ------------------------------------------------------------------------------

class _Plain:
    """A custom driver whose list_fingerprint() is 0.8.0's: no place=."""

    def __init__(self):
        self.calls = []

    def list_fingerprint(self, selector, frame_chain=()):
        self.calls.append((selector, frame_chain))
        return lists.of_elements(lxml.html.fromstring(V1).xpath(CARDS))


class _Keywords(_Plain):
    def list_fingerprint(self, selector, frame_chain=(), **options):
        self.calls.append((selector, frame_chain, options))
        return lists.of_elements(lxml.html.fromstring(V1).xpath(CARDS),
                                 place=options.get('place', False))


class _Broken(_Plain):
    def list_fingerprint(self, selector, frame_chain=(), *, place=False):
        raise TypeError('a bug inside the driver')


def test_place_is_asked_only_of_a_list_fingerprint_that_takes_it():
    plain, keywords = _Plain(), _Keywords()
    assert not driver_places(plain) and driver_places(keywords) and not driver_places(object())
    lfp = driver_list_fingerprint(plain, CARDS, place=True)
    assert plain.calls == [(CARDS, ())] and lists.valid(lfp) and not lists.placed(lfp)
    assert lists.placed(driver_list_fingerprint(keywords, CARDS, place=True))
    assert keywords.calls[-1] == (CARDS, (), {'place': True})
    driver_list_fingerprint(keywords, CARDS)
    assert keywords.calls[-1] == (CARDS, (), {})             # a default call is 0.8.0's
    # A TypeError from inside the driver is an error like any other, not a sign to retry.
    assert driver_list_fingerprint(_Broken(), CARDS, place=True) is None


def test_list_items_of_a_driver_without_it_is_none_and_errors_raise():
    assert driver_list_items(object(), CARDS) is None

    class Failing:
        def list_items(self, selector, frame_chain=()):
            raise RuntimeError('the page went away')
    with pytest.raises(RuntimeError):
        driver_list_items(Failing(), CARDS)
    assert item_rows([{'tag': 'LI'}, 'junk', {'tag': 'input', 'type': 'checkbox',
                                             'visible': True}]) == [
        {'tag': 'li', 'role': 'li', 'type': '', 'visible': False},
        {'tag': 'input', 'role': 'checkbox', 'type': 'checkbox', 'visible': True}]
    with pytest.raises(ValueError):
        item_rows(None)


def test_the_playwright_driver_reads_place_and_items(make_page):
    page = make_page(V1, fingerprints=True)
    driver = PlaywrightSyncDriver(page)
    assert lists.placed(driver.list_fingerprint('xpath=' + CARDS, place=True))
    assert not lists.placed(driver.list_fingerprint('xpath=' + CARDS))
    assert (page.list_reads, page.place_reads) == (1, 1)
    rows = driver.list_items('xpath=' + CARDS)
    assert [r['role'] for r in rows] == ['div'] * 4 and all(r['visible'] for r in rows)
    assert page.items_reads == 1 and driver_places(driver)

    def broken(js):
        raise RuntimeError('Target closed')
    page.locator = lambda selector: type('L', (), {'evaluate_all': staticmethod(broken)})()
    with pytest.raises(RuntimeError, match='Target closed'):
        driver.list_items('xpath=' + CARDS)


def test_the_selenium_driver_reads_place_and_items():
    web = PlacedListDriver(V1)
    driver = SeleniumDriver(web)
    assert lists.placed(driver.list_fingerprint('css=.product-card', place=True))
    assert [r['tag'] for r in driver.list_items('css=.product-card')] == ['div'] * 4
    assert driver_places(driver)


def test_the_sync_bridge_has_the_list_methods_the_async_driver_has():
    class Async:
        async def count(self, selector, frame_chain=()):
            return 0

        async def is_visible(self, selector, frame_chain=()):
            return False

        async def handle(self, selector, frame_chain=()):
            return None

        async def list_fingerprint(self, selector, frame_chain=()):
            return None

    async def main():
        bridge = SyncBridge(Async(), asyncio.get_running_loop())
        return bridge
    bridge = asyncio.run(main())
    assert bridge.list_items is None and driver_list_items(bridge, CARDS) is None
    assert callable(bridge.list_fingerprint) and not driver_places(bridge)


# --- the list gate ----------------------------------------------------------------------------

class LiveDriver:
    """count() and list_items() over a page, for the list gate."""

    def __init__(self, html, hidden=False, fail=False):
        self.doc = lxml.html.fromstring(html)
        self.hidden, self.fail = hidden, fail

    def _els(self, selector):
        body = selector[len('xpath='):] if selector.startswith('xpath=') else selector
        if not body.startswith('/'):
            from lxml.cssselect import CSSSelector
            return CSSSelector(body)(self.doc)
        return [e for e in self.doc.xpath(body) if hasattr(e, 'tag')]

    def count(self, selector, frame_chain=()):
        return len(self._els(selector))

    def list_items(self, selector, frame_chain=()):
        if self.fail:
            raise RuntimeError('Target closed')
        return [{'tag': e.tag, 'role': e.tag, 'type': '', 'visible': not self.hidden}
                for e in self._els(selector)]


def test_the_live_check_of_a_list():
    meta = {'expectedRole': 'div'}
    assert list_live_reason(LiveDriver(V2), TILE, meta) == ''
    for driver in (None, object(), LiveDriver(V2, fail=True)):
        assert list_live_reason(driver, TILE, meta) == 'the live page could not be read'
    assert list_live_reason(LiveDriver(V2, hidden=True), TILE, meta) == (
        'none of its 4 elements is visible on the live page')
    assert list_live_reason(LiveDriver(V2), '//h1', meta) == (
        'it matches 1 element on the live page, and a list heal needs 2 or more')
    assert list_live_reason(LiveDriver(V2), TILE, {'expectedRole': 'li'}) == (
        'live element role mismatch: got div want li')


def _gate_store(tmp_path, code=CARDS):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('cards', [code])
    store.set_meta('cards', role='div', list_fingerprint=lists.of_elements(
        lxml.html.fromstring(V1).xpath(CARDS), place=True))
    return store


def test_the_list_gate(tmp_path):
    pytest.importorskip('cssselect')     # the live page counts the CSS form with it
    store = _gate_store(tmp_path)
    meta = store.meta('cards')
    live = LiveDriver(V2)
    assert list_gate(store, 'cards', TILE, V2, meta, live, 'div.product-tile') == (
        'ok', 'ok', 4, None)                                      # the code's selector is XPath
    css = _gate_store(tmp_path / 'css', '.product-card')
    assert list_gate(css, 'cards', TILE, V2, css.meta('cards'), live, 'div.product-tile') == (
        'ok', 'ok', 4, 'div.product-tile')
    assert list_gate(css, 'cards', TILE, V2, css.meta('cards'), live, 'div.nope')[3] is None
    assert list_gate(store, 'cards', '//section/div', V2, meta, live)[:2] == ('refused', 'no match')
    assert list_gate(store, 'cards', "//div[@id='grid']/div[2]", V2, meta, live)[1] == (
        lists.POSITIONAL)
    assert list_gate(store, 'cards', '//h1', V2, meta, live)[1] == (
        'it matches 1 element, and a list heal needs 2 or more')
    assert list_gate(store, 'cards', TILE, V2, meta, LiveDriver(V2, hidden=True))[1] == (
        'none of its 4 elements is visible on the live page')
    assert list_gate(store, 'cards', TILE, V2, meta, LiveDriver(V2, fail=True))[1] == (
        'the live page could not be read')
    # The list came while mendwright looked: the code's selector matches now.
    assert list_gate(store, 'cards', TILE, V1, meta, LiveDriver(V1)) == ('came', CARDS, 0, None)


def test_the_list_gate_refuses_what_a_rejection_holds_and_names_an_acceptance(tmp_path):
    store = _gate_store(tmp_path)
    meta = store.meta('cards')
    live = LiveDriver(V2)
    store.propose_list_heal('cards', TILE, proposal={'id': 'a', 'at': 'b', 'count': 4})
    assert list_gate(store, 'cards', TILE, V2, meta, live)[0] == 'ok'     # proposed again
    store._find('cards', TILE)['status'] = 'accepted'
    assert list_gate(store, 'cards', TILE, V2, meta, live)[0] == 'accepted'
    store.quarantine('cards', TILE)
    assert list_gate(store, 'cards', TILE, V2, meta, live)[1] == REJECTED
    other = "//div[@id='grid']/div"                                    # the same elements
    assert list_gate(store, 'cards', other, V2, meta, live)[1] == (
        'it takes elements of a list heal you rejected, or of a quarantined selector of this key')


# --- heal.locator_all(..., nonempty=True) ---------------------------------------------------------

NO_MATCH = 'No matching element for: cart (tried 1 candidate(s)).\n  -> '
ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]"
REJECTED = ('it is a list heal you rejected, or one mendwright quarantined: it quarantines a '
            'list heal you accepted once the selector it replaced matches again, even on one '
            'element')


def _open(page, root, provider=None, **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(root / 'o.json'), **kwargs)


def _journal(root):
    path = root / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _events(root, *names):
    return [e for e in _journal(root) if e['event'] in names]


def _rows(root, key='cart'):
    path = root / 'o.json'
    if not path.exists():
        return {}
    entry = json.loads(path.read_text(encoding='utf-8'))['selectors'].get(key) or {}
    return {c['xpath']: c for c in entry.get('candidates') or []}


def _shop(make_page, html=None, url='https://shop.test/cart', **kwargs):
    return make_page(html or cart(True), url=url, fingerprints=True, css=True, **kwargs)


def _recorded(make_page, root, **kwargs):
    """A cart page whose list 'cart' was recorded with nonempty=True, and its healer."""
    page = _shop(make_page)
    heal = _open(page, root, **kwargs)
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
    return page, heal


def _raised(heal, *args, **kwargs):
    """The text of the AttentionRequired a lookup of 'cart' with nonempty=True raises."""
    kwargs.setdefault('nonempty', True)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cart', *(args or ('.cart-item',)), **kwargs)
    return str(info.value)


def _flag(root):
    return ' --store "' + str(root / 'o.json') + '"'


# --- the arguments ------------------------------------------------------------------------------

def test_nonempty_takes_only_true_or_false(tmp_path, make_page):
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    for value, name in ((1, 'int'), ('yes', 'str'), (None, 'NoneType')):
        with pytest.raises(TypeError) as info:
            heal.locator_all('cart', '.cart-item', nonempty=value)
        assert str(info.value) == ("mendwright: heal.locator_all('cart', ..., nonempty=...) takes "
                                   'True or False, got ' + name + '.')
    with pytest.raises(ValueError) as info:
        heal.locator_all('cart', '.cart-item', nonempty=True, optional=True)
    assert str(info.value) == ("mendwright: heal.locator_all('cart', ..., nonempty=True, "
                               'optional=True) asks for two opposite things: nonempty=True says '
                               'the list always has items here, and optional=True says it may be '
                               'empty. Pass one of them.')
    assert page.queries == [] and not (tmp_path / 'o.json').exists()


def test_async_nonempty_is_checked_at_the_call(tmp_path, make_page):
    async def main():
        sync = _shop(make_page)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        with pytest.raises(TypeError, match='takes True or False, got int'):
            heal.locator_all('cart', '.cart-item', nonempty=1)       # not awaited
        with pytest.raises(ValueError, match='asks for two opposite things'):
            heal.locator_all('cart', '.cart-item', nonempty=True, optional=1)
        return sync
    sync = asyncio.run(main())
    assert sync.queries == [] and not (tmp_path / 'o.json').exists()


# --- recording where the list sits ------------------------------------------------------------------

def test_nonempty_records_where_the_list_sits_and_its_heading(tmp_path, make_page):
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    heal.locator_all('cart', '.cart-item')                            # a default call first
    assert not lists.placed(heal.store.meta('cart')['listFingerprint'])
    heal.locator_all('cart', '.cart-item', nonempty=True)             # not blocked by it
    lfp = heal.store.meta('cart')['listFingerprint']
    assert lists.placed(lfp) and lfp['label'] == 'Shopping cart'
    assert (page.list_reads, page.place_reads) == (1, 1)
    # Once placed, a default call of a later run records it placed again (sticky).
    again = _open(page, tmp_path)
    again.locator_all('cart', '.cart-item')
    assert page.place_reads == 2 and page.list_reads == 1
    assert lists.placed(load_store(None, str(tmp_path / 'o.json')).meta('cart')['listFingerprint'])


def test_a_read_of_the_place_that_fails_keeps_the_stored_place(tmp_path, make_page):
    _recorded(make_page, tmp_path)
    blind = _shop(make_page)
    blind.fingerprints = False                                        # the read gives nothing
    _open(blind, tmp_path).locator_all('cart', '.cart-item', nonempty=True)
    assert lists.placed(load_store(None, str(tmp_path / 'o.json')).meta('cart')['listFingerprint'])


class UnplacedDriver:
    """A custom driver whose list_fingerprint() is 0.8.0's: it takes no place=."""

    def __init__(self, page):
        self._inner = PlaywrightSyncDriver(page)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def count(self, selector, frame_chain=()):
        return self._inner.count(selector, frame_chain)

    def is_visible(self, selector, frame_chain=()):
        return self._inner.is_visible(selector, frame_chain)

    def handle(self, selector, frame_chain=()):
        return self._inner.handle(selector, frame_chain)

    def list_fingerprint(self, selector, frame_chain=()):
        return self._inner.list_fingerprint(selector, frame_chain)


def test_a_driver_that_cannot_record_a_place_records_the_plain_list_and_says_so(tmp_path,
                                                                                make_page):
    page = _shop(make_page)
    heal = _open(UnplacedDriver(page), tmp_path)
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
    lfp = heal.store.meta('cart')['listFingerprint']
    assert lists.valid(lfp) and not lists.placed(lfp) and page.place_reads == 0
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) == NO_MATCH + (
        "nonempty=True says 'cart' has items here, and none of its selectors matches. The driver "
        "of this page can't record where a list sits (its list_fingerprint() takes no place=), "
        "so mendwright can't check a list it finds. If the selector broke, change it in your "
        'code.')


def test_a_driver_that_cannot_record_a_place_never_replaces_a_placed_list(tmp_path, make_page):
    page, _ = _recorded(make_page, tmp_path)                          # placed by another driver
    placed = load_store(None, str(tmp_path / 'o.json')).meta('cart')['listFingerprint']
    heal = _open(UnplacedDriver(page), tmp_path)
    for nonempty in (True, False):
        assert len(heal.locator_all('cart', '.cart-item', nonempty=nonempty)) == 3
    assert page.list_reads >= 1                                       # it read the plain list
    assert load_store(None, str(tmp_path / 'o.json')).meta('cart')['listFingerprint'] == placed


# --- a proposal -----------------------------------------------------------------------------

def test_a_list_whose_rows_were_renamed_is_proposed_and_never_used(tmp_path, make_page, caplog):
    page, heal = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        message = _raised(heal)
    row = _rows(tmp_path)[ROWS]
    pid = row['proposal']['id']
    flag = _flag(tmp_path)
    assert message == NO_MATCH + (
        "'cart' matched 3 elements when its list fingerprint was recorded, under 'Shopping cart', "
        'in <section class="cart">, and none of its selectors matches now. nonempty=True says '
        'the list has items here, so mendwright looked for it and found 3 div elements, under '
        "'Shopping cart', in <section class=\"cart\">, that hold 3 of the 3 items it recorded: "
        + ROWS + ' (as CSS: div.cart-row). mendwright never uses a list it finds, because the '
        'items of an emptied list can show in another list on the page. If this is the list, '
        'look at it with: mendwright lists' + flag + '. Accept it with: mendwright lists accept '
        'cart ' + pid + flag + '. Then write it into your code with: mendwright fix --apply'
        + flag + '. If the list can be empty here, remove nonempty=True.')
    assert row['status'] == 'proposed' and row['list'] is True and row['css'] == 'div.cart-row'
    assert row['proposal']['was']['selectors'] == ['.cart-item']
    assert heal.coordinator.explain('cart') == message[len(NO_MATCH):]
    assert heal.heals == [] and heal.pending() == {}
    assert heal.coordinator.held == [{'key': 'cart', 'xpath': ROWS, 'id': pid,
                                      'label': 'Shopping cart', 'count': 3}]
    warned = [r.getMessage() for r in caplog.records if 'found a list' in r.getMessage()]
    assert len(warned) == 1 and warned[0].startswith(
        "mendwright: found a list for 'cart' (3 div elements under 'Shopping cart'): .cart-item -> "
        + ROWS + ' (')
    assert warned[0].endswith('s). It is not used. Check it with: mendwright lists')
    events = _journal(tmp_path)
    assert [e['event'] for e in events][-3:] == ['heal.requested', 'heal.proposed', 'heal.held']
    held = events[-1]
    assert {k: held[k] for k in ('key', 'provider', 'xpath', 'css', 'id', 'list', 'items',
                                  'label', 'differs', 'old', 'page', 'source')} == {
        'key': 'cart', 'provider': 'fingerprint', 'xpath': ROWS, 'css': 'div.cart-row',
        'id': pid, 'list': True, 'items': 3, 'label': 'Shopping cart', 'differs': [],
        'old': ['.cart-item'], 'page': '/cart', 'source': 'fingerprint'}
    assert events[-2]['reasoning'].endswith(', 3 elements, 3 of 3 recorded items')
    # Nothing uses it: the next lookup, in this run and a fresh one, looks again and raises.
    for run in (heal, _open(page, tmp_path)):
        assert _raised(run).startswith(NO_MATCH + "'cart' matched 3 elements")
        assert len(_rows(tmp_path)) == 2                              # the same row, again
    assert _rows(tmp_path)[ROWS]['proposal']['id'] == pid             # the same page, the same id


def test_another_page_state_is_a_proposal_of_its_own(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    _raised(heal)
    first = _rows(tmp_path)[ROWS]['proposal']['id']
    more = [('Blue mug', 'blue-mug', '$12.00'), ('Red mug', 'red-mug', '$13.00'),
            ('Tea pot', 'tea-pot', '$29.00'), ('Fork', 'fork', '$1')]
    page.set_html(cart(True, row='cart-row').replace(
        "<div class='cart-row'><a href='/p/tea-pot'>", _row_html('cart-row', more[3])
        + "<div class='cart-row'><a href='/p/tea-pot'>"))
    _raised(heal)
    assert _rows(tmp_path)[ROWS]['proposal']['id'] != first           # what it holds changed
    page.set_html(cart(True, row='basket-line'))
    message = _raised(heal)
    proposed = [c for c in _rows(tmp_path).values() if c.get('status') == 'proposed']
    assert len(proposed) == 2 and 'basket-line' in message


def _row_html(cls, item):
    name, slug, price = item
    return ("<div class='" + cls + "'><a href='/p/" + slug + "'>" + name + "</a><span "
            "class='price'>" + price + "</span><input class='qty' value='1'><button "
            "class='delete'>Delete</button></div>")


def test_what_is_not_the_same_is_said_in_the_message(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row').replace('<h1>Shopping cart</h1>', '<h1>Your bag</h1>')
                  .replace("<section class='cart'>", "<section class='bag'>"))
    message = _raised(heal)
    assert (" The heading is not the same: the list was under 'Shopping cart', and these are "
            "under 'Your bag'. The container is not the same: the list was in <section "
            'class="cart">, and these are in <section class="bag">. mendwright never uses') in (
        message)
    assert _rows(tmp_path)[ROWS]['proposal']['differs'] == ['heading', 'container']


def test_page_text_is_made_printable_in_the_store_and_the_message(tmp_path, make_page):
    # lxml keeps a C1 control character (CSI, \x9b) in a page's text, as a browser does.
    page = make_page(cart(True).replace('Shopping cart', 'Shopping\x9b31mcart'),
                     url='https://shop.test/cart', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cart', '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row').replace('Shopping cart', 'Shopping\x9b31mcart'))
    message = _raised(heal)
    proposal = _rows(tmp_path)[ROWS]['proposal']
    assert proposal['was']['label'] == proposal['now']['label'] == 'Shopping?31mcart'
    assert "under 'Shopping?31mcart'" in message and '\x9b' not in message


def test_a_key_that_needs_quotes_is_quoted_for_the_shell(tmp_path, make_page):
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    heal.locator_all('my cart', '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('my cart', '.cart-item', nonempty=True)
    assert 'mendwright lists accept "my cart" ' in str(info.value)


def test_a_key_with_a_control_character_is_never_printed_raw(tmp_path, make_page, capsys):
    from mendwright import cli
    key = 'cart\x1b[31m\x9b'
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    heal.locator_all(key, '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all(key, '.cart-item', nonempty=True)
    message = str(info.value)
    pid = _rows(tmp_path, key)[ROWS]['proposal']['id']
    note = r" (put the key 'cart\x1b[31m\x9b' in quotes for your shell)"
    assert "'cart?[31m?' matched 3 elements" in message
    assert 'Accept it with: mendwright lists accept KEY ' + pid + _flag(tmp_path) + note in message
    assert cli.main(['lists', '--store', str(tmp_path / 'o.json')]) == 0
    out = capsys.readouterr().out
    assert '  accept   mendwright lists accept KEY ' + pid + _flag(tmp_path) + note in out
    # The no-match line before it is 0.8.0's, and names the key as the code passed it.
    for text in (message.split('\n  -> ', 1)[1], out):
        assert not lists._CONTROL.search(text.replace('\n', ''))


# --- refusals ------------------------------------------------------------------------------------

def _searched(fragment, had='3 elements'):
    return NO_MATCH + ("'cart' matched " + had + ' when its list fingerprint was recorded, and '
                       'none of its selectors matches now. nonempty=True says the list has items '
                       'here, so mendwright looked for it on the page, but ' + fragment + '. If '
                       'the selector broke, change it in your code. If the list can be empty '
                       'here, remove nonempty=True.')


def _unsearched(text):
    return NO_MATCH + ("nonempty=True says 'cart' has items here, and none of its selectors "
                       'matches. ' + text + ' If the selector broke, change it in your code.')


def _nothing_saved(root, before):
    after = _rows(root)
    assert [x for x, c in after.items() if c.get('status') == 'proposed'] == [
        x for x, c in before.items() if c.get('status') == 'proposed']


def test_an_emptied_cart_is_refused_and_nothing_is_saved(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    before = _rows(tmp_path)
    page.set_html(cart(False))
    assert _raised(heal) == _searched(
        "each group of div elements on the page is another list: the one most like the list (3 "
        "elements) is under 'Saved for later', which labelled another part of the page when the "
        "list was recorded, and the list was under 'Shopping cart'")
    _nothing_saved(tmp_path, before)
    rejected = _events(tmp_path, 'heal.rejected')[-1]
    assert rejected['list'] is True and rejected['source'] == 'fingerprint'
    assert not _events(tmp_path, 'heal.held', 'heal.applied')


@pytest.mark.parametrize('off, cause', [('fingerprint=False', 'fingerprint=False'),
                                        ('MENDWRIGHT_FINGERPRINT=0', 'MENDWRIGHT_FINGERPRINT=0'),
                                        ('MENDWRIGHT_DISABLE=1', 'MENDWRIGHT_DISABLE=1')])
def test_with_the_tier_off_nonempty_waits_and_says_what_turned_it_off(tmp_path, make_page,
                                                                      monkeypatch, off, cause):
    _recorded(make_page, tmp_path)
    kwargs = {}
    if off == 'fingerprint=False':
        kwargs['fingerprint'] = False
    else:
        name, value = off.split('=')
        monkeypatch.setenv(name, value)
    page = _shop(make_page, cart(True, row='cart-row'))
    heal = _open(page, tmp_path, **kwargs)
    assert _raised(heal) == _unsearched(
        'mendwright looks for a list only while the fingerprint tier is on, and ' + cause
        + ' turns it off.')
    assert page.items_reads == 0 and not _events(tmp_path, 'heal.held')


def test_a_hand_built_coordinator_with_the_tier_off_is_named(tmp_path, make_page):
    from mendwright.coordinator import HealCoordinator
    from mendwright.drivers import wrap
    from mendwright.facade import Healer
    page = _shop(make_page)
    store = load_store(None, str(tmp_path / 'o.json'))
    driver = wrap(page)
    coordinator = HealCoordinator(store, provider=object(), driver=driver, html_provider=page.content,
                                  inventory_provider=list, guard=lambda: True)
    heal = Healer(page, driver, store, coordinator, timeout_ms=0)
    page.set_html(cart(False))
    assert _raised(heal) == _unsearched(
        "mendwright looks for a list only while the fingerprint tier is on, and the "
        "HealCoordinator's fingerprint=False turns it off.")


def test_a_list_never_seen_with_items_waits_the_whole_timeout_then_says_so(tmp_path, make_page):
    page = _shop(make_page, cart(False))
    heal = _open(page, tmp_path, timeout_ms=300)
    started = time.monotonic()
    assert _raised(heal, poll_ms=20) == _unsearched(
        'mendwright has never seen this list match 2 or more elements, so it has nothing to '
        "look for. It records a list each time the list's selector matches 2 or more.")
    assert time.monotonic() - started >= 0.29


def test_a_list_recorded_by_0_8_0_is_not_looked_for_until_nonempty_records_it(tmp_path,
                                                                             make_page):
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    heal.locator_all('cart', '.cart-item')                            # recorded as 0.8.0 did
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) == _searched(
        "its list fingerprint was recorded by mendwright 0.8.0 or before a call with "
        "nonempty=True matched the list, so it doesn't hold where the list was or its heading, "
        "and another list that holds its items can't be told from it. mendwright records them "
        'the next time the list matches in a call with nonempty=True')
    assert page.items_reads == 0
    page.set_html(cart(True))
    heal.locator_all('cart', '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    assert 'Accept it with: mendwright lists accept cart' in _raised(heal)


def test_a_key_marked_destructive_later_is_refused_before_anything(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    heal.store.set_meta('cart', destructive=True)
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) == NO_MATCH + (
        "'cart' is marked as a destructive control, and mendwright never auto-heals those, "
        'because a validated but wrong selector here could act on a live account. Fix the '
        'selector in your code or seed file by hand.')
    assert page.items_reads == 0


def test_another_page_is_refused_and_a_step_page_is_not(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    page.url = 'https://shop.test/checkout'
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) == NO_MATCH + (
        "'cart' was last found on /cart, and this page is /checkout. That looks like a change in "
        'the flow, not a renamed element, so mendwright did not heal it. If the step moved, '
        'update your script. If the URL itself changed in a redesign, pass '
        'heal_across_pages=True to mendwright.open().')
    across = _open(page, tmp_path, heal_across_pages=True)
    assert 'Accept it with:' in _raised(across)


def test_an_unknown_role_names_locator_all(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    heal.store._entry('cart')['expectedRole'] = 'unknown'
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) == NO_MATCH + (
        "mendwright does not know what kind of element 'cart' is (its expected role is unknown), "
        'so it cannot check a repaired selector. Pass role= to heal.locator_all(), for example '
        "role='textbox' for a text field, role='button', role='checkbox', role='link' or "
        "role='combobox', or set expectedRole for the key in the seed file.")


def test_the_guard_stops_the_search(tmp_path, make_page):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    heal = _open(page, tmp_path, guard=lambda: False)
    assert _raised(heal).endswith('a transaction guard is currently blocking heals (a payment or '
                                  'registration submission is pending), so mendwright did not try '
                                  'to heal. Run the step again once the guard allows heals.')
    assert page.items_reads == 0


class _Store:
    """A store that can't save a proposal: a SelectorStore without propose_list_heal()."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        if name == 'propose_list_heal':
            raise AttributeError(name)
        return getattr(self._store, name)


class _FailingStore(_Store):
    def propose_list_heal(self, *args, **kwargs):
        raise OSError('disk full')


def test_a_store_that_cannot_save_a_proposal_is_named(tmp_path, make_page):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    for cls, why in ((_Store, 'it has no propose_list_heal()'),
                     (_FailingStore, 'it raised OSError: disk full')):
        store = cls(load_store(None, str(tmp_path / 'o.json')))
        heal = mendwright.open(page, store=store, timeout_ms=0, journal=None)
        assert _raised(heal) == _unsearched(
            "The store passed to mendwright.open() can't save a list heal for you to check ("
            + why + '), so mendwright did not look.')
    assert not [c for c in _rows(tmp_path).values() if c.get('status') == 'proposed']


def test_the_live_check_refuses_a_group_hidden_on_the_live_page(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    hidden = cart(True, row='cart-row').replace("<section class='cart'>",
                                                "<section class='cart' style='display:none'>")
    page.set_html(hidden)
    assert _raised(heal) == _searched('no group of div elements is visible on the live page')


def test_a_list_fingerprint_without_list_items_is_refused(tmp_path, make_page):
    page = _shop(make_page)
    heal = _open(page, tmp_path)
    heal.locator_all('cart', '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    heal.coordinator.driver = UnplacedDriver(page)
    heal.coordinator.driver.list_items = None
    assert _raised(heal) == _unsearched(
        "The driver of this page can't read the elements of a list (it has no list_items()), so "
        "mendwright can't check a list it finds.")
    heal.coordinator.driver = None
    assert _raised(heal) == _unsearched(
        "The driver of this page can't be reached (this lookup has no live page), so mendwright "
        "can't check a list it finds.")


def test_a_state_word_the_list_did_not_have_is_refused(tmp_path, make_page):
    page = make_page(todomvc('todo'), url='https://todo.test/', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    heal.locator_all('cart', 'li.todo', nonempty=True)
    page.set_html(todomvc('completed'))
    xpath = "//li[contains(concat(' ', normalize-space(@class), ' '), ' completed ')]"
    assert _raised(heal, 'li.todo') == _searched(
        'the group it found, ' + xpath + ", is picked by 'completed', and 'completed' names a "
        "state the recorded list didn't have, so these are most likely the list's items in "
        'another state, or another list, such as completed tasks or items saved for later',
        had='4 elements')
    assert _events(tmp_path, 'heal.rejected')[-1]['xpath'] == xpath


def test_a_rejected_list_heal_is_refused_under_any_spelling(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    _raised(heal)
    edit_list_heal(tmp_path / 'o.json', 'cart', 'reject')
    again = _open(page, tmp_path)
    assert _raised(again) == _searched(
        'the group it found, ' + ROWS + ", failed mendwright's list checks (" + REJECTED + ')')


def test_dry_run_checks_and_saves_nothing(tmp_path, make_page, caplog):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    heal = _open(page, tmp_path, dry_run=True)
    with caplog.at_level(logging.INFO, logger='mendwright'):
        message = _raised(heal)
    assert message == NO_MATCH + (
        "dry-run mode is on, so the list mendwright found for 'cart', " + ROWS + ', was checked '
        'but not saved. Turn off dry_run to save it for you to check.')
    assert '[heal dry-run] key=cart proposed=' + ROWS + ' (list) valid=True (not saved)' in (
        caplog.text)
    assert ROWS not in _rows(tmp_path) and not _events(tmp_path, 'heal.held')


# --- came, and waiting ---------------------------------------------------------------------------

def test_a_list_that_comes_while_mendwright_waits_is_used(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    heal.timeout_ms = 5000
    page.set_html(cart(True).replace("<div class='cart-item'>", "<div class='skeleton'>"))
    query, looks = page.query, []

    def arriving(selector):
        looks.append(selector)
        if len(looks) == 4:
            page.set_html(cart(True))
        return query(selector)
    page.query = arriving
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True, poll_ms=1)) == 3
    assert len(looks) >= 4 and not _events(tmp_path, 'heal.held')


class _ComingDriver(PlaywrightSyncDriver):
    """The code's selector starts matching once the page was captured for the search."""

    def __init__(self, page, after):
        super().__init__(page)
        self._after = after

    def outer_html(self, scope=None):
        html = super().outer_html(scope)
        self._page.set_html(self._after)
        return html


def test_a_list_that_came_during_the_search_is_used_as_it_is(tmp_path, make_page, caplog):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    heal = _open(_ComingDriver(page, cart(True)), tmp_path)
    with caplog.at_level(logging.INFO, logger='mendwright'):
        items = heal.locator_all('cart', '.cart-item', nonempty=True)
    assert len(items) == 3 and ROWS not in _rows(tmp_path)
    assert ("mendwright: 'cart' came while mendwright looked for it, so .cart-item was used as "
            'it is.') in caplog.text


def test_a_list_that_came_and_went_during_the_search_is_refused(tmp_path, make_page):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    renamed = cart(True, row='cart-row')

    class Gone(_ComingDriver):
        def count(self, selector, frame_chain=()):
            found = super().count(selector, frame_chain)
            if selector == '.cart-item' and found:
                self._page.set_html(renamed)              # it went away again
            return found
    heal = _open(Gone(page, cart(True)), tmp_path)
    assert _raised(heal) == _searched(
        'the group it found, ' + ROWS + ", failed mendwright's list checks (the selector in your "
        'code, .cart-item, matched while mendwright looked and matches nothing now)')


def test_interruptions_are_handled_while_a_nonempty_list_waits(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    popup = "<div id='promo' role='dialog'><button id='close'>Close</button></div>"
    page.set_html(cart(True).replace('<main>', '<main>' + popup).replace(
        "class='cart-item'", "class='cart-item-hidden'"))
    clicks = []

    def close(p):
        clicks.append(1)
        page.set_html(cart(True))
    heal.interrupt('promo', '#promo', run=close)
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
    assert clicks == [1] and [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('promo', 'cart')]


def test_a_run_function_s_attention_required_is_raised_as_it_is(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    error = AttentionRequired('mendwright: the sign-in form changed')

    def relogin(p):
        raise error
    heal.interrupt('relogin', '#relogin', run=relogin)
    page.set_html(cart(True, row='cart-row').replace('<main>', "<main><div id='relogin'>x</div>"))
    with pytest.raises(AttentionRequired) as caught:
        heal.locator_all('cart', '.cart-item', nonempty=True)
    assert caught.value is error and not _events(tmp_path, 'heal.held')


# --- accepting, and retiring -----------------------------------------------------------------------

def _proposed(make_page, root):
    page, heal = _recorded(make_page, root)
    page.set_html(cart(True, row='cart-row'))
    _raised(heal)
    return page, heal, _rows(root)[ROWS]['proposal']['id']


def test_an_accepted_list_heal_waits_for_mendwright_fix(tmp_path, make_page, caplog):
    page, heal, pid = _proposed(make_page, tmp_path)
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid, now='2026-10-30T08:00:00+00:00')
    reads = page.items_reads
    again = _open(page, tmp_path)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        message = _raised(again)
    reason = ("'cart' matched 3 elements when its list fingerprint was recorded, and none of its "
              'selectors matches now. You accepted a list heal for it on 2026-10-30 08:00:00 UTC, '
              + ROWS + ', which matches 3 elements now, but mendwright takes a list only from the '
              'selectors in your code. Write it there with: mendwright fix --apply'
              + _flag(tmp_path) + '. If the list can be empty here, remove nonempty=True.')
    assert message == NO_MATCH + reason
    assert page.items_reads == reads                                  # no search
    assert "mendwright: 'cart' waits for mendwright fix: " + reason in caplog.text
    # The journal says why, so mendwright notify names the cause.
    assert _events(tmp_path, 'heal.declined')[-1] == dict(
        _events(tmp_path, 'heal.declined')[-1], key='cart', code='list_waits_fix', reason=reason)
    assert again.locator_all('cart', ROWS)                            # once it is in the code


def test_an_accepted_list_heal_that_waits_is_what_the_alert_says_needs_a_human(tmp_path,
                                                                               make_page):
    from mendwright import runsummary
    from mendwright.journal import Journal
    page, heal, pid = _proposed(make_page, tmp_path)
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid, now='2026-10-30T08:00:00+00:00')
    _raised(_open(page, tmp_path, journal=Journal(tmp_path / 'journal.jsonl', run='run-2')))
    events, _ = runsummary.select(_journal(tmp_path), run='run-2')
    assert [(e['event'], e['code']) for e in events] == [('heal.declined', 'list_waits_fix')]
    text = runsummary.message(runsummary.summarize(events), tests='failure')
    assert text == ('mendwright: the site changed.\n1 needs a human: `cart` matched nothing as a '
                    'list, and the list heal accepted for it waits for mendwright fix or its seed '
                    'file.\nThe tests failed.')


def test_a_newer_proposal_next_to_an_accepted_one_says_so(tmp_path, make_page):
    page, heal, pid = _proposed(make_page, tmp_path)
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid, now='2026-10-30T08:00:00+00:00')
    page.set_html(cart(True, row='basket-line'))
    message = _raised(_open(page, tmp_path))
    assert (" You accepted another list heal for 'cart' on 2026-10-30 08:00:00 UTC, " + ROWS
            + ', which matches nothing now. mendwright fix writes neither until you accept this '
            'one or reject it. If the list can be empty here, remove nonempty=True.') in message


def test_a_healer_that_lives_across_an_accept_does_not_propose_it_again(tmp_path, make_page):
    page, heal, pid = _proposed(make_page, tmp_path)
    held, reads = len(_events(tmp_path, 'heal.held')), page.items_reads
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid, now='2026-10-30T08:00:00+00:00')
    message = _raised(heal)                                           # the same Healer
    assert 'You accepted a list heal for it on 2026-10-30 08:00:00 UTC, ' + ROWS in message
    assert 'Accept it with' not in message and page.items_reads == reads     # no search
    assert len(_events(tmp_path, 'heal.held')) == held and len(heal.coordinator.held) == 1
    assert _rows(tmp_path)[ROWS]['status'] == 'accepted'


def test_a_healer_that_lives_across_a_reject_does_not_propose_it_again(tmp_path, make_page):
    page, heal, pid = _proposed(make_page, tmp_path)
    held = len(_events(tmp_path, 'heal.held'))
    edit_list_heal(tmp_path / 'o.json', 'cart', 'reject', pid)
    assert _raised(heal) == _searched(
        'the group it found, ' + ROWS + ", failed mendwright's list checks (" + REJECTED + ')')
    assert len(_events(tmp_path, 'heal.held')) == held and len(heal.coordinator.held) == 1
    assert _rows(tmp_path)[ROWS]['status'] == 'quarantined'


@pytest.mark.parametrize('action', ['accept', 'reject'])
def test_an_accept_or_reject_that_comes_during_the_search_is_kept(tmp_path, make_page, action):
    # The person accepts or rejects after the lookup read the file again, and before the
    # proposal is saved: the save takes in the file, and nothing is proposed.
    page, heal, pid = _proposed(make_page, tmp_path)
    held = len(_events(tmp_path, 'heal.held'))
    heal.store.reload_list_heals = lambda key: None
    capture = heal.coordinator.html_provider

    def html():
        edit_list_heal(tmp_path / 'o.json', 'cart', action, pid)
        return capture()
    heal.coordinator.html_provider = html
    message = _raised(heal)
    assert 'Accept it with' not in message and len(heal.coordinator.held) == 1
    assert len(_events(tmp_path, 'heal.held')) == held
    if action == 'accept':
        assert 'You accepted a list heal for it on ' in message
    else:
        assert message.endswith("failed mendwright's list checks (" + REJECTED + '). If the '
                                'selector broke, change it in your code. If the list can be '
                                'empty here, remove nonempty=True.')
    assert _rows(tmp_path)[ROWS]['status'] == ('accepted' if action == 'accept'
                                               else 'quarantined')


@pytest.mark.parametrize('nonempty', [True, False])
def test_the_old_selector_matching_again_retires_list_heals(tmp_path, make_page, caplog,
                                                            nonempty):
    page, heal, pid = _proposed(make_page, tmp_path)
    page.set_html(cart(True))                                         # the old selector works
    with caplog.at_level(logging.INFO, logger='mendwright'):
        assert len(heal.locator_all('cart', '.cart-item', nonempty=nonempty)) == 3
    assert _rows(tmp_path)[ROWS]['status'] == 'superseded'
    assert '.cart-item matches \'cart\' again, so the list heal found for it on ' in caplog.text
    assert 'is no longer proposed.' in caplog.text
    # Found again, accepted, and the old selector matches once more: quarantined.
    page.set_html(cart(True, row='cart-row'))
    _raised(heal)
    pid = _rows(tmp_path)[ROWS]['proposal']['id']
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid, now='2026-11-02T10:20:00+00:00')
    page.set_html(cart(True))
    again = _open(page, tmp_path)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        assert len(again.locator_all('cart', '.cart-item', nonempty=nonempty)) == 3
    assert _rows(tmp_path)[ROWS]['status'] == 'quarantined'
    assert ("mendwright: the selector in your code for 'cart', .cart-item, matches 3 elements "
            'again, so the list heal you accepted for it on 2026-11-02 10:20:00 UTC, ' + ROWS
            + ', is quarantined: the list was not broken, or the key is used on more than one '
            "page. mendwright fix won't write it.") in caplog.text
    quarantined = _events(tmp_path, 'heal.quarantine')[-1]
    assert quarantined['xpath'] == ROWS and quarantined['list'] is True
    assert quarantined['reason'] == 'the selector it replaced matches again'


def test_one_element_of_the_old_selector_quarantines_an_accepted_list_heal_and_says_so(
        tmp_path, make_page):
    # A list with one item is not empty, so one element that keeps the old class (a mini cart)
    # is enough: mendwright can't tell it from the cart, and fails closed.
    page, heal, pid = _proposed(make_page, tmp_path)
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid)
    mini = cart(True, row='cart-row').replace(
        '<h2>Saved for later', "<aside><div class='cart-item'>Blue mug</div></aside><h2>Saved for later")
    assert len(_open(_shop(make_page, mini), tmp_path).locator_all('cart', '.cart-item')) == 1
    assert _rows(tmp_path)[ROWS]['status'] == 'quarantined'
    again = _open(page, tmp_path)
    assert _raised(again) == _searched(
        'the group it found, ' + ROWS + ", failed mendwright's list checks (" + REJECTED + ')')
    assert 'once the selector it replaced matches again, even on one element' in REJECTED
    # The way back: the selector in the code, by hand.
    assert len(again.locator_all('cart', ROWS, nonempty=True)) == 3
    assert _rows(tmp_path)[ROWS]['status'] == 'active'


def _seeded(make_page, root, xpath, page=None):
    """A healer whose 'cart' comes only from a seed file holding xpath."""
    seed = root / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'cart': {
        'expectedRole': 'div', 'candidates': [
            {'xpath': xpath, 'source': 'seed', 'addedAt': '2026-10-01T08:00:00+00:00',
             'lastSucceededAt': None, 'status': 'active'}]}}}), encoding='utf-8')
    page = page or _shop(make_page)
    store = load_store(str(seed), str(root / 'o.json'))
    return page, mendwright.open(page, provider=None, store=store, timeout_ms=0)


def test_a_list_heal_of_a_key_from_a_seed_file_says_to_put_it_in_the_seed_file(tmp_path,
                                                                            make_page, capsys):
    from mendwright import cli
    page, heal = _seeded(make_page, tmp_path, '.cart-item')
    assert len(heal.locator_all('cart', nonempty=True)) == 3
    page.set_html(cart(True, row='cart-row'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cart', nonempty=True)
    pid = _rows(tmp_path)[ROWS]['proposal']['id']
    seed = (" Its selectors come from a seed file, which mendwright fix doesn't change, so "
            'then put it there in place of .cart-item. If the list can be empty here')
    assert ('Accept it with: mendwright lists accept cart ' + pid + _flag(tmp_path) + '.'
            + seed) in str(info.value)
    assert 'mendwright fix' not in str(info.value).split('Accept it with')[1].replace(
        "which mendwright fix doesn't change", '')
    assert cli.main(['lists', 'accept', 'cart', pid, '--store', str(tmp_path / 'o.json')]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "mendwright doesn't use it until it is one of the key's selectors. Its selectors come "
        "from a seed file, which mendwright fix doesn't change, so put it there in place of "
        '.cart-item.')
    page, heal = _seeded(make_page, tmp_path, '.cart-item', page)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cart', nonempty=True)
    assert str(info.value).endswith(
        ", which matches 3 elements now, but mendwright takes a list only from the key's own "
        "selectors. Its selectors come from a seed file, which mendwright fix doesn't change, so "
        'put it there in place of .cart-item. If the list can be empty here, remove '
        'nonempty=True.')
    # The person puts it in the seed file: the seed's row wins over the accepted list heal.
    page, heal = _seeded(make_page, tmp_path, ROWS, page)
    assert len(heal.locator_all('cart', nonempty=True)) == 3
    assert heal.store.list_heals('cart') == []
    assert ROWS not in _rows(tmp_path) or _rows(tmp_path)[ROWS]['source'] == 'seed'
    assert not [c for c in _rows(tmp_path).values() if c.get('list')]


def test_lists_and_report_say_to_put_a_seed_key_s_accepted_list_heal_in_the_seed_file(
        tmp_path, make_page, capsys, caplog):
    from mendwright import cli
    page, heal = _seeded(make_page, tmp_path, '.cart-item')
    heal.locator_all('cart', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    with pytest.raises(AttentionRequired):
        heal.locator_all('cart', nonempty=True)
    store = str(tmp_path / 'o.json')
    pid = _rows(tmp_path)[ROWS]['proposal']['id']
    edit_list_heal(store, 'cart', 'accept', pid, now='2026-10-30T08:00:00+00:00')
    assert cli.main(['lists', '--store', store]) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "1 accepted list heal waits for you to put it in the seed file its key's selectors come "
        "from, which mendwright fix doesn't change:",
        '  cart  ' + ROWS + ' in place of .cart-item  (accepted 2026-10-30 08:00:00 UTC)']
    note = ("list, accepted 2026-10-30 08:00:00 UTC; its selectors come from a seed file, which "
            "mendwright fix doesn't change, so put it there in place of .cart-item")
    for form in ('text', 'md', 'html'):
        assert cli.main(['report', '--store', store, '--format', form]) == 0
        out = capsys.readouterr().out
        assert (note.replace("'", '&#x27;') if form == 'html' else note) in out
        assert 'mendwright fix --apply' not in out
    assert cli.main(['lists', 'accept', 'cart', pid, '--store', store]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "mendwright lists: 'cart' has no list heal to check. Its list heal was accepted on "
        '2026-10-30 08:00:00 UTC and waits for you to put it in the seed file its selectors come '
        'from; to drop it, run: mendwright lists reject cart' + _flag(tmp_path)]
    page, heal = _seeded(make_page, tmp_path, '.cart-item', page)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        with pytest.raises(AttentionRequired):
            heal.locator_all('cart', nonempty=True)
    assert "mendwright: 'cart' waits for its seed file: 'cart' matched 3 elements" in caplog.text
    assert 'waits for mendwright fix' not in caplog.text


def test_another_selector_of_the_key_retires_nothing(tmp_path, make_page):
    page, heal, pid = _proposed(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    assert len(heal.locator_all('cart', '.cart-row', nonempty=True)) == 3   # another page's
    assert _rows(tmp_path)[ROWS]['status'] == 'proposed'


def test_async_a_list_whose_rows_were_renamed_is_proposed_and_never_used(tmp_path, make_page):
    async def main():
        sync = _shop(make_page)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        assert len(await heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
        sync.set_html(cart(True, row='cart-row'))
        with pytest.raises(AttentionRequired) as proposed:
            await heal.locator_all('cart', '.cart-item', nonempty=True)
        sync.set_html(cart(False))
        with pytest.raises(AttentionRequired) as emptied:
            await heal.locator_all('cart', '.cart-item', nonempty=True)
        pid = _rows(tmp_path)[ROWS]['proposal']['id']
        edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', pid)
        sync.set_html(cart(True, row='cart-row'))
        again = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                          timeout_ms=0)
        with pytest.raises(AttentionRequired) as waits:
            await again.locator_all('cart', '.cart-item', nonempty=True)
        sync.set_html(cart(True))
        rows = await again.locator_all('cart', '.cart-item', nonempty=True)
        return heal, sync, proposed.value, emptied.value, waits.value, rows
    heal, sync, proposed, emptied, waits, rows = asyncio.run(main())
    assert 'Accept it with: mendwright lists accept cart' in str(proposed)
    assert 'is another list' in str(emptied)
    assert 'You accepted a list heal for it on' in str(waits)
    assert len(rows) == 3 and _rows(tmp_path)[ROWS]['status'] == 'quarantined'
    assert heal.heals == [] and heal.coordinator.held and lists.placed(
        load_store(None, str(tmp_path / 'o.json')).meta('cart')['listFingerprint'])


def test_async_an_accepted_list_heal_waits_for_fix_with_no_search(tmp_path, make_page):
    # The same healer lives across the accept, as a session-wide one does.
    async def main():
        sync = _shop(make_page)
        heal = await mendwright.aio.open(_Page(sync), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        await heal.locator_all('cart', '.cart-item', nonempty=True)
        sync.set_html(cart(True, row='cart-row'))
        with pytest.raises(AttentionRequired):
            await heal.locator_all('cart', '.cart-item', nonempty=True)
        edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', _rows(tmp_path)[ROWS]['proposal']['id'],
                       now='2026-10-30T08:00:00+00:00')
        reads, events = sync.items_reads, len(_journal(tmp_path))
        with pytest.raises(AttentionRequired) as waits:
            await heal.locator_all('cart', '.cart-item', nonempty=True)
        return heal, sync, reads, events, waits.value
    heal, sync, reads, events, waits = asyncio.run(main())
    assert str(waits) == NO_MATCH + (
        "'cart' matched 3 elements when its list fingerprint was recorded, and none of its "
        'selectors matches now. You accepted a list heal for it on 2026-10-30 08:00:00 UTC, '
        + ROWS + ', which matches 3 elements now, but mendwright takes a list only from the '
        'selectors in your code. Write it there with: mendwright fix --apply' + _flag(tmp_path)
        + '. If the list can be empty here, remove nonempty=True.')
    # No search: the journal gets only the line that says why.
    assert sync.items_reads == reads
    assert [(e['event'], e['code']) for e in _journal(tmp_path)[events:]] == [
        ('heal.declined', 'list_waits_fix')]
    assert len(heal.coordinator.held) == 1


def test_async_a_list_that_came_during_the_search_is_used_as_it_is(tmp_path, make_page, caplog):
    page, _ = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))

    class Coming(_Page):
        """The code's selector starts matching once the page was captured for the search."""

        async def content(self):
            html = await super().content()
            self.sync.set_html(cart(True))
            return html

    async def main():
        heal = await mendwright.aio.open(Coming(page), store_path=str(tmp_path / 'o.json'),
                                         timeout_ms=0)
        return await heal.locator_all('cart', '.cart-item', nonempty=True)
    with caplog.at_level(logging.INFO, logger='mendwright'):
        items = asyncio.run(main())
    assert len(items) == 3 and ROWS not in _rows(tmp_path)
    assert ("mendwright: 'cart' came while mendwright looked for it, so .cart-item was used as "
            'it is.') in caplog.text
    assert not _events(tmp_path, 'heal.held', 'heal.rejected')


def test_selenium_a_list_whose_rows_were_renamed_is_proposed(tmp_path):
    web = PlacedListDriver(cart(True))
    heal = _open(web, tmp_path)
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
    web.set_html(cart(True, row='cart-row'))
    message = _raised(heal)
    assert 'Accept it with: mendwright lists accept cart' in message
    assert _rows(tmp_path)[ROWS]['status'] == 'proposed'


# What the search refuses, through heal.locator_all() (see test_lists_find.py for each one).

def _skeleton():
    return ("<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
            "<button type='button' disabled></button></div>")


def _results(cls, names, attrs=''):
    cards = ''.join("<div class='" + cls + "'" + attrs + "><img src='/i/x.png' alt='" + n
                    + "'><h3>" + n + "</h3><span class='price'>$9.00</span></div>" for n in names)
    return ("<html><body><main id='search'><h1>Search</h1><div id='results'>" + cards
            + '</div></main></body></html>')


MUGS = ['Blue mug', 'Red mug', 'Green mug', 'Mug set']
POPULAR = ['Tea pot', 'Kettle', 'Coffee grinder', 'French press']
RESULT = "//div[@class='result-card']"
ALONE = ("<html><body><main id='content'><section>" + card('product-tile', *PRODUCTS[0])
         + '</section></main></body></html>')
TWO_GRIDS = shop(grid('product-tile'), extra="<div id='m' class='grid'>" + grid('product-tile')
                 + '</div>', grid="id='d' class='grid'")
NEW_PRODUCTS = [('Lamp', '$40.00'), ('Chair', '$90.00'), ('Desk', '$250.00'), ('Sofa', '$900.00')]


@pytest.mark.parametrize('before, after, xpath, fragment', [
    pytest.param(V1, shop(_skeleton() * 4), CARDS, 'the only groups of div elements look like '
                 'placeholders (no text where the recorded items had text, or marked '
                 'aria-busy="true"), so the list may still be loading', id='L12-placeholders'),
    pytest.param(V1, ALONE, CARDS, 'the page has no group of 2 or more div elements that repeat '
                 "the same structure. The only element like the recorded items is one <div> "
                 "('Blue mug$12.00Add to cart'); mendwright never takes one element for a list",
                 id='L13-one-element'),
    pytest.param(shop(grid('product-card'), grid="class='grid'"), TWO_GRIDS, CARDS,
                 'two groups score alike (', id='L16-two-alike'),
    pytest.param(V1, shop(card('product-tile', *PRODUCTS[0])
                          + "<div class='banner'><p>Sale!</p></div>"), CARDS,
                 'the elements of the best group are not all shaped like the recorded items',
                 id='L17-shapes'),
    pytest.param(_results('result-card', MUGS), _results('popular-card', ['Blue mug'] + POPULAR),
                 RESULT, "the best group (5 elements under 'Search') holds only 1 of the 3 items "
                 'it recorded, and 2 are needed, so it may be another list in the same place',
                 id='L18a-too-few'),
    pytest.param(_results('result-card', MUGS, " data-testid='card'"),
                 _results('popular-card', POPULAR, " data-testid='card'"), RESULT,
                 'the best group shares an id, test id, name, link, label or text only with '
                 'every recorded item at once', id='L18b-shared-values'),
    pytest.param(V1, shop(grid('product-tile', NEW_PRODUCTS)), CARDS, 'the best group shares no '
                 'id, test id, name, link, label or text with the items it recorded, so a list '
                 "whose items change from run to run can't be found", id='L18c-no-identity'),
])
def test_what_the_search_refuses_is_said_and_saves_nothing(tmp_path, make_page, before, after,
                                                           xpath, fragment):
    page = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('cards', xpath, nonempty=True)) >= 2
    page.set_html(after)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cards', xpath, nonempty=True)
    message = str(info.value)
    assert "nonempty=True says the list has items here, so mendwright looked for it on the "            'page, but ' + fragment in message
    assert message.endswith('. If the selector broke, change it in your code. If the list can '
                            'be empty here, remove nonempty=True.')
    assert not [c for c in _rows(tmp_path, 'cards').values() if c.get('status') == 'proposed']
    assert _events(tmp_path, 'heal.rejected')[-1]['list'] is True
    assert not _events(tmp_path, 'heal.held', 'heal.applied')


def test_a_page_that_cannot_be_parsed_is_said(tmp_path, make_page):
    page, heal = _recorded(make_page, tmp_path)
    page.set_html(cart(True, row='cart-row'))
    heal.coordinator.html_provider = lambda: ''
    assert _raised(heal) == _searched('the captured page could not be parsed')
    assert not _events(tmp_path, 'heal.rejected')


def test_the_guide_quotes_what_a_proposal_says_and_the_state_words(tmp_path, make_page,
                                                                    stub_provider, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'docs' / 'guide.md'
    if not path.is_file():
        pytest.skip('docs/ is not in this copy of the project (the sdist leaves it out)')
    guide = path.read_text(encoding='utf-8')
    monkeypatch.chdir(tmp_path)              # the default store: the commands name no --store
    page = _shop(make_page)
    heal = mendwright.open(page, provider=stub_provider(), timeout_ms=0)
    heal.locator_all('cart', '.cart-item', nonempty=True)
    page.set_html(cart(True, row='cart-row'))
    assert _raised(heal) in guide
    words = re.search(r'in a class, id, name or text: (.*?)\. A list that was', guide).group(1)
    assert set(re.findall(r'`([a-z]+)`', words)) == lists.STATE_WORDS


def test_a_list_looked_up_with_nonempty_saves_its_address(make_page, tmp_path):
    # mendwright check loads a key's lastUrl: a list found with nonempty=True records it as
    # any lookup that matches does, and a proposal does not.
    page = _shop(make_page, url='https://Shop.Test/Cart/10482/?step=2')
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('cart', '.cart-item', nonempty=True)) == 3
    entry = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))['selectors']['cart']
    assert (entry['lastUrl'], entry['lastPage']) == ('https://shop.test/Cart/*/', '/cart/*')
