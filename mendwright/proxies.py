"""heal.page and heal.driver: the page or driver a script already uses, with its selector
lookups healed, so an existing script heals by changing one line:

    page = mendwright.open(page).page          # Playwright
    driver = mendwright.open(driver).driver    # Selenium

On a Playwright page, page.locator(selector) and the page shortcuts that take a selector
(click, fill, check, ...) go through heal.locator(selector, selector): the selector is the
key its heals are saved under. Everything else (goto, wait_for_*, evaluate, frames,
keyboard, ...) is the real page's. A locator built with options (has_text=...), a chained
locator (page.locator('a').locator('b')) and the get_by_* locators are the real ones, not
healed. is_visible() never waits or heals: it answers from the selectors mendwright has
for the key, as Playwright answers at once. Interruptions (heal.interrupt(),
open(cookies=...)) are handled before the real locators' actions too: mendwright registers
them with page.add_locator_handler (Playwright 1.42 and later, on a Page), which
Playwright does not run for page.mouse, page.keyboard or an action with force=True.
With open(dismiss=True), those locators come back as GuardedLocators instead: the real ones,
still not healed, whose actions look for interruptions and popups first, as
heal.locator()'s do.

On Selenium, driver.find_element(by, value) for By.XPATH, By.CSS_SELECTOR, By.ID and
By.NAME goes through heal.locator(f'{by}={value}', selector). find_element (for any By),
find_elements, and find_element(s) on an element return HealingElements: the element,
whose click, send_keys, clear and submit handle interruptions as heal.locator()'s actions
do. Only the elements heal.locator() found carry the heal canary. Everything else is the
real driver's.

Lists: Locator.all() on heal.page.locator(selector), and driver.find_elements(by, value)
for those four By values, give what the real call gives, but a list that matches nothing,
after it matched two or more elements before, is healed as heal.locator_all() heals one
(see mendwright.lists), and one that matches two or more is recorded for that, while the
fingerprint tier is on. On Selenium, a list that cannot be found or healed raises an
AttentionRequired that is also a NoSuchElementException, so WebDriverWait goes on polling.
The elements of a list found through a heal have their reads checked when
open(expect_values=...) has a check for the key: on Playwright they come back as
GuardedLocators for that.

The reads (get_attribute, text_content, input_value, is_checked, ...), set_input_files
and focus work on a hidden element in Playwright, and Selenium's find_element returns a
hidden element or the first of several. For these, a selector that matches something
mendwright cannot use (a hidden element, or several), with no saved heal that finds the
element, goes to the real page or driver as written; only a selector that matches
nothing is healed. The actions that need one visible element (click, fill, check, ...)
heal a selector that does not match one, as heal.locator() does.

The proxies pass isinstance() checks for the class they stand in for, so
playwright's expect(heal.page) still works. .native is the real object.
"""

from __future__ import annotations

import functools
from typing import Any

from .drivers.base import looks_like_locator
from .exceptions import AttentionRequired
from .facade import ACTIONS, ActionProxy, _raised_by_run
from .selectors import InvalidSelector, check_syntax
from .values import READS as VALUE_READS

# Locator methods that resolve the key (and heal it when it broke) before they run: the
# actions, and the reads that wait for the element in Playwright.
READS = ('is_checked', 'text_content', 'inner_text', 'inner_html', 'get_attribute',
         'input_value', 'is_enabled', 'is_disabled', 'is_editable')
HEALED_LOCATOR_METHODS = frozenset(ACTIONS) | frozenset(READS)
# The ones Playwright runs on an element in any state, hidden included. A selector that
# matches something goes to the real locator for these (see Healer._passes_through).
ANY_STATE = frozenset(READS) | frozenset({'set_input_files', 'focus'})

# Page methods that take a selector first and act on one element.
PAGE_SHORTCUTS = ('click', 'dblclick', 'fill', 'check', 'uncheck', 'select_option', 'press',
                  'type', 'hover', 'is_checked', 'text_content', 'inner_text', 'get_attribute',
                  'input_value')

