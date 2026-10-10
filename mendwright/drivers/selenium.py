"""Driver for the Selenium WebDriver API. Also covers undetected-chromedriver,
SeleniumBase and Appium, which expose the same surface (detected by duck typing the live
object, never by module name).

Selectors: XPath runs with By.XPATH and CSS with By.CSS_SELECTOR. Playwright engine
selectors ('text=...', "role=button[name='Save']", ' >> ' chains) have no Selenium
equivalent and raise InvalidSelector. So does a selector the browser cannot parse
(Selenium's InvalidSelectorException), instead of reading as "no match".

Frame handling is stateful in Selenium: there are no lazy frame locators, so to touch an
element inside a frame chain the driver must switch_to.frame(...) into each frame
(outermost first), do the work, then switch_to.default_content() to get back to the top
document. That restore always runs in a finally, even when the work (or a deeper switch)
raises; otherwise a failed heal would leave the driver inside an iframe and break every
later query. act() does its own switching, so interactions stay frame-safe, and handle()
returns a FramedElement for a frame candidate (see below) instead of a raw WebElement.

act_on(native, verb, ...) lets callers use Playwright verbs on a WebElement: fill,
check, uncheck, set_checked, select_option, type, press, hover and dblclick are mapped to
their Selenium equivalents; anything else is called on the element as is.

Probe calls (count, is_visible, scroll_into_view, the lookup in handle) run with the
WebDriver's implicit wait set to 0 and restore it afterwards, so a user's
driver.implicitly_wait(10) does not turn every missed candidate into a 10 second stall.
"""

import contextlib
import inspect

from ..selectors import CSS, XPATH, InvalidSelector, check_syntax, strip_prefix
from .base import INVENTORY_JS, element_summary, inventory_rows, is_async_api


def _by():
    """Selenium's By.XPATH, imported lazily so ``import mendwright`` works without
    selenium installed. Falls back to its literal value 'xpath' (what By.XPATH is in every
    Selenium release) so the module imports and tests run when selenium is absent."""
    try:
        from selenium.webdriver.common.by import By
        return By.XPATH
    except Exception:
        return 'xpath'


def _by_css():
    """Selenium's By.CSS_SELECTOR, with the same lazy import and literal fallback."""
    try:
        from selenium.webdriver.common.by import By
        return By.CSS_SELECTOR
    except Exception:
        return 'css selector'


def selenium_locator(selector):
    """(by, value) for driver.find_elements() for a mendwright selector.

    Raises TypeError for a non-string, and InvalidSelector for an empty selector, bad
    XPath, or a Playwright engine selector (which Selenium cannot run)."""
    kind = check_syntax(selector)
    if kind == XPATH:
        return _by(), strip_prefix(selector)
    if kind == CSS:
        return _by_css(), strip_prefix(selector)
    raise InvalidSelector.build(
        selector, kind,
        repr(selector.strip()) + ' is a Playwright selector, and Selenium cannot run it.',
        "Use an XPath such as \"//button[normalize-space()='Save']\" or a CSS selector "
        "such as 'button#save', or keep it and pass a Playwright page to mendwright.open().")


def _is_invalid_selector_error(exc):
    # Matched by class name so this works without importing selenium (and with fakes).
    return any(cls.__name__ == 'InvalidSelectorException' for cls in type(exc).__mro__)


@contextlib.contextmanager
def _no_implicit_wait(driver):
    """Run the block with the driver's implicit wait at 0, then restore the old value.

    Reads the current value with driver.timeouts.implicit_wait (Selenium 4) and sets it
    with driver.implicitly_wait(). Does nothing when the value is already 0 or cannot be
    read (Selenium 3, a remote end without GET /timeouts, a test fake)."""
    try:
        previous = driver.timeouts.implicit_wait
    except Exception:
        previous = None
    if not previous:
        yield
        return
    try:
        driver.implicitly_wait(0)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            driver.implicitly_wait(previous)
        except Exception:
            pass


