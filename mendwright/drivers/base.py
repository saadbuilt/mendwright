"""The browser extension point. Drivers are the only code in mendwright that knows a
browser library.

Resolution (resolver.py) and the facade talk to a Driver, never to a concrete browser
library. A Driver wraps one live page/session and answers a handful of selector queries,
each against an optional frame chain (frame selectors, outermost first). Selectors may be
XPath, CSS or Playwright engine selectors (see mendwright.selectors); a driver that
cannot run a kind raises mendwright.InvalidSelector. `handle()` returns the native
locator or element, so callers keep acting on it (loc.click(), loc.fill(), ...) as before.

`outer_html(scope)` must capture the same scope that resolution runs against. Otherwise a
selector can be unique in the snapshot and not on the live page.

Required: count, is_visible and handle (what is_driver checks); scroll_into_view, act,
url and outer_html are expected as well. Optional, and absent from older custom drivers:

  act_on(native, verb, *args, **kwargs)   run a Playwright-style verb on a handle() result
  describe(selector, frame_chain=())      {'tag', 'role', 'text', 'outer_html'} or None
  inventory(limit=200)                    [{'tag', 'role', 'id', 'name', 'text', 'aria_label'}]
  fingerprint(selector, frame_chain=())   the matched element's fingerprint, or None
  frame_matches(selector)                 the child frames the selector matches in
  framed(frame_selector)                  a driver for the document inside one iframe

For lists (see mendwright.lists):

  handles(selector, frame_chain=())       a native handle for each element it matches
  list_fingerprint(selector, frame_chain=())   the list fingerprint of what it matches
                                          (LIST_JS), or None for fewer than two elements
  list_items(selector, frame_chain=())    [{'tag', 'role', 'visible'}] for what it matches
                                          (ITEMS_JS), at most 200

For open(dismiss=True) (see mendwright.dismiss), each on a handle() result and run where it
lives (inside its frame chain on Selenium):

  popup_probe(native, known=None, scroll=False)   what covers the element (POPUP_PROBE_JS)
  popup_root(native, expect)              a handle on the popup's root (POPUP_ROOT_JS)
  popup_controls(native, root)            the buttons in it (POPUP_CONTROLS_JS)
  popup_click(native, root, expect, timeout_ms)   click one of them (POPUP_CONTROL_JS):
                                          True when it was clicked, False when it was
                                          not (it changed, or the click failed before it
                                          reached the page), UNSURE when the click raised
                                          after it may have reached the page
  press_escape(native, root)              press Escape, only when POPUP_FOCUS_JS says the
                                          keyboard focus is on the body or in the popup
                                          of a top-level page: the key goes to the
                                          focused element, whatever document it is in
  popup_release(root)                     let the root's handle go

Callers should go through driver_act_on, driver_describe, driver_inventory and
driver_fingerprint below, which fall back to getattr(native, verb)(...), None, [] and None
when a driver lacks the method, through driver_handles, driver_list_fingerprint and
driver_list_items, which fall back to [handle()], None and None (a driver without
list_fingerprint() records no list, so none is ever healed), and through the
driver_popup_* helpers, which fall back to None or False: a driver without them never
dismisses anything.
"""

import inspect
import re
from typing import Optional, Protocol, runtime_checkable

import lxml.html

from ..validation import _norm, _role_of

FrameChain = tuple  # tuple[str, ...] of frame selectors, outermost first


@runtime_checkable
class Driver(Protocol):
    """The methods a driver is expected to have. act_on and describe are optional extras
    (see the module docstring) and are left out on purpose, so isinstance(x, Driver) stays
    true for custom drivers written before they existed. inventory was listed here from
    the start; callers still treat it as optional through driver_inventory."""

    def count(self, selector: str, frame_chain: FrameChain = ()) -> int: ...
    def is_visible(self, selector: str, frame_chain: FrameChain = ()) -> bool: ...
    def scroll_into_view(self, selector: str, frame_chain: FrameChain = ()) -> None: ...
    def handle(self, selector: str, frame_chain: FrameChain = ()):  # native locator/element
        ...
    def act(self, selector: str, verb: str, *args, frame_chain: FrameChain = ()) -> None: ...
    def url(self) -> str: ...
    def outer_html(self, scope: Optional[str] = None) -> str: ...
    def inventory(self, limit: int = 200) -> list: ...


