"""mendwright.aio: async Playwright on the sync core. Offline: an async fake page over the
lxml-backed FakePage from conftest, a stub provider, asyncio.run() inside plain tests."""

import asyncio
import inspect
import json
import logging
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
        self.frames = []    # a Page's frames (a Frame has no such list)

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

    def __getattr__(self, method):
        if not method.startswith('get_by_'):
            raise AttributeError(method)

        def get_by(*args, **kwargs):
            self.threads.add(threading.get_ident())
            return AsyncFakeGetBy(self, getattr(self.sync, method)(*args, **kwargs))
        return get_by


class AsyncFakeGetBy(AsyncFakeLocator):
    """The async shape of conftest's FakeGetBy, from AsyncFakePage's get_by_* calls."""

    def __init__(self, page, loc):
        self._page = page
        self._loc = loc

    def and_(self, other):
        return AsyncFakeGetBy(self._page, self._loc.and_(other._loc))


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


def test_keys_marked_destructive_are_refused_without_a_model_call(apage, tmp_path,
                                                                 stub_provider):
    provider = stub_provider(HEALED)

    async def main():
        heal = await _open(apage, tmp_path, provider, destructive_keys={'agree'})
        with pytest.raises(AttentionRequired, match="'agree' is marked as a destructive control"):
            await heal.locator('agree', STALE, role='checkbox', timeout_ms=0).check()
        # destructive=False cannot clear a key passed in destructive_keys.
        with pytest.raises(AttentionRequired, match='never auto-heals those'):
            await heal.locator('agree', STALE, role='checkbox', timeout_ms=0,
                               destructive=False).check()
        with pytest.raises(AttentionRequired, match="'terms' is marked as a destructive control"):
            await heal.locator('terms', STALE, role='checkbox', timeout_ms=0,
                               destructive=True).check()

    run(main())
    assert provider.bundles == []


def test_destructive_true_is_kept_by_the_next_open(apage, tmp_path, stub_provider):
    provider = stub_provider(HEALED)

    async def main():
        first = await _open(apage, tmp_path, provider)
        await first.locator('terms', "//input[@id='new-id']", role='checkbox',
                            destructive=True).check()
        # Next run: the selector in the code is stale, and destructive= is left out.
        second = await _open(apage, tmp_path, provider)
        with pytest.raises(AttentionRequired, match='is marked as a destructive control'):
            await second.locator('terms', STALE, role='checkbox', timeout_ms=0).check()

    run(main())
    assert provider.bundles == []


def test_a_word_in_the_key_does_not_block_a_heal(apage, tmp_path, stub_provider):
    provider = stub_provider(HEALED)

    async def main():
        heal = await _open(apage, tmp_path, provider)
        await heal.locator('confirm_payment', STALE, role='checkbox', timeout_ms=0).check()

    run(main())
    assert len(provider.bundles) == 1
    assert apage.sync.actions[-1][:2] == ('xpath=' + HEALED, 'check')


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


@pytest.mark.parametrize('marked', [True, False])
def test_a_saved_heal_is_quarantined_when_the_key_is_marked_destructive(marked, make_page,
                                                                        tmp_path, stub_provider):
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
            await heal.locator('next_step', '#continue', role='button', timeout_ms=0,
                               destructive=True if marked else None).click()
        finally:
            status = heal.store.status('next_step', "//button[@id='p']")
            assert status == ('quarantined' if marked else 'active')

    if marked:
        with pytest.raises(AttentionRequired,
                           match="'next_step' is marked as a destructive control"):
            run(main())
        assert page.sync.actions == []
    else:
        run(main())    # the label 'Pay now' alone does not stop the saved heal
        assert page.sync.actions[-1][:2] == ("xpath=//button[@id='p']", 'click')
    assert provider.bundles == []


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


# --- fingerprint healing --------------------------------------------------------------------------

FP_V1 = ("<form id='f'><label><input type='checkbox' id='old-id' name='terms'>"
         "<span>I accept the terms</span></label></form>")
FP_V2 = ("<form id='f'><label><input type='checkbox' name='agreement' data-testid='consent'>"
         "<span>I accept the terms</span></label></form>")