def _no_such_element(message):
    try:
        from selenium.common.exceptions import NoSuchElementException
        return NoSuchElementException(message)
    except Exception:
        return LookupError(message)


# --- Playwright verbs on a WebElement ---------------------------------------------------

# Playwright options with no Selenium equivalent that do not change what the action does,
# so code written for both drivers can pass them. Anything else is a TypeError.
_IGNORED_OPTIONS = frozenset(('timeout', 'force', 'no_wait_after', 'delay'))

MAPPED_VERBS = ('fill', 'check', 'uncheck', 'set_checked', 'select_option', 'type', 'click', 'clear',
                'press_sequentially', 'press', 'hover', 'dblclick')

# Playwright key names -> attribute names on selenium's Keys.
_KEY_NAMES = {
    'Enter': 'ENTER', 'Tab': 'TAB', 'Escape': 'ESCAPE', 'Backspace': 'BACKSPACE',
    'Delete': 'DELETE', 'Insert': 'INSERT', 'Home': 'HOME', 'End': 'END',
    'PageUp': 'PAGE_UP', 'PageDown': 'PAGE_DOWN', 'ArrowDown': 'ARROW_DOWN',
    'ArrowUp': 'ARROW_UP', 'ArrowLeft': 'ARROW_LEFT', 'ArrowRight': 'ARROW_RIGHT',
    'Space': 'SPACE', 'Shift': 'SHIFT', 'Control': 'CONTROL', 'ControlOrMeta': 'CONTROL',
    'Alt': 'ALT', 'Meta': 'META',
    **{'F' + str(n): 'F' + str(n) for n in range(1, 13)},
}

# The W3C WebDriver code points behind those names (the values selenium's Keys holds).
# Used only when selenium itself cannot be imported.
_KEY_CODES = {
    'NULL': '\ue000', 'BACKSPACE': '\ue003', 'TAB': '\ue004', 'ENTER': '\ue007',
    'SHIFT': '\ue008', 'CONTROL': '\ue009', 'ALT': '\ue00a', 'ESCAPE': '\ue00c',
    'SPACE': '\ue00d', 'PAGE_UP': '\ue00e', 'PAGE_DOWN': '\ue00f', 'END': '\ue010',
    'HOME': '\ue011', 'ARROW_LEFT': '\ue012', 'ARROW_UP': '\ue013', 'ARROW_RIGHT': '\ue014',
    'ARROW_DOWN': '\ue015', 'INSERT': '\ue016', 'DELETE': '\ue017', 'META': '\ue03d',
    **{'F' + str(n): chr(0xE030 + n) for n in range(1, 13)},
}


def _key_value(attr):
    try:
        from selenium.webdriver.common.keys import Keys
        return getattr(Keys, attr)
    except Exception:
        return _KEY_CODES[attr]


def _key_sequence(key):
    """Playwright key syntax ('Enter', 'Shift+Tab', 'Control+a') -> send_keys arguments."""
    if not isinstance(key, str) or not key:
        raise TypeError("mendwright: press() takes a key name such as 'Enter', got "
                        + repr(key) + '.')
    if key == '+':
        parts = ['+']
    else:
        parts = key.split('+')
        if parts[-1] == '' and len(parts) > 1:        # 'Control++' presses the plus key
            parts = parts[:-2] + ['+']
    *modifiers, main = parts
    seq = []
    for name in modifiers + [main]:
        if name in _KEY_NAMES:
            seq.append(_key_value(_KEY_NAMES[name]))
        elif len(name) == 1:
            seq.append(name.lower() if modifiers else name)
        else:
            raise ValueError(
                'mendwright: press() does not know the key ' + repr(name) + ' on Selenium. '
                "Use a Playwright key name such as 'Enter', 'Tab' or 'ArrowDown', or pass "
                "selenium's Keys to act_on(native, 'send_keys', ...).")
    if modifiers:
        seq.append(_key_value('NULL'))  # release the modifiers
    return seq


