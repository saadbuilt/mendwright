"""fingerprint(selector) on every driver: one round trip, the same shape from a browser
script and from lxml, None for anything but exactly one match, and saved in the store."""

import asyncio
import json

import lxml.html
import pytest

from mendwright import InvalidSelector, load_store
from mendwright.aio import SyncBridge
from mendwright.demo import StandInPage
from mendwright.drivers import driver_fingerprint
from mendwright.drivers.playwright_async import AsyncPlaywrightDriver
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.fingerprint import FINGERPRINT_JS, of_element, raw_of_element

HTML = ("<html><body><form id='f'><label>Email<input id='e' name='email' value='secret'>"
        "</label><button class='btn' type='submit'>Send</button><button>Send</button>"
        "</form></body></html>")


class Loc:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def _els(self):
        body = self.selector.split('=', 1)[1]
        return self.page.doc.xpath(body) if self.selector.startswith('xpath=') else []

    def evaluate_all(self, js):
        self.page.calls.append(js)
        if self.page.error:
            raise self.page.error
        els = self._els()
        return raw_of_element(els[0]) if js == FINGERPRINT_JS and len(els) == 1 else None


class Page:
    url = 'https://example.test/'

    def __init__(self, html=HTML, error=None):
        self.doc = lxml.html.fromstring(html)
        self.calls = []
        self.error = error

    def locator(self, selector):
        return Loc(self, selector)

    def frame_locator(self, selector):
        return self


def test_playwright_reads_it_in_one_round_trip():
    page = Page()
    fp = PlaywrightSyncDriver(page).fingerprint("//input[@id='e']")
    assert page.calls == [FINGERPRINT_JS]
    assert fp == of_element(page.doc.xpath("//input[@id='e']")[0])
    assert fp['label'] == 'Email' and 'secret' not in json.dumps(fp)


def test_none_for_no_match_several_matches_or_an_error():
    driver = PlaywrightSyncDriver(Page())
    assert driver.fingerprint("//input[@id='missing']") is None
    assert driver.fingerprint('//button') is None
    assert PlaywrightSyncDriver(Page(error=RuntimeError('detached'))).fingerprint('//button') is None
    with pytest.raises(InvalidSelector):
        driver.fingerprint('//input[')


def test_async_playwright_reads_it_too():
    class AsyncLoc(Loc):
        async def evaluate_all(self, js):
            return Loc.evaluate_all(self, js)

    class AsyncPage(Page):
        def locator(self, selector):
            return AsyncLoc(self, selector)

    page = AsyncPage()
    fp = asyncio.run(AsyncPlaywrightDriver(page).fingerprint("//button[@class='btn']"))
    assert fp['text'] == 'Send' and fp['classes'] == ['btn']


class Element:
    def __init__(self, el):
        self.el = el


class WebDriver:
    def __init__(self, html=HTML):
        self.doc = lxml.html.fromstring(html)
        self.scripts = []
        self.switch_to = object()

    def find_elements(self, by, value):
        return [Element(e) for e in self.doc.xpath(value)]

    def execute_script(self, script, *args):
        self.scripts.append(script)
        assert script == 'return (' + FINGERPRINT_JS + ')([arguments[0]]);'
        return raw_of_element(args[0].el)


def test_selenium_reads_it_with_one_script():
    web = WebDriver()
    fp = SeleniumDriver(web).fingerprint("//input[@id='e']")
    assert len(web.scripts) == 1
    assert fp == of_element(web.doc.xpath("//input[@id='e']")[0])
    assert SeleniumDriver(web).fingerprint('//button') is None    # two buttons
    assert len(web.scripts) == 1


def test_the_stand_in_page_answers_the_script_like_a_browser():
    page = StandInPage(HTML)
    loc = page.locator("xpath=//input[@id='e']")
    assert loc.evaluate_all(FINGERPRINT_JS) == raw_of_element(page.query("xpath=//input[@id='e']")[0])
    assert page.locator('xpath=//button').evaluate_all(FINGERPRINT_JS) is None


def test_driver_fingerprint_falls_back_to_none():
    class NoMethod:
        pass

    class Bad:
        def fingerprint(self, selector, frame_chain=()):
            return {'tag': 'a'}       # not a stored fingerprint

    class Raises:
        def fingerprint(self, selector, frame_chain=()):
            raise RuntimeError('gone')

    for driver in (NoMethod(), Bad(), Raises()):
        assert driver_fingerprint(driver, '//a') is None
    assert driver_fingerprint(PlaywrightSyncDriver(Page()), "//input[@id='e']")['tag'] == 'input'


def test_the_sync_bridge_has_it_only_when_the_async_driver_does():
    class Plain:
        async def count(self, selector, frame_chain=()):
            return 1

        async def is_visible(self, selector, frame_chain=()):
            return True

        async def handle(self, selector, frame_chain=()):
            return selector

    loop = asyncio.new_event_loop()
    try:
        assert SyncBridge(Plain(), loop).fingerprint is None
        assert callable(SyncBridge(AsyncPlaywrightDriver(Page()), loop).fingerprint)
    finally:
        loop.close()


# --- the store -------------------------------------------------------------------------------

def test_intent_and_fingerprint_are_saved_with_the_key(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, path)
    fp = of_element(lxml.html.fromstring(HTML).xpath("//input[@id='e']")[0])
    store.set_code_candidates('email', ['#e'])
    store.set_meta('email', intent='the email field of the contact form', fingerprint=fp)
    again = load_store(None, path).meta('email')
    assert again['intent'] == 'the email field of the contact form'
    assert again['fingerprint'] == fp
    saved = json.loads(path.read_text(encoding='utf-8'))['selectors']['email']
    assert saved['intent'] == 'the email field of the contact form' and saved['fingerprint'] == fp


def test_bad_intent_or_fingerprint_values_are_refused(tmp_path):
    store = load_store(None, tmp_path / 'o.json')
    with pytest.raises(ValueError, match='intent must be a non-empty string'):
        store.set_meta('k', intent='  ')
    with pytest.raises(ValueError, match='takes a fingerprint from mendwright.fingerprint'):
        store.set_meta('k', fingerprint={'tag': 'a'})


def test_a_malformed_fingerprint_in_the_file_is_ignored(tmp_path):
    path = tmp_path / 'o.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'fingerprint': {'v': 1}, 'intent': 7,
        'candidates': [{'xpath': '#k', 'source': 'code', 'status': 'active'}]}}}),
        encoding='utf-8')
    meta = load_store(None, path).meta('k')
    assert meta['fingerprint'] is None and meta['intent'] is None


def test_another_process_s_fingerprint_is_kept_on_a_write(tmp_path):
    path = tmp_path / 'o.json'
    fp = of_element(lxml.html.fromstring(HTML).xpath("//input[@id='e']")[0])
    mine = load_store(None, path)
    theirs = load_store(None, path)
    theirs.set_code_candidates('email', ['#e'])
    theirs.set_meta('email', fingerprint=fp)
    mine.set_code_candidates('other', ['#o'])       # a write that merges the file
    assert load_store(None, path).meta('email')['fingerprint'] == fp