def test_the_async_healer_records_a_fingerprint_and_heals_from_it(make_page, tmp_path,
                                                                  stub_provider):
    sync = make_page(FP_V1, fingerprints=True)
    page = AsyncFakePage(sync)
    provider = stub_provider("//input[@name='agreement']")

    async def main():
        heal = await _open(page, tmp_path, provider, timeout_ms=0)
        await heal.locator('terms', '#old-id', poll_ms=0)
        assert heal.store.meta('terms')['fingerprint']['attrs']['name'] == 'terms'
        sync.set_html(FP_V2)
        box = await heal.locator('terms', '#old-id', poll_ms=0)
        await box.check(expect=lambda: True)
        return heal

    heal = run(main())
    assert provider.bundles == [] and provider.outcomes == []
    assert heal.heals == [HealEvent('terms', ('#old-id',), "//input[@data-testid='consent']",
                                    True, 'fingerprint')]
    assert sync.fingerprint_reads == 1


def test_the_async_healer_takes_fingerprint_false(make_page, tmp_path, stub_provider):
    sync = make_page(FP_V1, fingerprints=True)
    page = AsyncFakePage(sync)
    provider = stub_provider("//input[@name='agreement']")

    async def main():
        heal = await _open(page, tmp_path, provider, timeout_ms=0, fingerprint=False)
        await heal.locator('terms', '#old-id', poll_ms=0)
        sync.set_html(FP_V2)
        await heal.locator('terms', '#old-id', poll_ms=0)
        return heal

    heal = run(main())
    assert sync.fingerprint_reads == 0 and len(provider.bundles) == 1
    assert heal.heals[-1].source == 'model'


# --- heal.page.get_by_*() with heal_get_by=True ----------------------------------------------

CALL = 'get_by_role("button", name="Sign in")'
LOGIN = "<html><body><main><button id='login' class='primary'>Sign in</button></main></body></html>"
RENAMED = LOGIN.replace('>Sign in<', '>Log in<')


def _call_page(make_page, html=LOGIN):
    sync = make_page(html, url='https://shop.test/login', fingerprints=True)
    return sync, AsyncFakePage(sync)


async def _sign_in(heal, **kwargs):
    return await heal.page.get_by_role('button', name='Sign in').click(**kwargs)


def test_the_sync_bridge_forwards_role_match():
    class Driver:
        async def count(self, selector, frame_chain=()):
            return 1

    class WithRole(Driver):
        async def role_match(self, selector, role, level=None, frame_chain=()):
            return (selector, role, level, frame_chain)

    async def main():
        loop = asyncio.get_running_loop()
        bridge = SyncBridge(WithRole(), loop)
        return await asyncio.to_thread(bridge.role_match, '//b', 'tab', 2)

    assert run(main()) == ('//b', 'tab', 2, ())
    loop = asyncio.new_event_loop()
    try:
        assert SyncBridge(Driver(), loop).role_match is None
    finally:
        loop.close()


def test_an_async_call_acts_records_and_heals_from_its_fingerprint(make_page, tmp_path,
                                                                    stub_provider):
    sync, page = _call_page(make_page)
    provider = stub_provider("//button[@id='login']")

    async def main():
        heal = await _open(page, tmp_path, provider, timeout_ms=0, heal_get_by=True)
        button = heal.page.get_by_role('button', name='Sign in')
        assert repr(button) == "<AsyncHealingGetByLocator '" + CALL + "'>"
        await button.click()
        assert heal.store.meta(CALL)['expectedRole'] == 'button'
        assert heal.store.meta(CALL)['fingerprint'] is not None
        assert await heal.present(CALL) is True
        sync.set_html(RENAMED)
        assert await button.is_visible() is False       # the call as written
        await _sign_in(heal)
        assert await heal.present(CALL) is True         # the saved heal, while the call misses
        assert await heal.present("get_by_role('button', name='Sign in')") is True
        return heal

    heal = run(main())
    assert [a[:2] for a in sync.actions] == [("get_by_role('button', name='Sign in')", 'click'),
                                             ("xpath=//button[@id='login']", 'click')]
    assert [(h.key, h.new, h.source) for h in heal.heals] == [
        (CALL, "//button[@id='login']", 'fingerprint')]
    assert provider.bundles == []


