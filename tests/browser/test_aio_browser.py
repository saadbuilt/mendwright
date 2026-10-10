"""mendwright.aio against real browsers: async Playwright on Chromium, Firefox and WebKit.

Run with: python -m pytest -m browser tests/browser/test_aio_browser.py

Pages come from set_content(), the provider is a local stub (no network, no model), and
playwright is imported inside the fixture, so collecting this file without playwright
installed is fine. One event loop and one browser per engine are shared by the module.
"""

import asyncio
import threading

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, HealEvent

pytestmark = pytest.mark.browser

ENGINES = ('chromium', 'firefox', 'webkit')


class Stub:
    """Proposes a fixed xpath and records the bundles, outcomes and propose() threads."""

    def __init__(self, xpath=None):
        self.xpath = xpath
        self.bundles = []
        self.outcomes = []
        self.threads = []

    def propose(self, bundle):
        self.bundles.append(bundle)
        self.threads.append(threading.get_ident())
        if self.xpath is None:
            return None
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': 'stub'}

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, bool(success)))


class Engine:
    """One event loop and one launched browser, shared by the tests of one engine."""

    def __init__(self, name):
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._pw = None
        self.browser = None

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    async def start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self.browser = await getattr(self._pw, self.name).launch()

    async def stop(self):
        if self.browser is not None:
            await self.browser.close()
        if self._pw is not None:
            await self._pw.stop()

    def close(self):
        try:
            self.run(self.stop())
            self.run(self.loop.shutdown_default_executor())
        finally:
            self.loop.close()

    async def page(self, html):
        context = await self.browser.new_context()
        page = await context.new_page()
        await page.set_content(html)
        return page


@pytest.fixture(scope='module', params=ENGINES)
def engine(request):
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        pytest.skip('playwright is not installed')
    eng = Engine(request.param)
    try:
        eng.run(eng.start())
    except Exception as exc:
        eng.close()
        pytest.skip(request.param + ' could not be launched: ' + str(exc).strip().splitlines()[0])
    yield eng
    eng.close()


def _store(tmp_path):
    return str(tmp_path / 'overrides.json')


def _active(heal, key):
    return [c['xpath'] for c in heal.store.candidates(key)]


TERMS = "<form><label>I agree <input type='checkbox' id='terms'></label></form>"
TERMS_V2 = "<form><label>I agree <input type='checkbox' id='terms-v2'></label></form>"
HEALED = "//input[@type='checkbox']"


def test_the_documented_usage(engine, tmp_path):
    async def main():
        page = await engine.page(TERMS)
        try:
            heal = await mendwright.aio.open(page, provider=Stub(), store_path=_store(tmp_path))
            await heal.locator('terms', '#terms', role='checkbox').check()
            return heal, await page.locator('#terms').is_checked()
        finally:
            await page.context.close()

    heal, checked = engine.run(main())
    assert checked
    assert heal.heals == [] and _active(heal, 'terms') == ['#terms']


def test_a_broken_selector_heals_in_a_worker_thread(engine, tmp_path):
    provider = Stub(HEALED)

    async def main():
        page = await engine.page(TERMS_V2)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=300)
            box = heal.locator('terms', '#terms', role='checkbox')
            await box.check(expect=lambda: box.native.is_checked())
            checked = await page.locator('#terms-v2').is_checked()
            # A second run on the same store resolves through the heal, without the model.
            again = await mendwright.aio.open(page, provider=provider,
                                              store_path=_store(tmp_path), timeout_ms=300)
            await again.locator('terms', '#terms', role='checkbox').uncheck()
            unchecked = not await page.locator('#terms-v2').is_checked()
            return heal, checked, unchecked, threading.get_ident()
        finally:
            await page.context.close()

    heal, checked, unchecked, loop_thread = engine.run(main())
    assert checked and unchecked
    assert len(provider.bundles) == 1
    assert provider.threads[0] != loop_thread
    assert heal.heals == [HealEvent('terms', ('#terms',), HEALED, True)]
    assert provider.outcomes == [('terms', HEALED, True)]
    assert HEALED in _active(heal, 'terms')


def test_the_live_gate_rejects_an_element_hidden_by_css(engine, tmp_path):
    # The stylesheet hides the button, which lxml cannot see: the proposal passes the
    # static check on the captured HTML and fails the live check on the page.
    html = ("<style>#go { display: none }</style>"
            "<form><button id='go' type='button' onclick='window.clicked = 1'>Go</button></form>")
    provider = Stub("//button[@id='go']")

    async def main():
        page = await engine.page(html)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=200)
            try:
                await heal.locator('go', '#start', role='button').click()
            except AttentionRequired as exc:
                error = str(exc)
            else:
                error = ''
            return heal, error, await page.evaluate('window.clicked || 0')
        finally:
            await page.context.close()

    heal, error, clicked = engine.run(main())
    assert 'not uniquely present on the live page' in error
    assert len(provider.bundles) == 2           # the first proposal plus one re-prompt
    assert clicked == 0
    assert heal.heals == [] and "//button[@id='go']" not in _active(heal, 'go')


