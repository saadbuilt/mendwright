"""Async Playwright support: the healing of mendwright.open(), for playwright.async_api.

    import mendwright.aio

    heal = await mendwright.aio.open(page)
    await heal.locator('terms', '#terms', role='checkbox').check()

heal.locator() returns at once. The element is resolved, and healed if its selectors
miss, when you await an action on it or await the proxy itself
(``box = await heal.locator(...)``).

Every safety decision is made by the sync core, unchanged:
- Resolution polls on the event loop, trying candidates in the same order as the sync
  Healer (the selector in your code first), with ``await asyncio.sleep()`` between polls.
- A heal runs the sync HealCoordinator in a worker thread (asyncio.to_thread). It checks
  destructive keys, the guard and the rate limit, captures the page, asks the provider,
  validates the proposal against the captured HTML and then live on the real page, and
  saves it. It reaches the page through SyncBridge, which hands each driver call back to
  the event loop and waits for the answer. So the live gate checks the page as it is now,
  and a slow provider call blocks only the worker thread.
- A failed action quarantines a just-healed selector; a usage error does not.

Calls on one AsyncHealer take turns (an asyncio.Lock), so the worker thread never touches
the store while the event loop does. asyncio.gather() over several locators of one healer
is safe, but they resolve one after another. A step cancelled during a heal ends once the
heal thread has finished, because a thread cannot be stopped. Providers, stores and
journals stay sync.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import time
from typing import Any, Callable, Iterable, Sequence

from .coordinator import HealCoordinator, resolve_with_heal
from .drivers.base import _member, is_async_api, is_driver, looks_like_locator, unsupported_message
from .drivers.playwright_async import AsyncPlaywrightDriver, is_async_driver
from .drivers.playwright_async import detect as _detect_async_playwright
from .drivers.registry import _REGISTRY
from .exceptions import AttentionRequired
from .facade import (_INPUT_BUTTON, _UNREAD, _register, _unregister, _USAGE_ERRORS, DEFAULT_TIMEOUT_MS,
                     ActionProxy, Healer, HealEvent, Role, Selector, _healed_target_error,
                     _label_source, _locator_args, _pick_label, _setup, _value_label)
from .limits import Guardrails
from .resolver import _frame_chain, _no_match_message, _require_visible, _split_runnable
from .selectors import InvalidSelector

# 'open' is left out on purpose, as in the package: `from mendwright.aio import *` must not
# shadow builtins.open. Call it as mendwright.aio.open(page).
__all__ = ['AsyncHealer', 'AsyncActionProxy', 'SyncBridge']

# expect= on an async action: a callable returning a bool, or an awaitable of one.
AsyncExpect = Callable[[], Any]

_UNRESOLVED = object()


async def _awaited(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _in_thread(fn, *args):
    """fn(*args) in a worker thread (asyncio.to_thread).

    A thread cannot be stopped. If the caller is cancelled meanwhile, this still waits for
    the thread to finish before passing the cancellation on, so the healer's lock is held
    for as long as the thread can touch the store."""
    __tracebackhide__ = True  # pytest shows the caller
    worker = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.wait([worker])
            except asyncio.CancelledError:
                pass
        if not worker.cancelled():
            worker.exception()  # retrieved: the cancellation is what the caller sees
        raise


# --- the bridge the sync heal core uses from its worker thread ---------------------------

class SyncBridge:
    """A sync driver over an async one, for the sync heal core running in a worker thread.

    Each call is scheduled on the event loop that owns the page
    (asyncio.run_coroutine_threadsafe) and waited for, up to ``timeout`` seconds, so the
    page is only ever touched on its own thread. Calling the bridge on that event loop's
    thread would wait for itself forever, so that raises RuntimeError at once.

    It has the driver methods the coordinator uses (count, is_visible, scroll_into_view,
    handle, url, outer_html, describe, inventory), and run() for any other callable.
    describe is None when the async driver has none."""

    timeout = 30.0

    def __init__(self, driver: Any, loop: asyncio.AbstractEventLoop, *,
                 timeout: float | None = None) -> None:
        self._driver = driver
        self._loop = loop
        if timeout is not None:
            self.timeout = timeout
        if not callable(_member(driver, 'describe')):
            # The live gate reads the element through describe() when the driver has it
            # and fails closed when that read fails. A custom async driver without one
            # keeps the count and visibility checks only.
            self.describe = None

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """fn(*args, **kwargs) called on the event loop thread, awaited when it returns an
        awaitable, and its result returned here. Errors from fn propagate."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "mendwright: a sync driver call reached the event loop thread that owns the "
                "page, where waiting for the page would hang forever. On the event loop, "
                "await the async driver instead. The sync bridge is only for worker threads, "
                "such as one started with asyncio.to_thread().")
        if loop.is_closed() or not loop.is_running():
            raise RuntimeError(
                'mendwright: the event loop that owns the page is not running, so the page '
                'cannot be reached. Keep the loop and the browser open until the step that '
                'is healing has finished.')

        async def call():
            return await _awaited(fn(*args, **kwargs))

        future = asyncio.run_coroutine_threadsafe(call(), loop)
        try:
            return future.result(self.timeout)
        except concurrent.futures.TimeoutError:
            if future.done():
                raise  # fn itself raised a TimeoutError
            future.cancel()
            raise TimeoutError(
                'mendwright: the page did not answer within ' + format(self.timeout, 'g')
                + ' seconds. Check that nothing blocks the event loop while a step heals, '
                'or raise mendwright.aio.SyncBridge.timeout.') from None

    def count(self, selector, frame_chain=()):
        return self.run(self._driver.count, selector, frame_chain)

    def is_visible(self, selector, frame_chain=()):
        return self.run(self._driver.is_visible, selector, frame_chain)

    def scroll_into_view(self, selector, frame_chain=()):
        return self.run(self._driver.scroll_into_view, selector, frame_chain)

    def handle(self, selector, frame_chain=()):
        return self.run(self._driver.handle, selector, frame_chain)

    def url(self):
        return self.run(self._driver.url)

    def outer_html(self, scope=None):
        return self.run(self._driver.outer_html, scope)

    def describe(self, selector, frame_chain=()):
        return self.run(self._driver.describe, selector, frame_chain)

    def inventory(self, limit=200):
        return self.run(self._driver.inventory, limit)