def test_an_async_call_that_matches_several_goes_to_playwright(make_page, tmp_path,
                                                             stub_provider):
    sync, page = _call_page(make_page, LOGIN.replace('</main>', '<button>Sign in</button></main>'))

    async def main():
        heal = await _open(page, tmp_path, stub_provider(None), timeout_ms=10000,
                           heal_get_by=True)
        proxy = heal._get_by_locator(mendwright.getby.parse(CALL),
                                     page.get_by_role('button', name='Sign in'))
        await proxy
        assert proxy._canary is False and proxy._healed is None
        with pytest.raises(Exception, match='strict mode violation') as caught:
            await _sign_in(heal)
        assert not isinstance(caught.value, AttentionRequired)
        return heal

    run(main())
    assert not (tmp_path / 'o.json').exists()


def test_an_async_call_the_browser_rejects_goes_to_playwright_at_once(make_page, tmp_path,
                                                                      stub_provider):
    sync, page = _call_page(make_page)
    sync.reject['get_by_role'] = 'Locator.count: Error: "level" attribute is only supported for roles'

    async def main():
        heal = await _open(page, tmp_path, stub_provider(None), timeout_ms=10000,
                           heal_get_by=True)
        started = time.monotonic()
        with pytest.raises(Exception, match='"level" attribute') as caught:
            await heal.page.get_by_role('button', name='Sign in', level=2).click()
        assert time.monotonic() - started < 5
        assert not isinstance(caught.value, (AttentionRequired, InvalidSelector))

    run(main())
    assert not (tmp_path / 'o.json').exists()


def test_an_async_call_with_no_fingerprint_raises_a_timeout_error(make_page, tmp_path,
                                                                  stub_provider):
    timeout_error = pytest.importorskip('playwright.async_api').TimeoutError
    _sync, page = _call_page(make_page, RENAMED)
    provider = stub_provider("//button[@id='login']")

    async def main():
        heal = await _open(page, tmp_path, provider, timeout_ms=0, heal_get_by=True)
        with pytest.raises(timeout_error) as caught:
            await _sign_in(heal)
        return caught.value

    error = run(main())
    assert isinstance(error, mendwright.getby.GetByNotFound)
    assert str(error).endswith(mendwright.getby.no_fingerprint_message(CALL))
    assert provider.bundles == []


def test_an_async_heal_without_the_calls_aria_role_is_refused(make_page, tmp_path,
                                                              stub_provider):
    pytest.importorskip('playwright.async_api')     # GetByNotFound is Playwright's TimeoutError too
    sync, page = _call_page(make_page, "<html><body><button role='tab' id='settings'>Settings"
                                       '</button></body></html>')
    provider = stub_provider("//button[@id='settings']")

    async def main():
        heal = await _open(page, tmp_path, provider, timeout_ms=0, heal_get_by=True)
        await heal.page.get_by_role('tab', name='Settings').click()
        sync.set_html("<html><body><button id='settings'>Preferences</button></body></html>")
        with pytest.raises(mendwright.getby.GetByNotFound) as caught:
            await heal.page.get_by_role('tab', name='Settings').click()
        return heal, caught.value

    heal, error = run(main())
    assert "Playwright does not give it the ARIA role 'tab')" in str(error)
    assert heal.heals == [] and provider.bundles == []


def test_the_async_calls_own_element_reports_nothing_to_the_canary(make_page, tmp_path,
                                                                   stub_provider):
    heal_xpath = "//button[@id='login']"
    sync, page = _call_page(make_page)

    async def main():
        heal = await _open(page, tmp_path, stub_provider(heal_xpath), timeout_ms=0,
                           heal_get_by=True)
        await _sign_in(heal)
        sync.set_html(RENAMED)
        await _sign_in(heal)
        assert heal.pending() == {CALL: heal_xpath}
        sync.set_html(LOGIN)                     # the call matches again
        sync.fail['click'] = RuntimeError('Locator.click: Element is not attached to the DOM')
        with pytest.raises(RuntimeError):
            await _sign_in(heal)
        return heal

    heal = run(main())
    assert sync.actions[-1][0] == "get_by_role('button', name='Sign in')"
    assert heal.pending() == {CALL: heal_xpath}
    assert heal.store.status(CALL, heal_xpath) == 'active'