# Selenium's By values that heal, and how each becomes a selector mendwright stores.
BY_XPATH, BY_CSS, BY_ID, BY_NAME = 'xpath', 'css selector', 'id', 'name'


def _healable(selector):
    """True when selector is a string mendwright can take as it is."""
    if not isinstance(selector, str):
        return False
    try:
        check_syntax(selector)
    except (InvalidSelector, TypeError):
        return False
    return True


def _timeout_ms(kwargs):
    """Playwright's timeout= (milliseconds) as the lookup's timeout_ms, or None for the
    Healer's own. timeout=0 means no limit in Playwright, so it gives None too: it must not
    turn into a lookup that gives up at once and heals a page that is still loading."""
    value = kwargs.get('timeout')
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value)
    return None


def unwrap(obj):
    """The real page or driver behind a proxy from heal.page or heal.driver, else obj."""
    return getattr(obj, '_mw_target', obj) if type(obj) in _PROXY_TYPES else obj


class _Proxy:
    """Forwards every attribute it does not define to the object it wraps, and reports
    that object's class, so isinstance() checks written for it still pass."""

    def __init__(self, healer, target):
        object.__setattr__(self, '_mw_healer', healer)
        object.__setattr__(self, '_mw_target', target)

    @property
    def __class__(self):
        return type(self._mw_target)

    @property
    def native(self) -> Any:
        """The real page, locator or driver."""
        return self._mw_target

    def __getattr__(self, name):
        return getattr(self._mw_target, name)

    def __setattr__(self, name, value):
        setattr(self._mw_target, name, value)

    def __dir__(self):
        return sorted(set(dir(self._mw_target)) | {'native'})


# --- Playwright, sync ----------------------------------------------------------------------------

class HealingLocator(_Proxy):
    """What heal.page.locator(selector) returns. Actions and waiting reads go through
    heal.locator(selector, selector); anything else is the real Locator's, unhealed."""

    def __init__(self, healer, target, selector):
        super().__init__(healer, target)
        object.__setattr__(self, '_mw_selector', selector)

    def _mw_healed(self, name):
        def call(*args, **kwargs):
            healer, selector = self._mw_healer, self._mw_selector
            if name in ANY_STATE and healer._passes_through(selector, selector):
                return getattr(self._mw_target, name)(*args, **kwargs)
            proxy = healer.locator(selector, selector, timeout_ms=_timeout_ms(kwargs))
            return getattr(proxy, name)(*args, **kwargs)
        call.__name__ = name
        return call

    def __getattr__(self, name):
        if name in HEALED_LOCATOR_METHODS:
            return self._mw_healed(name)
        if name == 'is_visible':
            return self._mw_is_visible
        # With open(dismiss=True), a locator chained from this one is guarded (see
        # GuardedLocator); otherwise it is the real one, as everything else is.
        return _chained(self, name, getattr(self._mw_target, name))

    def _mw_step(self):
        return 'page.locator(' + repr(self._mw_selector) + ')'

    def all(self):
        """Locator.all(): a Locator for each element the selector matches, as Playwright
        gives them, with the list healed (see Healer.locator_all) when it matches nothing
        after it matched two or more elements before. With open(dismiss=True), or for a
        list found through a heal of a key open(expect_values=...) has a check for, each
        one is a GuardedLocator."""
        __tracebackhide__ = True  # pytest shows the caller
        healer, selector = self._mw_healer, self._mw_selector
        items, check = self._mw_target.all(), None
        if healer._list_routed(selector, len(items)):
            items, healed, expect = healer._find_all(selector, selector, items)
            check = (selector, healed, expect) if healed and expect else None
        return _items(self, items, check)

    def _mw_is_visible(self, **kwargs):
        if not self._mw_healer._knows(self._mw_selector):
            return self._mw_target.is_visible(**kwargs)
        return self._mw_healer._peek_visible(self._mw_selector)

    def __repr__(self):
        return '<HealingLocator ' + repr(self._mw_selector) + '>'


