"""wrap() accepts sync Pages/Frames and Selenium drivers, and turns away Locators,
FrameLocators and async objects with a TypeError that says what to pass instead."""

import pytest

from mendwright import PlaywrightSyncDriver, SeleniumDriver, wrap
from mendwright.drivers import registry
from mendwright.drivers.base import is_async_api, looks_like_locator
from mendwright.drivers.playwright_sync import detect as pw_detect
from mendwright.drivers.selenium import detect as sel_detect


class SyncPage:
    url = 'https://example.test/'

    def locator(self, sel):
        return None

    def frame_locator(self, sel):
        return self

    def content(self):
        return '<html></html>'


class SyncFrame(SyncPage):
    page = None                    # a Frame has .page too, like a Locator

    def parent_frame(self):
        return None


class SyncLocator:
    """Shape of playwright.sync_api.Locator: chainable, nth(), count(), no content()."""
    page = None

    def locator(self, sel):
        return self

    def frame_locator(self, sel):
        return self

    def nth(self, i):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True


class SyncFrameLocator:
    """Shape of playwright.sync_api.FrameLocator: chainable, nth(), owner, no count()."""
    owner = None

    def locator(self, sel):
        return self

    def frame_locator(self, sel):
        return self

    def nth(self, i):
        return self


class AsyncPage:
    url = 'https://example.test/'

    def locator(self, sel):
        return None

    def frame_locator(self, sel):
        return self

    async def content(self):
        return ''

    async def goto(self, url):
        return None


class AsyncLocator:
    def locator(self, sel):
        return self

    def frame_locator(self, sel):
        return self

    def nth(self, i):
        return self

    async def count(self):
        return 1


class DriverlessChrome:
    """selenium_driverless style: WebDriver-shaped, but find_elements is a coroutine."""
    switch_to = object()

    async def find_elements(self, by, value):
        return []


def test_sync_page_and_frame_are_accepted():
    assert isinstance(wrap(SyncPage()), PlaywrightSyncDriver)
    assert isinstance(wrap(SyncFrame()), PlaywrightSyncDriver)


@pytest.mark.parametrize('obj', [SyncLocator(), SyncFrameLocator()])
def test_locator_and_frame_locator_are_rejected_with_a_hint(obj):
    assert looks_like_locator(obj)
    assert pw_detect(obj) is False
    with pytest.raises(TypeError) as info:
        wrap(obj)
    msg = str(info.value)
    assert type(obj).__name__ in msg
    assert 'locator.page' in msg and 'page.frames' in msg


@pytest.mark.parametrize('obj', [AsyncPage(), AsyncLocator(), DriverlessChrome()])
def test_async_objects_get_a_specific_error(obj):
    assert is_async_api(obj)
    assert pw_detect(obj) is False and sel_detect(obj) is False
    with pytest.raises(TypeError) as info:
        wrap(obj)
    msg = str(info.value)
    assert 'async' in msg
    assert 'sync Playwright API and sync Selenium' in msg
    assert 'playwright.sync_api' in msg


def test_unknown_object_keeps_the_generic_message():
    with pytest.raises(TypeError) as info:
        wrap(object())
    assert 'could not detect a browser driver for object' in str(info.value)
    assert 'mendwright.register_driver(detect, make)' in str(info.value)


def test_detection_never_runs_properties():
    # Selenium's WebDriver.title is a property that makes a browser round trip.
    class Chrome:
        switch_to = object()

        @property
        def title(self):
            raise AssertionError('property was evaluated')

        @property
        def content(self):
            raise AssertionError('property was evaluated')

        def find_elements(self, by, value):
            return []

    assert is_async_api(Chrome()) is False
    assert isinstance(wrap(Chrome()), SeleniumDriver)


def test_a_front_registered_driver_can_still_take_an_async_page():
    class AsyncAdapter:
        def __init__(self, page):
            self.page = page

    entry = (lambda o: isinstance(o, AsyncPage), AsyncAdapter)
    registry.register_driver_front(*entry)
    try:
        assert isinstance(wrap(AsyncPage()), AsyncAdapter)
    finally:
        registry._REGISTRY.remove(entry)