def test_an_async_call_that_starts_to_match_several_during_the_wait_passes_through_at_once(
        make_page, tmp_path, stub_provider):
    sync, page = _call_page(make_page, RENAMED)
    real = sync._get_by
    slept = []

    def later(method, args, kwargs):
        if sync.trips >= 2:       # after the first look and the first poll
            sync.set_html(LOGIN.replace('</main>', '<button>Sign in</button></main>'))
        return real(method, args, kwargs)

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    now = [0.0]
    sync._get_by = later

    async def main():
        heal = await _open(page, tmp_path, stub_provider("//button[@id='login']"),
                           timeout_ms=10000, heal_get_by=True)
        heal._sleep, heal._clock = fake_sleep, lambda: now[0]
        with pytest.raises(Exception, match='strict mode violation') as caught:
            await _sign_in(heal)
        assert not isinstance(caught.value, AttentionRequired)
        return heal

    heal = run(main())
    assert len(slept) <= 2 and heal.heals == []
    assert [c['xpath'] for c in heal.store.candidates(CALL)] == [CALL]


def test_an_async_call_that_matches_in_an_iframe_is_not_healed_or_followed(make_page, tmp_path,
                                                                           stub_provider):
    pytest.importorskip('playwright.async_api')     # GetByNotFound is Playwright's TimeoutError too
    page = AsyncFakePage(make_page("<html><body><main><p>Pay below</p></main></body></html>",
                                   url='https://shop.test/checkout', fingerprints=True))
    child = AsyncFakePage(make_page("<html><body><button id='pay'>Pay</button></body></html>",
                                    url='https://pay.test/form'))
    child.name, child.parent_frame = 'payment', page
    main = AsyncFakePage(page.sync)
    main.parent_frame = None
    page.frames = [main, child]
    key = 'get_by_role("button", name="Pay")'
    provider = stub_provider("//button[@id='pay']")

    async def main_():
        heal = await _open(page, tmp_path, provider, timeout_ms=0, heal_get_by=True)
        heal.store.add_candidate(key, "//button[@id='pay']")   # a heal that matches only there
        with pytest.raises(mendwright.getby.GetByNotFound) as caught:
            await heal.page.get_by_role('button', name='Pay').click()
        return heal, caught.value

    heal, error = run(main_())
    assert str(error).endswith(
        "-> '" + key + "' is inside an iframe: " + key + ' matches nothing in the page '
        "mendwright was opened on, but matches inside the iframe named 'payment'. mendwright "
        'did not look for it there, because mendwright does not follow a get_by_* call into an '
        'iframe, and did not ask a model to heal it. Open mendwright on that Frame instead: '
        "heal = await mendwright.aio.open(page.frame(name='payment'), heal_get_by=True).")
    assert not any(c.get('frameXPath') for c in heal.store.candidates(key))
    assert heal.heals == [] and provider.bundles == []


def test_async_reserved_keys_name_the_method_that_refused_them(make_page, tmp_path,
                                                                stub_provider):
    _sync, page = _call_page(make_page)
    call = 'get_by_role("button", name="Go")'

    async def main():
        heal = await _open(page, tmp_path, stub_provider(None), timeout_ms=0, heal_get_by=True)
        refused = []
        for entry, make in (('heal.locator()', lambda: heal.locator(call, '#go')),
                            ('heal.locator_all()', lambda: heal.locator_all(call, '#go')),
                            ('heal.interrupt()', lambda: heal.interrupt(call, '#go'))):
            with pytest.raises(ValueError) as caught:
                make()
            refused.append((entry, str(caught.value)))
        return refused

    for entry, message in run(main()):
        assert message == (
            "mendwright: '" + call + "' is how mendwright names a heal.page.get_by_role() call, "
            'so ' + entry + ' cannot take it as a key. Pick another key.')
    assert not (tmp_path / 'o.json').exists()