class HealingPage(_Proxy):
    """What heal.page returns for a Playwright page: see the module docstring."""

    def locator(self, selector, *args, **kwargs):
        target = self._mw_target.locator(selector, *args, **kwargs)
        if args or kwargs or not _healable(selector):
            if not self._mw_healer._dismiss_on:
                return target        # options, or a selector mendwright cannot store
            # With open(dismiss=True), that real locator is guarded (see GuardedLocator).
            return _guard(self, target, 'page.' + _call_text('locator', (selector,) + args,
                                                             kwargs))
        return HealingLocator(self._mw_healer, target, selector)

    def __getattr__(self, name):
        # With open(dismiss=True), get_by_* and frame_locator() hand back guarded locators.
        return _chained(self, name, getattr(self._mw_target, name))

    def _mw_step(self):
        return 'page'

    def _mw_shortcut(self, name, selector, args, kwargs):
        if not _healable(selector) or (name in ANY_STATE
                                       and self._mw_healer._passes_through(selector, selector)):
            return getattr(self._mw_target, name)(selector, *args, **kwargs)
        kwargs.pop('strict', None)   # a Locator is always strict and takes no strict=
        proxy = self._mw_healer.locator(selector, selector, timeout_ms=_timeout_ms(kwargs))
        return getattr(proxy, name)(*args, **kwargs)

    def is_visible(self, selector, **kwargs):
        """Whether selector (or a heal saved for it) shows one visible element now. Like
        Playwright, it does not wait; unlike the actions, it never heals. A selector
        mendwright has no record of is asked of the page itself."""
        if not _healable(selector) or not self._mw_healer._knows(selector):
            return self._mw_target.is_visible(selector, **kwargs)
        return self._mw_healer._peek_visible(selector)

    def __repr__(self):
        return '<HealingPage ' + repr(self._mw_target) + '>'


def _items(proxy, items, check=None):
    """The Locators of proxy's all(), each guarded with open(dismiss=True) (see _guard), and
    with check, (key, healed, expect) for a list found through a heal, guarded whether or
    not dismiss is on, with its reads checked (see GuardedLocator)."""
    step = proxy._mw_step() + '.all()'
    if check is None:
        return [_guard(proxy, item, step + '[' + str(i) + ']') for i, item in enumerate(items)]
    return [proxy._mw_guard_type(proxy._mw_healer, item, step + '[' + str(i) + ']', check)
            for i, item in enumerate(items)]


def _page_shortcut(name):
    def shortcut(self, selector, *args, **kwargs):
        return self._mw_shortcut(name, selector, args, kwargs)
    shortcut.__name__ = name
    shortcut.__doc__ = ('page.' + name + '(selector, ...) through heal.locator(selector, '
                        'selector).' + name + '(...).')
    return shortcut


for _name in PAGE_SHORTCUTS:
    setattr(HealingPage, _name, _page_shortcut(_name))


# --- guard-only locators, for open(dismiss=True) ----------------------------------------------

# What builds another locator on a Page, Frame, Locator or FrameLocator, as methods and as
# properties. With open(dismiss=True), heal.page hands back what they build as a
# GuardedLocator; with it off, the real one, as before.
LOCATOR_BUILDERS = frozenset(('locator', 'get_by_role', 'get_by_text', 'get_by_label',
                              'get_by_placeholder', 'get_by_alt_text', 'get_by_title',
                              'get_by_test_id', 'frame_locator', 'nth', 'filter', 'and_',
                              'or_'))
LOCATOR_PROPERTIES = frozenset(('first', 'last', 'owner', 'content_frame'))


def _arg_text(value):
    """An argument as a GuardedLocator's name shows it."""
    if type(value) in (HealingLocator, AsyncHealingLocator, GuardedLocator, AsyncGuardedLocator):
        return value._mw_step()
    if looks_like_locator(value):
        return '<locator>'
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + '...'


