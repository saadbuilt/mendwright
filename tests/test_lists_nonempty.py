"""heal.locator_all(key, selector, nonempty=True): a list that always has items at that point.
When none of the key's selectors matches, mendwright waits, looks for the list from its list
fingerprint (lists.find) and saves what it finds as a proposed list heal for a person to
check, and raises. It never gives or acts on a list it found: heal.locator_all() gives only
what the key's own selectors match. Offline: conftest's lxml-backed FakePage, the fake
Selenium driver of test_proxies.py, no browser and no model (WrongPicker, the stub model of
test_lists_emptied.py that takes the other list, is never asked)."""

import asyncio

import lxml.html
import pytest

from mendwright import lists
from mendwright.aio import SyncBridge
from mendwright.client import list_gate
from mendwright.drivers.base import (driver_list_fingerprint, driver_list_items, driver_places,
                                     item_rows)
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.gate import list_live_reason
from mendwright.store import load_store

from test_lists import CARDS, TILE, V1, V2, ListDriver


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
    assert list_gate(store, 'cards', TILE, V2, meta, live)[1] == (
        'it is a list heal you rejected, or one mendwright quarantined')
    other = "//div[@id='grid']/div"                                    # the same elements
    assert list_gate(store, 'cards', other, V2, meta, live)[1] == (
        'it takes elements of a list heal you rejected, or of a quarantined selector of this key')