def test_async_open_warns_about_a_call_key_written_on_page(make_page, tmp_path, stub_provider,
                                                           caplog):
    _sync, page = _call_page(make_page)
    on_page, total = "page.get_by_role('button', name='Sign in')", "page.get_by_test_id('total')"

    async def main():
        with caplog.at_level(logging.WARNING, logger='mendwright'):
            heal = await _open(page, tmp_path, stub_provider(None), timeout_ms=0,
                               heal_get_by=True, destructive_keys={on_page},
                               expect_values={total: 'money'})
        return heal

    heal = run(main())
    parse = mendwright.getby.parse
    assert [r.getMessage() for r in caplog.records] == [
        mendwright.getby.on_object_message('expect_values=', total,
                                           parse("get_by_test_id('total')")),
        mendwright.getby.on_object_message('destructive_keys=', on_page, parse(CALL))]
    assert not heal.store.is_locked_destructive(CALL)


def test_the_async_call_off_by_default_is_the_real_locator(make_page, tmp_path, stub_provider):
    _sync, page = _call_page(make_page)

    async def main():
        heal = await _open(page, tmp_path, stub_provider(None), timeout_ms=0)
        assert type(heal.page.get_by_role('button', name='Sign in')) is AsyncFakeGetBy
        with pytest.raises(TypeError, match='heal_get_by=True, heal_get_by=False or '
                                            'heal_get_by=None'):
            await _open(page, tmp_path, stub_provider(None), heal_get_by=1)

    run(main())


def test_the_async_healer_saves_the_address_the_sync_one_saves(make_page, tmp_path):
    url = 'https://u:p@Shop.Example/Orders/10482/?t=1#x'
    sync_page = make_page(V2, url=url)
    heal = mendwright.open(sync_page, store_path=str(tmp_path / 's.json'))
    heal.locator('email', '#email', role='input', timeout_ms=0, poll_ms=0)

    async def main():
        aheal = await mendwright.aio.open(AsyncFakePage(make_page(V2, url=url)),
                                          store_path=str(tmp_path / 'a.json'))
        await aheal.locator('email', '#email', role='input', timeout_ms=0, poll_ms=0)
        return aheal

    aheal = run(main())
    assert aheal.store.last_url('email') == heal.store.last_url('email') == (
        'https://shop.example/Orders/*/')
    assert aheal.store.meta('email')['lastPage'] == '/orders/*'


# --- await heal.drift() -------------------------------------------------------------------------

DRIFT_V1 = ("<form><input id='email' name='email'>"
            "<label><input type='checkbox' id='terms' name='terms'> I accept the terms</label>"
            "<button type='button' id='pay'>Pay now</button>"
            "<ul><li class='card'>A</li><li class='card'>B</li></ul></form>")
DRIFT_V2 = ("<form><input id='email' name='email'>"
            "<label><input type='checkbox' name='terms' data-testid='legal'> I accept the "
            "terms</label><button type='button' id='pay-now'>Pay now</button>"
            "<ul><li class='tile'>A</li></ul></form>")
DRIFT_URL = 'https://shop.example/signup'


def _drift_store(make_page, tmp_path, stub_provider):
    page = make_page(DRIFT_V1, url=DRIFT_URL, fingerprints=True)
    heal = mendwright.open(page, provider=stub_provider(None),
                           store_path=str(tmp_path / 'o.json'))
    heal.locator('email', '#email', role='input', timeout_ms=0, poll_ms=0)
    heal.locator('terms', '#terms', role='checkbox', timeout_ms=0, poll_ms=0)
    heal.locator('pay', '#pay', role='button', destructive=True, timeout_ms=0, poll_ms=0)
    heal.locator_all('cards', "//li[@class='card']", timeout_ms=0, poll_ms=0)


def _verdicts(report):
    return [(k.key, k.verdict, k.detail, k.reason, k.heal) for k in report.keys]