def is_driver(obj) -> bool:
    """Already a Driver? (duck-typed on the resolution-critical methods)."""
    return all(callable(getattr(obj, m, None)) for m in ('count', 'is_visible', 'handle'))


# --- optional methods, with a fallback for drivers that lack them -----------------------

def driver_act_on(driver, native, verb, *args, **kwargs):
    """Run ``verb`` on ``native`` (a handle() result) through driver.act_on when the driver
    has it, else as getattr(native, verb)(*args, **kwargs). Errors propagate: a wrong verb
    is an AttributeError (raised before any browser call by the built-in drivers), a bad
    argument a TypeError or ValueError, and anything else came from the action itself."""
    act_on = getattr(driver, 'act_on', None)
    if callable(act_on):
        return act_on(native, verb, *args, **kwargs)
    return getattr(native, verb)(*args, **kwargs)


def driver_describe(driver, selector, frame_chain=()):
    """driver.describe(selector, frame_chain) when the driver has it, else None.
    InvalidSelector propagates; any other error gives None."""
    from ..selectors import InvalidSelector
    describe = getattr(driver, 'describe', None)
    if not callable(describe):
        return None
    try:
        out = describe(selector, frame_chain)
    except InvalidSelector:
        raise
    except Exception:
        return None
    return out if isinstance(out, dict) else None


def driver_fingerprint(driver, selector, frame_chain=()):
    """driver.fingerprint(selector, frame_chain) when the driver has it, else None: the
    fingerprint (see mendwright.fingerprint) of the one element the selector matches. Any
    error, or a result that is not a fingerprint, gives None."""
    from ..fingerprint import valid
    method = getattr(driver, 'fingerprint', None)
    if not callable(method):
        return None
    try:
        out = method(selector, frame_chain)
    except Exception:
        return None
    return out if valid(out) else None


def driver_handles(driver, selector, frame_chain=()):
    """driver.handles(selector, frame_chain) when the driver has it, else [handle()]: a
    native handle for each element the selector matches. Errors propagate."""
    method = getattr(driver, 'handles', None)
    if callable(method):
        return list(method(selector, frame_chain))
    return [driver.handle(selector, frame_chain)]


def driver_list_fingerprint(driver, selector, frame_chain=()):
    """driver.list_fingerprint(selector, frame_chain) when the driver has it, else None: the
    list fingerprint (see mendwright.lists) of the elements the selector matches. Any error,
    or a result that is not one, gives None."""
    from ..lists import valid
    method = getattr(driver, 'list_fingerprint', None)
    if not callable(method):
        return None
    try:
        out = method(selector, frame_chain)
    except Exception:
        return None
    return out if valid(out) else None


def driver_list_items(driver, selector, frame_chain=()):
    """driver.list_items(selector, frame_chain) when the driver has it: [{'tag', 'role',
    'visible'}] for the elements the selector matches. None when the driver lacks it or the
    read failed, or for a result that is not a list. InvalidSelector propagates."""
    from ..selectors import InvalidSelector
    method = getattr(driver, 'list_items', None)
    if not callable(method):
        return None
    try:
        out = method(selector, frame_chain)
    except InvalidSelector:
        raise
    except Exception:
        return None
    return out if isinstance(out, list) else None


def item_rows(raw):
    """ITEMS_JS's result as list_items() rows: {'tag', 'role', 'visible'}, the role computed
    with role_for(). Anything malformed is skipped; a non-list gives None."""
    if not isinstance(raw, list):
        return None
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tag = str(item.get('tag') or '').lower()
        rows.append({'tag': tag, 'role': role_for(tag, item.get('role'), item.get('type')),
                     'visible': item.get('visible') is True})
    return rows


def driver_framed(driver, frame_selector):
    """driver.framed(frame_selector) when the driver has it, else None: a driver for the
    document inside the one iframe that frame_selector matches. None on any error."""
    method = getattr(driver, 'framed', None)
    if not callable(method):
        return None
    try:
        return method(frame_selector)
    except Exception:
        return None


