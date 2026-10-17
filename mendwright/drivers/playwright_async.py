"""Driver for the async Playwright API (playwright.async_api, and the async forms of
patchright and camoufox, detected by duck typing). mendwright.aio uses it; wrap() and
mendwright.open() stay sync and reject async pages.

It mirrors PlaywrightSyncDriver with coroutines: selectors of any kind (XPath runs as
'xpath=...', CSS as 'css=...', engine selectors pass through, a get_by_* call runs as that
call), frame chains of frame selectors (outermost first) that may end in ShadowHost steps
(a CSS selector inside an open shadow root, checked with PLACE_JS as the sync driver does),
InvalidSelector straight away for
a malformed selector or a call whose arguments the browser rejects, and a safe default (0,
False, None) for any other error on the read methods so resolution keeps polling.
page.locator(), frame_locator() and the get_by_* methods are plain calls in the async API;
only the queries and actions are awaited.
"""

import inspect

from .. import getby
from ..fingerprint import FINGERPRINT_JS, from_raw
from ..flow import click_may_have_run
from ..getby import GetByCall
from ..lists import ITEMS_JS, LIST_JS, LIST_PLACE_JS
from ..lists import from_raw as list_from_raw
from ..shadow import ShadowHost, host_css, roots_of, with_hosts
from .base import (DESCRIBE_JS, FRAME_ATTRS_JS, INVENTORY_JS, PLACE_JS, PLACED, POPUP_CONTROL_JS,
                   POPUP_CONTROLS_JS, POPUP_FOCUS_JS, POPUP_PROBE_JS, POPUP_ROOT_JS,
                   SHADOW_HTML_JS, SHADOW_INVENTORY_JS, SHADOW_ROOTS_JS, UNSURE, _member,
                   VISIBLE_MATCH_JS, frame_selector, inventory_rows, is_async_api, item_rows,
                   looks_like_locator, placement_of, shadow_arg, summary_from_js,
                   visible_match_from_js)
from .playwright_sync import (_frame_locator, _raise_if_invalid, all_placed, chain_steps,
                              child_frames, frame_info, is_direct_child, placed_count,
                              playwright_target, step_hosts)


