"""Driver for the sync Playwright API. Also covers camoufox and patchright, which expose
the same methods (detected by those methods, never by module name).

Selectors can be any kind mendwright.selectors knows: XPath runs as 'xpath=...', CSS as
'css=...', and Playwright engine selectors ('text=...', "role=button[name='Save']",
chains with ' >> ') pass through unchanged. A get_by_* call written out, such as
'get_by_role("button", name="Go")' (see mendwright.getby), runs as that call on the page.
Frame chains take the same kinds, but for calls, and may end in ShadowHost steps (see
mendwright.shadow): the selector is then CSS, and counts only where it matches inside the
shadow root those hosts lead to. Playwright's CSS pierces every open shadow root, so each
read of such a selector first checks where its matches are (PLACE_JS), and finds nothing
unless every one of them is in that shadow root.

A malformed selector raises InvalidSelector straight away, whether lxml catches it before
the call or the browser rejects it, and so does a get_by_* call whose arguments the
browser rejects when it runs (level= on a button, say). Other errors on the read methods
(a detached frame, a navigation in progress) fall back to a safe default so the resolver
keeps polling.
"""

import re

from .. import getby
from ..fingerprint import FINGERPRINT_JS, from_raw
from ..flow import click_may_have_run
from ..getby import GetByCall
from ..lists import ITEMS_JS, LIST_JS, LIST_PLACE_JS
from ..lists import from_raw as list_from_raw
from ..selectors import CSS, GET_BY, XPATH, InvalidSelector, check_syntax, strip_prefix
from ..shadow import ShadowHost, host_css, inner_css, roots_of, split_chain, with_hosts
from .base import (DESCRIBE_JS, FRAME_ATTRS_JS, INVENTORY_JS, PLACE_JS, PLACED, POPUP_CONTROL_JS,
                   POPUP_CONTROLS_JS, POPUP_FOCUS_JS, POPUP_PROBE_JS, POPUP_ROOT_JS,
                   SHADOW_HTML_JS, SHADOW_INVENTORY_JS, SHADOW_ROOTS_JS, UNSURE,
                   VISIBLE_MATCH_JS, frame_selector, inventory_rows, is_async_api, item_rows,
                   looks_like_locator, placement_of, shadow_arg, summary_from_js,
                   visible_match_from_js)

# How Playwright words a selector it cannot parse. The CSS and engine messages come from
# Playwright's own parser and read the same in Chromium, Firefox and WebKit; the XPath
# ones differ per browser but lxml normally catches those first.
_BROWSER_SELECTOR_ERROR = re.compile(
    r'while parsing|Unknown engine|not a valid (?:XPath|selector)|not a legal expression'
    r'|did not match the expected pattern', re.IGNORECASE)

# How Playwright's role engine words a get_by_* call whose arguments it rejects when the
# call runs, not when it is built: level= or a state on a role that has none, for example
# '"level" attribute is only supported for roles: "heading", ...'.
_GET_BY_REJECTED = re.compile(
    r'is only supported for roles|Unknown attribute|does not support .* matcher'
    r'|must be a string or a regular expression|Role must not be empty')


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


def playwright_target(selector):
    """What a driver builds a Locator from for a mendwright selector: the GetByCall of a
    get_by_* call (see mendwright.getby), else the string playwright_selector() gives.

    Raises TypeError for a non-string and InvalidSelector for an empty selector or bad
    XPath."""
    if check_syntax(selector, calls=True) == GET_BY:
        return getby.parse(selector)
    return playwright_selector(selector)


def browser_rejected(exc):
    """True for a Playwright error that says the browser cannot parse a selector."""
    lines = str(exc).strip().splitlines()
    return bool(lines) and bool(_BROWSER_SELECTOR_ERROR.search(lines[0]))


def _raise_if_invalid(exc, selector, frame_chain, target=None):
    """Re-raise a Playwright 'cannot parse this selector' error as InvalidSelector, and,
    when target is a GetByCall, an error of the role engine about its arguments too (see
    _GET_BY_REJECTED)."""
    lines = str(exc).strip().splitlines()
    first = lines[0] if lines else ''
    detail = first.split(': ', 1)[1] if re.match(r'^\w+\.\w+: ', first) else first
    if first and isinstance(target, GetByCall) and _GET_BY_REJECTED.search(first):
        detail = detail[len('Error: '):] if detail.startswith('Error: ') else detail
        raise InvalidSelector.build(
            None if frame_chain else selector, GET_BY,
            'the browser rejected a get_by_* call (' + detail.rstrip('.') + ').',
            "Check the call's arguments against what Playwright's " + target.method
            + '() takes.') from exc
    if not first or not _BROWSER_SELECTOR_ERROR.search(first):
        return
    raise InvalidSelector.build(
        None if frame_chain else selector, None,
        'the browser rejected a selector (' + detail.rstrip('.') + ').',
        "Fix the selector, or use an XPath such as \"//button[@id='save']\", which "
        "mendwright.selectors.check_syntax() can check without a browser.") from exc