def _call_text(name, args, kwargs):
    """A call as a GuardedLocator's name shows it: get_by_role('button', name='Go')."""
    parts = [_arg_text(a) for a in args] + [k + '=' + _arg_text(v) for k, v in kwargs.items()]
    return name + '(' + ', '.join(parts) + ')'


def _guard(proxy, value, name):
    """value as a guard-only locator of the same family as proxy, when open(dismiss=True)
    was passed and value is a Locator or FrameLocator; else value itself."""
    if not proxy._mw_healer._dismiss_on or not looks_like_locator(value):
        return value
    return proxy._mw_guard_type(proxy._mw_healer, value, name)


def _chained(proxy, name, value):
    """Attribute ``name`` of proxy's target (``value``), as proxy hands it back: a locator
    it builds is guarded (_guard), named after proxy's step (_mw_step) and the call;
    anything else is value."""
    if not proxy._mw_healer._dismiss_on:
        return value
    if name in LOCATOR_PROPERTIES:
        return _guard(proxy, value, proxy._mw_step() + '.' + name)
    if name in LOCATOR_BUILDERS and callable(value):
        def build(*args, **kwargs):
            return _guard(proxy, value(*args, **kwargs),
                          proxy._mw_step() + '.' + _call_text(name, args, kwargs))
        build.__name__ = name
        return build
    return value


class GuardedLocator(_Proxy):
    """With open(dismiss=True), what heal.page hands back for the locators it does not
    heal: get_by_role() and the other get_by_* locators, a locator built with options, a
    chained locator, .first, .last, .nth() and .filter(), on a sync Playwright page or
    frame. It is the real Locator (or FrameLocator), not healed, and nothing about it is
    saved. Its actions run through the same guard as heal.locator()'s (ActionProxy),
    without the heal canary: registered interruptions are handled first, a popup that
    covers the element is dismissed (see mendwright.dismiss), and an action that failed
    because something covered the element is tried once more after one was. Locators built
    from it are guarded the same way.

    It passes isinstance() checks for the Locator, so Playwright's expect() takes it, and
    .native is the Locator. Log lines and handled_interruptions name its step after the
    calls that built it, such as "page.get_by_role('button', name='Continue')".

    The elements Locator.all() gives for a list found through a heal are GuardedLocators
    too when open(expect_values=...) has a check for the key, dismiss= or not: check is
    (key, healed, expect), and their reads (inner_text(), text_content(), input_value(),
    get_attribute()) are checked as heal.locator_all()'s are: a value that fails
    quarantines the heal and raises AttentionRequired (see Healer._check_value)."""

    def __init__(self, healer, target, name, check=None):
        super().__init__(healer, target)
        object.__setattr__(self, '_mw_name', name)
        object.__setattr__(self, '_mw_check', check)

    def __getattr__(self, name):
        value = getattr(self._mw_target, name)
        if name in ACTIONS and callable(value):
            return self._mw_guarded(name)
        if name in VALUE_READS and self._mw_check is not None and callable(value):
            return self._mw_read(name, value)
        return _chained(self, name, value)

    def _mw_read(self, verb, read):
        def call(*args, **kwargs):
            __tracebackhide__ = True  # pytest shows the caller
            key, healed, expect = self._mw_check
            return self._mw_healer._check_value(key, healed, verb, args,
                                                read(*args, **kwargs), expect, False)
        call.__name__ = verb
        return call

    def _mw_step(self):
        return self._mw_name

    def _mw_guarded(self, verb):
        def call(*args, **kwargs):
            action = ActionProxy(self._mw_healer, self._mw_name, self._mw_target,
                                 timeout_ms=_timeout_ms(kwargs))
            action._canary = False     # no heal found this element
            return action._act(verb, args, kwargs, None)
        call.__name__ = verb
        return call

    def __repr__(self):
        return '<GuardedLocator ' + repr(self._mw_name) + '>'


# --- Playwright, async ---------------------------------------------------------------------------