# The attributes of an <iframe> or <frame> element that frame_selector() builds from.
FRAME_ATTRS_JS = ('e => ({tag: e.tagName.toLowerCase(), id: e.id || "", '
                  'name: e.getAttribute("name") || "", title: e.getAttribute("title") || "", '
                  'src: e.getAttribute("src") || ""})')
_FRAME_SRC_SKIP = ('about:', 'data:', 'javascript:', 'blob:')


def frame_selector(info, count):
    """A CSS selector that matches one iframe element and nothing else, built from its
    attributes (``info``, as FRAME_ATTRS_JS reads them), or None. Tried in order: its id,
    name, title, then its src up to the query or fragment (matched whole, so /embed never
    matches /embed-ads, and the query, which can hold a token, is never saved);
    ``count(selector)`` must be 1. A generated-looking id or name is skipped. The bare tag
    is never used: a later page could have one other iframe, and a heal would run there."""
    from ..candidates import generated
    from ..css import css_string
    if not isinstance(info, dict):
        return None
    tag = info.get('tag') if info.get('tag') in ('iframe', 'frame') else 'iframe'
    options = []
    for name in ('id', 'name', 'title'):
        value = info.get(name)
        if isinstance(value, str) and value.strip() and len(value) <= 200 and not (
                name in ('id', 'name') and generated(value)):
            options.append(tag + '[' + name + '=' + css_string(value) + ']')
    src = info.get('src')
    if isinstance(src, str) and src.strip() and not src.startswith(_FRAME_SRC_SKIP):
        cut = re.search(r'[?#]', src)
        base = src[:cut.start()] if cut else src
        if base and len(base) <= 300:
            if cut:   # the src as far as its query or fragment, and that character
                options.append(tag + '[src^=' + css_string(base + cut.group(0)) + ']')
            else:
                options.append(tag + '[src=' + css_string(base) + ']')
    for option in options:
        try:
            if count(option) == 1:
                return option
        except Exception:
            continue
    return None


def _optional(driver, name, default, *args):
    """driver.<name>(*args) when the driver has it, else default. Also default on any
    error: a popup that cannot be read or reached is left alone."""
    method = getattr(driver, name, None)
    if not callable(method):
        return default
    try:
        return method(*args)
    except Exception:
        return default


def driver_popup_probe(driver, native, known=None, scroll=False):
    """driver.popup_probe(native, known, scroll): POPUP_PROBE_JS's result for the element,
    or None when the driver lacks it or the read failed. scroll=True scrolls an element
    whose middle is outside the window into view first, as the action would."""
    return _optional(driver, 'popup_probe', None, native, known, scroll)


def driver_popup_root(driver, native, expect):
    """driver.popup_root(native, expect): a handle on the popup's root, or None."""
    return _optional(driver, 'popup_root', None, native, expect)


def driver_popup_controls(driver, native, root):
    """driver.popup_controls(native, root): the controls POPUP_CONTROLS_JS lists, or []."""
    out = _optional(driver, 'popup_controls', None, native, root)
    return out if isinstance(out, list) else []


# What popup_click() returns for a click that raised an error after it may have reached
# the page (mendwright.flow.click_may_have_run): mendwright records it and stops.
UNSURE = 'unsure'


def driver_popup_click(driver, native, root, expect, timeout_ms):
    """driver.popup_click(...): True when the control was found as expected and clicked,
    UNSURE when the click raised an error after it may have reached the page, else False
    (also when the driver lacks it or raised)."""
    out = _optional(driver, 'popup_click', False, native, root, expect, timeout_ms)
    return out if out is True or out == UNSURE else False


def driver_press_escape(driver, native, root):
    """driver.press_escape(native, root): True when Escape was pressed. False when the
    driver lacks it, or when the keyboard focus is not on the page's body or in the popup
    whose root is root, or the element is in an iframe (POPUP_FOCUS_JS): Escape goes to the
    focused element, and could clear a field the script filled or close a dialog it uses."""
    return _optional(driver, 'press_escape', False, native, root) is True


