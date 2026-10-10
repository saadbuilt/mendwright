"""mendwright.aio: async Playwright on the sync core. Offline: an async fake page over the
lxml-backed FakePage from conftest, a stub provider, asyncio.run() inside plain tests."""

import asyncio
import inspect
import threading
import time

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, InvalidSelector
from mendwright.aio import AsyncActionProxy, AsyncHealer, SyncBridge
from mendwright.drivers.playwright_async import AsyncPlaywrightDriver, detect, is_async_driver
from mendwright.facade import ACTIONS, HealEvent, live_healers

V2 = ("<form><label>terms<input type='checkbox' id='new-id'></label>"
      "<a id='l' href='#'>Docs</a><input id='email' name='email'>"
      "<button id='hidden-go' style='display:none'>Go</button></form>")
STALE = "//input[@id='old-id']"
HEALED = "//input[@type='checkbox']"


def run(coro):
    return asyncio.run(coro)


class AsyncFakeLocator:
    """The async Playwright Locator shape over conftest's FakeLocator."""

    def __init__(self, page, selector):
        self._page = page
        self._loc = page.sync.locator(selector)

    def _touch(self):
        self._page.threads.add(threading.get_ident())

    async def count(self):
        self._touch()
        return self._loc.count()

    async def is_visible(self):
        self._touch()
        return self._loc.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        self._touch()

    async def evaluate_all(self, js):
        self._touch()
        return self._loc.evaluate_all(js)

    async def evaluate(self, js):
        self._touch()
        els = self._loc._elements()
        import lxml.html
        return lxml.html.tostring(els[0], encoding='unicode') if els else None

    async def get_attribute(self, name, **kwargs):
        self._touch()
        return self._loc.get_attribute(name)

    async def inner_text(self, **kwargs):
        self._touch()
        return self._loc.inner_text()

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)   # AttributeError for an unknown verb

        async def run_action(*args, **kwargs):
            self._touch()
            return action(*args, **kwargs)
        return run_action


class AsyncFakePage:
    """The async Playwright Page shape: locator() and frame_locator() are plain calls,
    content() and evaluate() are coroutines. Records the threads it was touched from."""

    def __init__(self, sync_page):
        self.sync = sync_page
        self.threads = set()

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector):
        self.threads.add(threading.get_ident())
        return AsyncFakeLocator(self, selector)

    def frame_locator(self, selector):
        self.sync.queries.append('frame:' + selector)
        return self

    async def content(self):
        self.threads.add(threading.get_ident())
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        self.threads.add(threading.get_ident())
        return self.sync.evaluate(js, arg)


class ThreadedStub:
    """conftest's StubProvider, also recording the thread propose() runs on."""

    def __init__(self, stub):
        self._stub = stub
        self.threads = []

    def __getattr__(self, name):
        return getattr(self._stub, name)

    def propose(self, bundle):
        self.threads.append(threading.get_ident())
        return self._stub.propose(bundle)


@pytest.fixture
def apage(make_page):
    return AsyncFakePage(make_page(V2))


def _active(heal, key='terms'):
    return [c['xpath'] for c in heal.store.candidates(key)]


async def _open(apage, tmp_path, provider=None, **kwargs):
    return await mendwright.aio.open(apage, provider=provider,
                                     store_path=str(tmp_path / 'o.json'), **kwargs)


async def _healed(apage, tmp_path, stub_provider):
    """A heal that just happened for 'terms', its canary pending."""
    provider = stub_provider(HEALED)
    heal = await _open(apage, tmp_path, provider)
    box = await heal.locator('terms', STALE, role='checkbox', timeout_ms=0, poll_ms=0)
    assert heal.pending() == {'terms': HEALED}
    return heal, provider, box


# --- resolve ------------------------------------------------------------------------------