class AsyncHealingLocator(HealingLocator):
    """heal.page.locator(selector) on an AsyncHealer: the healed methods are coroutines."""

    def _mw_healed(self, name):
        async def call(*args, **kwargs):
            healer, selector = self._mw_healer, self._mw_selector
            if name in ANY_STATE and await healer._passes_through(selector, selector):
                return await getattr(self._mw_target, name)(*args, **kwargs)
            proxy = healer.locator(selector, selector, timeout_ms=_timeout_ms(kwargs))
            return await getattr(proxy, name)(*args, **kwargs)
        call.__name__ = name
        return call

    async def _mw_is_visible(self, **kwargs):
        if not self._mw_healer._knows(self._mw_selector):
            return await self._mw_target.is_visible(**kwargs)
        return await self._mw_healer._peek_visible(self._mw_selector)

    async def all(self):
        """HealingLocator.all(), awaited."""
        __tracebackhide__ = True  # pytest shows the caller
        healer, selector = self._mw_healer, self._mw_selector
        items, check = await self._mw_target.all(), None
        if healer._list_routed(selector, len(items)):
            items, healed, expect = await healer._find_all(selector, selector, items)
            check = (selector, healed, expect) if healed and expect else None
        return _items(self, items, check)


class AsyncHealingPage(HealingPage):
    """What heal.page returns on an AsyncHealer: the shortcuts are coroutines."""

    def locator(self, selector, *args, **kwargs):
        target = self._mw_target.locator(selector, *args, **kwargs)
        if args or kwargs or not _healable(selector):
            if not self._mw_healer._dismiss_on:
                return target
            return _guard(self, target, 'page.' + _call_text('locator', (selector,) + args,
                                                             kwargs))
        return AsyncHealingLocator(self._mw_healer, target, selector)

    async def _mw_shortcut(self, name, selector, args, kwargs):
        if not _healable(selector) or (
                name in ANY_STATE and await self._mw_healer._passes_through(selector, selector)):
            return await getattr(self._mw_target, name)(selector, *args, **kwargs)
        kwargs.pop('strict', None)
        proxy = self._mw_healer.locator(selector, selector, timeout_ms=_timeout_ms(kwargs))
        return await getattr(proxy, name)(*args, **kwargs)

    async def is_visible(self, selector, **kwargs):
        if not _healable(selector) or not self._mw_healer._knows(selector):
            return await self._mw_target.is_visible(selector, **kwargs)
        return await self._mw_healer._peek_visible(selector)

    def __repr__(self):
        return '<AsyncHealingPage ' + repr(self._mw_target) + '>'


class AsyncGuardedLocator(GuardedLocator):
    """GuardedLocator on an AsyncHealer: its actions, and its checked reads, are
    coroutines."""

    def _mw_read(self, verb, read):
        async def call(*args, **kwargs):
            __tracebackhide__ = True  # pytest shows the caller
            key, healed, expect = self._mw_check
            return await self._mw_healer._check_value(key, healed, verb, args,
                                                      await read(*args, **kwargs), expect, False)
        call.__name__ = verb
        return call

    def _mw_guarded(self, verb):
        async def call(*args, **kwargs):
            from .aio import _guarded
            action = _guarded(self._mw_healer, self._mw_name, self._mw_target,
                              _timeout_ms(kwargs))
            return await action._act(verb, args, kwargs, None)
        call.__name__ = verb
        return call

    def __repr__(self):
        return '<AsyncGuardedLocator ' + repr(self._mw_name) + '>'


# The guard-only locator each proxy builds (see _guard).
HealingPage._mw_guard_type = HealingLocator._mw_guard_type = GuardedLocator
GuardedLocator._mw_guard_type = GuardedLocator
AsyncHealingPage._mw_guard_type = AsyncHealingLocator._mw_guard_type = AsyncGuardedLocator
AsyncGuardedLocator._mw_guard_type = AsyncGuardedLocator


# --- Selenium ------------------------------------------------------------------------------------