# --- opening -------------------------------------------------------------------------------

def _sync_detected(obj):
    """True when mendwright.open() would take obj: a sync driver or registered page type."""
    if is_driver(obj):
        return True
    for detect, _make in list(_REGISTRY):
        try:
            if detect(obj):
                return True
        except Exception:
            continue
    return False


def _unsupported(obj):
    name = type(obj).__name__
    module = type(obj).__module__ or ''
    if looks_like_locator(obj) and (is_async_api(obj) or '.async_api' in module):
        return ('mendwright: got a Playwright ' + name + ', which points at elements, not at a '
                'page. mendwright needs the page to capture HTML when it heals. Pass the page '
                'instead: heal = await mendwright.aio.open(locator.page), or a Frame from '
                'page.frames to work inside an iframe.')
    if is_async_api(obj) or '.async_api' in module:
        return unsupported_message(obj)
    if _sync_detected(obj):
        return ('mendwright: ' + name + ' is a sync page or driver, and mendwright.aio.open() '
                'takes an async Playwright Page or Frame. Pass it to mendwright.open(page) '
                'instead, or open the browser with playwright.async_api.async_playwright().')
    return ('mendwright: could not detect an async Playwright page in ' + name + '. Pass a '
            'Page or Frame from playwright.async_api to mendwright.aio.open(), or a driver '
            'whose count, is_visible and handle are coroutine functions.')


def _async_driver(page):
    if is_async_driver(page):
        return page
    if _detect_async_playwright(page):
        return AsyncPlaywrightDriver(page)
    raise TypeError(_unsupported(page))