async def _awaited(value):
    """value, awaited when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


class AsyncPlaywrightDriver:
    """Wraps an async Playwright Page or Frame (or a patchright / camoufox one).

    Every method is a coroutine: count, is_visible, scroll_into_view, handle, act, act_on,
    url, outer_html, describe, fingerprint, visible_match, role_match, inventory, handles,
    list_fingerprint and list_items, with the same arguments and results as
    PlaywrightSyncDriver. handle() returns the native async Locator."""

    # Playwright's CSS pierces every open shadow root (see base.css_pierces).
    pierces_shadow = True

    def __init__(self, page):
        self._page = page

    def _targets(self, selector, frame_chain):
        """Convert the selector and frame chain up front so a bad one raises here,
        outside any try that would swallow it (see playwright_sync.chain_steps)."""
        return chain_steps(selector, frame_chain)

    def _context(self, frames):
        """The page, then a FrameLocator for each frame step and a Locator for each shadow
        host step (see PlaywrightSyncDriver._context)."""
        ctx = self._page
        for frame in frames:
            ctx = ctx.locator(frame) if isinstance(frame, ShadowHost) else ctx.frame_locator(frame)
        return ctx

    def _build(self, target, frames):
        """The Locator for a playwright_target() result (see PlaywrightSyncDriver._build).
        Building one is a plain call in the async API."""
        ctx = self._context(frames)
        if isinstance(target, GetByCall):
            return getby.build(ctx, target)
        return ctx.locator(target)

    async def _probe(self, selector, frame_chain, op, default):
        target, frames = self._targets(selector, frame_chain)
        hosts = step_hosts(frames)
        try:
            loc = self._build(target, frames)
            if hosts and not all_placed(await loc.evaluate_all(PLACE_JS, hosts)):
                return default      # nothing, or something outside its shadow root
            return await op(loc)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            return default

    async def count(self, selector, frame_chain=()):
        target, frames = self._targets(selector, frame_chain)
        hosts = step_hosts(frames)
        if hosts:
            try:
                return placed_count(await self._build(target, frames).evaluate_all(
                    PLACE_JS, hosts))
            except Exception as exc:
                _raise_if_invalid(exc, selector, frame_chain, target)
                return 0

        async def op(loc):
            return await loc.count()
        return await self._probe(selector, frame_chain, op, 0)

    async def is_visible(self, selector, frame_chain=()):
        async def op(loc):
            return bool(await loc.is_visible())
        return await self._probe(selector, frame_chain, op, False)

    async def scroll_into_view(self, selector, frame_chain=()):
        async def op(loc):
            await loc.scroll_into_view_if_needed()
        await self._probe(selector, frame_chain, op, None)

    async def handle(self, selector, frame_chain=()):
        """The native async Playwright Locator, for the caller to act on."""
        return self._build(*self._targets(selector, frame_chain))

    async def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        target, frames = self._targets(selector, frame_chain)
        loc = self._build(target, frames)
        hosts = step_hosts(frames)
        try:
            if hosts and not all_placed(await loc.evaluate_all(PLACE_JS, hosts)):
                raise LookupError('mendwright: ' + repr(selector) + ' matches nothing in its '
                                  'shadow root')
            return await self.act_on(loc, verb, *args, **kwargs)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            raise

    async def act_on(self, native, verb, *args, **kwargs):
        """Run a Playwright verb on a handle() result and await it:
        await getattr(native, verb)(*args, **kwargs). Keyword options such as timeout= or
        force= pass straight through. A verb the Locator does not have raises
        AttributeError, and arguments it does not take raise TypeError, before anything
        reaches the browser."""
        return await _awaited(getattr(native, verb)(*args, **kwargs))

    async def placement(self, native, hosts):
        """PlaywrightSyncDriver.placement(), awaited: where the Locator of a selector inside
        the shadow root that hosts lead to matches now, PLACED, NOWHERE or ELSEWHERE. None
        on any error."""
        try:
            found = await native.evaluate_all(PLACE_JS, [host_css(h) for h in hosts])
        except Exception:
            return None
        return placement_of(found)

    async def placed(self, native, hosts):
        """PlaywrightSyncDriver.placed(), awaited: whether placement() is PLACED: the Locator
        matches one element now, and that one inside the shadow root. False on any error."""
        return await self.placement(native, hosts) == PLACED

    async def url(self):
        try:
            return self._page.url
        except Exception:
            return ''

    async def outer_html(self, scope=None):
        try:
            if scope:
                loc = await self.handle(scope)
                return await loc.evaluate('e => e.outerHTML')
            return await self._page.content()
        except Exception:
            return ''

    async def describe(self, selector, frame_chain=()):
        """{'tag', 'role', 'text', 'outer_html'} for the one element the selector matches,
        or None when it matches none or several (see PlaywrightSyncDriver.describe)."""
        async def op(loc):
            return summary_from_js(await loc.evaluate_all(DESCRIBE_JS))
        return await self._probe(selector, frame_chain, op, None)

    async def visible_match(self, selector, frame_chain=()):
        """describe()'s result for the first visible element the selector matches, plus
        'visible', or None (see PlaywrightSyncDriver.visible_match)."""
        async def op(loc):
            return visible_match_from_js(
                await loc.locator('visible=true').evaluate_all(VISIBLE_MATCH_JS))
        return await self._probe(selector, frame_chain, op, None)

    async def fingerprint(self, selector, frame_chain=(), *, hosts=False):
        """The fingerprint of the one element the selector matches, or None, and with
        hosts=True its shadow hosts (see PlaywrightSyncDriver.fingerprint)."""
        async def op(loc):
            raw = await loc.evaluate_all(FINGERPRINT_JS)
            return with_hosts(from_raw(raw), raw) if hosts else from_raw(raw)
        return await self._probe(selector, frame_chain, op, None)

    async def handles(self, selector, frame_chain=()):
        """A Locator for each element the selector matches now (see
        PlaywrightSyncDriver.handles)."""
        target, frames = self._targets(selector, frame_chain)
        loc = self._build(target, frames)
        hosts = step_hosts(frames)
        try:
            if hosts and not all_placed(await loc.evaluate_all(PLACE_JS, hosts)):
                return []
            count = await loc.count()
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            raise
        return [loc.nth(i) for i in range(count)]

    async def list_fingerprint(self, selector, frame_chain=(), *, place=False):
        """The list fingerprint of the elements the selector matches, or None (see
        PlaywrightSyncDriver.list_fingerprint)."""
        script = LIST_PLACE_JS if place else LIST_JS

        async def op(loc):
            return list_from_raw(await loc.evaluate_all(script))
        return await self._probe(selector, frame_chain, op, None)

    async def list_items(self, selector, frame_chain=()):
        """[{'tag', 'role', 'type', 'visible'}] for the first 200 elements the selector
        matches; errors raise (see PlaywrightSyncDriver.list_items)."""
        target, frames = self._targets(selector, frame_chain)
        loc = self._build(target, frames)
        hosts = step_hosts(frames)
        try:
            if hosts and not all_placed(await loc.evaluate_all(PLACE_JS, hosts)):
                return []
            return item_rows(await loc.evaluate_all(ITEMS_JS))
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            raise

    async def frame_matches(self, selector):
        """The frames inside this page (any depth) in which selector matches at least one
        element, as [{'name', 'url', 'count', 'nested', 'selector'}] (see
        PlaywrightSyncDriver.frame_matches)."""
        target = playwright_target(selector)
        found = []
        for frame in child_frames(self._page):
            try:
                n = await _frame_locator(frame, target).count()
            except Exception:
                continue
            if n > 0:
                info = dict(frame_info(frame), count=n,
                            nested=not is_direct_child(frame, self._page), selector=None)
                if not info['nested']:
                    try:
                        handle = await frame.frame_element()
                        attrs = await handle.evaluate(FRAME_ATTRS_JS)
                        counts = {}
                        for option in _frame_options(attrs):
                            counts[option] = await self.count(option)
                        info['selector'] = frame_selector(attrs, counts.get)
                    except Exception:
                        pass
                found.append(info)
        return found

    async def role_match(self, selector, role, level=None, frame_chain=()):
        """Whether Playwright gives the one element selector matches the ARIA role ``role``
        (and level ``level``): True, False, or None on any error (see
        PlaywrightSyncDriver.role_match). One round trip."""
        try:
            target, frames = self._targets(selector, frame_chain)
            options = {} if level is None else {'level': level}
            built = self._build(target, frames)
            hosts = step_hosts(frames)
            if hosts and not all_placed(await built.evaluate_all(PLACE_JS, hosts)):
                return False
            loc = self._context(frames).get_by_role(role, **options).and_(built)
            return await loc.count() == 1
        except Exception:
            return None

    async def framed(self, frame_selector):
        """An AsyncPlaywrightDriver for the document inside the one iframe that
        frame_selector matches in this page, or None."""
        try:
            loc = self._build(*self._targets(frame_selector, ()))
            if await loc.count() != 1:
                return None
            handle = await loc.element_handle(timeout=2000)
            frame = await handle.content_frame()
        except Exception:
            return None
        return AsyncPlaywrightDriver(frame) if frame is not None else None

    async def inventory(self, limit=200):
        """Up to ``limit`` interactive elements of the page (or frame) this driver wraps,
        as PlaywrightSyncDriver.inventory() returns them. Never includes input values.
        [] on any error."""
        try:
            raw = await self._page.evaluate(INVENTORY_JS, limit)
        except Exception:
            return []
        return inventory_rows(raw, limit)

    # --- open shadow roots (see PlaywrightSyncDriver and mendwright.shadow) ----------------

    async def shadow_html(self, hosts):
        try:
            out = await self._page.evaluate(SHADOW_HTML_JS, [host_css(h) for h in hosts])
        except Exception:
            return None
        return out if isinstance(out, str) else None

    async def shadow_inventory(self, hosts, limit=200):
        try:
            raw = await self._page.evaluate(SHADOW_INVENTORY_JS, {
                'hosts': [host_css(h) for h in hosts], 'limit': limit})
        except Exception:
            return []
        return inventory_rows(raw, limit)

    async def shadow_matches(self, selector):
        arg = shadow_arg(selector)
        if arg is None:
            return [], False
        try:
            raw = await self._page.evaluate(SHADOW_ROOTS_JS, arg)
        except Exception:
            return [], False
        return roots_of(raw)

    # --- popups, for open(dismiss=True) (see PlaywrightSyncDriver) --------------------------

    async def popup_probe(self, native, known=None, scroll=False):
        return await native.evaluate_all(POPUP_PROBE_JS, {'known': known, 'scroll': scroll})

    async def popup_root(self, native, expect):
        handle = await native.evaluate_handle(POPUP_ROOT_JS, expect, timeout=2000)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
        return element

    async def popup_controls(self, native, root):
        return await root.evaluate(POPUP_CONTROLS_JS)

    async def popup_click(self, native, root, expect, timeout_ms):
        handle = await root.evaluate_handle(POPUP_CONTROL_JS, expect)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            return False
        try:
            await element.click(timeout=timeout_ms)
        except Exception as exc:
            return UNSURE if click_may_have_run(exc) else False   # see the sync driver
        finally:
            try:
                await element.dispose()
            except Exception:
                pass
        return True

    async def press_escape(self, native, root):
        if await root.evaluate(POPUP_FOCUS_JS) is not True:
            return False      # the key would not reach the popup (see the sync driver)
        page = self._page
        keyboard = getattr(page, 'keyboard', None) or page.page.keyboard
        await keyboard.press('Escape')
        return True

    async def popup_release(self, root):
        await root.dispose()


def _frame_options(attrs):
    """Every selector frame_selector() may try for these attributes, so their counts can
    be awaited first."""
    tried = []
    frame_selector(attrs, lambda s: tried.append(s) or 0)
    return tried


def detect(obj) -> bool:
    """Playwright ASYNC family: has .locator + .frame_locator, its queries are coroutines,
    and it is a Page or Frame rather than a Locator or FrameLocator."""
    return (hasattr(obj, 'locator') and hasattr(obj, 'frame_locator')
            and is_async_api(obj) and not looks_like_locator(obj))


def is_async_driver(obj) -> bool:
    """Already an async driver: count, is_visible and handle are coroutine functions.
    Read without running properties, like the other detectors."""
    return all(inspect.iscoroutinefunction(_member(obj, m))
               for m in ('count', 'is_visible', 'handle'))