def test_await_drift_gives_the_sync_verdicts(make_page, tmp_path, stub_provider):
    _drift_store(make_page, tmp_path, stub_provider)
    before = (tmp_path / 'o.json').read_bytes()
    sync_page = make_page(DRIFT_V2, url=DRIFT_URL, fingerprints=True)
    sync = mendwright.open(sync_page, provider=stub_provider(None),
                           store_path=str(tmp_path / 'o.json')).drift(timeout_ms=0)
    page = AsyncFakePage(make_page(DRIFT_V2, url=DRIFT_URL, fingerprints=True))
    provider = stub_provider(None)

    async def main():
        heal = await _open(page, tmp_path, provider)
        report = await heal.drift(timeout_ms=0)
        return heal, report

    heal, report = run(main())
    assert _verdicts(report) == _verdicts(sync)
    assert {k.key: k.verdict for k in report.keys} == {
        'cards': 'list-missing', 'email': 'ok', 'pay': 'refused', 'terms': 'would-heal'}
    assert (tmp_path / 'o.json').read_bytes() == before
    assert page.sync.actions == [] and provider.bundles == [] and heal.heals == []


class AsyncDocs:
    """A custom async driver over a sync one, with frame_matches() and framed() as
    coroutines, as AsyncPlaywrightDriver has them."""

    def __init__(self, sync):
        self.sync = sync

    async def count(self, selector, frame_chain=()):
        return self.sync.count(selector, frame_chain)

    async def is_visible(self, selector, frame_chain=()):
        return self.sync.is_visible(selector, frame_chain)

    async def handle(self, selector, frame_chain=()):
        return self.sync.handle(selector, frame_chain)

    async def scroll_into_view(self, selector, frame_chain=()):
        return self.sync.scroll_into_view(selector, frame_chain)

    async def describe(self, selector, frame_chain=()):
        return self.sync.describe(selector, frame_chain)

    async def url(self):
        return self.sync.url()

    async def outer_html(self, scope=None):
        return self.sync.outer_html(scope)

    async def frame_matches(self, selector):
        return self.sync.frame_matches(selector)

    async def framed(self, frame_selector):
        inner = self.sync.framed(frame_selector)
        return AsyncDocs(inner) if inner is not None else None


def _two_docs():
    """A sync driver over a page with one iframe, whose checkbox is only in the iframe."""
    import lxml.html

    class Doc:
        def __init__(self, html, inner=None):
            self.doc, self.inner, self.actions = lxml.html.fromstring(html), inner, []

        def _find(self, selector, chain=()):
            body = selector
            if body.startswith(('css=', 'xpath=')):
                body = body.split('=', 1)[1]
            doc = self.inner.doc if chain else self.doc
            if body.startswith('#'):
                return doc.xpath('//*[@id=$i]', i=body[1:])
            return [e for e in doc.xpath(body) if hasattr(e, 'tag')]

        def count(self, selector, frame_chain=()):
            return len(self._find(selector, frame_chain))

        def is_visible(self, selector, frame_chain=()):
            return self.count(selector, frame_chain) == 1

        def handle(self, selector, frame_chain=()):
            self.actions.append(('handle', selector))
            return selector

        def scroll_into_view(self, selector, frame_chain=()):
            self.actions.append(('scroll', selector))

        def describe(self, selector, frame_chain=()):
            found = self._find(selector, frame_chain)
            if len(found) != 1:
                return None
            role = 'checkbox' if found[0].get('type') == 'checkbox' else found[0].tag
            return {'tag': found[0].tag, 'role': role, 'text': '', 'outer_html': ''}

        def url(self):
            return DRIFT_URL

        def outer_html(self, scope=None):
            return lxml.html.tostring(self.doc, encoding='unicode')

    class TwoDocs(Doc):
        def frame_matches(self, selector):
            n = self.inner.count(selector)
            return [{'name': 'consent', 'url': 'about:srcdoc', 'nested': False,
                     'selector': 'iframe[name="consent"]', 'count': n}] if n else []

        def framed(self, frame_selector):
            return self.inner

    inner = Doc("<label><input type='checkbox' id='inner-terms'> I agree</label>")
    return TwoDocs("<main><iframe name='consent'></iframe></main>", inner=inner)