async def open(page: Any, *, provider: Any = None, store: Any = None,
               store_path: str | os.PathLike | None = None,
               guard: Callable[[], Any] | None = None, dry_run: bool | None = None,
               destructive_keys: Iterable[str] = frozenset(),
               guardrails: Guardrails | None = None, journal: Any = None,
               rate_per_key_per_hour: int | None = 3, rate_global_per_hour: int | None = 60,
               timeout_ms: int | None = None) -> AsyncHealer:
    """Wrap an async Playwright Page or Frame into an AsyncHealer. Await it inside the
    event loop that runs the page.

    Takes the same arguments, environment variables (MENDWRIGHT_STORE,
    MENDWRIGHT_TIMEOUT_MS, MENDWRIGHT_DRY_RUN, MENDWRIGHT_DISABLE) and defaults as
    mendwright.open(). guard may be a plain or an async callable; it runs on the event
    loop. The provider stays sync and is called in a worker thread. A driver whose count,
    is_visible and handle are coroutine functions is used as it is.
    """
    driver = _async_driver(page)
    bridge = SyncBridge(driver, asyncio.get_running_loop())
    if guard is not None:
        user_guard = guard

        def guard():
            # Called by the coordinator in the worker thread. An async guard must be
            # awaited (a bare coroutine is truthy and would always allow the heal).
            return bool(bridge.run(user_guard))

    store, coord, timeout_ms = _setup(
        bridge, provider=provider, store=store, store_path=store_path, guard=guard,
        dry_run=dry_run, destructive_keys=destructive_keys, guardrails=guardrails,
        journal=journal, rate_per_key_per_hour=rate_per_key_per_hour,
        rate_global_per_hour=rate_global_per_hour, timeout_ms=timeout_ms,
        entry='mendwright.aio.open()')
    return AsyncHealer(page, driver, store, coord, timeout_ms=timeout_ms)


# --- the async healer --------------------------------------------------------------------

async def _match_handle(driver, candidate, require_visible):
    """resolver._match_handle, awaited: the native handle if the candidate matches exactly
    one (visible) element, else None. InvalidSelector propagates."""
    selector, fc = candidate['xpath'], _frame_chain(candidate)
    try:
        if (await driver.count(selector, fc)) == 1 and (
                not require_visible or await driver.is_visible(selector, fc)):
            return await driver.handle(selector, fc)
    except InvalidSelector:
        raise
    except Exception:
        return None
    return None


async def _url(driver):
    url = getattr(driver, 'url', None)
    if not callable(url):
        return ''
    try:
        return await _awaited(url())
    except Exception:
        return ''


async def _live_label(described, native):
    """facade._live_label, with the element read awaited."""
    text, read_value = _label_source(described)
    if read_value:
        try:
            text = _value_label(await _awaited(native.get_attribute('value')))
        except Exception:
            text = ''
    return _pick_label(text, described)


async def _input_value(described, native):
    """facade._input_value, with the element read awaited."""
    if not _INPUT_BUTTON.match((described or {}).get('outer_html') or ''):
        return ''
    try:
        return _value_label(await _awaited(native.get_attribute('value')))
    except Exception:
        return ''