def _make_select(el):
    """selenium's Select for a <select> element (imported lazily)."""
    from selenium.webdriver.support.select import Select
    return Select(el)


def _action_chains(webdriver):
    """selenium's ActionChains (imported lazily)."""
    from selenium.webdriver.common.action_chains import ActionChains
    return ActionChains(webdriver)


def _is_no_such_element(exc):
    return any(cls.__name__ == 'NoSuchElementException' for cls in type(exc).__mro__)


def _check_options(verb, kwargs):
    unknown = sorted(k for k in kwargs if k not in _IGNORED_OPTIONS)
    if unknown:
        raise TypeError(
            'mendwright: ' + verb + '() on Selenium does not support '
            + ', '.join(k + '=' for k in unknown) + '. Remove them, or call a WebElement '
            "method directly with act_on(native, 'send_keys', ...) or similar.")


def _one_arg(verb, args, kwargs, name):
    if name in kwargs:
        if args:
            raise TypeError('mendwright: ' + verb + '() got ' + name + ' twice.')
        return kwargs.pop(name)
    if len(args) != 1:
        raise TypeError('mendwright: ' + verb + '() takes one ' + name + ' argument, got '
                        + str(len(args)) + '.')
    return args[0]


def _as_list(verb, value, what):
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TypeError('mendwright: ' + verb + '() on Selenium takes ' + what
                    + ' as a string or a list of strings, got ' + type(value).__name__ + '.')


def _is_checked(el):
    if el.is_selected():
        return True
    try:
        return (el.get_attribute('aria-checked') or '').lower() == 'true'
    except Exception:
        return False


def _set_checked(el, want):
    if _is_checked(el) == want:
        return None
    el.click()
    if _is_checked(el) != want:
        raise RuntimeError(
            'mendwright: clicking the element did not change its checked state. If the real '
            'control is a styled label or switch, point the selector at that, or use '
            "act_on(native, 'click').")
    return None


def _select_option(el, values, labels, indexes):
    select = _make_select(el)
    if getattr(select, 'is_multiple', False):
        select.deselect_all()
    for value in values:
        try:
            select.select_by_value(value)
        except Exception as exc:
            if not _is_no_such_element(exc):
                raise
            select.select_by_visible_text(value)
    for label in labels:
        select.select_by_visible_text(label)
    for index in indexes:
        select.select_by_index(index)
    return None