def test_the_documented_usage_resolves_and_acts(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        await heal.locator('terms', '#new-id', role='checkbox').check(force=True)
        return heal

    heal = run(main())
    assert isinstance(heal, AsyncHealer)
    assert apage.sync.actions == [('css=#new-id', 'check', (), {'force': True})]
    assert _active(heal) == ['#new-id']
    assert heal.heals == [] and heal.pending() == {}


def test_locator_returns_at_once_and_checks_its_arguments_first(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        before = len(apage.sync.queries)
        box = heal.locator('terms', '#new-id', role='checkbox')
        assert isinstance(box, AsyncActionProxy) and not box.resolved
        assert len(apage.sync.queries) == before      # nothing touched the page yet
        with pytest.raises(RuntimeError, match='Await the proxy first'):
            _ = box.native
        with pytest.raises(ValueError):
            heal.locator('terms', '#new-id', role='frobnicator')
        with pytest.raises(InvalidSelector):
            heal.locator('terms', '//input[')
        with pytest.raises(TypeError, match='selector string'):
            heal.locator('terms', object())
        with pytest.raises(TypeError, match='both selector and inline_xpath'):
            heal.locator('terms', '#a', inline_xpath='#b')
        assert 'unresolved' in repr(box)

    run(main())


def test_awaiting_the_proxy_resolves_it_once(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        box = await heal.locator('email', '#email')
        queries = len(apage.sync.queries)
        assert box.resolved and box.key == 'email'
        assert await box is box
        assert len(apage.sync.queries) == queries
        await box.fill('x', timeout=5)
        assert await box.get_attribute('name') == 'email'
        return heal, box

    heal, box = run(main())
    assert isinstance(box.native, AsyncFakeLocator)
    assert apage.sync.actions == [('css=#email', 'fill', ('x',), {'timeout': 5})]
    # role= was left out: it is read from the live element and saved, as in sync.
    assert heal.store.meta('email')['expectedRole'] == 'input'
    assert not heal.store.role_is_guess('email')


def test_a_read_on_an_unresolved_proxy_resolves_first(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        return await heal.locator('terms', '#new-id', role='checkbox').get_attribute('type')

    assert run(main()) == 'checkbox'


def test_resolution_polls_with_the_injected_sleep(apage, tmp_path, stub_provider):
    now = [0.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 2:     # the element shows up on the third look
            apage.sync.set_html("<form><input type='checkbox' id='late'></form>")

    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        heal._sleep, heal._clock = fake_sleep, lambda: now[0]
        await heal.locator('late', '#late', role='checkbox', timeout_ms=1000, poll_ms=50).check()

    run(main())
    assert sleeps == [0.05, 0.05]
    assert apage.sync.actions[-1][:2] == ('css=#late', 'check')


def test_a_miss_without_a_provider_says_what_to_do(apage, tmp_path):
    async def main():
        heal = await mendwright.aio.open(apage, store_path=str(tmp_path / 'o.json'),
                                         provider=None, timeout_ms=0)
        await heal.locator('terms', STALE, role='checkbox').check()

    with pytest.raises(AttentionRequired, match='No matching element for: terms'):
        run(main())


def test_code_wins_over_stored_and_healed_selectors(apage, tmp_path, stub_provider):
    async def main():
        heal, _, _ = await _healed(apage, tmp_path, stub_provider)
        await heal.locator('terms', '#new-id', role='checkbox').check()
        return heal

    heal = run(main())
    assert _active(heal) == ['#new-id', HEALED]          # the heal stays as a fallback
    assert heal.store.status('terms', STALE) == 'superseded'


def test_present(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        await heal.locator('terms', '#new-id', role='checkbox')
        assert await heal.present('terms') is True
        apage.sync.set_html('<form></form>')
        assert await heal.present('terms') is False

    run(main())


# --- heal through the bridge ----------------------------------------------------------------

def test_a_heal_runs_the_sync_core_in_a_worker_thread(apage, tmp_path, stub_provider):
    provider = ThreadedStub(stub_provider(HEALED))

    async def main():
        heal = await _open(apage, tmp_path, provider)
        await heal.locator('terms', STALE, role='checkbox', timeout_ms=0).check()
        return heal, threading.get_ident()

    heal, loop_thread = run(main())
    assert provider.threads and loop_thread not in provider.threads
    assert apage.threads == {loop_thread}                # the page is only touched on the loop
    assert apage.sync.actions == [('xpath=' + HEALED, 'check', (), {})]
    assert heal.heals == [HealEvent('terms', (STALE,), HEALED, None)]
    assert heal.pending() == {'terms': HEALED}
    assert HEALED in _active(heal)
    # The bundle was captured from the page through the bridge.
    assert 'new-id' in provider.bundles[0]['html']


def test_the_live_gate_runs_on_the_real_page(apage, tmp_path, stub_provider):
    # Statically fine (one <button> in the HTML) but hidden on the page: the live check
    # through the bridge rejects it, and the model is asked once more.
    provider = stub_provider("//button[@id='hidden-go']")

    async def main():
        heal = await _open(apage, tmp_path, provider)
        await heal.locator('go', "//button[@id='gone']", role='button', timeout_ms=0).click()

    with pytest.raises(AttentionRequired) as info:
        run(main())
    assert 'not uniquely present on the live page' in str(info.value)
    assert len(provider.bundles) == 2
    assert "xpath=//button[@id='hidden-go']" in apage.sync.queries
    assert apage.sync.actions == []


def test_destructive_keys_are_refused_without_a_model_call(apage, tmp_path, stub_provider):
    provider = stub_provider(HEALED)

    async def main():
        heal = await _open(apage, tmp_path, provider, destructive_keys={'agree'})
        with pytest.raises(AttentionRequired, match="looks like a destructive control"):
            await heal.locator('pay_now', STALE, role='checkbox', timeout_ms=0).check()
        with pytest.raises(AttentionRequired, match='never auto-heals those'):
            await heal.locator('agree', STALE, role='checkbox', timeout_ms=0).check()
        # destructive=False opts out of the word check, as in sync.
        await heal.locator('pay_now', STALE, role='checkbox', timeout_ms=0,
                           destructive=False).check()

    run(main())
    assert len(provider.bundles) == 1


def test_a_destructive_button_label_is_learned_and_refused_next_run(make_page, tmp_path,
                                                                    stub_provider):
    page = AsyncFakePage(make_page("<form><input type='submit' id='go' value='Buy  now'></form>"))
    provider = stub_provider("//input[@type='submit']")

    async def main():
        heal = await _open(page, tmp_path, provider)
        await heal.locator('next_step', '#go', role='button').click()
        assert heal.store.meta('next_step')['expectedText'] == ['Buy now']
        heal = await _open(page, tmp_path, provider)
        await heal.locator('next_step', '#moved', role='button', timeout_ms=0).click()

    with pytest.raises(AttentionRequired, match="contains 'buy'"):
        run(main())
    assert provider.bundles == []


def test_a_selector_inside_an_iframe_stops_before_any_model_call(make_page, tmp_path,
                                                                 stub_provider):
    page = AsyncFakePage(make_page("<main><iframe name='consent'></iframe></main>"))
    child = AsyncFakePage(make_page("<input type='checkbox' id='inner-terms'>",
                                    url='about:srcdoc'))
    child.name, child.parent_frame = 'consent', page
    main = AsyncFakePage(page.sync)
    main.parent_frame = None
    page.frames = [main, child]
    provider = stub_provider("//input[@type='checkbox']")

    async def main_():
        heal = await _open(page, tmp_path, provider)
        await heal.locator('frame_terms', '#inner-terms', role='checkbox', timeout_ms=0).check()

    with pytest.raises(AttentionRequired) as info:
        run(main_())
    assert ("heal = await mendwright.aio.open(page.frame(name='consent'))"
            in str(info.value))
    assert provider.bundles == []


def test_an_aria_label_is_learned_and_refused_next_run(make_page, tmp_path, stub_provider):
    page = AsyncFakePage(make_page("<form><button id='b' aria-label='Delete account'>"
                                   "&#215;</button></form>"))
    provider = stub_provider('//button')

    async def main():
        heal = await _open(page, tmp_path, provider)
        await heal.locator('row_action', '#b', role='button').click()
        assert heal.store.meta('row_action')['expectedText'] == ['Delete account']
        heal = await _open(page, tmp_path, provider)
        await heal.locator('row_action', '#moved', role='button', timeout_ms=0).click()

    with pytest.raises(AttentionRequired, match="contains 'delete'"):
        run(main())
    assert provider.bundles == []


def test_a_neutral_key_is_not_healed_onto_a_destructive_label(make_page, tmp_path,
                                                             stub_provider):
    page = AsyncFakePage(make_page("<form><button id='p'>Pay now</button></form>"))
    provider = stub_provider("//button[@id='p']")

    async def main():
        heal = await _open(page, tmp_path, provider)
        await heal.locator('next_step', '#continue', role='button', timeout_ms=0).click()

    with pytest.raises(AttentionRequired, match="looks destructive"):
        run(main())
    assert len(provider.bundles) == 2
    assert page.sync.actions == []


def test_a_saved_heal_on_a_destructive_control_is_quarantined(make_page, tmp_path,
                                                             stub_provider):
    import json
    sp = tmp_path / 'o.json'
    sp.write_text(json.dumps({'schema': 2, 'selectors': {'next_step': {
        'expectedRole': 'button', 'candidates': [
            {'xpath': '#continue', 'source': 'code', 'status': 'active'},
            {'xpath': "//button[@id='p']", 'source': 'heal', 'status': 'active'}]}}}),
        encoding='utf-8')
    page = AsyncFakePage(make_page("<form><button id='p'>Pay now</button></form>"))
    provider = stub_provider(None)

    async def main():
        heal = await _open(page, tmp_path, provider)
        try:
            await heal.locator('next_step', '#continue', role='button', timeout_ms=0).click()
        finally:
            assert heal.store.status('next_step', "//button[@id='p']") == 'quarantined'

    with pytest.raises(AttentionRequired, match="its label 'Pay now' contains 'pay'"):
        run(main())
    assert page.sync.actions == [] and provider.bundles == []


@pytest.mark.parametrize('kind', ['sync', 'async'])
def test_a_closed_guard_blocks_the_heal(kind, apage, tmp_path, stub_provider):
    provider = stub_provider(HEALED)
    calls = []

    def sync_guard():
        calls.append(threading.get_ident())
        return False

    async def async_guard():
        calls.append(threading.get_ident())
        return False

    async def main():
        guard = sync_guard if kind == 'sync' else async_guard
        heal = await _open(apage, tmp_path, provider, guard=guard)
        await heal.locator('terms', STALE, role='checkbox', timeout_ms=0).check()

    with pytest.raises(AttentionRequired, match='transaction guard'):
        run(main())
    assert provider.bundles == []
    assert len(calls) == 1


def test_disable_resolves_but_never_calls_the_provider(apage, tmp_path, stub_provider,
                                                       monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    provider = stub_provider(HEALED)

    async def main():
        heal = await _open(apage, tmp_path, provider)
        await heal.locator('terms', '#new-id', role='checkbox').check()
        await heal.locator('terms', STALE, role='checkbox', timeout_ms=0).check()

    with pytest.raises(AttentionRequired, match='MENDWRIGHT_DISABLE'):
        run(main())
    assert provider.bundles == []


def test_gathered_heals_on_one_healer_take_turns(make_page, tmp_path, stub_provider):
    page = AsyncFakePage(make_page("<form><input type='checkbox' id='a1'>"
                                   "<input id='b1' name='b'></form>"))

    class TwoKeys:
        def __init__(self):
            self.bundles = []
            self.outcomes = []
            self.running = 0
            self.most = 0

        def propose(self, bundle):
            self.bundles.append(bundle['key'])
            self.running += 1
            self.most = max(self.most, self.running)
            time.sleep(0.05)   # a slow provider: the other locator must wait its turn
            self.running -= 1
            xpath = "//input[@id='a1']" if bundle['key'] == 'a' else "//input[@id='b1']"
            return {'xpath': xpath, 'fallbacks': []}

        def report_outcome(self, key, xpath, success):
            self.outcomes.append((key, xpath, success))

    provider = TwoKeys()

    async def main():
        heal = await _open(page, tmp_path, provider)
        a = heal.locator('a', '#gone-a', role='checkbox', timeout_ms=0)
        b = heal.locator('b', '#gone-b', role='input', timeout_ms=0)
        await asyncio.gather(a.check(), b.fill('x'))
        return heal

    heal = run(main())
    assert sorted(provider.bundles) == ['a', 'b'] and provider.most == 1
    assert heal.pending() == {'a': "//input[@id='a1']", 'b': "//input[@id='b1']"}
    assert [h.key for h in heal.heals] in (['a', 'b'], ['b', 'a'])


def test_a_cancelled_step_holds_the_lock_until_its_heal_thread_ends(apage, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def propose(self, bundle):
            entered.set()
            release.wait(5)
            return {'xpath': HEALED, 'fallbacks': []}

        def report_outcome(self, key, xpath, success):
            pass

    async def main():
        heal = await _open(apage, tmp_path, Slow())
        step = asyncio.ensure_future(
            heal.locator('terms', STALE, role='checkbox', timeout_ms=0).check())
        await asyncio.to_thread(entered.wait, 5)
        step.cancel()
        await asyncio.sleep(0.05)
        assert not step.done() and heal._lock.locked()   # the thread still owns the store
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await step
        assert not heal._lock.locked()
        return heal

    heal = run(main())
    assert heal.pending() == {'terms': HEALED}   # the heal finished; nothing acted on it
    assert apage.sync.actions == []


# --- the canary ------------------------------------------------------------------------------

@pytest.mark.parametrize('verb', [v for v in ACTIONS if v not in ('send_keys', 'submit')])
def test_a_failed_action_quarantines_the_heal(verb, apage, tmp_path, stub_provider):
    async def main():
        heal, provider, box = await _healed(apage, tmp_path, stub_provider)
        apage.sync.fail[verb] = RuntimeError('Timeout 500ms exceeded')
        with pytest.raises(RuntimeError):
            await getattr(box, verb)()
        return heal, provider

    heal, provider = run(main())
    assert HEALED not in _active(heal)
    assert provider.outcomes == [('terms', HEALED, False)]
    assert heal.pending() == {}
    assert heal.heals[-1].confirmed is False


@pytest.mark.parametrize('error', [TypeError('unexpected keyword'), ValueError('bad key'),
                                   AttributeError('no such verb')])
def test_usage_errors_do_not_quarantine(error, apage, tmp_path, stub_provider):
    async def main():
        heal, provider, box = await _healed(apage, tmp_path, stub_provider)
        apage.sync.fail['check'] = error
        with pytest.raises(type(error)):
            await box.check()
        return heal, provider

    heal, provider = run(main())
    assert HEALED in _active(heal)
    assert provider.outcomes == []
    assert heal.pending() == {'terms': HEALED}


def test_expect_takes_a_sync_or_an_async_callable(apage, tmp_path, stub_provider):
    async def yes():
        return True

    async def broken():
        raise RuntimeError('page went away')

    async def main(expect, path):
        heal, provider, box = await _healed(apage, path, stub_provider)
        await box.click(expect=expect)
        return heal, provider

    for expect, ok in ((lambda: True, True), (yes, True), (lambda: yes(), True),
                       (lambda: 0, False), (broken, False)):
        path = tmp_path / str(len(list(tmp_path.iterdir())))
        heal, provider = run(main(expect, path))
        assert provider.outcomes == [('terms', HEALED, ok)], expect
        assert (HEALED in _active(heal)) is ok


def test_confirm_and_the_runner_hooks(apage, tmp_path, stub_provider):
    async def main():
        heal, provider, box = await _healed(apage, tmp_path, stub_provider)
        assert box.confirm(True) is box
        assert provider.outcomes == [('terms', HEALED, True)]
        healed = [h for h in live_healers() if h is heal]
        assert healed and heal._core not in live_healers()
        heal2, _, _ = await _healed(apage, tmp_path / 'b', stub_provider)
        assert heal2.confirm_all(False) == 1
        assert heal2.heals[-1].confirmed is False and heal2.pending() == {}

    run(main())


def test_drag_to_resolves_and_unwraps_another_proxy(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        source = heal.locator('terms', '#new-id', role='checkbox')
        target = heal.locator('docs', '#l', role='link')
        await source.drag_to(target)
        return target

    target = run(main())
    assert target.resolved
    assert apage.sync.actions[-1][1:3] == ('drag_to', (target.native,))


def test_every_action_is_awaitable_and_annotated():
    import typing
    for verb in ACTIONS:
        assert inspect.iscoroutinefunction(getattr(AsyncActionProxy, verb)), verb
        assert 'expect' in typing.get_type_hints(getattr(AsyncActionProxy, verb)), verb
    assert inspect.iscoroutinefunction(mendwright.aio.open)
    assert typing.get_type_hints(mendwright.aio.open)['return'] is AsyncHealer
    assert typing.get_type_hints(AsyncHealer.locator)['return'] is AsyncActionProxy
    assert 'open' not in mendwright.aio.__all__


# --- the bridge -------------------------------------------------------------------------------

class _NoNeverAwaited:
    """Fails if a coroutine was left un-awaited (a refused call must not create one)."""

    def __enter__(self):
        import warnings
        self._catch = warnings.catch_warnings(record=True)
        self.records = self._catch.__enter__()
        warnings.simplefilter('always')
        return self

    def __exit__(self, *exc):
        import gc
        gc.collect()
        self._catch.__exit__(*exc)
        assert not [r for r in self.records if 'never awaited' in str(r.message)]
        return False


def test_the_bridge_refuses_the_event_loop_thread_at_once(apage, tmp_path, stub_provider):
    async def main():
        heal = await _open(apage, tmp_path, stub_provider(None))
        bridge = heal.coordinator.driver
        assert isinstance(bridge, SyncBridge)
        bridge.timeout = 5
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='event loop thread'):
            bridge.count("//input[@id='new-id']")
        assert time.monotonic() - started < 1
        # From a worker thread it reaches the page on the loop.
        assert await asyncio.to_thread(bridge.count, "//input[@id='new-id']") == 1
        assert 'new-id' in await asyncio.to_thread(bridge.outer_html)
        assert await asyncio.to_thread(bridge.inventory) == []

    with _NoNeverAwaited():
        run(main())


def test_the_bridge_times_out_and_cancels(tmp_path):
    class StuckDriver:
        def __init__(self):
            self.cancelled = False

        async def count(self, selector, frame_chain=()):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def is_visible(self, selector, frame_chain=()):
            return True

        async def handle(self, selector, frame_chain=()):
            return selector

    async def main():
        driver = StuckDriver()
        bridge = SyncBridge(driver, asyncio.get_running_loop(), timeout=0.05)
        with pytest.raises(TimeoutError, match='did not answer within 0.05 seconds'):
            await asyncio.to_thread(bridge.count, '#x')
        await asyncio.sleep(0)
        return driver

    assert run(main()).cancelled


def test_the_bridge_needs_a_running_loop():
    loop = asyncio.new_event_loop()
    try:
        bridge = SyncBridge(object(), loop)
        with pytest.raises(RuntimeError, match='not running'):
            bridge.run(lambda: 1)
    finally:
        loop.close()


# --- opening and detection --------------------------------------------------------------------

def test_open_rejects_sync_pages_locators_and_unknown_objects(make_page):
    class AsyncLocatorShape:
        def locator(self, sel):
            return self

        def frame_locator(self, sel):
            return self

        def nth(self, i):
            return self

        async def count(self):
            return 1

    async def main(obj):
        await mendwright.aio.open(obj)

    with pytest.raises(TypeError, match=r'sync page or driver.*mendwright\.open\(page\)'):
        run(main(make_page(V2)))
    with pytest.raises(TypeError, match=r'mendwright\.aio\.open\(locator\.page\)'):
        run(main(AsyncLocatorShape()))
    with pytest.raises(TypeError, match='could not detect an async Playwright page in object'):
        run(main(object()))


def test_the_sync_open_points_async_pages_to_aio(apage):
    with pytest.raises(TypeError) as info:
        mendwright.open(apage)
    assert 'heal = await mendwright.aio.open(page)' in str(info.value)


def test_async_selenium_style_drivers_are_still_unsupported():
    class DriverlessChrome:
        switch_to = object()

        async def find_elements(self, by, value):
            return []

    with pytest.raises(TypeError) as info:
        mendwright.open(DriverlessChrome())
    msg = str(info.value)
    assert 'Async Selenium-style drivers are not supported' in msg
    assert 'mendwright.aio.open()' in msg


def test_detection_and_a_custom_async_driver(apage, tmp_path, stub_provider):
    assert detect(apage) and not detect(apage.sync)
    assert is_async_driver(AsyncPlaywrightDriver(apage)) and not is_async_driver(apage)

    class Custom:
        def __init__(self):
            self.acts = []

        async def count(self, selector, frame_chain=()):
            return 1

        async def is_visible(self, selector, frame_chain=()):
            return True

        async def handle(self, selector, frame_chain=()):
            return 'el:' + selector

        async def act_on(self, native, verb, *args, **kwargs):
            self.acts.append((native, verb, args, kwargs))
            return 'done'

    driver = Custom()

    async def main():
        heal = await _open(driver, tmp_path, stub_provider(None))
        assert heal.driver is driver
        return await heal.locator('terms', '#a', role='checkbox').fill('hi', timeout=1)

    assert run(main()) == 'done'
    assert driver.acts == [('el:#a', 'fill', ('hi',), {'timeout': 1})]


# --- the async driver -------------------------------------------------------------------------

def test_the_async_driver_mirrors_the_sync_one(apage):
    driver = AsyncPlaywrightDriver(apage)

    async def main():
        assert await driver.count("//input[@id='new-id']") == 1
        assert await driver.count('#new-id') == 1
        assert await driver.is_visible('#new-id') is True
        assert await driver.is_visible("//button[@id='hidden-go']") is False
        assert await driver.count("//input[@id='new-id']", ('#frame',)) == 1
        described = await driver.describe('#new-id')
        assert described['role'] == 'checkbox' and described['tag'] == 'input'
        assert await driver.describe('//input') is None          # several matches
        assert await driver.url() == 'https://example.test/'
        assert '<form>' in await driver.outer_html()
        assert 'new-id' in await driver.outer_html('#new-id')
        assert await driver.inventory() == []
        loc = await driver.handle('#new-id')
        assert await driver.act_on(loc, 'check', force=True) is None
        with pytest.raises(AttributeError):
            await driver.act_on(loc, 'fly')
        await driver.act('#new-id', 'click')
        with pytest.raises(InvalidSelector):
            await driver.count('//input[')

    run(main())
    assert 'frame:css=#frame' in apage.sync.queries
    assert apage.sync.actions == [('css=#new-id', 'check', (), {'force': True}),
                                  ('css=#new-id', 'click', (), {})]


def test_the_async_driver_maps_browser_parse_errors(apage):
    class Broken(AsyncFakeLocator):
        async def count(self):
            raise RuntimeError(
                'Locator.count: Unexpected token "@" while parsing css selector "a@b"')

    class Flaky(AsyncFakeLocator):
        async def count(self):
            raise RuntimeError('Target page, context or browser has been closed')

        async def evaluate_all(self, js):
            raise RuntimeError('Target page, context or browser has been closed')

    driver = AsyncPlaywrightDriver(apage)

    async def main():
        apage.locator = lambda sel: Broken(apage, sel)
        with pytest.raises(InvalidSelector, match='the browser rejected a selector'):
            await driver.count('a@b')
        apage.locator = lambda sel: Flaky(apage, sel)
        assert await driver.count('#new-id') == 0
        assert await driver.describe('#new-id') is None

    run(main())
