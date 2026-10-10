"""Driver for the async Playwright API (playwright.async_api, and the async forms of
patchright and camoufox, detected by duck typing). mendwright.aio uses it; wrap() and
mendwright.open() stay sync and reject async pages.

It mirrors PlaywrightSyncDriver with coroutines: selectors of any kind (XPath runs as
'xpath=...', CSS as 'css=...', engine selectors pass through), frame chains of frame
selectors (outermost first), InvalidSelector straight away for a malformed selector, and a
safe default (0, False, None) for any other error on the read methods so resolution keeps
polling. page.locator() and frame_locator() are plain calls in the async API; only the
queries and actions are awaited.
"""

import inspect

from .base import (DESCRIBE_JS, INVENTORY_JS, _member, inventory_rows, is_async_api,
                   looks_like_locator, summary_from_js)
from .playwright_sync import _raise_if_invalid, child_frames, frame_info, playwright_selector


async def _awaited(value):
    """value, awaited when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


class AsyncPlaywrightDriver:
    """Wraps an async Playwright Page or Frame (or a patchright / camoufox one).

    Every method is a coroutine: count, is_visible, scroll_into_view, handle, act, act_on,
    url, outer_html, describe and inventory, with the same arguments and results as
    PlaywrightSyncDriver. handle() returns the native async Locator."""

    def __init__(self, page):
        self._page = page

    def _targets(self, selector, frame_chain):
        """Convert the selector and frame chain up front so a bad one raises here,
        outside any try that would swallow it."""
        return playwright_selector(selector), tuple(playwright_selector(f) for f in frame_chain)

    def _build(self, target, frames):
        ctx = self._page
        for frame in frames:
            ctx = ctx.frame_locator(frame)
        return ctx.locator(target)

    async def _probe(self, selector, frame_chain, op, default):
        target, frames = self._targets(selector, frame_chain)
        try:
            return await op(self._build(target, frames))
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            return default

    async def count(self, selector, frame_chain=()):
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
        loc = await self.handle(selector, frame_chain)
        try:
            return await self.act_on(loc, verb, *args, **kwargs)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            raise

    async def act_on(self, native, verb, *args, **kwargs):
        """Run a Playwright verb on a handle() result and await it:
        await getattr(native, verb)(*args, **kwargs). Keyword options such as timeout= or
        force= pass straight through. A verb the Locator does not have raises
        AttributeError, and arguments it does not take raise TypeError, before anything
        reaches the browser."""
        return await _awaited(getattr(native, verb)(*args, **kwargs))

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

    async def frame_matches(self, selector):
        """The frames inside this page (any depth) in which selector matches at least one
        element, as [{'name', 'url'}] (see PlaywrightSyncDriver.frame_matches)."""
        target = playwright_selector(selector)
        found = []
        for frame in child_frames(self._page):
            try:
                if await frame.locator(target).count() > 0:
                    found.append(frame_info(frame))
            except Exception:
                continue
        return found

    async def inventory(self, limit=200):
        """Up to ``limit`` interactive elements of the page (or frame) this driver wraps,
        as PlaywrightSyncDriver.inventory() returns them. Never includes input values.
        [] on any error."""
        try:
            raw = await self._page.evaluate(INVENTORY_JS, limit)
        except Exception:
            return []
        return inventory_rows(raw, limit)


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
