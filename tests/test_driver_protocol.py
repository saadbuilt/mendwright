"""act_on / describe / inventory are optional on a Driver. A custom driver that only has
the old methods still works, and the driver_* helpers fall back for it."""

import pytest

from mendwright import InvalidSelector, PlaywrightSyncDriver, SeleniumDriver, resolve, wrap
from mendwright.drivers import (
    driver_act_on,
    driver_describe,
    driver_inventory,
    is_driver,
)


class OldDriver:
    """A custom driver written against the original protocol only."""

    def __init__(self):
        self.native = Native()

    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def handle(self, selector, frame_chain=()):
        return self.native

    def scroll_into_view(self, selector, frame_chain=()):
        pass


class Native:
    def __init__(self):
        self.calls = []

    def click(self, *args, **kwargs):
        self.calls.append(('click', args, kwargs))


class Store:
    def candidates(self, key):
        return [{'xpath': '//a'}]

    def meta(self, key):
        return {'expectedRole': 'link'}

    def record_success(self, key, xpath):
        pass


def test_old_style_custom_driver_still_resolves_and_falls_back():
    d = OldDriver()
    assert is_driver(d) and wrap(d) is d
    assert resolve(Store(), d, 'k', timeout_ms=0, sleep=lambda s: None) is d.native
    driver_act_on(d, d.native, 'click', timeout=5)
    assert d.native.calls == [('click', (), {'timeout': 5})]
    assert driver_describe(d, '//a') is None
    assert driver_inventory(d) == []


def test_helpers_use_the_driver_methods_when_present():
    class Rich(OldDriver):
        def act_on(self, native, verb, *args, **kwargs):
            return ('act_on', verb, args, kwargs)

        def describe(self, selector, frame_chain=()):
            return {'tag': 'a', 'role': 'link', 'text': 'x', 'outer_html': '<a>x</a>',
                    'frames': frame_chain}

        def inventory(self, limit=200):
            return [{'limit': limit}]

    d = Rich()
    assert driver_act_on(d, d.native, 'fill', 'x', timeout=1) == ('act_on', 'fill', ('x',), {'timeout': 1})
    assert driver_describe(d, '//a', ('//f',))['frames'] == ('//f',)
    assert driver_inventory(d, 7) == [{'limit': 7}]


def test_describe_helper_swallows_driver_errors_but_not_invalid_selectors():
    class Flaky(OldDriver):
        def describe(self, selector, frame_chain=()):
            if selector == 'bad':
                raise InvalidSelector('mendwright: bad')
            if selector == 'weird':
                return 'not a dict'
            raise RuntimeError('frame detached')

    assert driver_describe(Flaky(), '//a') is None
    assert driver_describe(Flaky(), 'weird') is None
    with pytest.raises(InvalidSelector):
        driver_describe(Flaky(), 'bad')


def test_inventory_helper_tolerates_old_signatures_errors_and_bad_results():
    class NoArg(OldDriver):
        def inventory(self):
            return [{'tag': 'a'}]

    class Broken(OldDriver):
        def inventory(self, limit=200):
            raise RuntimeError('closed')

    class NotAList(OldDriver):
        def inventory(self, limit=200):
            return None

    assert driver_inventory(NoArg()) == [{'tag': 'a'}]
    assert driver_inventory(Broken()) == []
    assert driver_inventory(NotAList()) == []


def test_act_on_helper_lets_errors_through():
    d = OldDriver()
    with pytest.raises(AttributeError):
        driver_act_on(d, d.native, 'no_such_verb')


def test_built_in_drivers_have_the_optional_methods():
    for cls in (PlaywrightSyncDriver, SeleniumDriver):
        for name in ('act_on', 'describe', 'inventory'):
            assert callable(getattr(cls, name)), (cls.__name__, name)
