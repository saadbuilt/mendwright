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

from ..fingerprint import FINGERPRINT_JS, from_raw
from ..selectors import CSS, XPATH, InvalidSelector, check_syntax, strip_prefix
from .base import (DESCRIBE_JS, FRAME_ATTRS_JS, INVENTORY_JS, POPUP_CONTROL_JS,
                   POPUP_CONTROLS_JS, POPUP_FOCUS_JS, POPUP_PROBE_JS, POPUP_ROOT_JS,
                   frame_selector, inventory_rows, is_async_api, looks_like_locator,
                   summary_from_js)

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


def browser_rejected(exc):
    """True for a Playwright error that says the browser cannot parse a selector."""
    lines = str(exc).strip().splitlines()
    return bool(lines) and bool(_BROWSER_SELECTOR_ERROR.search(lines[0]))


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


def is_direct_child(frame, page):
    """True when frame sits directly in ``page`` (a Page, through its main frame, or a
    Frame), not inside another iframe."""
    try:
        parent = getattr(frame, 'parent_frame', None)
        if isinstance(getattr(page, 'frames', None), list):   # a Page
            return parent is not None and getattr(parent, 'parent_frame', None) is None
        return parent is page
    except Exception:
        return False


class PlaywrightSyncDriver:
    """Wraps a sync Playwright Page or Frame (or a camoufox / patchright one).

    Required Driver methods: count, is_visible, scroll_into_view, handle, act, url,
    outer_html. All take a selector of any kind plus an optional frame chain (frame
    selectors, outermost first). Optional extras, which callers reach through the
    driver_* helpers in mendwright.drivers.base: act_on, describe, inventory, fingerprint."""

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

    def fingerprint(self, selector, frame_chain=()):
        """The fingerprint of the one element the selector matches (see
        mendwright.fingerprint), or None when it matches none or several. One round trip
        (evaluate_all), which reads no input value. A malformed selector raises
        InvalidSelector; any other error gives None."""
        return self._probe(selector, frame_chain,
                           lambda loc: from_raw(loc.evaluate_all(FINGERPRINT_JS)), None)

    def frame_matches(self, selector):
        """The frames inside this page (any depth) in which selector matches at least one
        element, as [{'name', 'url', 'count', 'nested', 'selector'}]: how many elements it
        matches there, whether the frame sits inside another iframe, and, for one that does
        not, a CSS selector for its iframe element in this page (see base.frame_selector),
        or None when nothing tells it apart. A malformed selector raises InvalidSelector;
        any other error skips that frame."""
        target = playwright_selector(selector)
        found = []
        for frame in child_frames(self._page):
            try:
                n = frame.locator(target).count()
            except Exception:
                continue
            if n > 0:
                info = dict(frame_info(frame), count=n,
                            nested=not is_direct_child(frame, self._page), selector=None)
                if not info['nested']:
                    try:
                        attrs = frame.frame_element().evaluate(FRAME_ATTRS_JS)
                        info['selector'] = frame_selector(attrs, self.count)
                    except Exception:
                        pass
                found.append(info)
        return found

    def framed(self, frame_selector):
        """A driver for the document inside the one iframe that frame_selector matches in
        this page, or None when it matches none or several or has no document."""
        try:
            loc = self._build(*self._targets(frame_selector, ()))
            if loc.count() != 1:
                return None
            frame = loc.element_handle(timeout=2000).content_frame()
        except Exception:
            return None
        return PlaywrightSyncDriver(frame) if frame is not None else None

    def inventory(self, limit=200):
        """Up to ``limit`` interactive elements of the page (or frame) this driver wraps:
        [{'tag', 'role', 'id', 'name', 'text', 'aria_label'}, ...] in document order. One
        round trip (page.evaluate). Never includes input values. [] on any error."""
        try:
            raw = self._page.evaluate(INVENTORY_JS, limit)
        except Exception:
            return []
        return inventory_rows(raw, limit)

    # --- popups, for open(dismiss=True) (see mendwright.dismiss) ---------------------------

    def popup_probe(self, native, known=None, scroll=False):
        """What covers the element of a handle() result (POPUP_PROBE_JS), in one round trip
        that does not wait: null unless the Locator matches one element. known is a popup's
        root from popup_root(), to learn whether it is gone. scroll=True scrolls an element
        whose middle is outside the window into view first."""
        return native.evaluate_all(POPUP_PROBE_JS, {'known': known, 'scroll': scroll})

    def popup_root(self, native, expect):
        """An ElementHandle on the root of the popup over the element (POPUP_ROOT_JS), or
        None when it is no longer what mendwright.dismiss.expect_of() describes."""
        handle = native.evaluate_handle(POPUP_ROOT_JS, expect, timeout=2000)
        element = handle.as_element()
        if element is None:
            handle.dispose()
        return element

    def popup_controls(self, native, root):
        return root.evaluate(POPUP_CONTROLS_JS)

    def popup_click(self, native, root, expect, timeout_ms):
        """Click control number expect['index'] of the popup, a real click through
        Playwright's checks, when it is still the one mendwright chose. True when it was
        clicked."""
        handle = root.evaluate_handle(POPUP_CONTROL_JS, expect)
        element = handle.as_element()
        if element is None:
            handle.dispose()
            return False
        try:
            element.click(timeout=timeout_ms)
        finally:
            element.dispose()
        return True

    def press_escape(self, native, root):
        """Press Escape on the page's keyboard (a Frame's page for a Frame), when
        POPUP_FOCUS_JS says the key reaches the popup whose root is root: the keyboard sends
        it to the focused element, in whatever frame has the focus. True when it was
        pressed."""
        if root.evaluate(POPUP_FOCUS_JS) is not True:
            return False
        page = self._page
        keyboard = getattr(page, 'keyboard', None) or page.page.keyboard
        keyboard.press('Escape')
        return True

    def popup_release(self, root):
        root.dispose()


def detect(obj) -> bool:
    """Playwright SYNC family (incl. camoufox/patchright): has .locator + .frame_locator,
    is not async, and is a Page or Frame rather than a Locator or FrameLocator. Anything
    rejected here gets a specific TypeError from wrap() (see base.unsupported_message)."""
    return (hasattr(obj, 'locator') and hasattr(obj, 'frame_locator')
            and not is_async_api(obj) and not looks_like_locator(obj))
