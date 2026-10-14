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


# --- flow changes: interruptions, the cookies= preset, optional steps, the page guard ---------

_COOKIE_BANNER = """<div id="cookie-banner" role="dialog" aria-label="Cookie consent"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4)">
  <button id="cookie-accept" type="button" style="position: absolute; left: 16px; bottom: 16px"
    onclick="document.getElementById('consent').textContent = 'accepted';
             document.getElementById('cookie-banner').remove()">Accept all</button></div>"""
_SIGNUP = ("<p>Consent: <span id='consent'>none</span></p><form><input id='email'>"
           "<button type='button' id='create' onclick=\"document.getElementById('done')"
           ".textContent = 'created'\">Create account</button></form><p id='done'></p>")
_ONETRUST = _SIGNUP.replace('<form>', """<div id="onetrust-consent-sdk"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">
  <div id="onetrust-banner-sdk" role="region" aria-label="Cookie banner"
    style="position: absolute; left: 0; right: 0; bottom: 0; background: #fff">
    <button id="onetrust-reject-all-handler" onclick="choose('rejected')">Reject All</button>
    <button id="onetrust-accept-btn-handler" onclick="choose('accepted')">Accept All</button>
  </div></div>
<script>function choose(c) { document.getElementById('consent').textContent = c;
  document.getElementById('onetrust-consent-sdk').remove(); }</script><form>""")


async def _close(page):
    try:
        await page.context.close()
    except Exception:
        pass   # some Firefox builds fail BrowserContext.close; the browser closes later


async def _texts(page):
    return (await page.input_value('#email'), await page.text_content('#consent'),
            await page.text_content('#done'))


def test_flow_a_banner_that_now_opens_first(engine, tmp_path):
    provider = Stub("//button[@id='create']")

    async def main():
        page = await engine.page(_SIGNUP + _COOKIE_BANNER)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=300)
            heal.interrupt('cookies', '#cookie-accept')
            await heal.locator('email', '#email').fill('ada@example.com')
            await heal.locator('create', '#create').click()
            old_step = heal.locator('cookies', '#cookie-accept')
            result = await old_step.click()
            return heal, result, old_step.present, await _texts(page)
        finally:
            await _close(page)

    heal, result, present, texts = engine.run(main())
    assert result is None and present is False
    assert texts == ('ada@example.com', 'accepted', 'created')
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [('cookies', 'email')]
    assert provider.bundles == []


@pytest.mark.parametrize('choice, outcome', [('reject', 'rejected'), ('accept', 'accepted')])
def test_flow_the_cookies_preset(engine, tmp_path, choice, outcome):
    async def main():
        page = await engine.page(_ONETRUST)
        try:
            heal = await mendwright.aio.open(page, provider=Stub(), store_path=_store(tmp_path),
                                             timeout_ms=300, cookies=choice)
            await heal.locator('email', '#email').fill('ada@example.com')
            return heal, await _texts(page)
        finally:
            await _close(page)

    heal, texts = engine.run(main())
    assert texts[:2] == ('ada@example.com', outcome)
    assert [e.key for e in heal.handled_interruptions] == ['cookie_banner']


def test_flow_actions_mendwright_does_not_route_reject_the_banner_first(engine, tmp_path):
    # get_by_role and a chained locator are the real page's: Playwright's overlay handler
    # hands over to mendwright before them.
    async def main():
        page = await engine.page(_ONETRUST)
        try:
            heal = await mendwright.aio.open(page, provider=Stub(), store_path=_store(tmp_path),
                                             timeout_ms=300, cookies='reject')
            await heal.page.get_by_role('button', name='Create account').click(timeout=5000)
            await heal.page.locator('form').locator('#email').fill('ada@example.com')
            return heal, await _texts(page)
        finally:
            await _close(page)

    heal, texts = engine.run(main())
    assert texts == ('ada@example.com', 'rejected', 'created')
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookie_banner', '<page action>')]


_RACE = _ONETRUST.replace('id="onetrust-consent-sdk"', 'id="onetrust-consent-sdk" hidden') + """
<script>const create = document.getElementById('create'); create.disabled = true;
setTimeout(() => document.getElementById('onetrust-consent-sdk').hidden = false, 500);
setTimeout(() => { create.disabled = false; }, 1200);</script>"""


@pytest.mark.parametrize('routed', [True, False])
def test_flow_a_banner_that_opens_while_an_action_waits(engine, tmp_path, routed):
    # Create account is disabled until 1.2 s and the banner opens at 0.5 s, after the
    # check before the click: Playwright hands over to mendwright while the click waits.
    async def main():
        page = await engine.page(_RACE)
        try:
            heal = await mendwright.aio.open(page, provider=Stub(), store_path=_store(tmp_path),
                                             timeout_ms=300, cookies='reject')
            started = asyncio.get_running_loop().time()
            if routed:
                await heal.locator('create', '#create').click(timeout=30000)
            else:
                await heal.page.get_by_role('button', name='Create account').click(timeout=30000)
            return heal, asyncio.get_running_loop().time() - started, await _texts(page)
        finally:
            await _close(page)

    heal, elapsed, texts = engine.run(main())
    assert elapsed < 6              # without the handler: the click's whole 30 s timeout
    assert texts[1:] == ('rejected', 'created')
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookie_banner', 'create' if routed else '<page action>')]


def test_flow_an_optional_step(engine, tmp_path):
    provider = Stub("//button[@id='create']")

    async def main():
        page = await engine.page(_SIGNUP)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=200)
            promo = heal.locator('promo', '#promo-close', optional=True, role='button')
            clicked = await promo.click()
            there = heal.locator('create', '#create', optional=True)
            await there.click()
            return clicked, promo.present, there.present, await _texts(page)
        finally:
            await _close(page)

    clicked, absent, present, texts = engine.run(main())
    assert clicked is None and absent is False and present is True
    assert texts[2] == 'created'
    assert provider.bundles == []