def chain_steps(selector, frame_chain):
    """What a driver builds a Locator from for a selector in a frame chain:
    (playwright_target(selector), steps), the steps being the frame selectors as
    playwright_selector() gives them, then a ShadowHost('css=...') for each shadow host.
    Raises InvalidSelector for a bad selector or chain, and for a selector inside a shadow
    root that is not CSS."""
    frames, hosts = split_chain(frame_chain)
    target = playwright_target(selector)
    if hosts:
        inner_css(selector)
    return target, (tuple(playwright_selector(f) for f in frames)
                    + tuple(ShadowHost('css=' + h) for h in hosts))


def step_hosts(steps):
    """The CSS of the ShadowHost steps of chain_steps()' steps, for PLACE_JS."""
    return [str(s)[len('css='):] for s in steps if isinstance(s, ShadowHost)]


def placed_count(placed):
    """count() from PLACE_JS's result: how many elements the Locator matches when one of
    them is in the shadow root, else 0. Two matches with one outside count 2, so the
    selector is not unique; one match outside counts 0."""
    if not isinstance(placed, dict):
        return 0
    n, inside = placed.get('n'), placed.get('inside')
    if not isinstance(n, int) or not isinstance(inside, int):
        return 0
    return n if inside > 0 else 0


def all_placed(placed):
    """True when PLACE_JS says the Locator matches something, and all of it is in the shadow
    root: only then does a read go on (see PlaywrightSyncDriver._probe)."""
    return (isinstance(placed, dict) and isinstance(placed.get('n'), int)
            and placed['n'] > 0 and placed.get('inside') == placed['n'])


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
    driver_* helpers in mendwright.drivers.base: act_on, describe, inventory, fingerprint,
    visible_match, role_match, for lists handles, list_fingerprint and list_items, and for
    open shadow roots shadow_html, shadow_inventory and shadow_matches."""

    # Playwright's CSS pierces every open shadow root (see base.css_pierces).
    pierces_shadow = True

    def __init__(self, page):
        self._page = page

    def _targets(self, selector, frame_chain):
        """Convert the selector and frame chain up front so a bad one raises here,
        outside any try that would swallow it (see chain_steps)."""
        return chain_steps(selector, frame_chain)

    def _context(self, frames):
        """The page, then a FrameLocator for each frame step and a Locator for each shadow
        host step: Playwright's CSS pierces into the host's shadow root from there."""
        ctx = self._page
        for frame in frames:
            ctx = ctx.locator(frame) if isinstance(frame, ShadowHost) else ctx.frame_locator(frame)
        return ctx

    def _build(self, target, frames):
        """The Locator for a playwright_target() result inside the frame chain: the call
        for a GetByCall (getby.build), else ctx.locator(target)."""
        ctx = self._context(frames)
        if isinstance(target, GetByCall):
            return getby.build(ctx, target)
        return ctx.locator(target)

    def _probe(self, selector, frame_chain, op, default):
        target, frames = self._targets(selector, frame_chain)
        hosts = step_hosts(frames)
        try:
            loc = self._build(target, frames)
            if hosts and not all_placed(loc.evaluate_all(PLACE_JS, hosts)):
                return default      # nothing, or something outside its shadow root
            return op(loc)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            return default

    def count(self, selector, frame_chain=()):
        target, frames = self._targets(selector, frame_chain)
        hosts = step_hosts(frames)
        if not hosts:
            return self._probe(selector, frame_chain, lambda loc: loc.count(), 0)
        try:
            return placed_count(self._build(target, frames).evaluate_all(PLACE_JS, hosts))
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            return 0

    def is_visible(self, selector, frame_chain=()):
        return self._probe(selector, frame_chain, lambda loc: bool(loc.is_visible()), False)

    def scroll_into_view(self, selector, frame_chain=()):
        self._probe(selector, frame_chain, lambda loc: loc.scroll_into_view_if_needed(), None)

    def handle(self, selector, frame_chain=()):
        """The native Playwright Locator, for the caller to act on."""
        return self._build(*self._targets(selector, frame_chain))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        target, frames = self._targets(selector, frame_chain)
        loc = self._build(target, frames)
        hosts = step_hosts(frames)
        try:
            if hosts and not all_placed(loc.evaluate_all(PLACE_JS, hosts)):
                raise LookupError('mendwright: ' + repr(selector) + ' matches nothing in its '
                                  'shadow root')
            return self.act_on(loc, verb, *args, **kwargs)
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            raise

    def act_on(self, native, verb, *args, **kwargs):
        """Run a Playwright verb on a handle() result: getattr(native, verb)(*args, **kwargs).
        Keyword options such as timeout= or force= pass straight through. A verb the
        Locator does not have raises AttributeError before anything reaches the browser."""
        return getattr(native, verb)(*args, **kwargs)

    def placement(self, native, hosts):
        """Where a handle() result for a selector inside the shadow root that hosts (CSS
        selectors, outermost first) lead to matches now (PLACE_JS, see base.placement_of):
        PLACED when it matches one element and that one inside the shadow root, NOWHERE
        when it matches nothing at all, ELSEWHERE when it matches something outside the
        shadow root or several elements. Playwright resolves a Locator again for each action
        and read, and its CSS pierces every open shadow root, so the facade checks this right
        before each one (see ActionProxy._wait_placed). One round trip that does not wait;
        None on any error."""
        try:
            found = native.evaluate_all(PLACE_JS, [host_css(h) for h in hosts])
        except Exception:
            return None
        return placement_of(found)

    def placed(self, native, hosts):
        """Whether placement() is PLACED: the Locator matches one element now, and that one
        inside the shadow root. False on any error."""
        return self.placement(native, hosts) == PLACED

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

    def visible_match(self, selector, frame_chain=()):
        """describe()'s result for the first visible element the selector matches, plus
        'visible': how many of its matches are visible, counted up to 2. None when none is.
        One round trip. A malformed selector raises InvalidSelector; any other error gives
        None."""
        return self._probe(selector, frame_chain, lambda loc: visible_match_from_js(
            loc.locator('visible=true').evaluate_all(VISIBLE_MATCH_JS)), None)

    def fingerprint(self, selector, frame_chain=(), *, hosts=False):
        """The fingerprint of the one element the selector matches (see
        mendwright.fingerprint), or None when it matches none or several; with hosts=True,
        with the shadow hosts around the element under shadowHosts too ([] on the page, see
        mendwright.shadow.with_hosts). One round trip (evaluate_all), which reads no input
        value. A malformed selector raises InvalidSelector; any other error gives None."""
        def read(loc):
            raw = loc.evaluate_all(FINGERPRINT_JS)
            return with_hosts(from_raw(raw), raw) if hosts else from_raw(raw)
        return self._probe(selector, frame_chain, read, None)

    def handles(self, selector, frame_chain=()):
        """A Locator for each element the selector matches now, as Locator.all() gives them
        (nth(0), nth(1), ...); inside a shadow root, none unless all of them are in it. A
        malformed selector raises InvalidSelector."""
        target, frames = self._targets(selector, frame_chain)
        loc = self._build(target, frames)
        hosts = step_hosts(frames)
        try:
            if hosts and not all_placed(loc.evaluate_all(PLACE_JS, hosts)):
                return []
            count = loc.count()
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain, target)
            raise
        return [loc.nth(i) for i in range(count)]

    def list_fingerprint(self, selector, frame_chain=(), *, place=False):
        """The list fingerprint of the elements the selector matches (see mendwright.lists),
        or None when it matches fewer than two; with place=True, with the list's label and
        where it sits (LIST_PLACE_JS). One round trip (evaluate_all), which reads no input
        value. A malformed selector raises InvalidSelector; any other error gives None."""
        script = LIST_PLACE_JS if place else LIST_JS
        return self._probe(selector, frame_chain,
                           lambda loc: list_from_raw(loc.evaluate_all(script)), None)

    def list_items(self, selector, frame_chain=()):
        """[{'tag', 'role', 'type', 'visible'}] for the first 200 elements the selector
        matches, in one round trip (evaluate_all) that does not wait. A malformed selector
        raises InvalidSelector, and any other error raises as it is: the live check of a
        list fails on it."""
        target, frames = self._targets(selector, frame_chain)
        hosts = step_hosts(frames)
        try:
            loc = self._build(target, frames)
            if hosts and not all_placed(loc.evaluate_all(PLACE_JS, hosts)):
                return []
            return item_rows(loc.evaluate_all(ITEMS_JS))
        except Exception as exc:
            _raise_if_invalid(exc, selector, frame_chain)
            raise

    def frame_matches(self, selector):
        """The frames inside this page (any depth) in which selector matches at least one
        element, as [{'name', 'url', 'count', 'nested', 'selector'}]: how many elements it
        matches there, whether the frame sits inside another iframe, and, for one that does
        not, a CSS selector for its iframe element in this page (see base.frame_selector),
        or None when nothing tells it apart. A malformed selector raises InvalidSelector;
        any other error skips that frame. A get_by_* call runs as that call in each frame."""
        target = playwright_target(selector)
        found = []
        for frame in child_frames(self._page):
            try:
                n = _frame_locator(frame, target).count()
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

    def role_match(self, selector, role, level=None, frame_chain=()):
        """Whether Playwright gives the one element selector matches the ARIA role ``role``
        (and heading level ``level``), by its own matcher: True when
        get_by_role(role, level=level).and_(the selector's Locator) matches one element,
        False when it does not, None on any error. One round trip. Like a get_by_role() call
        without include_hidden=True, it leaves out an element hidden from the accessibility
        tree."""
        try:
            target, frames = self._targets(selector, frame_chain)
            options = {} if level is None else {'level': level}
            built = self._build(target, frames)
            hosts = step_hosts(frames)
            if hosts and not all_placed(built.evaluate_all(PLACE_JS, hosts)):
                return False
            loc = self._context(frames).get_by_role(role, **options).and_(built)
            return loc.count() == 1
        except Exception:
            return None

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

    # --- open shadow roots (see mendwright.shadow) -----------------------------------------

    def shadow_html(self, hosts):
        """The HTML in the open shadow root that hosts (CSS selectors, outermost first) lead
        to in this page or frame, or None when they lead to none: a host missing, matching
        several elements or having a closed shadow root. One round trip."""
        try:
            out = self._page.evaluate(SHADOW_HTML_JS, [host_css(h) for h in hosts])
        except Exception:
            return None
        return out if isinstance(out, str) else None

    def shadow_inventory(self, hosts, limit=200):
        """inventory() of the open shadow root that hosts lead to, [] when they lead to none.
        One round trip."""
        try:
            raw = self._page.evaluate(SHADOW_INVENTORY_JS, {
                'hosts': [host_css(h) for h in hosts], 'limit': limit})
        except Exception:
            return []
        return inventory_rows(raw, limit)

    def shadow_matches(self, selector):
        """(roots, cut): the open shadow roots of this page or frame, at any depth, that a
        CSS selector matches something in (with how many elements), or, for an XPath, every
        one with its HTML, for mendwright to evaluate (see mendwright.shadow.roots_of). One
        round trip. ([], False) for an engine selector, a get_by_* call, or a read that
        failed. A malformed selector raises InvalidSelector."""
        arg = shadow_arg(selector)
        if arg is None:
            return [], False
        try:
            raw = self._page.evaluate(SHADOW_ROOTS_JS, arg)
        except Exception:
            return [], False
        return roots_of(raw)

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
        clicked, False when it was not, UNSURE when the click raised after it may have
        reached the page (it started a navigation that did not commit in time, say)."""
        handle = root.evaluate_handle(POPUP_CONTROL_JS, expect)
        element = handle.as_element()
        if element is None:
            handle.dispose()
            return False
        try:
            element.click(timeout=timeout_ms)
        except Exception as exc:
            return UNSURE if click_may_have_run(exc) else False
        finally:
            try:
                element.dispose()
            except Exception:
                pass             # the page navigated or closed
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


def _frame_locator(frame, target):
    """The Locator for a playwright_target() result in a Frame (see frame_matches)."""
    if isinstance(target, GetByCall):
        return getby.build(frame, target)
    return frame.locator(target)


def detect(obj) -> bool:
    """Playwright SYNC family (incl. camoufox/patchright): has .locator + .frame_locator,
    is not async, and is a Page or Frame rather than a Locator or FrameLocator. Anything
    rejected here gets a specific TypeError from wrap() (see base.unsupported_message)."""
    return (hasattr(obj, 'locator') and hasattr(obj, 'frame_locator')
            and not is_async_api(obj) and not looks_like_locator(obj))