def selenium_verb(verb, args, kwargs, element_type=None):
    """Check a Playwright-style call and return run(webdriver, element) that performs it.

    Everything that can be checked (argument shapes, options, key names, whether the
    element type has the method) is checked here, before anything reaches the browser.
    Unmapped verbs are called on the element as is, and raise AttributeError up front
    when ``element_type`` is given and has no such method."""
    kwargs = dict(kwargs)
    if verb == 'fill':
        value = _one_arg(verb, args, kwargs, 'value')
        _check_options(verb, kwargs)

        def run(webdriver, el):
            el.clear()
            if value:
                el.send_keys(value)
        return run
    if verb in ('check', 'uncheck'):
        if args:
            raise TypeError('mendwright: ' + verb + '() takes no positional arguments.')
        _check_options(verb, kwargs)
        want = verb == 'check'
        return lambda webdriver, el: _set_checked(el, want)
    if verb == 'set_checked':
        want = bool(_one_arg(verb, args, kwargs, 'checked'))
        _check_options(verb, kwargs)
        return lambda webdriver, el: _set_checked(el, want)
    if verb == 'select_option':
        if len(args) > 1:
            raise TypeError('mendwright: select_option() takes one value argument.')
        value = args[0] if args else kwargs.pop('value', None)
        values = _as_list(verb, value, 'value')
        labels = _as_list(verb, kwargs.pop('label', None), 'label=')
        indexes = _as_list(verb, kwargs.pop('index', None), 'index=')
        _check_options(verb, kwargs)
        if not (values or labels or indexes):
            raise TypeError('mendwright: select_option() needs a value, label= or index=.')
        return lambda webdriver, el: _select_option(el, values, labels, indexes)
    if verb in ('type', 'press_sequentially'):
        text = _one_arg(verb, args, kwargs, 'text')
        _check_options(verb, kwargs)
        return lambda webdriver, el: el.send_keys(text)
    if verb == 'press':
        keys = _key_sequence(_one_arg(verb, args, kwargs, 'key'))
        _check_options(verb, kwargs)
        return lambda webdriver, el: el.send_keys(*keys)
    if verb in ('hover', 'dblclick'):
        if args:
            raise TypeError('mendwright: ' + verb + '() takes no positional arguments.')
        _check_options(verb, kwargs)
        if verb == 'hover':
            return lambda webdriver, el: _action_chains(webdriver).move_to_element(el).perform()
        return lambda webdriver, el: _action_chains(webdriver).double_click(el).perform()
    if verb in ('click', 'clear'):
        if args:
            raise TypeError('mendwright: ' + verb + '() takes no positional arguments on Selenium.')
        _check_options(verb, kwargs)
        return lambda webdriver, el: getattr(el, verb)()
    if element_type is not None and inspect.getattr_static(element_type, verb, None) is None:
        raise _no_verb(element_type.__name__, verb)
    # Playwright-only options (timeout=, force=, ...) mean nothing to a WebElement method;
    # drop them so code written for both drivers runs on either.
    for name in _IGNORED_OPTIONS:
        kwargs.pop(name, None)

    def run(webdriver, el):
        try:
            method = getattr(el, verb)
        except AttributeError:
            raise _no_verb(type(el).__name__, verb) from None
        return method(*args, **kwargs)
    return run


def _no_verb(type_name, verb):
    return AttributeError(
        'mendwright: ' + type_name + ' has no ' + repr(verb) + '. On Selenium, act_on maps '
        + ', '.join(MAPPED_VERBS) + '; any other verb must be a WebElement method such as '
        'click or send_keys.')


class FramedElement:
    """Stands in for a WebElement that lives inside an iframe.

    A WebElement found inside a frame stops working once the driver switches back to the
    top document, and SeleniumDriver switches back after every call. This proxy switches
    into the frame chain for each method call or property read, finds the element again,
    does the work, and switches back (always, in a finally). So ``proxy.click()``,
    ``proxy.send_keys('x')`` and ``proxy.text`` work on a frameXPath candidate.

    Limits: anything a method returns that points into the frame (``find_element()``
    results, the option elements behind ``Select(proxy)``) is only usable while switched
    into that frame. Use SeleniumDriver.act_on (for example verb 'select_option') or
    ``in_frame(fn)`` for multi-step work.
    """

    def __init__(self, selenium_driver, selector, frame_chain, element_type=None):
        self._mw = (selenium_driver, selector, tuple(frame_chain), element_type)

    @property
    def selector(self):
        return self._mw[1]

    @property
    def frame_chain(self):
        return self._mw[2]

    def in_frame(self, fn):
        """Call fn(element) with the driver switched into the frame chain and the element
        found again; return fn's result. The top document is restored afterwards."""
        sd, selector, chain, _ = self._mw
        return sd._run(chain, lambda: fn(sd._find_one(selector)), probe=False)

    def __getattr__(self, name):
        if name.startswith('__') or name == '_mw':
            raise AttributeError(name)
        element_type = self._mw[3]
        member = inspect.getattr_static(element_type, name, None) if element_type else None
        if callable(member) and not isinstance(member, property):
            return self._method(name)
        value = self.in_frame(lambda el: getattr(el, name))
        if member is None and callable(value):
            return self._method(name)
        return value

    def _method(self, name):
        def call(*args, **kwargs):
            return self.in_frame(lambda el: getattr(el, name)(*args, **kwargs))
        call.__name__ = name
        return call

    def __repr__(self):
        return '<FramedElement ' + repr(self.selector) + ' in ' + repr(self.frame_chain) + '>'


