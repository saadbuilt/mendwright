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
  keys marked destructive, the guard and the rate limit, captures the page, asks the provider,
  validates the proposal against the captured HTML and then live on the real page, and
  saves it. It reaches the page through SyncBridge, which hands each driver call back to
  the event loop and waits for the answer. So the live gate checks the page as it is now,
  and a slow provider call blocks only the worker thread.
- A failed action quarantines a just-healed selector; a usage error does not.
- Flow changes work as on the sync Healer: heal.interrupt(), open(cookies=...),
  locator(optional=True) and the page guard (open(heal_across_pages=...)). Looking for
  and handling an interruption runs the sync Healer's code in a worker thread through
  SyncBridge, so both make the same decisions. Interruptions are also registered with
  page.add_locator_handler, as on the sync Healer; Playwright calls that handler on the
  event loop, and it hands the work to a worker thread the same way. With
  open(dismiss=True), the look at what covers an element before an action is awaited on
  the event loop; dismissing a popup runs the sync Healer's code in a worker thread, as
  handling an interruption does. heal.step() and run_steps() (see mendwright.steps) look
  at the landmarks on the event loop and await each step's function.

Calls on one AsyncHealer take turns (an asyncio.Lock), so the worker thread never touches
the store while the event loop does. asyncio.gather() over several locators of one healer
is safe, but they resolve one after another. A step cancelled during a heal ends once the
heal thread has finished, because a thread cannot be stopped. Providers, stores and
journals stay sync.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import os
import time
import weakref
from typing import Any, Callable, Iterable, Sequence

from .coordinator import HealCoordinator, resolve_with_heal
from .drivers.base import _member, is_async_api, is_driver, looks_like_locator, unsupported_message
from .drivers.playwright_async import AsyncPlaywrightDriver, is_async_driver
from .drivers.playwright_async import detect as _detect_async_playwright
from .drivers.registry import _REGISTRY
from .exceptions import AttentionRequired
from .fingerprint import valid as valid_fingerprint
from .dismiss import NOT_LOOKED_AT, parse_probe, popup_of
from .facade import _dismiss_choice
from .facade import (_register, _unregister, _USAGE_ERRORS, DEFAULT_TIMEOUT_MS, ActionProxy, Healer,
                     HealEvent, Role, Selector, _NOT_FOLLOWED, _cookie_preset,
                     _healed_target_error, _join_overlay, _leave_overlay, _locator_args,
                     _overlay_locator, _overlay_options, _overlay_owner, _raised_by_run,
                     _removable, _reraise, _setup, latest_success)
from .flow import (InterruptionLoop, absent_message, is_interception_error, is_stale_error,
                   page_path, rejected_selector_message, run_plain_message)
from .interruptions import Budget, function_name
from .limits import Guardrails
from .resolver import _frame_chain, _no_match_message, _require_visible, _split_runnable
from .selectors import XPATH, InvalidSelector, classify
from . import steps as _steps

# 'open' is left out on purpose, as in the package: `from mendwright.aio import *` must not
# shadow builtins.open. Call it as mendwright.aio.open(page).
__all__ = ['AsyncHealer', 'AsyncActionProxy', 'SyncBridge']

logger = logging.getLogger('mendwright')

# expect= on an async action: a callable returning a bool, or an awaitable of one.
AsyncExpect = Callable[[], Any]

_UNRESOLVED = object()
_ABSENT = object()   # _locate()'s result for a step that was skipped