def driver_popup_release(driver, root):
    """driver.popup_release(root), when the driver has it. Never raises."""
    _optional(driver, 'popup_release', None, root)


def driver_inventory(driver, limit=200):
    """driver.inventory(limit) when the driver has it, else []. Also [] on any error or a
    result that is not a list. Accepts an older inventory() that takes no argument."""
    inventory = getattr(driver, 'inventory', None)
    if not callable(inventory):
        return []
    try:
        try:
            out = inventory(limit)
        except TypeError:
            out = inventory()
    except Exception:
        return []
    return out if isinstance(out, list) else []


# --- detection helpers shared by the built-in detectors and wrap() ----------------------

# Methods whose async form gives an async API away: Playwright's async Page/Frame/Locator,
# and async WebDriver-like libraries (selenium_driverless and similar).
_ASYNC_PROBES = ('content', 'goto', 'wait_for_timeout', 'query_selector', 'count',
                 'is_visible', 'click', 'evaluate', 'find_elements', 'find_element',
                 'execute_script', 'get')


def _member(obj, name):
    """obj's attribute ``name`` without running a property (a Selenium property such as
    ``title`` makes a browser round trip). Falls back to a normal lookup for objects that
    forward attributes through __getattr__."""
    try:
        attr = inspect.getattr_static(obj, name)
    except AttributeError:
        try:
            return getattr(obj, name, None)
        except Exception:
            return None
    if isinstance(attr, (staticmethod, classmethod)):
        return attr.__func__
    if isinstance(attr, property):
        return None
    return attr


def is_async_api(obj) -> bool:
    """True if obj comes from an async browser API (its query methods are coroutines)."""
    for name in _ASYNC_PROBES:
        attr = _member(obj, name)
        if attr is not None and inspect.iscoroutinefunction(attr):
            return True
    return False


def looks_like_locator(obj) -> bool:
    """True for a Playwright Locator or FrameLocator: chainable (locator + frame_locator)
    with nth(), but no document of its own (no content()). A Page or Frame has content()
    and no nth(). Such an object can resolve elements, but a heal can never pass because
    there is no page HTML to capture."""
    return (callable(_member(obj, 'locator')) and callable(_member(obj, 'frame_locator'))
            and callable(_member(obj, 'nth')) and not callable(_member(obj, 'content')))


def _looks_like_playwright(obj, module):
    """An async object from the Playwright family (Page, Frame, Locator, FrameLocator, also
    from patchright or camoufox) rather than an async WebDriver-like driver."""
    return '.async_api' in module or (callable(_member(obj, 'locator'))
                                      and callable(_member(obj, 'frame_locator')))


def unsupported_message(obj) -> str:
    """The TypeError text wrap() uses when no registered driver accepts obj."""
    name = type(obj).__name__
    module = type(obj).__module__ or ''
    # The module name is only a hint for the message (an async FrameLocator has no
    # coroutine methods to give it away); detection itself never looks at it.
    if is_async_api(obj) or '.async_api' in module:
        if not _looks_like_playwright(obj, module):
            return ('mendwright: ' + name + ' (from ' + module + ') is an async driver. '
                    'mendwright supports the sync Playwright API and sync Selenium, plus async '
                    'Playwright through mendwright.aio. Async Selenium-style drivers are not '
                    'supported. Use a selenium.webdriver driver with mendwright.open(), or '
                    'switch to Playwright: playwright.sync_api with mendwright.open(), or '
                    'playwright.async_api with mendwright.aio.open().')
        if looks_like_locator(obj):
            what = ('an async Playwright ' + name + ', which points at elements, not at a '
                    'page')
            step = ('pass the page instead: heal = await mendwright.aio.open(page), with '
                    'locator.page for a Locator, or a Frame from page.frames to work inside '
                    'an iframe')
        else:
            what = 'an async Playwright object'
            step = 'use the async entry point: heal = await mendwright.aio.open(page)'
        return ('mendwright: ' + name + ' (from ' + module + ') is ' + what + '. '
                'mendwright.open() takes the sync Playwright API and sync Selenium only. '
                'For async Playwright, ' + step + '. To stay sync, open the browser with '
                'playwright.sync_api.sync_playwright() instead.')
    if looks_like_locator(obj):
        return ('mendwright: got a Playwright ' + name + ', which points at elements, not at a '
                'page. mendwright needs the page to capture HTML when it heals. Pass the page '
                'instead (locator.page for a Locator), or a Frame from page.frames to work '
                'inside an iframe.')
    return ('mendwright: could not detect a browser driver for ' + name + '. Pass a '
            'Playwright/Selenium page, or register one with mendwright.register_driver(detect, make).')