class AsyncHealer:
    """The async Healer: resolves selector keys on one async page, heals them when they
    break, and hands back an AsyncActionProxy. Made by mendwright.aio.open().

    pending(), confirm_all() and heals work as on the sync Healer, and the healer appears
    in mendwright.facade.live_healers(), so test-runner hooks can confirm its heals."""

    def __init__(self, page: Any, driver: Any, store: Any, coordinator: HealCoordinator, *,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.page = page
        self.driver = driver
        self.store = store
        self.coordinator = coordinator
        # The sync Healer's own helpers (argument checks, metadata, the destructive word
        # check, role and label learning) run on the same store; only page work is async
        # here. This healer stands in for it in live_healers().
        self._core = Healer(page, driver, store, coordinator, timeout_ms=timeout_ms)
        _unregister(self._core)
        _register(self)
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def timeout_ms(self) -> int:
        """The default locator() timeout in milliseconds."""
        return self._core.timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._core.timeout_ms = value

    def locator(self, key: str, selector: Selector | None = None, *, role: Role | None = None,
                text: str | Sequence[str] | None = None, destructive: bool | None = None,
                timeout_ms: int | None = None, poll_ms: int = 200,
                inline_xpath: Selector | None = None) -> AsyncActionProxy:
        """Like Healer.locator(), but returns at once: the key is resolved (and healed and
        retried if its candidates miss) when an action on the proxy is awaited, or the
        proxy itself. The arguments mean the same as on Healer.locator().

        A non-string selector (TypeError), a malformed one (InvalidSelector) or an unknown
        role (ValueError) raises here, before anything is awaited. AttentionRequired, when
        the element cannot be found or healed, is raised by the await."""
        roles, code = _locator_args(key, selector, role, inline_xpath)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        return AsyncActionProxy(self, key, (roles, code, text, destructive, timeout, poll_ms))

    async def present(self, key: str) -> bool:
        """True if one of key's stored selectors matches a single element right now.
        No waiting and no healing."""
        async with self._lock:
            store = self.store
            require_visible = _require_visible(store, key)
            candidates = store.candidates(key)
            runnable, errors = _split_runnable(key, candidates)
            for candidate in runnable:
                try:
                    if await _match_handle(self.driver, candidate, require_visible) is not None:
                        return True
                except InvalidSelector as exc:
                    errors.append(exc.for_key(key))
            if candidates and len(errors) == len(candidates):
                raise errors[0]
            return False

    # --- heals awaiting confirmation (hooks for test runners) ------------------------

    def pending(self) -> dict:
        """{key: healed xpath} for heals whose canary has not reported yet. A copy."""
        return self._core.pending()

    def confirm_all(self, success: bool) -> int:
        """Report every pending heal as confirmed (True) or failed (False, which
        quarantines it), and return how many there were. Sync, like Healer.confirm_all();
        call it when no step of this healer is running."""
        return self._core.confirm_all(success)

    @property
    def heals(self) -> list[HealEvent]:
        """The heals this healer applied, oldest first, as HealEvent objects."""
        return self._core.heals

    # --- resolution ----------------------------------------------------------------

    async def _resolve_proxy(self, proxy):
        __tracebackhide__ = True  # pytest shows the caller
        async with self._lock:
            if proxy._native is _UNRESOLVED:
                proxy._native = await self._locate(proxy._key, *proxy._plan)
        return proxy._native

    async def _locate(self, key, roles, code, text, destructive, timeout, poll_ms):
        """Healer.locator() after its argument checks, with the page work awaited.
        Runs under self._lock."""
        __tracebackhide__ = True  # pytest shows the caller
        self._core._prime(key, roles, code, text, destructive)

        async def attempt():
            return await self._resolve(key, timeout, poll_ms)

        try:
            native = await attempt()
        except AttentionRequired as exc:
            await self._stop_if_in_a_frame(key, exc)
            native = await self._heal(key, exc, attempt)
        described = await self._check_healed_target(key, native)
        await self._learn(key, roles, native, text_given=text is not None, described=described)
        return native

    async def _check_healed_target(self, key, native):
        """Healer._check_healed_target(), with the page reads awaited: a match that came
        from a heal and points at a destructive control is quarantined, and
        AttentionRequired is raised before anything acts on it."""
        __tracebackhide__ = True  # pytest shows the caller
        core = self._core
        healed = core._healed_winner(key)
        if healed is None:
            return _UNREAD
        xpath, meta = healed
        described, value = None, ''
        if not meta['destructive'] and meta.get('refuseDestructiveTarget'):
            described = await self._describe_winner(key)
            value = await _input_value(described, native)
        why = core._healed_target_problem(key, meta, described, [value])
        if why:
            coordinator = self.coordinator
            if coordinator.pending.get(key) == xpath:
                # This call's own heal. A gateway provider reports the outcome over HTTP.
                await _in_thread(coordinator.confirm_pending, key, False)
            else:
                self.store.quarantine(key, xpath)
                coordinator.journal.quarantine(key, xpath=xpath)
            raise _healed_target_error(key, xpath, why, core._locked(key))
        return _UNREAD if described is None else described

    async def _resolve(self, key, timeout_ms, poll_ms):
        """mendwright.resolver.resolve(), awaited: the candidates in the same order, bad
        selectors dropped (InvalidSelector at once when none can run), the first unique
        (visible) match returned and recorded, AttentionRequired at the deadline."""
        store, driver = self.store, self.driver
        candidates = store.candidates(key)
        if not candidates:
            raise AttentionRequired('No selector candidates for: ' + key)
        runnable, errors = _split_runnable(key, candidates)
        if not runnable:
            raise errors[0]
        require_visible = _require_visible(store, key)
        deadline = self._clock() + timeout_ms / 1000.0
        while True:
            for candidate in list(runnable):
                try:
                    handle = await _match_handle(driver, candidate, require_visible)
                except InvalidSelector as exc:
                    runnable.remove(candidate)
                    errors.append(exc.for_key(key))
                    if not runnable:
                        raise errors[0] from None
                    continue
                if handle is not None:
                    try:
                        await driver.scroll_into_view(candidate['xpath'], _frame_chain(candidate))
                    except Exception:
                        pass
                    store.record_success(key, candidate['xpath'])
                    return handle
            if self._clock() >= deadline:
                raise AttentionRequired(_no_match_message(key, candidates, errors))
            await self._sleep(poll_ms / 1000.0)

    async def _stop_if_in_a_frame(self, key, exc):
        """Healer._stop_if_in_a_frame(), with the frame lookups awaited."""
        __tracebackhide__ = True
        if not callable(_member(self.driver, 'frame_matches')):
            return
        for selector in self._core._frame_candidates(key):
            try:
                frames = await _awaited(self.driver.frame_matches(selector))
            except Exception:
                continue
            if frames:
                self._core._frame_stop(key, exc, selector, frames, 'await mendwright.aio.open')

    async def _heal(self, key, exc, attempt):
        """Run the sync heal (coordinator.resolve_with_heal) in a worker thread, then retry
        attempt() on the event loop. The coordinator reaches the page through SyncBridge."""
        __tracebackhide__ = True  # pytest shows the caller
        meta = self._core._heal_meta(key)
        context = {'url': await _url(self.driver)}
        missed = [exc]

        def resolve_fn():
            # resolve_with_heal calls this once to see the miss and once more after a heal
            # was applied. That retry is awaited on the event loop below instead.
            if missed:
                raise missed.pop()
            return None

        await _in_thread(resolve_with_heal, resolve_fn, self.coordinator, key, meta, context)
        return await attempt()

    async def _learn(self, key, roles, native, *, text_given, described=_UNREAD):
        """Healer._learn(), with describe() and the label read awaited."""
        core = self._core
        todo = core._learn_todo(key, roles, text_given=text_given)
        if todo is None:
            return
        if described is _UNREAD:
            described = await self._describe_winner(key)
        label = ''
        if todo[2] and described is not None:
            label = await _live_label(described, native)
        core._learn_from(key, roles, todo, described, label)

    async def _describe_winner(self, key):
        target = self._core._winner_target(key)
        describe = _member(self.driver, 'describe')
        if target is None or not callable(describe):
            return None
        try:
            out = await _awaited(self.driver.describe(*target))
        except Exception:
            return None
        return out if isinstance(out, dict) else None

    async def _confirm(self, key, success):
        """coordinator.confirm_pending(), in a worker thread (a gateway provider reports
        the outcome over HTTP) and under the lock."""
        async with self._lock:
            if key in self.coordinator.pending:
                await _in_thread(self.coordinator.confirm_pending, key, bool(success))

    def __repr__(self) -> str:
        return '<AsyncHealer page=' + repr(self.page) + '>'


async def _unwrap(value):
    if isinstance(value, AsyncActionProxy):
        return await value._ensure()
    if isinstance(value, ActionProxy):
        return value.native
    return value


async def _act_on(driver, native, verb, *args, **kwargs):
    act_on = getattr(driver, 'act_on', None)
    if callable(act_on):
        return await _awaited(act_on(native, verb, *args, **kwargs))
    return await _awaited(getattr(native, verb)(*args, **kwargs))


class AsyncActionProxy:
    """The element an AsyncHealer resolves, with the heal canary attached.

    Actions (mendwright.facade.ACTIONS) are coroutines: ``await proxy.check()``. The first
    one, or ``await proxy``, resolves the key (healing it if needed). Keyword arguments
    pass through, and the native result comes back. If the action itself fails in the
    browser, a selector healed for this step is quarantined. A usage error
    (AttributeError, TypeError, ValueError) is raised as is and quarantines nothing.

    expect= takes a callable returning a bool, or an awaitable of one, for example
    ``expect=lambda: box.native.is_checked()``. Its result confirms the heal (True) or
    quarantines it (False or an error). Other attributes fall through to the native
    Locator once the proxy is resolved; before that, a method call such as
    ``await heal.locator(...).inner_text()`` resolves first."""

    def __init__(self, healer: AsyncHealer, key: str, plan: tuple) -> None:
        self._healer = healer
        self._key = key
        self._plan = plan
        self._native = _UNRESOLVED

    @property
    def key(self) -> str:
        return self._key

    @property
    def resolved(self) -> bool:
        """True once the key has been resolved to an element."""
        return self._native is not _UNRESOLVED

    @property
    def native(self) -> Any:
        """The native async Playwright Locator. Pass it to APIs that check the type, such as
        ``await expect(proxy.native).to_be_checked()``. Await the proxy first."""
        if self._native is _UNRESOLVED:
            raise RuntimeError(
                "mendwright: '" + self._key + "' has not been resolved yet, so there is no "
                "native Locator. Await the proxy first, for example "
                "box = await heal.locator('" + self._key + "', ...), then use box.native.")
        return self._native

    async def _ensure(self):
        __tracebackhide__ = True  # pytest shows the caller
        if self._native is _UNRESOLVED:
            await self._healer._resolve_proxy(self)
        return self._native

    async def _resolved_self(self):
        __tracebackhide__ = True  # pytest shows the caller
        await self._ensure()
        return self

    def __await__(self):
        return self._resolved_self().__await__()

    async def _act(self, verb: str, args: tuple, kwargs: dict, expect: AsyncExpect | None) -> Any:
        __tracebackhide__ = True  # pytest shows the caller
        native = await self._ensure()
        args = tuple([await _unwrap(a) for a in args])
        kwargs = {k: await _unwrap(v) for k, v in kwargs.items()}
        healer = self._healer
        try:
            result = await _act_on(healer.driver, native, verb, *args, **kwargs)
        except _USAGE_ERRORS:
            raise
        except Exception:
            await healer._confirm(self._key, False)  # the browser action failed
            raise
        if expect is not None:
            try:
                ok = bool(await _awaited(expect()))
            except Exception:
                ok = False
            await healer._confirm(self._key, ok)
        return result

    async def click(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('click', args, kwargs, expect)

    async def dblclick(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('dblclick', args, kwargs, expect)

    async def fill(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('fill', args, kwargs, expect)

    async def type(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('type', args, kwargs, expect)

    async def press(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('press', args, kwargs, expect)

    async def press_sequentially(self, *args: Any, expect: AsyncExpect | None = None,
                                 **kwargs: Any) -> Any:
        return await self._act('press_sequentially', args, kwargs, expect)

    async def check(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('check', args, kwargs, expect)

    async def uncheck(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('uncheck', args, kwargs, expect)

    async def set_checked(self, *args: Any, expect: AsyncExpect | None = None,
                          **kwargs: Any) -> Any:
        return await self._act('set_checked', args, kwargs, expect)

    async def select_option(self, *args: Any, expect: AsyncExpect | None = None,
                            **kwargs: Any) -> Any:
        return await self._act('select_option', args, kwargs, expect)

    async def hover(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('hover', args, kwargs, expect)

    async def tap(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('tap', args, kwargs, expect)

    async def clear(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('clear', args, kwargs, expect)

    async def focus(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('focus', args, kwargs, expect)

    async def set_input_files(self, *args: Any, expect: AsyncExpect | None = None,
                              **kwargs: Any) -> Any:
        return await self._act('set_input_files', args, kwargs, expect)

    async def drag_to(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        """Drag to another element. The target may be another proxy."""
        return await self._act('drag_to', args, kwargs, expect)

    async def send_keys(self, *args: Any, expect: AsyncExpect | None = None,
                        **kwargs: Any) -> Any:
        return await self._act('send_keys', args, kwargs, expect)

    async def submit(self, *args: Any, expect: AsyncExpect | None = None, **kwargs: Any) -> Any:
        return await self._act('submit', args, kwargs, expect)

    def confirm(self, success: bool) -> AsyncActionProxy:
        """Confirm (True) or quarantine (False) a heal for this key, from a check of your
        own. Sync, like ActionProxy.confirm()."""
        self._healer.coordinator.confirm_pending(self._key, bool(success))
        return self

    def __getattr__(self, name: str) -> Any:
        # Reads and anything else not in ACTIONS go to the native Locator. Before the
        # proxy is resolved, a method call resolves it first.
        native = self.__dict__.get('_native', _UNRESOLVED)
        if native is not _UNRESOLVED:
            return getattr(native, name)
        if name.startswith('_'):
            raise AttributeError(name)

        async def deferred(*args, **kwargs):
            target = await self._ensure()
            attr = getattr(target, name)
            if not callable(attr):
                raise TypeError(
                    'mendwright: ' + name + ' is not a method of the native Locator. Await '
                    'the proxy first, then read it: box = await heal.locator(...); box.'
                    + name)
            return await _awaited(attr(*args, **kwargs))

        deferred.__name__ = name
        return deferred

    def __repr__(self) -> str:
        native = 'unresolved' if self._native is _UNRESOLVED else repr(self._native)
        return '<AsyncActionProxy key=' + repr(self._key) + ' native=' + native + '>'