async def _awaited(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _registering(shared):
    """The asyncio.Lock under which the async healers of a page change its overlay
    handlers (see AsyncHealer._watch), kept on the page's _PageOverlays."""
    if shared.registering is None:
        shared.registering = asyncio.Lock()
    return shared.registering


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


def _scoped_heal(scope, resolve_fn, coordinator, key, meta, context):
    """resolve_with_heal() with the coordinator scoped to an iframe (see
    HealCoordinator.scoped). Runs in a worker thread."""
    frame, bridge = scope
    with coordinator.scoped(bridge, frame):
        return resolve_with_heal(resolve_fn, coordinator, key, meta, context)


# --- the bridge the sync heal core uses from its worker thread ---------------------------

class SyncBridge:
    """A sync driver over an async one, for the sync heal core running in a worker thread.

    Each call is scheduled on the event loop that owns the page
    (asyncio.run_coroutine_threadsafe) and waited for, up to ``timeout`` seconds, so the
    page is only ever touched on its own thread. Calling the bridge on that event loop's
    thread would wait for itself forever, so that raises RuntimeError at once.

    It has the driver methods the coordinator uses (count, is_visible, scroll_into_view,
    handle, url, outer_html, describe, inventory, fingerprint), act_on for an
    interruption's action, visible_match for the WARNING about a selector that matches
    several elements, the popup methods of open(dismiss=True) (popup_probe and the
    others), run() for any other callable, and wait() for a run= function of
    heal.interrupt(). describe, fingerprint, visible_match and the popup methods are None
    when the async driver has none."""

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
        if not callable(_member(driver, 'fingerprint')):
            self.fingerprint = None
        if not callable(_member(driver, 'visible_match')):
            self.visible_match = None
        for name in _POPUP_METHODS:
            if not callable(_member(driver, name)):
                setattr(self, name, None)   # open(dismiss=True) leaves popups alone

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """fn(*args, **kwargs) called on the event loop thread, awaited when it returns an
        awaitable, and its result returned here. Errors from fn propagate."""
        return self._call(fn, args, kwargs, self.timeout)

    def wait(self, fn: Callable[..., Any], *args: Any) -> Any:
        """run(fn, *args) with no time limit: for a run= function of heal.interrupt(), the
        user's own code, which may wait for the page as long as it needs. It runs in a task
        that copies this thread's context variables, as asyncio.to_thread() does."""
        return self._call(fn, args, {}, None)

    def _call(self, fn, args, kwargs, timeout):
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
            return future.result(timeout)
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

    def fingerprint(self, selector, frame_chain=()):
        return self.run(self._driver.fingerprint, selector, frame_chain)

    def visible_match(self, selector, frame_chain=()):
        return self.run(self._driver.visible_match, selector, frame_chain)

    def act_on(self, native, verb, *args, **kwargs):
        """Run verb on a handle() result, through the async driver's act_on when it has
        one, else as getattr(native, verb)(...), awaited on the event loop."""
        if callable(_member(self._driver, 'act_on')):
            return self.run(self._driver.act_on, native, verb, *args, **kwargs)
        return self.run(getattr(native, verb), *args, **kwargs)

    # The popup methods of open(dismiss=True) (see mendwright.drivers.base). None when the
    # async driver has none.

    def popup_probe(self, native, known=None, scroll=False):
        return self.run(self._driver.popup_probe, native, known, scroll)

    def popup_root(self, native, expect):
        return self.run(self._driver.popup_root, native, expect)

    def popup_controls(self, native, root):
        return self.run(self._driver.popup_controls, native, root)

    def popup_click(self, native, root, expect, timeout_ms):
        return self.run(self._driver.popup_click, native, root, expect, timeout_ms)

    def press_escape(self, native, root):
        return self.run(self._driver.press_escape, native, root)

    def popup_release(self, root):
        return self.run(self._driver.popup_release, root)


_POPUP_METHODS = ('popup_probe', 'popup_root', 'popup_controls', 'popup_click', 'press_escape',
                  'popup_release')


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
               timeout_ms: int | None = None, cookies: str | None = None,
               heal_across_pages: bool = False, fingerprint: bool | None = None,
               dismiss: bool = False) -> AsyncHealer:
    """Wrap an async Playwright Page or Frame into an AsyncHealer. Await it inside the
    event loop that runs the page.

    Takes the same arguments, environment variables (MENDWRIGHT_STORE,
    MENDWRIGHT_TIMEOUT_MS, MENDWRIGHT_DRY_RUN, MENDWRIGHT_DISABLE, MENDWRIGHT_FINGERPRINT)
    and defaults as mendwright.open(), cookies=, heal_across_pages=, fingerprint= and
    dismiss= included. guard may be a plain or an async callable; it runs on the event
    loop. The provider stays sync and is called in a worker thread. A driver whose count,
    is_visible and handle are coroutine functions is used as it is.
    """
    from .proxies import unwrap
    page = unwrap(page)    # heal.page of another healer: use the real page
    preset = _cookie_preset(cookies, 'mendwright.aio.open()')
    _dismiss_choice(dismiss, 'mendwright.aio.open()')
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
        entry='mendwright.aio.open()', heal_across_pages=heal_across_pages,
        fingerprint=fingerprint)
    healer = AsyncHealer(page, driver, store, coord, timeout_ms=timeout_ms)
    healer._core._dismiss_on = dismiss
    if preset is not None:
        healer._core._interruptions[preset.key] = preset
        await healer._watch(preset)
    return healer


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