# --- element summaries for describe() and inventory() -----------------------------------

OUTER_HTML_LIMIT = 2000   # describe(): outer_html is cut to this many characters
DESCRIBE_TEXT_LIMIT = 200  # describe(): text is cut to this many characters
INVENTORY_TEXT_LIMIT = 80  # inventory(): text and aria_label are cut to this many characters

# value="..." attributes can hold what a user typed (some frameworks mirror the live value
# into the attribute), so they never leave describe().
_VALUE_ATTR = re.compile(r"""\svalue=("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)

# describe(): run against every match of a locator; null unless exactly one matched.
DESCRIBE_JS = ('els => els.length === 1 ? '
               '{tag: els[0].tagName.toLowerCase(), html: els[0].outerHTML} : null')

# inventory(): one pass over the document's interactive elements. Form fields and
# editable regions report no text, so typed values never leave the page.
INVENTORY_JS = r"""(limit) => {
  const out = [];
  const nodes = document.querySelectorAll('button, a, input, select, textarea, [role]');
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const fieldRole = /^(textbox|searchbox|combobox|spinbutton|slider)$/i;
  for (const e of nodes) {
    if (out.length >= limit) break;
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    const role = e.getAttribute('role') || '';
    const field = tag === 'input' || tag === 'textarea' || tag === 'select'
      || e.isContentEditable || fieldRole.test(role);
    out.push({tag: tag, role: role, type: type, id: e.getAttribute('id') || '',
              name: e.getAttribute('name') || '',
              text: field ? '' : norm(e.textContent).slice(0, 80),
              aria_label: e.getAttribute('aria-label') || ''});
  }
  return out;
}"""


# --- popups, for open(dismiss=True) (see mendwright.dismiss) -------------------------------
#
# The scripts below only report what the page shows; mendwright.dismiss decides. Each is a
# function expression with the helpers inside it, so it runs as it is in Playwright's
# evaluate calls and, called with its arguments, in Selenium's execute_script.

_POPUP_HELPERS = r"""
  // An element's parent, out of a shadow root to its host.
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  // Whether a is b or holds it, across shadow roots.
  const holds = (a, b) => { for (let n = b; n; n = up(n)) { if (n === a) return true; } return false; };
  const style = (el) => (el.ownerDocument.defaultView || window).getComputedStyle(el);
  const shown = (el) => {
    if (!el || !el.isConnected) return false;
    const s = style(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const clean = (s, n) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  // What is on top at the middle of t: {hit, w, h}, or {why} when t is not covered or
  // cannot be: it has no size, is hidden, or its middle is outside the window.
  const cover = (t) => {
    const win = t.ownerDocument.defaultView || window;
    const w = win.innerWidth, h = win.innerHeight;
    const r = t.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0) || !shown(t)) return {why: 'hidden'};
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= w || y >= h) return {why: 'outside'};
    let hit = t.ownerDocument.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (!hit || holds(t, hit) || holds(hit, t)) return {why: 'free'};
    return {hit: hit, w: w, h: h};
  };
  // What may be a button: mendwright.dismiss.is_control decides which of them count.
  const control = (el) => el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'INPUT'
    || (el.getAttribute('role') || '').trim().toLowerCase() === 'button';
  // The controls of a popup in page order, open shadow roots included.
  const controls = (root) => {
    const out = [];
    const walk = (scope) => {
      for (const el of scope.querySelectorAll('*')) {
        if (control(el) && shown(el)) out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
        if (out.length >= 50) return;
      }
    };
    if (root.shadowRoot) walk(root.shadowRoot);
    walk(root);
    return out.slice(0, 50);
  };
  // A popup's own text: its text outside its controls (open shadow roots included), as
  // mendwright.dismiss.setting reads it: "Close your account?" around [Cancel] [Close].
  const prose = (root) => {
    const parts = [];
    let size = 0;
    const walk = (scope) => {
      for (const node of scope.childNodes) {
        if (size > 3000) return;
        if (node.nodeType === 3) { parts.push(node.data); size += node.data.length; continue; }
        if (node.nodeType !== 1 || control(node)
            || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT)$/.test(node.tagName)) continue;
        if (style(node).display === 'none') continue;
        if (node.shadowRoot) walk(node.shadowRoot);
        walk(node);
      }
    };
    if (root.shadowRoot) walk(root.shadowRoot);
    walk(root);
    return clean(parts.join(' '), 1500);
  };
  // A link that goes somewhere: an href other than '#' and javascript: code that does
  // nothing (javascript:, javascript:void(0), javascript:;), as dismiss.goes_nowhere says.
  const real = (a) => {
    if (!a.hasAttribute('href')) return false;
    const href = a.getAttribute('href').trim().toLowerCase();
    if (href === '#') return false;
    if (!href.startsWith('javascript:')) return true;
    const code = href.slice('javascript:'.length).replace(/\s+/g, '').replace(/;+$/, '');
    return !['', 'void(0)', 'void0', 'void(null)'].includes(code);
  };
  // Whether a click on el may follow a link: one holds el, or el holds one.
  const linked = (el) => {
    for (let n = up(el); n; n = up(n)) { if (n.tagName === 'A' && real(n)) return true; }
    for (const a of el.querySelectorAll('a[href]')) { if (real(a)) return true; }
    return false;
  };
  // Whether el is a <label> or in one: a click on it may check the label's box.
  const labelled = (el) => {
    for (let n = el; n; n = up(n)) { if (n.tagName === 'LABEL') return true; }
    return false;
  };
  const FIELDS = 'input:not([type=button]):not([type=submit]):not([type=reset])'
    + ':not([type=hidden]):not([type=image]), select, textarea, [contenteditable]';
  // Whether a click on el submits or resets its form. A form with method="dialog" counts
  // too: it fires its submit event, and the page's handlers of it run.
  const submits = (el) => {
    const form = el.form;
    if (!form || !(el.tagName === 'BUTTON' || el.tagName === 'INPUT')) return false;
    const type = String(el.type || '').toLowerCase();
    return type === 'reset' || type === 'submit' || type === 'image';
  };
  // The text of the elements el's aria-labelledby names: its accessible name.
  const labelledby = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const scope = el.getRootNode();
    return ids.map((id) => {
      const n = (scope && scope.getElementById ? scope.getElementById(id) : null)
        || el.ownerDocument.getElementById(id);
      return n ? n.textContent : '';
    }).join(' ');
  };
  // A control's facts. text is what it shows (innerText leaves out the text hidden with
  // display: none), all its whole text. The value attribute is read for button-like inputs
  // only, whose value is their label: a text field's can hold what was typed.
  const facts = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').trim().toLowerCase();
    return {
      tag: tag, type: type, role: (el.getAttribute('role') || '').trim().toLowerCase(),
      href: tag === 'a' ? el.getAttribute('href') : null,
      text: clean(typeof el.innerText === 'string' ? el.innerText : el.textContent, 200),
      all: clean(el.textContent, 200),
      value: tag === 'input' && ['button', 'submit', 'reset'].includes(type)
        ? clean(el.getAttribute('value'), 200) : '',
      aria: clean(el.getAttribute('aria-label'), 200),
      title: clean(el.getAttribute('title'), 200),
      disabled: el.disabled === true
        || (el.getAttribute('aria-disabled') || '').trim().toLowerCase() === 'true',
      link: linked(el), label: labelled(el), field: el.querySelector(FIELDS) !== null,
      submits: submits(el), labelledby: clean(labelledby(el), 200),
      // A radio, checkbox, switch, option or toggle: a click on it chooses something.
      checked: ['aria-checked', 'aria-pressed', 'aria-selected'].some((a) => el.hasAttribute(a)),
    };
  };