def _css_string(value):
    """value as a double-quoted CSS string."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ch in '\n\r\f':
            out.append('\\' + format(ord(ch), 'x') + ' ')
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def selenium_selector(by, value):
    """The selector mendwright stores for driver.find_element(by, value), or None for a
    By it does not heal (link text, tag name, class name, ...). By.ID and By.NAME become
    attribute selectors, which also work for ids such as ':r0:' that #id cannot spell."""
    if not isinstance(value, str) or not value:
        return None
    if by == BY_XPATH:
        return 'xpath=' + value
    if by == BY_CSS:
        return 'css=' + value
    if by == BY_ID:
        return 'css=[id=' + _css_string(value) + ']'
    if by == BY_NAME:
        return 'css=[name=' + _css_string(value) + ']'
    return None


@functools.lru_cache(maxsize=None)
def _not_found_class(selenium_error):
    """An error class that is both AttentionRequired and selenium's
    NoSuchElementException, so code that catches either still does."""

    class ElementNotFound(AttentionRequired, selenium_error):
        def __init__(self, message):
            AttentionRequired.__init__(self, message)
            self.msg, self.screen, self.stacktrace = message, None, None

        def __str__(self):
            return self.msg

    ElementNotFound.__module__ = __name__
    return ElementNotFound


def not_found_error(exc):
    """exc (an AttentionRequired) as one that is also selenium's NoSuchElementException,
    or exc itself when selenium cannot be imported."""
    try:
        from selenium.common.exceptions import NoSuchElementException
    except Exception:
        return exc
    return _not_found_class(NoSuchElementException)(str(exc))


def _by_key(by, value):
    """The key heal.driver logs an element found with find_element(by, value) under."""
    return str(by) + '=' + str(value)


def _found(healer, element, key, healed=None, expect=None):
    """element, found by the real driver or as one of a list, as a HealingElement whose
    actions handle interruptions without the heal canary. healed and expect are the heal a
    list was found through and the check its elements' reads must pass, which can only
    refuse the heal (see Healer._check_value)."""
    action = ActionProxy(healer, key, element, healed=healed, expect=expect)
    action._canary = False
    return HealingElement(healer, element, action)


class HealingElement(_Proxy):
    """What heal.driver's find_element and find_elements return: the WebElement (or the
    FramedElement inside an iframe), whose click, send_keys, clear and submit run through
    the same guard as heal.locator()'s actions (ActionProxy). Registered interruptions the
    page shows over the element are handled first, and an action that failed because
    something covered the element is retried once, only after one was handled; otherwise
    its own error is raised. With open(dismiss=True), a popup nobody registered that
    covers the element is dismissed first too (see mendwright.dismiss). With no
    interruption registered and dismiss off, an action costs no browser call more than the
    element's own; with dismiss=True, one more, to look for a popup. ActionChains actions
    on it are Selenium's own.

    It passes isinstance(el, WebElement), compares and hashes equal to the element it
    wraps, and works with execute_script, ActionChains and expected_conditions. One that
    wraps a FramedElement does not, as a FramedElement does not: use its in_frame().
    find_element and find_elements on it return elements wrapped the same way. Everything
    else is the element's own; .native is the element."""

    def __init__(self, healer, target, action):
        super().__init__(healer, target)
        object.__setattr__(self, '_mw_action', action)

    def click(self):
        return self._mw_action._act('click', (), {}, None)

    def send_keys(self, *value):
        return self._mw_action._act('send_keys', value, {}, None)

    def clear(self):
        return self._mw_action._act('clear', (), {}, None)

    def submit(self):
        return self._mw_action._act('submit', (), {}, None)

    # The reads expect_value= checks for an element heal.locator() found through a heal, or
    # one of a list found through a heal (see ActionProxy.__getattr__). Any other element's
    # are its own.

    @property
    def text(self):
        return self._mw_action.text

    def get_attribute(self, name):
        return self._mw_action.get_attribute(name)

    def get_dom_attribute(self, name):
        return self._mw_action.get_dom_attribute(name)

    def find_element(self, by=BY_ID, value=None):
        return _found(self._mw_healer, self._mw_target.find_element(by, value),
                      _by_key(by, value))

    def find_elements(self, by=BY_ID, value=None):
        healer, key = self._mw_healer, _by_key(by, value)
        return [_found(healer, el, key) for el in self._mw_target.find_elements(by, value)]

    def __eq__(self, other):
        return self._mw_target == unwrap(other)

    def __ne__(self, other):
        return self._mw_target != unwrap(other)

    def __hash__(self):
        return hash(self._mw_target)

    def __repr__(self):
        return '<HealingElement ' + repr(self._mw_action.key) + ' ' + repr(self._mw_target) + '>'