def test_await_drift_follows_a_key_into_an_iframe(tmp_path, stub_provider):
    from mendwright.store import load_store
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('terms', ['#inner-terms'])
    store.set_meta('terms', role='checkbox')
    store.set_last_page('terms', '/signup')
    sync = mendwright.open(_two_docs(), provider=stub_provider(None),
                           store_path=str(tmp_path / 'o.json')).drift(['terms'], timeout_ms=0)
    docs = _two_docs()

    async def main():
        heal = await _open(AsyncDocs(docs), tmp_path, stub_provider(None))
        return await heal.drift(['terms'], timeout_ms=0)

    report = run(main())
    [k] = report.keys
    assert (k.verdict, k.detail) == ('would-heal', 'frame')
    assert _verdicts(report) == _verdicts(sync)
    assert docs.actions == [] and docs.inner.actions == []


def test_await_drift_holds_the_lock_of_the_healer(make_page, tmp_path, stub_provider,
                                                 monkeypatch):
    _drift_store(make_page, tmp_path, stub_provider)
    from mendwright import drift as drift_module
    real = drift_module.probe_page
    order = []

    def probe(*args, **kwargs):
        order.append('drift starts')
        try:
            return real(*args, **kwargs)
        finally:
            order.append('drift ends')
    monkeypatch.setattr(drift_module, 'probe_page', probe)
    page = AsyncFakePage(make_page(DRIFT_V2, url=DRIFT_URL, fingerprints=True))

    async def main():
        heal = await _open(page, tmp_path, stub_provider(None))
        checking = asyncio.ensure_future(heal.drift(['terms'], timeout_ms=200, poll_ms=20))
        await asyncio.sleep(0.05)
        assert heal._lock.locked()

        async def look():
            await heal.locator('email', '#email', role='input', timeout_ms=0, poll_ms=0)
            order.append('locator')
        await asyncio.gather(checking, look())

    run(main())
    assert order == ['drift starts', 'drift ends', 'locator']


def test_await_drift_looks_a_call_up_as_the_sync_one_does(make_page, tmp_path, stub_provider):
    sync_page = make_page(LOGIN, url='https://shop.test/login', fingerprints=True)
    heal = mendwright.open(sync_page, provider=stub_provider(None),
                           store_path=str(tmp_path / 'o.json'), heal_get_by=True, timeout_ms=0)
    heal.page.get_by_role('button', name='Sign in').click()
    sync_page.set_html(RENAMED)
    heal.page.get_by_role('button', name='Sign in').click()          # a heal is saved
    twins = RENAMED.replace('</main>', '<button>Sign in</button><button>Sign in</button></main>')
    sync = mendwright.open(make_page(twins, url='https://shop.test/login', fingerprints=True),
                           provider=stub_provider(None),
                           store_path=str(tmp_path / 'o.json')).drift(timeout_ms=0)

    async def main():
        page = AsyncFakePage(make_page(twins, url='https://shop.test/login', fingerprints=True))
        return await (await _open(page, tmp_path, stub_provider(None))).drift(timeout_ms=0)

    report = run(main())
    assert _verdicts(report) == _verdicts(sync)
    [k] = report.keys
    assert (k.key, k.verdict, [p.result for p in k.probes]) == (
        CALL, 'refused', ['matches 2 elements', 'not tried'])


def test_an_async_call_saves_its_address(make_page, tmp_path, stub_provider):
    async def main():
        page = AsyncFakePage(make_page(LOGIN, url='https://Shop.Test/Login/10482/?x=1',
                                       fingerprints=True))
        heal = await _open(page, tmp_path, stub_provider(None), heal_get_by=True)
        await _sign_in(heal)

    run(main())
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))['selectors']
    assert saved[CALL]['lastUrl'] == 'https://shop.test/Login/*/'


def test_await_drift_checks_its_arguments_first(apage, tmp_path):
    async def main():
        heal = await _open(apage, tmp_path)
        with pytest.raises(TypeError, match='not one string'):
            await heal.drift('terms')

    apage.sync.queries.clear()
    run(main())
    assert apage.sync.queries == []
