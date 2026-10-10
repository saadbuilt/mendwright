"""Driver for the sync Playwright API. Also covers camoufox and patchright, which expose
the same methods (detected by those methods, never by module name).

Selectors can be any kind mendwright.selectors knows: XPath runs as 'xpath=...', CSS as
'css=...', and Playwright engine selectors ('text=...', "role=button[name='Save']",
chains with ' >> ') pass through unchanged. Frame chains take the same kinds.

A malformed selector raises InvalidSelector straight away, whether lxml catches it before
the call or the browser rejects it. Other errors on the read methods (a detached frame,
a navigation in progress) fall back to a safe default so the resolver keeps polling.
"""

import re

from ..selectors import CSS, XPATH, InvalidSelector, check_syntax, strip_prefix
from .base import (DESCRIBE_JS, INVENTORY_JS, inventory_rows, is_async_api,
                   looks_like_locator, summary_from_js)

# How Playwright words a selector it cannot parse. The CSS and engine messages come from
# Playwright's own parser and read the same in Chromium, Firefox and WebKit; the XPath
# ones differ per browser but lxml normally catches those first.
_BROWSER_SELECTOR_ERROR = re.compile(
    r'while parsing|Unknown engine|not a valid (?:XPath|selector)|not a legal expression'
    r'|did not match the expected pattern', re.IGNORECASE)


def playwright_selector(selector):
    """The string to hand to page.locator() / frame_locator() for a mendwright selector.

    Raises TypeError for a non-string and InvalidSelector for an empty selector or bad
    XPath (see mendwright.selectors.check_syntax)."""
    kind = check_syntax(selector)
    if kind == XPATH:
        return 'xpath=' + strip_prefix(selector)
    if kind == CSS:
        return 'css=' + strip_prefix(selector)
    return selector.strip()


def _raise_if_invalid(exc, selector, frame_chain):
    """Re-raise a Playwright 'cannot parse this selector' error as InvalidSelector."""
    lines = str(exc).strip().splitlines()
    first = lines[0] if lines else ''
    if not first or not _BROWSER_SELECTOR_ERROR.search(first):
        return
    detail = first.split(': ', 1)[1] if re.match(r'^\w+\.\w+: ', first) else first
    raise InvalidSelector.build(
        None if frame_chain else selector, None,
        'the browser rejected a selector (' + detail.rstrip('.') + ').',
        "Fix the selector, or use an XPath such as \"//button[@id='save']\", which "
        "mendwright.selectors.check_syntax() can check without a browser.") from exc


def child_frames(page):
    """The frames inside a Playwright Page or Frame, at any depth, without the page's own
    main frame. [] for anything else."""
    try:
        frames = getattr(page, 'frames', None)
        if isinstance(frames, list):   # a Page: every frame; its main frame has no parent
            return [f for f in frames if getattr(f, 'parent_frame', None) is not None]
        out, todo = [], list(getattr(page, 'child_frames', None) or [])
        while todo:                    # a Frame: walk down its children
            frame = todo.pop(0)
            out.append(frame)
            todo.extend(getattr(frame, 'child_frames', None) or [])
        return out
    except Exception:
        return []


def frame_info(frame):
    """{'name', 'url'} of a Playwright Frame, '' when it cannot be read."""
    out = {}
    for field in ('name', 'url'):
        try:
            value = getattr(frame, field)
        except Exception:
            value = ''
        out[field] = value if isinstance(value, str) else ''
    return out


class PlaywrightSyncDriver:
    """Wraps a sync Playwright Page or Frame (or a camoufox / patchright one).

    Required Driver methods: count, is_visible, scroll_into_view, handle, act, url,
    outer_html. All take a selector of any kind plus an optional frame chain (frame
    selectors, outermost first). Optional extras, which callers reach through the
    driver_* helpers in mendwright.drivers.base: act_on, describe, inventory."""

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

    def _probe(self, selector, frame_chain, op, default):
        target, frames = self._targets(selector, frame_chain)
        try:
            return op(self._build(target, frames))
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            return default

    def count(self, selector, frame_chain=()):
        return self._probe(selector, frame_chain, lambda loc: loc.count(), 0)

    def is_visible(self, selector, frame_chain=()):
        return self._probe(selector, frame_chain, lambda loc: bool(loc.is_visible()), False)

    def scroll_into_view(self, selector, frame_chain=()):
        self._probe(selector, frame_chain, lambda loc: loc.scroll_into_view_if_needed(), None)

    def handle(self, selector, frame_chain=()):
        """The native Playwright Locator, for the caller to act on."""
        return self._build(*self._targets(selector, frame_chain))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        loc = self.handle(selector, frame_chain)
        try:
            return self.act_on(loc, verb, *args, **kwargs)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            raise

    def act_on(self, native, verb, *args, **kwargs):
        """Run a Playwright verb on a handle() result: getattr(native, verb)(*args, **kwargs).
        Keyword options such as timeout= or force= pass straight through. A verb the
        Locator does not have raises AttributeError before anything reaches the browser."""
        return getattr(native, verb)(*args, **kwargs)

    def url(self):
        try:
            return self._page.url
        except Exception:
            return ''

    def outer_html(self, scope=None):
        try:
            if scope:
                return self.handle(scope).evaluate('e => e.outerHTML')
            return self._page.content()
        except Exception:
            return ''

    def describe(self, selector, frame_chain=()):
        """{'tag', 'role', 'text', 'outer_html'} for the one element the selector matches,
        or None when it matches none or several. One round trip (evaluate_all). role and
        text are computed the way the gate computes them (see base.element_summary).
        A malformed selector raises InvalidSelector; any other error gives None."""
        return self._probe(selector, frame_chain,
                           lambda loc: summary_from_js(loc.evaluate_all(DESCRIBE_JS)), None)

    def frame_matches(self, selector):
        """The frames inside this page (any depth) in which selector matches at least one
        element, as [{'name', 'url'}]. mendwright looks only in the page or frame it was
        opened on, so a selector that misses there may still match inside an iframe.
        A malformed selector raises InvalidSelector; any other error skips that frame."""
        target = playwright_selector(selector)
        found = []
        for frame in child_frames(self._page):
            try:
                if frame.locator(target).count() > 0:
                    found.append(frame_info(frame))
            except Exception:
                continue
        return found

    def inventory(self, limit=200):
        """Up to ``limit`` interactive elements of the page (or frame) this driver wraps:
        [{'tag', 'role', 'id', 'name', 'text', 'aria_label'}, ...] in document order. One
        round trip (page.evaluate). Never includes input values. [] on any error."""
        try:
            raw = self._page.evaluate(INVENTORY_JS, limit)
        except Exception:
            return []
        return inventory_rows(raw, limit)


def detect(obj) -> bool:
    """Playwright SYNC family (incl. camoufox/patchright): has .locator + .frame_locator,
    is not async, and is a Page or Frame rather than a Locator or FrameLocator. Anything
    rejected here gets a specific TypeError from wrap() (see base.unsupported_message)."""
    return (hasattr(obj, 'locator') and hasattr(obj, 'frame_locator')
            and not is_async_api(obj) and not looks_like_locator(obj))