class HealingDriver(_Proxy):
    """What heal.driver returns for a Selenium WebDriver: see the module docstring."""

    def find_element(self, by=BY_ID, value=None):
        """driver.find_element(by, value), healed for By.XPATH, By.CSS_SELECTOR, By.ID and
        By.NAME: it waits up to the Healer's timeout_ms, heals a selector that broke, and
        returns the element as a HealingElement (see above), wrapping a FramedElement
        inside an iframe. When the element cannot be found or healed it raises
        AttentionRequired, which is also a selenium NoSuchElementException, so except
        clauses and WebDriverWait that expect one still work. Any other By, and a selector
        that matches a hidden element or several, goes to the real driver, and its element
        comes back wrapped the same way."""
        selector = selenium_selector(by, value)
        key = _by_key(by, value)
        if (selector is None or not _healable(selector)
                or self._mw_healer._passes_through(key, selector)):
            return _found(self._mw_healer, self._mw_target.find_element(by, value), key)
        __tracebackhide__ = True
        try:
            action = self._mw_healer.locator(key, selector)
        except AttentionRequired as exc:
            if _raised_by_run(exc):
                raise   # from a run= function of heal.interrupt(): raised as it is
            raise not_found_error(exc) from exc
        if not action.present:
            return None     # a key registered with heal.interrupt(), and not on the page
        return HealingElement(self._mw_healer, action.native, action)

    def find_elements(self, by=BY_ID, value=None):
        """The real driver's find_elements(by, value), with each element wrapped as a
        HealingElement. For By.XPATH, By.CSS_SELECTOR, By.ID and By.NAME, a list that
        matches nothing, after it matched two or more elements before, is healed as
        heal.locator_all() heals one (see mendwright.lists): mendwright waits up to the
        Healer's timeout_ms, and heals it or raises AttentionRequired, which is also a
        selenium NoSuchElementException, so WebDriverWait goes on polling. One that matches
        two or more is recorded for that while the fingerprint tier is on, and its elements
        are the ones the real call found. One that matches nothing and never matched two
        comes back empty at once, as before, and so does any list while the fingerprint tier
        is off and no saved list heal matches."""
        healer, key = self._mw_healer, _by_key(by, value)
        elements = self._mw_target.find_elements(by, value)
        selector = selenium_selector(by, value)
        if (selector is None or not _healable(selector)
                or not healer._list_routed(key, len(elements))):
            return [_found(healer, el, key) for el in elements]
        __tracebackhide__ = True
        try:
            elements, healed, expect = healer._find_all(key, selector, elements)
        except AttentionRequired as exc:
            if _raised_by_run(exc):
                raise   # from a run= function of heal.interrupt(): raised as it is
            raise not_found_error(exc) from exc
        return [_found(healer, el, key, healed, expect) for el in elements]

    def __repr__(self):
        return '<HealingDriver ' + repr(self._mw_target) + '>'


_PROXY_TYPES = (HealingPage, AsyncHealingPage, HealingLocator, AsyncHealingLocator, HealingDriver,
                HealingElement, GuardedLocator, AsyncGuardedLocator)

__all__ = ['HealingPage', 'HealingLocator', 'AsyncHealingPage', 'AsyncHealingLocator',
           'HealingDriver', 'HealingElement', 'GuardedLocator', 'AsyncGuardedLocator',
           'selenium_selector', 'not_found_error', 'unwrap']