def test_actions_pass_keyword_arguments(engine, tmp_path):
    html = """<form>
      <input id='name'>
      <select id='colour'><option value='r'>Red</option><option value='b'>Blue</option></select>
      <input type='checkbox' id='news'>
      <button type='button' id='save' ondblclick='window.saved = (window.saved || 0) + 2'
              onclick='window.saved = (window.saved || 0) + 1'>Save</button>
    </form>"""

    async def main():
        page = await engine.page(html)
        try:
            heal = await mendwright.aio.open(page, provider=Stub(), store_path=_store(tmp_path))
            name = heal.locator('name', '#name', role='input')
            await name.fill('Ada', timeout=2000)
            await name.press('End', timeout=2000)
            await name.press_sequentially(' L', delay=5, timeout=2000)
            chosen = await heal.locator('colour', '#colour').select_option(label='Blue',
                                                                           timeout=2000)
            await heal.locator('news', '#news', role='checkbox').set_checked(True, force=True)
            save = heal.locator('save', "//button[@id='save']", role='button')
            await save.hover(timeout=2000)
            await save.click(timeout=2000, expect=lambda: True)
            await save.dblclick(timeout=2000)
            with pytest.raises(TypeError):
                await save.click(no_such_option=1)
            return (await page.input_value('#name'), chosen,
                    await page.is_checked('#news'), await page.evaluate('window.saved'),
                    heal.store.meta('colour')['expectedRole'])
        finally:
            await page.context.close()

    value, chosen, news, saved, colour_role = engine.run(main())
    assert value == 'Ada L'
    assert chosen == ['b']
    assert news is True
    assert saved == 1 + (1 + 1 + 2)           # click, then dblclick (two clicks and a dblclick)
    assert colour_role == 'combobox'          # role= was left out: read from the page


def test_a_failed_action_quarantines_the_heal(engine, tmp_path):
    provider = Stub(HEALED)

    async def main():
        page = await engine.page(TERMS_V2)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=200)
            box = await heal.locator('terms', '#terms', role='checkbox')
            await page.evaluate("document.querySelector('#terms-v2').remove()")
            with pytest.raises(Exception) as info:
                await box.check(timeout=300)
            return heal, info.value
        finally:
            await page.context.close()

    heal, error = engine.run(main())
    assert 'Timeout' in type(error).__name__ or 'Timeout' in str(error)
    assert provider.outcomes == [('terms', HEALED, False)]
    assert heal.store.status('terms', HEALED) == 'quarantined'
    assert heal.pending() == {}


@pytest.mark.parametrize('how', ['destructive_keys', 'destructive_arg'])
def test_destructive_keys_are_never_healed(how, engine, tmp_path):
    provider = Stub("//button[@id='pay']")

    async def main():
        page = await engine.page("<button id='pay'>Pay now</button>")
        try:
            heal = await mendwright.aio.open(
                page, provider=provider, store_path=_store(tmp_path), timeout_ms=100,
                destructive_keys={'pay_button'} if how == 'destructive_keys' else frozenset())
            with pytest.raises(AttentionRequired,
                               match="'pay_button' is marked as a destructive control"):
                await heal.locator('pay_button', '#old-pay', role='button',
                                   destructive=True if how == 'destructive_arg' else None).click()
        finally:
            await page.context.close()

    engine.run(main())
    assert provider.bundles == []


def test_a_frame_and_playwright_expect(engine, tmp_path):
    html = ("<iframe id='f' srcdoc=\"<label>ok <input type='checkbox' id='inner'></label>\">"
            "</iframe>")

    async def main():
        from playwright.async_api import expect
        page = await engine.page(html)
        try:
            frame = page.frame_locator('#f')
            await frame.locator('#inner').wait_for(state='attached')
            inner = [f for f in page.frames if f != page.main_frame][0]
            heal = await mendwright.aio.open(inner, provider=Stub(), store_path=_store(tmp_path))
            box = await heal.locator('inner', '#inner', role='checkbox')
            await box.check()
            await expect(box.native).to_be_checked()
            return await heal.present('inner')
        finally:
            await page.context.close()

    assert engine.run(main()) is True


def test_selector_kinds_bad_selectors_and_frame_chains(engine, tmp_path):
    html = (TERMS + "<iframe id='f' srcdoc=\"<button id='inner'>Inner</button>\"></iframe>")
    provider = Stub(HEALED)

    async def main():
        import time

        from mendwright import InvalidSelector
        from mendwright.drivers.playwright_async import AsyncPlaywrightDriver
        page = await engine.page(html)
        try:
            await page.frame_locator('#f').locator('#inner').wait_for(state='attached')
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path))
            # A Playwright engine selector resolves and acts like any other.
            await heal.locator('terms', "role=checkbox[name='I agree']", role='checkbox').check()
            checked = await page.is_checked('#terms')
            # A CSS selector the browser rejects fails at once, with no model call.
            started = time.monotonic()
            with pytest.raises(InvalidSelector, match='the browser rejected a selector'):
                await heal.locator('bad', 'a@@b', role='button').click()
            elapsed = time.monotonic() - started
            # Frame chains go through frame_locator(), as in the sync driver.
            driver = AsyncPlaywrightDriver(page)
            framed = (await driver.count('#inner', ('#f',)),
                      await driver.count("//button[@id='inner']", ("//iframe[@id='f']",)),
                      (await driver.describe('#inner', ('#f',)))['role'],
                      await driver.count('#inner'))
            return checked, elapsed, framed
        finally:
            await page.context.close()

    checked, elapsed, framed = engine.run(main())
    assert checked
    assert elapsed < 3 and provider.bundles == []
    assert framed == (1, 1, 'button', 0)


def test_the_sync_open_points_an_async_page_to_aio(engine):
    async def main():
        page = await engine.page(TERMS)
        try:
            with pytest.raises(TypeError) as info:
                mendwright.open(page)
            return str(info.value)
        finally:
            await page.context.close()

    assert 'heal = await mendwright.aio.open(page)' in engine.run(main())