def test_flow_the_page_guard_across_two_urls(engine, tmp_path):
    from urllib.parse import urlsplit
    pages = {'/signup': "<form><input id='email'><button type='button' id='continue'>Continue"
                        "</button></form>",
             '/signup/details': "<form><input id='phone'><button type='button' id='help'>Help"
                                "</button></form>"}
    provider = Stub("//button[@id='help']")

    async def serve(route):
        await route.fulfill(status=200, content_type='text/html',
                            body=pages.get(urlsplit(route.request.url).path, ''))

    async def main():
        page = await engine.page('')
        try:
            await page.route('https://mendwright.test/**', serve)
            await page.goto('https://mendwright.test/signup')
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=200)
            await heal.locator('continue', '#continue').click()
            await page.goto('https://mendwright.test/signup/details')
            try:
                await heal.locator('continue', '#continue')
            except AttentionRequired as exc:
                error = str(exc)
            else:
                error = ''
            calls = len(provider.bundles)
            across = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                               timeout_ms=200, heal_across_pages=True)
            await across.locator('continue', '#continue')
            return error, calls, across
        finally:
            await _close(page)

    error, calls, across = engine.run(main())
    assert ("'continue' was last found on /signup, and this page is /signup/details. That looks "
            "like a change in the flow") in error
    assert 'pass heal_across_pages=True to mendwright.aio.open().' in error
    assert calls == 0
    assert len(provider.bundles) == 1 and across.heals[0].new == "//button[@id='help']"


FP_V1 = ("<form id='signup'><label><input type='checkbox' id='terms' name='terms'> "
         "I accept the terms</label></form>")
FP_V2 = ("<form id='signup'><label><input type='checkbox' name='agreement' "
         "data-testid='consent'> I accept the terms</label></form>")


def test_a_redesign_heals_from_the_fingerprint_with_no_model(engine, tmp_path):
    provider = Stub()

    async def main():
        page = await engine.page(FP_V1)
        try:
            heal = await mendwright.aio.open(page, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=300)
            await heal.locator('terms', '#terms').check()
            recorded = heal.store.meta('terms')['fingerprint']
            await page.set_content(FP_V2)
            box = heal.locator('terms', '#terms')
            await box.check(expect=lambda: box.native.is_checked())
            return heal, recorded, await page.locator("[data-testid='consent']").is_checked()
        finally:
            await page.context.close()

    heal, recorded, checked = engine.run(main())
    assert recorded['label'] == 'I accept the terms' and recorded['attrs']['name'] == 'terms'
    assert checked
    assert provider.bundles == [] and provider.outcomes == []
    assert heal.heals == [HealEvent('terms', ('#terms',), "//input[@data-testid='consent']",
                                    True, 'fingerprint')]


def test_heal_page_runs_an_unchanged_async_script(engine, tmp_path):
    provider = Stub(HEALED)

    async def main():
        raw = await engine.page(TERMS)
        try:
            heal = await mendwright.aio.open(raw, provider=provider, store_path=_store(tmp_path),
                                             timeout_ms=300, fingerprint=False)
            page = heal.page                   # the one changed line

            async def script():
                await page.locator('#terms').check()
                return await page.is_checked('#terms')

            first = await script()
            await page.set_content(TERMS_V2)   # passes through to the real page
            hidden = await page.is_visible('#terms')
            second = await script()
            visible = await page.locator('#terms').is_visible()
            return first, hidden, second, visible, await raw.locator('#terms-v2').is_checked()
        finally:
            await raw.context.close()

    first, hidden, second, visible, checked = engine.run(main())
    assert (first, hidden, second, visible, checked) == (True, False, True, True, True)
    assert len(provider.bundles) == 1


FRAMED = ("<h1>Outer</h1><iframe id='consent' srcdoc=\"<label>I agree <input type='checkbox' "
          "name='agree' id='inner-terms'></label>\"></iframe>")


def test_a_key_is_followed_into_an_iframe_and_heals_there(engine, tmp_path):
    # Opened on the page: the selector matches only inside the iframe, so it is looked up
    # there with no model call. After a redesign inside the iframe, the heal captures and
    # checks the iframe's document.
    async def main():
        page = await engine.page(FRAMED)
        try:
            await page.frame_locator('#consent').locator('#inner-terms').wait_for(state='attached')
            first = Stub()
            heal = await mendwright.aio.open(page, provider=first, store_path=_store(tmp_path))
            await heal.locator('terms', '#inner-terms', role='checkbox').check()
            followed = list(heal.heals)

            await page.set_content(FRAMED.replace('inner-terms', 'terms-v2'))
            await page.frame_locator('#consent').locator('#terms-v2').wait_for(state='attached')
            second = Stub("//input[@name='agree']")
            again = await mendwright.aio.open(page, provider=second, store_path=_store(tmp_path),
                                              fingerprint=False)
            await again.locator('terms', '#inner-terms', role='checkbox', timeout_ms=500).check()
            checked = await page.frame_locator('#consent').locator('#terms-v2').is_checked()
            return followed, first, again.heals, second, checked
        finally:
            await page.context.close()

    followed, first, heals, second, checked = engine.run(main())
    assert [h.source for h in followed] == ['frame'] and first.bundles == []
    assert [h.source for h in heals] == ['model'] and checked
    assert 'terms-v2' in second.bundles[0]['html'] and 'Outer' not in second.bundles[0]['html']
