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
from mendwright.drivers.base import (driver_list_fingerprint, driver_list_items, driver_places,
                                     item_rows)
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver

from test_lists import CARDS, V1, ListDriver


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