"""

# For a Locator's evaluate_all, or (with [element]) execute_script: null unless one element
# matched. Else {covered, gone} and, when covered, chain: the element on top and each
# container up to (not including) body, with what mendwright.dismiss.popup_of reads; when
# not covered, why (see cover above). gone says whether arg.known (a popup's root from
# POPUP_ROOT_JS) was removed or hidden. With arg.scroll, an element whose middle is outside
# the window is scrolled to the middle of it first, as the action would scroll it.
POPUP_PROBE_JS = r"""(els, arg) => {""" + _POPUP_HELPERS + r"""
  if (!els || els.length !== 1) return null;
  const t = els[0];
  const known = arg && arg.known ? arg.known : null;
  const gone = known ? !shown(known) : null;
  let c = cover(t);
  if (c.why === 'outside' && arg && arg.scroll) {
    try { t.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); }
    catch (e) { t.scrollIntoView({block: 'center', inline: 'center'}); }
    c = cover(t);
  }
  if (!c.hit) return {covered: false, why: c.why, gone: gone};
  const doc = c.hit.ownerDocument;
  const chain = [];
  for (let n = c.hit; n && n.nodeType === 1 && n !== doc.body && n !== doc.documentElement
       && chain.length < 60; n = up(n)) {
    const s = style(n), r = n.getBoundingClientRect();
    const iw = Math.max(0, Math.min(r.right, c.w) - Math.max(r.left, 0));
    const ih = Math.max(0, Math.min(r.bottom, c.h) - Math.max(r.top, 0));
    const role = (n.getAttribute('role') || '').trim().toLowerCase();
    const modal = (n.getAttribute('aria-modal') || '').trim().toLowerCase() === 'true';
    const open = n.tagName === 'DIALOG' && n.open === true;
    const inside = holds(n, t);
    const named = !inside && (role === 'dialog' || role === 'alertdialog' || modal || open
                              || s.position === 'fixed' || s.position === 'sticky');
    chain.push({tag: n.tagName.toLowerCase(), id: clean(n.getAttribute('id'), 100),
                cls: clean(n.getAttribute('class'), 200), role: role, modal: modal, open: open,
                pos: s.position, area: c.w * c.h ? (iw * ih) / (c.w * c.h) : 0, holds: inside,
                events: s.pointerEvents !== 'none',
                aria: named ? clean(n.getAttribute('aria-label'), 200) : '',
                text: named ? clean(n.innerText || n.textContent, 1500) : '',
                prose: named ? prose(n) : ''});
  }
  return {covered: true, gone: gone, chain: chain};
}"""

# For a Locator's evaluate_handle (element first): the element arg.depth steps up from the
# one on top at the middle of el, when its tag and id are still arg.tag and arg.id; else null.
POPUP_ROOT_JS = r"""(t, arg) => {""" + _POPUP_HELPERS + r"""
  const c = cover(t);
  if (!c.hit) return null;
  let n = c.hit;
  for (let i = 0; n && i < arg.depth; i++) n = up(n);
  if (!n || n.tagName.toLowerCase() !== arg.tag || clean(n.getAttribute('id'), 100) !== arg.id) return null;
  return n;
}"""

# For a root's evaluate: the facts of each control in it, in page order.
POPUP_CONTROLS_JS = r"""(root) => {""" + _POPUP_HELPERS + r"""
  return controls(root).map(facts);
}"""

# For a root's evaluate_handle: control number arg.index, when every fact of it is still
# what arg.facts says (mendwright.dismiss.expect_control); else null.
POPUP_CONTROL_JS = r"""(root, arg) => {""" + _POPUP_HELPERS + r"""
  const el = controls(root)[arg.index];
  const want = (arg && arg.facts) || {};
  if (!el || Object.keys(want).length === 0) return null;
  const f = facts(el);
  const same = (a, b) => JSON.stringify(a === undefined ? null : a)
    === JSON.stringify(b === undefined ? null : b);
  return Object.keys(want).every((k) => same(f[k], want[k])) ? el : null;
}"""

# For a root's evaluate, before Escape is pressed: true when Escape would reach the popup,
# because the keyboard focus (through open shadow roots) is on the body, or nowhere, or
# inside the root, in a top-level page. In an iframe it is false: the key goes to the
# focused element of the page, which may be in the page around the iframe.
POPUP_FOCUS_JS = r"""(root) => {""" + _POPUP_HELPERS + r"""
  const doc = root.ownerDocument;
  const win = doc.defaultView || window;
  if (win !== win.top) return false;
  let a = doc.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return !a || a === doc.body || a === doc.documentElement || holds(root, a);
}"""


def _bare_element(tag):
    try:
        return lxml.html.Element(tag or 'span'), False
    except Exception:  # a tag name lxml will not take (rare custom or namespaced tags)
        return lxml.html.Element('span'), True


def role_for(tag, role=None, type_=None):
    """The gate's role (validation._role_of) for an element known only by its tag, role
    attribute and type attribute. Used by inventory()."""
    tag = (tag or '').lower()
    el, stand_in = _bare_element(tag)
    if role:
        el.set('role', str(role))
    if type_:
        el.set('type', str(type_))
    out = _role_of(el)
    return (tag or 'other') if stand_in and out == 'span' else out


def _parse_element(tag, outer_html):
    """The element itself, parsed from its outerHTML. fragment_fromstring handles almost
    everything; <html>/<body>/<head> need a document parse; a bare element is the last
    resort so the role still reflects the tag."""
    try:
        el = lxml.html.fragment_fromstring(outer_html)
        if not tag or (isinstance(el.tag, str) and el.tag.lower() == tag):
            return el
    except Exception:
        pass
    try:
        for el in lxml.html.document_fromstring(outer_html).iter(tag):
            return el
    except Exception:
        pass
    return _bare_element(tag)[0]


def element_summary(tag, outer_html):
    """describe()'s result for one element: {'tag', 'role', 'text', 'outer_html'}.

    role and text are computed the way the gate computes them (validation._role_of and
    the normalized text_content() that validate() compares for destructive keys), on the
    element parsed from its full outerHTML. outer_html is returned without value=
    attributes and cut to OUTER_HTML_LIMIT characters; a textarea reports no text."""
    tag = (tag or '').lower()
    outer = outer_html or ''
    el = _parse_element(tag, outer)
    text = '' if tag == 'textarea' else _norm(el.text_content())[:DESCRIBE_TEXT_LIMIT]
    return {'tag': tag or (el.tag if isinstance(el.tag, str) else ''),
            'role': _role_of(el), 'text': text,
            'outer_html': _VALUE_ATTR.sub('', outer)[:OUTER_HTML_LIMIT]}


def summary_from_js(data):
    """element_summary() for DESCRIBE_JS's result, or None when it did not match once."""
    if not isinstance(data, dict) or not isinstance(data.get('html'), str):
        return None
    return element_summary(data.get('tag'), data['html'])


def inventory_rows(raw, limit=200):
    """Turn INVENTORY_JS's result into inventory() rows:
    {'tag', 'role', 'id', 'name', 'text', 'aria_label'}, role computed with role_for().
    Anything malformed is skipped; a non-list gives []."""
    if not isinstance(raw, list):
        return []
    try:
        limit = max(0, int(limit))
    except Exception:
        limit = 200
    rows = []
    for item in raw:
        if len(rows) >= limit:
            break
        if not isinstance(item, dict):
            continue
        tag = str(item.get('tag') or '').lower()
        rows.append({
            'tag': tag,
            'role': role_for(tag, item.get('role'), item.get('type')),
            'id': str(item.get('id') or '')[:200],
            'name': str(item.get('name') or '')[:200],
            'text': _norm(str(item.get('text') or ''))[:INVENTORY_TEXT_LIMIT],
            'aria_label': _norm(str(item.get('aria_label') or ''))[:INVENTORY_TEXT_LIMIT],
        })
    return rows