class SeleniumDriver:
    """Wraps a Selenium WebDriver (or a compatible one).

    Required Driver methods: count, is_visible, scroll_into_view, handle, act, url,
    outer_html. All take a selector of any kind Selenium can run plus an optional frame
    chain (frame selectors, outermost first). Optional extras, which callers reach
    through the driver_* helpers in mendwright.drivers.base: act_on, describe, inventory."""

    def __init__(self, driver):
        self._driver = driver

    def _find_all(self, selector):
        """find_elements for any selector kind, in the current frame context."""
        by, value = selenium_locator(selector)
        try:
            return self._driver.find_elements(by, value)
        except Exception as exc:
            if not _is_invalid_selector_error(exc):
                raise
            lines = str(exc).strip().splitlines()
            said = lines[0] if lines else type(exc).__name__
            if said.startswith('Message: '):
                said = said[len('Message: '):]
            raise InvalidSelector.build(
                selector, 'xpath' if by == _by() else 'css',
                'the browser rejected ' + repr(value) + ' (' + said.rstrip('.: ') + ').',
                "Fix the selector, or use an XPath such as \"//button[@id='save']\", which "
                "mendwright.selectors.check_syntax() can check without a browser.") from exc

    def _precheck(self, selector, frame_chain):
        """Raise for a bad selector before any switching, outside the read methods' try."""
        selenium_locator(selector)
        for frame in frame_chain:
            selenium_locator(frame)

    def _find_one(self, selector):
        els = self._find_all(selector)
        if not els:
            raise _no_such_element('mendwright[selenium]: no element matches ' + repr(selector))
        return els[0]

    def _run(self, frame_chain, op, probe=True):
        """Switch into each frame in ``frame_chain`` (outermost first), run ``op()`` in the
        innermost frame, and always restore ``default_content()`` in a finally, even if a
        switch or ``op`` raises. ``op``'s return value (or its exception) is passed through;
        the read methods below wrap this and fall back to their safe default.

        With probe=True the implicit wait is 0 for the duration. Actions pass probe=False
        so they keep whatever implicit wait the user configured."""
        if probe:
            with _no_implicit_wait(self._driver):
                return self._switch_and_run(frame_chain, op)
        return self._switch_and_run(frame_chain, op)

    def _switch_and_run(self, frame_chain, op):
        entered = False
        try:
            for frame_selector in frame_chain:
                frames = self._find_all(frame_selector)
                if not frames:
                    raise LookupError('mendwright[selenium]: frame not found: ' + str(frame_selector))
                entered = True  # set before the switch so finally restores even if it raises
                self._driver.switch_to.frame(frames[0])
            return op()
        finally:
            if entered:
                self._driver.switch_to.default_content()

    def _read(self, selector, frame_chain, op, default):
        self._precheck(selector, frame_chain)
        try:
            return self._run(frame_chain, op)
        except InvalidSelector:
            raise
        except Exception:
            return default

    def count(self, selector, frame_chain=()):
        return self._read(selector, frame_chain, lambda: len(self._find_all(selector)), 0)

    def is_visible(self, selector, frame_chain=()):
        def op():
            els = self._find_all(selector)
            return bool(els) and bool(els[0].is_displayed())

        return self._read(selector, frame_chain, op, False)

    def scroll_into_view(self, selector, frame_chain=()):
        def op():
            els = self._find_all(selector)
            if els:
                self._driver.execute_script(
                    'arguments[0].scrollIntoView({block: "center", inline: "center"})', els[0])

        self._read(selector, frame_chain, op, None)

    def handle(self, selector, frame_chain=()):
        """The native WebElement. For a frame chain, a FramedElement that switches into
        the frames on each use (a raw WebElement would fail once the driver is back at
        the top document)."""
        self._precheck(selector, frame_chain)
        el = self._run(frame_chain, lambda: self._find_all(selector)[0])
        if not frame_chain:
            return el
        return FramedElement(self, selector, frame_chain, type(el))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        """Find the element (inside the frame chain) and run verb on it, with the same
        verb mapping as act_on."""
        self._precheck(selector, frame_chain)
        run = selenium_verb(verb, args, kwargs)
        return self._run(frame_chain, lambda: run(self._driver, self._find_all(selector)[0]),
                         probe=False)

    def act_on(self, native, verb, *args, **kwargs):
        """Run a Playwright-style verb on a handle() result.

        fill(value) -> clear() + send_keys(value); check()/uncheck()/set_checked(b) -> click()
        only when the checked state differs (and raise if the click did not change it);
        select_option(value | label= | index=) -> Select by value, then by visible text;
        type(text) -> send_keys(text); press('Enter' | 'Shift+Tab' | ...) -> send_keys with
        selenium Keys; hover()/dblclick() -> ActionChains. Any other verb is called on the
        element as is: act_on(el, 'send_keys', 'x') is el.send_keys('x').

        Playwright's timeout=, force=, no_wait_after= and delay= are accepted and ignored on
        mapped verbs; other unknown options raise TypeError. A bad argument or a verb the
        element does not have raises before anything reaches the browser. A FramedElement
        is handled inside its frame chain."""
        if isinstance(native, FramedElement):
            run = selenium_verb(verb, args, kwargs, native._mw[3])
            return native.in_frame(lambda el: run(self._driver, el))
        return selenium_verb(verb, args, kwargs)(self._driver, native)

    def url(self):
        try:
            return self._driver.current_url
        except Exception:
            return ''

    def outer_html(self, scope=None):
        try:
            if scope:
                with _no_implicit_wait(self._driver):
                    els = self._find_all(scope)
                return els[0].get_attribute('outerHTML') if els else ''
            return self._driver.page_source
        except Exception:
            return ''

    def describe(self, selector, frame_chain=()):
        """{'tag', 'role', 'text', 'outer_html'} for the one element the selector matches,
        or None when it matches none or several. role and text are computed the way the
        gate computes them (see base.element_summary). Runs inside the frame chain with
        the implicit wait at 0. A malformed selector raises InvalidSelector; any other
        error gives None."""
        def op():
            els = self._find_all(selector)
            if len(els) != 1:
                return None
            data = self._driver.execute_script(
                'return {tag: arguments[0].tagName.toLowerCase(), html: arguments[0].outerHTML};',
                els[0])
            if not isinstance(data, dict) or not isinstance(data.get('html'), str):
                return None
            return element_summary(data.get('tag'), data['html'])

        return self._read(selector, frame_chain, op, None)

    def inventory(self, limit=200):
        """Up to ``limit`` interactive elements of the current document:
        [{'tag', 'role', 'id', 'name', 'text', 'aria_label'}, ...] in document order. One
        round trip (execute_script). Never includes input values. [] on any error."""
        try:
            raw = self._driver.execute_script(
                'return (' + INVENTORY_JS + ')(arguments[0]);', limit)
        except Exception:
            return []
        return inventory_rows(raw, limit)


def detect(obj) -> bool:
    """Selenium WebDriver family (incl. undetected-chromedriver, SeleniumBase, Appium):
    has ``find_elements`` AND ``switch_to``. Duck-typed, never by module name. A WebElement
    has ``find_elements`` but NO ``switch_to``, so it is never mistaken for a driver; and a
    Playwright page has neither, so there is no collision with PlaywrightSyncDriver. An
    async look-alike (selenium_driverless, whose find_elements is a coroutine) is rejected
    so wrap() can say that async is not supported instead of every lookup reading 0."""
    return (hasattr(obj, 'find_elements') and hasattr(obj, 'switch_to')
            and not is_async_api(obj))


def register():
    """Register SeleniumDriver with the auto-detection registry. mendwright.drivers
    already does this on import; calling it again only adds a duplicate entry."""
    from .registry import register_driver
    register_driver(detect, SeleniumDriver)
