"""Driver auto-detection + the Playwright-sync adapter + custom-driver registration."""

import pytest

from mendwright import PlaywrightSyncDriver, register_driver, wrap
from mendwright.drivers import is_driver


class FakeLoc:
    def __init__(self, n=1, visible=True):
        self._n, self._v = n, visible
        self.clicked = False
    def count(self):
        return self._n
    def is_visible(self):
        return self._v
    def scroll_into_view_if_needed(self):
        pass
    def click(self):
        self.clicked = True


class FakeFrame:
    def __init__(self, mapping):
        self.mapping = mapping
    def locator(self, sel):
        return self.mapping[sel.split('xpath=', 1)[1]]


class FakePage:
    """Playwright-like: has .locator AND .frame_locator (a real Page/Frame surface)."""
    def __init__(self, mapping, frames=None):
        self.mapping = mapping
        self.frames = frames or {}
        self._url = 'https://example.test/x'
    def locator(self, sel):
        return self.mapping[sel.split('xpath=', 1)[1]]
    def frame_locator(self, sel):
        return self.frames[sel.split('xpath=', 1)[1]]
    @property
    def url(self):
        return self._url


def test_wrap_detects_playwright_family():
    d = wrap(FakePage({'//a': FakeLoc()}))
    assert isinstance(d, PlaywrightSyncDriver)


def test_wrap_returns_driver_as_is():
    d = wrap(FakePage({'//a': FakeLoc()}))
    assert wrap(d) is d          # already a Driver -> not re-wrapped
    assert is_driver(d)


def test_locator_is_not_mistaken_for_a_page():
    # A Playwright Locator has .locator (chaining) but NO .frame_locator; must NOT detect.
    class Locatorish:
        def locator(self, sel):
            return self
    with pytest.raises(TypeError):
        wrap(Locatorish())


def test_playwright_driver_delegates_count_visible_handle():
    loc = FakeLoc(n=1, visible=True)
    d = wrap(FakePage({'//x': loc}))
    assert d.count('//x') == 1
    assert d.is_visible('//x') is True
    assert d.handle('//x') is loc           # native handle returned for the caller to act on
    d.act('//x', 'click')
    assert loc.clicked is True


def test_frame_chain_walks_frame_locator():
    inner = FakeLoc(n=1)
    page = FakePage({}, frames={'//iframe': FakeFrame({'//btn': inner})})
    d = wrap(page)
    assert d.count('//btn', frame_chain=('//iframe',)) == 1
    assert d.handle('//btn', frame_chain=('//iframe',)) is inner


def test_register_custom_driver():
    class Marker:
        pass
    class CustomDriver:
        def __init__(self, obj):
            self.obj = obj
        def count(self, x, frame_chain=()):
            return 1
        def is_visible(self, x, frame_chain=()):
            return True
        def handle(self, x, frame_chain=()):
            return ('custom', x)
    register_driver(lambda o: isinstance(o, Marker), CustomDriver)
    d = wrap(Marker())
    assert isinstance(d, CustomDriver)
    assert d.handle('//z') == ('custom', '//z')