class AsyncHealer:
    """The async Healer: resolves selector keys on one async page, heals them when they
    break, and hands back an AsyncActionProxy. Made by mendwright.aio.open().

    pending(), confirm_all(), heals, interrupt() and handled_interruptions work as on the
    sync Healer, and the healer appears in mendwright.facade.live_healers(), so
    test-runner hooks can confirm its heals."""

    def __init__(self, page: Any, driver: Any, store: Any, coordinator: HealCoordinator, *,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._page = page
        self._driver = driver
        self.store = store
        self.coordinator = coordinator
        # The sync Healer's own helpers (argument checks, metadata, role learning) run on
        # the same store; only page work is async here. This healer stands in for it in
        # live_healers().
        self._core = Healer(page, driver, store, coordinator, timeout_ms=timeout_ms)
        self._core._runner = self._run_function
        _unregister(self._core)
        _register(self)
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._bridge = None
        self._watching: set = set()     # interrupt()'s registration tasks still running

    @property
    def page(self) -> Any:
        """The page to use in place of your own: page = (await mendwright.aio.open(page)).page.
        For an async Playwright page it is an AsyncHealingPage: page.locator(selector) and
        the page shortcuts that take a selector work as on Healer.page, with the actions
        and reads awaited. Everything else is the real page's. For a custom async driver,
        the object mendwright was opened on."""
        proxy = self.__dict__.get('_page_proxy')
        if proxy is None:
            from .proxies import AsyncHealingPage
            healing = isinstance(self._driver, AsyncPlaywrightDriver)
            proxy = AsyncHealingPage(self, self._page) if healing else self._page
            self._page_proxy = proxy
        return proxy

    @property
    def driver(self) -> Any:
        """mendwright's own async Driver for the page."""
        return self._driver

    def _knows(self, key):
        return self._core._knows(key)

    async def _passes_through(self, key, selector):
        """Healer._passes_through(), awaited: True when selector matches something on the
        page now, but not one visible element, and no saved selector of key finds one."""
        driver = self._driver
        try:
            count = await _awaited(driver.count(selector))
        except Exception:
            return False
        if not count:
            return False
        if count == 1:
            try:
                if await _awaited(driver.is_visible(selector)):
                    return False
            except Exception:
                return False
        try:
            return not await self.present(key)
        except Exception:
            return True

    async def _peek_visible(self, key):
        """Healer._peek_visible(), awaited: no wait, no heal, no scrolling, and nothing
        recorded in the store."""
        async with self._turn():
            require_visible = _require_visible(self.store, key)
            runnable, _errors = _split_runnable(key, self.store.candidates(key))
            for candidate in runnable:
                try:
                    native = await _match_handle(self._driver, candidate, require_visible)
                except Exception:
                    continue
                if native is not None:
                    try:
                        return bool(await _act_on(self._driver, native, 'is_visible'))
                    except Exception:
                        return False
            return False

    @property
    def timeout_ms(self) -> int:
        """The default locator() timeout in milliseconds."""
        return self._core.timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._core.timeout_ms = value

    def interrupt(self, key: str, selector: Selector | None, *, action: str | None = None,
                  args: Sequence[Any] = (), role: Role | None = None,
                  run: Callable[[Any], Any] | None = None) -> None:
        """Register a step that may appear at any point of the flow, as
        Healer.interrupt() does, or remove one with interrupt(key, None). Sync: it only
        registers. The action is awaited when the interruption is handled. run= takes an
        async function, which gets heal.page and is awaited on the event loop, or a plain
        one that returns an awaitable; one that returns anything else raises TypeError
        when the interruption is handled.

        On an async Playwright Page (Playwright 1.42 and later) Playwright's overlay
        handler is registered too, as on the sync Healer. That call is a coroutine, so it
        runs as a task on the event loop and is in place a moment later, before Playwright
        checks the page again while an action waits. Removing it works the same way, and
        the changes to one page's handlers run one at a time, in the order of the calls."""
        core = self._core
        core.interrupt(key, selector, action=action, args=args, role=role, run=run)
        if not isinstance(self._driver, AsyncPlaywrightDriver):
            return
        if selector is None:
            self._on_loop(self._unwatch, key)
        else:
            self._on_loop(self._watch, core._interruptions[key])

    def _on_loop(self, fn, arg):
        """Run coroutine function fn(arg) on the page's event loop: as a task when called
        on that loop, else (from another thread) through the SyncBridge, which waits for
        it. Never raises."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(fn(arg))
            self._watching.add(task)        # the loop keeps only a weak reference to a task
            task.add_done_callback(self._watching.discard)
            return
        bridge = getattr(self.coordinator, 'driver', None)
        try:
            bridge.run(fn, arg)
        except Exception:
            logger.debug('mendwright: could not change an overlay handler for %r',
                         getattr(arg, 'key', arg), exc_info=True)

    def _run_function(self, item, bridge):
        """Healer._runner for this healer: await interruption item's run= function on the
        event loop, from the worker thread the sync Healer's code handles interruptions
        in, and wait for it there. The task it runs in copies that thread's context, so it
        is inside the run (facade._IN_RUN) and its own calls skip the lock (_turn), which
        the step that handles the interruption holds while it waits."""
        bridge.wait(self._await_run, item)

    async def _await_run(self, item):
        """item.run(heal.page), awaited: TypeError for a plain function that returned no
        awaitable, since heal.page's calls are coroutines here."""
        result = item.run(self.page)
        if not inspect.isawaitable(result):
            raise TypeError(run_plain_message(item.key, function_name(item.run), result))
        await result

    def _turn(self):
        """The healer's lock, so that its calls take turns (see the module docs), or none
        inside a run= function of interrupt(): the step that runs it holds the lock and
        waits for it, and its calls would otherwise wait for that step forever."""
        return contextlib.nullcontext() if self._core._in_run() else self._lock

    @property
    def handled_interruptions(self) -> list:
        """The interruptions this healer handled, oldest first, as
        InterruptionEvent(key, before)."""
        return self._core.handled_interruptions

    def locator(self, key: str, selector: Selector | None = None, *, role: Role | None = None,
                text: str | Sequence[str] | None = None, destructive: bool | None = None,
                timeout_ms: int | None = None, poll_ms: int = 200,
                inline_xpath: Selector | None = None, optional: bool = False,
                intent: str | None = None) -> AsyncActionProxy:
        """Like Healer.locator(), but returns at once: the key is resolved (and healed and
        retried if its candidates miss) when an action on the proxy is awaited, or the
        proxy itself. The arguments mean the same as on Healer.locator(), optional= and
        intent= included.

        A non-string selector (TypeError), a malformed one (InvalidSelector), an unknown
        role (ValueError) or intent= with nothing stored and no role= (ValueError) raises
        here, before anything is awaited. AttentionRequired, when the element cannot be
        found or healed, is raised by the await."""
        roles, code = _locator_args(key, selector, role, inline_xpath, intent)
        self._core._check_intent(key, code, roles, intent)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        return AsyncActionProxy(self, key, (roles, code, text, destructive, timeout, poll_ms,
                                            bool(optional), intent))

    async def present(self, key: str) -> bool:
        """True if one of key's stored selectors matches a single element right now.
        No waiting and no healing."""
        async with self._turn():
            store = self.store
            require_visible = _require_visible(store, key)
            candidates = store.candidates(key)
            runnable, errors = _split_runnable(key, candidates)
            for candidate in runnable:
                try:
                    if await _match_handle(self._driver, candidate, require_visible) is not None:
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
        async with self._turn():
            if proxy._native is _UNRESOLVED:
                native = await self._locate(proxy._key, *proxy._plan)
                if native is _ABSENT:
                    proxy._present = False
                    native = None
                proxy._native = native
        return proxy._native

    async def _locate(self, key, roles, code, text, destructive, timeout, poll_ms,
                      optional=False, intent=None):
        """Healer.locator() after its argument checks, with the page work awaited.
        Runs under self._lock (or, inside a run= function, under the lock of the step that
        runs it: see _turn). Returns _ABSENT for a step that was skipped."""
        __tracebackhide__ = True  # pytest shows the caller
        core = self._core
        core._prime(key, roles, code, text, destructive, intent)
        first_wait = core._first_wait(key, timeout)

        async def attempt(wait=timeout):
            return await self._resolve(key, wait, poll_ms)

        try:
            native = await self._resolve_step(key, lambda: attempt(first_wait), Budget(),
                                              timeout, poll_ms)
        except InterruptionLoop:
            raise   # the page is blocked: a heal would not help
        except AttentionRequired as exc:
            if _raised_by_run(exc):
                raise   # a run= function's own, not a miss of this step
            skip = core._skip_reason(key, optional)
            if skip:
                core._record_skip(key, skip)
                return _ABSENT
            native = await self._follow_into_frame(key, exc, attempt)
            if native is _NOT_FOLLOWED:
                native = await self._heal(key, exc, attempt)
        await self._check_healed_target(key)
        await self._learn(key, roles)
        core._remember_page(key, await _url(self._driver))
        await self._remember_fingerprint(key)
        return native

    async def _resolve_step(self, key, attempt, budget, timeout, poll_ms):
        """Healer._resolve_step(), awaited: after a miss, handle the registered
        interruptions and try again while one was handled."""
        __tracebackhide__ = True
        try:
            return await attempt()
        except AttentionRequired as exc:
            missed = exc
        while await self._interrupt(key, budget, timeout, poll_ms):
            try:
                return await attempt()
            except AttentionRequired as exc:
                missed = exc
        raise missed

    def _sync_driver(self):
        """The SyncBridge the sync Healer code uses from a worker thread."""
        if self._bridge is None:
            bridge = getattr(self.coordinator, 'driver', None)
            if not isinstance(bridge, SyncBridge):
                bridge = SyncBridge(self._driver, asyncio.get_running_loop())
            self._bridge = bridge
        return self._bridge

    async def _interrupt(self, before, budget, timeout_ms, poll_ms):
        """Healer._interrupt(), run in a worker thread through SyncBridge so the sync and
        async healers make the same decisions. No thread when nothing is registered."""
        __tracebackhide__ = True
        core = self._core
        if not core._others(before):
            return False
        return await _in_thread(core._interrupt, before, budget, timeout_ms, poll_ms,
                                self._sync_driver())

    async def _interrupt_for_action(self, before, budget, timeout_ms, poll_ms):
        """_interrupt() for an action, under the lock. Actions of one healer may run at the
        same time (asyncio.gather), and two of them must not handle one interruption at
        once, for example both clicking the same banner: the second one waits, then finds
        it gone. locator() calls _interrupt() itself, because it already holds the lock."""
        __tracebackhide__ = True
        if not self._core._others(before):
            return False
        async with self._lock:
            return await self._interrupt(before, budget, timeout_ms, poll_ms)

    async def _before_action(self, key, timeout_ms, poll_ms):
        """Healer._before_action(), awaited. Returns the step's budget."""
        __tracebackhide__ = True
        budget = Budget()
        await self._interrupt_for_action(key, budget, timeout_ms, poll_ms)
        return budget

    @property
    def _dismiss_on(self):
        """Whether open(dismiss=True) was passed (see Healer._dismiss)."""
        return self._core._dismiss_on

    async def _dismiss_before(self, key, native, verb, budget, timeout_ms, poll_ms):
        """Healer._dismiss_before(), awaited."""
        __tracebackhide__ = True
        if not self._core._dismiss_on or verb in NOT_LOOKED_AT:
            return False
        return await self._dismiss_for_action(key, native, budget, timeout_ms, poll_ms)

    async def _unblock_for_action(self, key, native, budget, timeout_ms, poll_ms):
        """Healer._unblock(), awaited: a registered interruption first, then a popup."""
        __tracebackhide__ = True
        if await self._interrupt_for_action(key, budget, timeout_ms, poll_ms):
            return True
        return await self._dismiss_for_action(key, native, budget, timeout_ms, poll_ms)

    async def _dismiss_for_action(self, key, native, budget, timeout_ms, poll_ms):
        """Healer._dismiss() for an action. The look at what covers the element is awaited
        on the event loop, so an action whose element is not covered costs one round trip
        and no thread. A popup is dismissed by the sync Healer's code in a worker thread
        through SyncBridge, under the lock, as _interrupt_for_action() handles an
        interruption: two actions awaited together never click the same popup. That code
        looks again first, since another action may have dismissed it meanwhile. Nothing
        is looked at for the actions of a run= function (see Healer._dismiss)."""
        __tracebackhide__ = True
        core = self._core
        probe = _member(self._driver, 'popup_probe')
        if (not core._dismiss_on or native is None or budget.gave_up or not callable(probe)
                or core._in_run()):
            return False
        try:
            seen = await _awaited(self._driver.popup_probe(native, None, True))
        except Exception:
            return False
        if popup_of(parse_probe(seen)) is None:
            return False
        async with self._lock:
            return await _in_thread(core._dismiss, key, native, budget, timeout_ms, poll_ms,
                                    self._sync_driver())

    async def _watch(self, item):
        """Healer._watch() for an async Playwright Page: register interruption ``item``
        with page.add_locator_handler (Playwright 1.42 and later), keeping one handler per
        page and key as the sync Healer does. Does nothing for a Frame or a custom driver.

        It changes the page's handlers only under the page's registering lock: it awaits
        Playwright between reading the shared entries and writing them, and two calls for
        one key could otherwise both add a handler, one of which would stay on the page
        and never act."""
        page, shared, key = self._page, self._core._overlays, item.key
        overlays = shared.handlers
        if not isinstance(self._driver, AsyncPlaywrightDriver) or not callable(
                getattr(page, 'add_locator_handler', None)):
            return
        async with _registering(shared):
            shared.listen(page)
            selectors = await self._parsed(item)
            if selectors and _join_overlay(overlays, key, selectors, self):
                return
            old = overlays.pop(key, None)
            if old is not None:
                await self._remove_overlay(old[0])
            if not selectors:
                return
            token = object()

            async def handler(*_locator):
                healer = _overlay_owner(overlays, key, token)   # the page keeps no healer alive
                if healer is not None:
                    await healer._overlay(key)

            try:
                locator = _overlay_locator(page, selectors)
                overlays[key] = (selectors, (weakref.ref(self),), token)
                await page.add_locator_handler(locator, handler,
                                               **_overlay_options(page.add_locator_handler))
            except Exception:
                if overlays.get(key, (None, None, None))[2] is token:
                    overlays.pop(key, None)
                logger.debug('mendwright: could not register an overlay handler for %r', key,
                             exc_info=True)

    async def _unwatch(self, key):
        """Healer._unwatch(), awaited, under the page's registering lock."""
        shared = self._core._overlays
        async with _registering(shared):
            await self._remove_overlay(_leave_overlay(shared.handlers, key, self))

    async def _remove_overlay(self, selectors):
        """Healer._remove_overlay(), awaited."""
        remove = getattr(self._page, 'remove_locator_handler', None)
        if not callable(remove) or not _removable(self._core._overlays.handlers, selectors):
            return
        try:
            await remove(_overlay_locator(self._page, selectors))
        except Exception:
            pass    # left on the page, it does nothing: its token is gone

    async def _parsed(self, item):
        """Healer._parsed(), awaited: the selectors of item that the browser can parse."""
        from .drivers.playwright_sync import browser_rejected, playwright_selector
        core = self._core
        parsed, kept = core._overlays.parsed, []
        for selector in item.selectors:
            if classify(selector) != XPATH and selector not in parsed:
                try:
                    await self._page.locator(playwright_selector(selector)).count()
                except Exception as exc:
                    if browser_rejected(exc):
                        core.coordinator._emit(logging.WARNING, rejected_selector_message(
                            item.key, selector, exc))
                        continue
                else:
                    parsed.add(selector)
            kept.append(selector)
        return tuple(kept)

    async def _overlay(self, key):
        """What Playwright runs on the event loop when the page shows interruption key's
        element before an action (see Healer._watch). The sync Healer's code decides and
        acts, in a worker thread through SyncBridge, as _interrupt() does.

        It does nothing while this healer holds its lock: the step holding it may be the
        very action Playwright runs this for (the scroll at the end of a lookup), which
        would then wait for itself. That step handles interruptions itself, and an action
        mendwright did not route gets the handler again when Playwright retries it."""
        core = self._core
        if not core._overlay_wanted(key) or self._lock.locked():
            return
        async with self._lock:
            await _in_thread(core._overlay_run, key, self._sync_driver())

    async def _check_healed_target(self, key):
        """Healer._check_healed_target(): a match that came from a heal, on a key marked
        destructive, is quarantined, and AttentionRequired is raised before anything acts
        on it."""
        __tracebackhide__ = True  # pytest shows the caller
        core = self._core
        xpath = core._destructive_heal(key)
        if xpath is None:
            return
        coordinator = self.coordinator
        if coordinator.pending.get(key) == xpath:
            # A heal from earlier in this process. A gateway provider reports the outcome
            # over HTTP.
            await _in_thread(coordinator.confirm_pending, key, False)
        else:
            self.store.quarantine(key, xpath)
            coordinator.journal.quarantine(key, xpath=xpath)
        raise _healed_target_error(key, xpath, core._locked(key))

    async def _resolve(self, key, timeout_ms, poll_ms):
        """mendwright.resolver.resolve(), awaited: the candidates in the same order, bad
        selectors dropped (InvalidSelector at once when none can run), the first unique
        (visible) match returned and recorded, AttentionRequired at the deadline."""
        store, driver = self.store, self._driver
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

    async def _follow_into_frame(self, key, exc, attempt):
        """Healer._follow_into_frame(), with the frame lookups awaited and the live check
        and the store write in a worker thread (through the coordinator's SyncBridge)."""
        __tracebackhide__ = True
        core = self._core
        if not callable(_member(self._driver, 'frame_matches')):
            return _NOT_FOLLOWED
        opener = 'await mendwright.aio.open'
        matches = []
        for selector in core._frame_candidates(key):
            try:
                frames = await _awaited(self._driver.frame_matches(selector))
            except Exception:
                continue
            if frames:
                matches.append((selector, frames))
        if not matches:
            return _NOT_FOLLOWED
        selector, frames, frame, alt = core._pick_frame(key, exc, matches, opener)
        core._frame_guard(key, exc, await _url(self._driver))
        # In a worker thread: the guard and the live check reach the page through the bridge.
        why = await _in_thread(core._frame_follow, key, selector, frame, alt,
                               self.coordinator.driver)
        if why:
            core._frame_stop(key, exc, selector, frames, opener, why)
        return await attempt()

    async def _heal_scope(self, key):
        """Healer._heal_scope(), with the iframe's driver made on the event loop and wrapped
        in a SyncBridge for the heal core."""
        best = latest_success(self.store, key)
        frame = best.get('frameXPath') if best else None
        if not isinstance(frame, str) or not frame or not callable(_member(self._driver, 'framed')):
            return None
        try:
            framed = await _awaited(self._driver.framed(frame))
        except Exception:
            return None
        if framed is None:
            return None
        return frame, SyncBridge(framed, asyncio.get_running_loop())

    async def _heal(self, key, exc, attempt):
        """Run the sync heal (coordinator.resolve_with_heal) in a worker thread, then retry
        attempt() on the event loop. The coordinator reaches the page through SyncBridge."""
        __tracebackhide__ = True  # pytest shows the caller
        meta = self.store.meta(key)
        context = self._core._flow_context(key, await _url(self._driver))
        missed = [exc]

        def resolve_fn():
            # resolve_with_heal calls this once to see the miss and once more after a heal
            # was applied. That retry is awaited on the event loop below instead.
            if missed:
                raise missed.pop()
            return None

        scope = await self._heal_scope(key)
        if scope is None:
            await _in_thread(resolve_with_heal, resolve_fn, self.coordinator, key, meta, context)
        else:
            # The key was last found inside an iframe: capture, check and save the heal there.
            await _in_thread(_scoped_heal, scope, resolve_fn, self.coordinator, key, meta, context)
        return await attempt()

    async def _learn(self, key, roles):
        """Healer._learn(), with describe() awaited."""
        core = self._core
        todo = core._learn_todo(key, roles)
        if todo is None:
            return
        core._learn_from(key, roles, todo, await self._describe_winner(key))

    async def _remember_fingerprint(self, key):
        """Healer's fingerprint recording after a match, with the read awaited."""
        core = self._core
        target = core._fingerprint_target(key)
        if target is None:
            return
        fp = None
        if callable(_member(self._driver, 'fingerprint')):
            try:
                fp = await _awaited(self._driver.fingerprint(target, core._winner_target(key)[1]))
            except Exception:
                fp = None
        core._keep_fingerprint(key, target, fp if valid_fingerprint(fp) else None)

    async def _describe_winner(self, key):
        target = self._core._winner_target(key)
        describe = _member(self._driver, 'describe')
        if target is None or not callable(describe):
            return None
        try:
            out = await _awaited(self._driver.describe(*target))
        except Exception:
            return None
        return out if isinstance(out, dict) else None

    async def _confirm(self, key, success):
        """coordinator.confirm_pending(), in a worker thread (a gateway provider reports
        the outcome over HTTP) and under the lock."""
        async with self._turn():
            if key in self.coordinator.pending:
                await _in_thread(self.coordinator.confirm_pending, key, bool(success))

    # --- steps in any order (see mendwright.steps) ------------------------------------

    def step(self, name: str, when: Selector | None = None,
             fn: Callable[[Any], Any] | None = None, *, url: str | None = None,
             last: bool = False, optional: bool = False) -> Any:
        """Register a step for run_steps(), as Healer.step() does. The function gets
        heal.page and may be async (it is awaited) or plain, when it returns an awaitable,
        such as lambda page: page.click('#place'): heal.page's calls are coroutines, so a
        plain function that makes them without await would do nothing, and run_steps()
        raises TypeError for one that returns anything else. Sync: it only registers."""
        return _steps.add(self._core, name, when, fn, url=url, last=last, optional=optional,
                          awaits=True)

    async def _interrupt_waiting(self, budget, timeout_ms, poll_ms, failed):
        """mendwright.steps.interrupt() while run_steps() waits for a step: in a worker
        thread through SyncBridge and under the lock, as _interrupt_for_action() runs
        _interrupt() before an action. No thread when nothing is left to look for."""
        __tracebackhide__ = True
        core = self._core
        if all(item.key in failed for item in core._others(_steps.WAITING_STEP)):
            return False
        async with self._lock:
            return await _in_thread(_steps.interrupt, core, budget, timeout_ms, poll_ms, failed,
                                    self._sync_driver())

    async def _several_landmark(self, name, selector, count, looked, url):
        """Healer._several() for a landmark of step name, in a worker thread through
        SyncBridge, once per page: no thread once it was named for this page."""
        core = self._core
        if ('landmark', name, page_path(url)) in core._several_seen:
            return
        await _in_thread(core._several, 'landmark', name, selector, count, looked,
                         self._sync_driver(), url)

    async def run_steps(self, *, timeout_ms: int | None = None, poll_ms: int = 200) -> list:
        """Healer.run_steps(), with the page work awaited: the landmarks are looked at on
        the event loop, the interruptions are handled as before an action, and each step's
        function is awaited. The healer's lock is not held while a step runs, so its
        actions take it as usual."""
        __tracebackhide__ = True  # pytest shows the caller
        return await _steps.run_async(self, timeout_ms, poll_ms)

    def __repr__(self) -> str:
        return '<AsyncHealer page=' + repr(self._page) + '>'


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
    ``await heal.locator(...).inner_text()`` resolves first.

    Interruptions are handled before an action and after an interception error, as on
    ActionProxy, and so are popups with open(dismiss=True). A skipped step (.present is
    False once resolved) does nothing."""

    def __init__(self, healer: AsyncHealer, key: str, plan: tuple) -> None:
        self._healer = healer
        self._key = key
        self._plan = plan
        self._native = _UNRESOLVED
        self._present = True

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
        ``await expect(proxy.native).to_be_checked()``. Await the proxy first. None for a
        skipped step."""
        if self._native is _UNRESOLVED:
            raise RuntimeError(
                "mendwright: '" + self._key + "' has not been resolved yet, so there is no "
                "native Locator. Await the proxy first, for example "
                "box = await heal.locator('" + self._key + "', ...), then use box.native.")
        return self._native

    @property
    def present(self) -> bool:
        """False when the step was skipped because its element was not there (an
        optional step, or an interruption), else True. Await the proxy first."""
        if self._native is _UNRESOLVED:
            raise RuntimeError(
                "mendwright: '" + self._key + "' has not been resolved yet, so mendwright does "
                "not know whether it is on the page. Await the proxy first, for example "
                "box = await heal.locator('" + self._key + "', ..., optional=True), then read "
                "box.present.")
        return self._present

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
        if not self._present:
            return None   # a skipped step: nothing to act on
        args = tuple([await _unwrap(a) for a in args])
        kwargs = {k: await _unwrap(v) for k, v in kwargs.items()}
        healer = self._healer
        timeout, poll_ms = self._plan[4], self._plan[5]
        budget = await healer._before_action(self._key, timeout, poll_ms)
        await healer._dismiss_before(self._key, native, verb, budget, timeout, poll_ms)
        with healer._core._running(self._key, budget, timeout, poll_ms):
            try:
                result = await _act_on(healer._driver, native, verb, *args, **kwargs)
            except _USAGE_ERRORS:
                raise
            except Exception as exc:
                _reraise(budget)    # a run= function failed in the overlay handler meanwhile
                intercepted, used = is_interception_error(exc), budget.used
                if not (intercepted and await healer._unblock_for_action(
                        self._key, native, budget, timeout, poll_ms)):
                    if intercepted and budget.used == used and not budget.gave_up:
                        healer._core._report_nothing_matched(self._key)
                    if not is_stale_error(exc):
                        await healer._confirm(self._key, False)  # the browser action failed
                    raise
                try:
                    result = await _act_on(healer._driver, native, verb, *args, **kwargs)
                except _USAGE_ERRORS:
                    raise
                except Exception as again:
                    _reraise(budget)
                    if not is_stale_error(again):
                        await healer._confirm(self._key, False)
                    raise
            _reraise(budget)
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
            if not self.__dict__.get('_present', True) and not name.startswith('__'):
                raise AttributeError(absent_message(self._key))
            return getattr(native, name)
        if name.startswith('_'):
            raise AttributeError(name)

        async def deferred(*args, **kwargs):
            target = await self._ensure()
            if not self._present:
                raise AttributeError(absent_message(self._key))
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
        if self._native is not _UNRESOLVED and not self._present:
            return '<AsyncActionProxy key=' + repr(self._key) + ' skipped: not on the page>'
        native = 'unresolved' if self._native is _UNRESOLVED else repr(self._native)
        return '<AsyncActionProxy key=' + repr(self._key) + ' native=' + native + '>'


def _guarded(healer, key, native, timeout_ms=None):
    """An AsyncActionProxy on a Locator mendwright did not resolve, for heal.page's
    guard-only locators with open(dismiss=True) (see mendwright.proxies.GuardedLocator):
    its actions get the checks of AsyncActionProxy._act, and nothing is healed, confirmed
    or saved."""
    timeout = healer.timeout_ms if timeout_ms is None else timeout_ms
    proxy = AsyncActionProxy(healer, key, ((), None, None, None, timeout, 200, False, None))
    proxy._native = native
    return proxy
