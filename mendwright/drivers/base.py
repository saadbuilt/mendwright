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

Callers should go through driver_act_on, driver_describe and driver_inventory below, which
fall back to getattr(native, verb)(...), None and [] when a driver lacks the method.
"""

import inspect
import re
from typing import Optional, Protocol, runtime_checkable

import lxml.html

from ..destructive import destructive_label, element_labels, is_labelled_control
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


def described_destructive_label(described, extra_labels=()):
    """(label, word) when the element a describe() result stands for is a button, link or
    input button and one of its labels contains a destructive word, else None.

    The labels are its text and the aria-label, title and alt text read from its
    outer_html, each checked on its own (see mendwright.destructive.element_labels).
    describe() strips value= attributes, so a caller that can read an input button's
    value from the element passes it in extra_labels."""
    if not isinstance(described, dict):
        return None
    el = _parse_element(described.get('tag'), described.get('outer_html') or '')
    if not is_labelled_control(el, described.get('role')):
        return None
    labels = [_norm(described.get('text') or '')] + element_labels(el, text=False)
    labels += [_norm(x) for x in extra_labels if isinstance(x, str)]
    return destructive_label([label for label in dict.fromkeys(labels) if label])


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
