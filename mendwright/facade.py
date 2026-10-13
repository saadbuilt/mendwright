"""The zero-config facade: mendwright.open(page) gives a Healer, and Healer.locator(...)
resolves a key, heals and retries it when it breaks, and arms the canary, in one call.

It is a layer over the low-level core (resolve, resolve_with_heal, HealCoordinator,
SelectorStore), which keeps working as before. The facade adds the conveniences that need
a live page: the selector in your code wins over stored ones, the role is read from the
element, and actions run through the driver so the same verbs work on Playwright and
Selenium.

It also handles changes in a script's flow (see mendwright.flow): interruptions that can
appear at any point (heal.interrupt(), and the cookies= preset of open()), optional steps
(locator(optional=True)), and a guard that does not heal a key on a page it was never
found on (open(heal_across_pages=...)).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Sequence, Union
from urllib.parse import urlsplit, urlunsplit

from .client import _stored_status, add_heal
from .coordinator import HealCoordinator, resolve_with_heal
from .drivers import (driver_act_on, driver_describe, driver_fingerprint, driver_framed,
                      driver_inventory, wrap)
from .exceptions import AttentionRequired
from .flow import (MAX_INTERRUPTIONS_PER_STEP, SETTLE_MS, InterruptionLoop, absent_message,
                   handled_message, is_interception_error, journal_event, loop_message,
                   nothing_matched_message, page_path, skip_message, unhandled_message)
from .gate import live_reason
from .interruptions import (COOKIE_CHOICES, Budget, InterruptionEvent, cookie_choice_error,
                            cookie_preset, make_interruption, nth_targets)
from .journal import Journal
from .limits import Guardrails, RateLimiter
from .providers import GatewayProvider
from .resolver import resolve
from .roles import infer_role, role_choices
from .selectors import CSS, XPATH, InvalidSelector, check_syntax, classify, normalize, strip_prefix
from .store import load_store

logger = logging.getLogger('mendwright')

# What role= takes: the gate's own words, the ARIA names it maps, or any HTML tag name.
Role = Union[Literal['button', 'checkbox', 'radio', 'link', 'input', 'combobox', 'textbox',
                     'searchbox', 'spinbutton', 'slider', 'listbox', 'switch', 'textarea'],
             str]
# What locator() takes as its selector: one selector string, or several to try in order.
Selector = Union[str, Sequence[str]]
# expect= on an action: a callable that returns True when the action did what it should.
Expect = Callable[[], Any]


class _NullProvider:
    """Used when no provider is configured: resolution still works, and only the
    fingerprint tier (when it is on) can heal; the provider itself never proposes.

    It advertises an actionable ``unavailable_reason`` that the coordinator surfaces on an
    AttentionRequired, so a broken selector with no model yields "configure a provider"
    (with the exact next step) instead of a bare "no matching element"."""

    _DEFAULT = (
        'no model provider is configured, so only fingerprint healing (no model) can repair a '
        'broken selector. Set a provider key and model (for example ANTHROPIC_API_KEY and '
        'ANTHROPIC_MODEL), or pass provider=... to mendwright.open(). Run mendwright doctor '
        'to see what it finds.')

    def __init__(self, reason=None, summary=None):
        self.unavailable_reason = reason or self._DEFAULT
        # A shorter reason for the per-heal log line, when the full one is logged once by
        # open(). The AttentionRequired always carries the full reason.
        self.unavailable_summary = summary

    def propose(self, bundle):
        return None

    def report_outcome(self, key, xpath, success):
        pass


# Every Healer that is still referenced somewhere, for tools such as a pytest plugin that
# confirm or report heals at the end of a test. See live_healers().
_HEALERS: weakref.WeakSet = weakref.WeakSet()


# Callbacks run for every new Healer. See add_healer_listener().
_LISTENERS: list = []


def live_healers() -> list:
    """The Healers made by open() that are still in use, in no particular order."""
    return list(_HEALERS)


def add_healer_listener(callback: Callable[[Any], Any]) -> None:
    """Call ``callback(healer)`` for every Healer (sync or async) created from now on.

    Meant for test-runner integrations such as the pytest plugin, which uses it to find
    the heals a test made. Remove it with remove_healer_listener(). A callback that raises
    is logged and ignored."""
    _LISTENERS.append(callback)


def remove_healer_listener(callback: Callable[[Any], Any]) -> None:
    """Stop calling ``callback``. Removing one that was never added does nothing."""
    try:
        _LISTENERS.remove(callback)
    except ValueError:
        pass


def _register(healer) -> None:
    _HEALERS.add(healer)
    for callback in list(_LISTENERS):
        try:
            callback(healer)
        except Exception:
            logger.warning('mendwright: a healer listener raised; ignoring it', exc_info=True)


def _unregister(healer) -> None:
    _HEALERS.discard(healer)


@dataclass(frozen=True)
class HealEvent:
    """One heal a Healer applied: the key, the selectors that had stopped matching, the
    new xpath, the canary result (None while unconfirmed, then True or False), and where
    the new xpath came from: 'fingerprint' (no model call), 'model', 'intent' (a key
    that had no selector, found by a model from its intent=), or 'frame' (the key's own
    selector, which matched only inside an iframe and is looked up there now)."""
    key: str
    old: tuple
    new: str
    confirmed: bool | None = None
    source: str = 'model'


DEFAULT_STORE_PATH = './.mendwright/overrides.json'
DEFAULT_TIMEOUT_MS = 10000

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('0', 'false', 'no', 'off', '')

# One rate limiter per (store file, caps) for the whole process, so opening several
# Healers on one store does not multiply the hourly budget.
_LIMITERS: dict = {}
_LIMITERS_LOCK = threading.Lock()


def _env_flag(name):
    """True/False from an env var such as MENDWRIGHT_DRY_RUN=1, or None when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError('mendwright: ' + name + ' must be 1 or 0, got ' + repr(raw) + '. '
                     'Set it to 1 to turn it on, or unset it.')


def fingerprint_setting(fingerprint=None):
    """Whether the fingerprint tier is on: the fingerprint= argument when it is not None,
    else MENDWRIGHT_FINGERPRINT (1 or 0), else on. MENDWRIGHT_DISABLE=1 turns it off with
    every other heal. Raises ValueError for a MENDWRIGHT_FINGERPRINT that is not a flag."""
    if _env_flag('MENDWRIGHT_DISABLE'):
        return False
    if fingerprint is not None:
        return bool(fingerprint)
    env = _env_flag('MENDWRIGHT_FINGERPRINT')
    return True if env is None else env


def _env_timeout():
    raw = os.environ.get('MENDWRIGHT_TIMEOUT_MS')
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError('mendwright: MENDWRIGHT_TIMEOUT_MS must be a whole number of '
                         'milliseconds, got ' + repr(raw) + '. Fix it, or pass timeout_ms= '
                         'to mendwright.open().')
    return value


def _shared_limiter(store_path, per_key, per_hour):
    key = (os.path.normcase(os.path.abspath(store_path)), per_key, per_hour)
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = RateLimiter(per_key_per_hour=per_key, global_per_hour=per_hour)
            _LIMITERS[key] = limiter
        return limiter


def _outer_html(driver):
    try:
        return driver.outer_html()
    except Exception:
        return ''


def open(page: Any, *, provider: Any = None, store: Any = None,
         store_path: str | os.PathLike | None = None, guard: Callable[[], bool] | None = None,
         dry_run: bool | None = None, destructive_keys: Iterable[str] = frozenset(),
         guardrails: Guardrails | None = None, journal: Any = None,
         rate_per_key_per_hour: int | None = 3, rate_global_per_hour: int | None = 60,
         timeout_ms: int | None = None, cookies: str | None = None,
         heal_across_pages: bool = False, fingerprint: bool | None = None) -> Healer:
    """Wrap a live page or driver into a Healer.

    - The driver is detected from the object (sync Playwright, camoufox, patchright,
      Selenium, or one added with register_driver).
    - provider defaults to auto_provider(), which reads the environment. If none can be
      built, resolution still works, only fingerprint heals happen (no model), other
      heals are declined with the reason, and one WARNING is logged.
    - fingerprint (default on, or MENDWRIGHT_FINGERPRINT) is the first tier: each time a
      key's selector matches, mendwright records what the element looks like, and when the
      selector breaks it first looks for that element on the page, with no model call. A
      confident match goes through the same checks as a model's proposal. Anything else
      goes on to the provider. fingerprint=False turns it off.
    - store defaults to a JSON override file at store_path (no seed needed).
    - guard is a callable returning False while healing must wait (default: always True).
    - timeout_ms is how long locator() polls before treating a selector as broken.
    - cookies='reject' or cookies='accept' registers an interruption (see
      Healer.interrupt) for the consent banners of common consent managers, under the
      key 'cookie_banner'. None (the default) clicks nothing you did not ask for.
    - heal_across_pages=True lets a key heal on a page where its element was never found.
      By default such a heal is declined before any model call, because it looks like a
      step that moved to another page rather than a renamed element.

    Settings an argument leaves at None come from the environment:
    MENDWRIGHT_STORE (store_path, default ./.mendwright/overrides.json),
    MENDWRIGHT_TIMEOUT_MS (timeout_ms, default 10000) and MENDWRIGHT_DRY_RUN=1 (dry_run).
    MENDWRIGHT_DISABLE=1 turns healing off: selectors still resolve, nothing is healed and
    no provider is called, even one passed as provider=.

    Heals are written to journal.jsonl next to the store. A direct in-process provider
    (not GatewayProvider, which meters server-side) also gets an hourly rate limit, shared
    by every Healer on the same store file in this process.
    """
    from .proxies import unwrap
    page = unwrap(page)    # heal.page or heal.driver of another Healer: use the real one
    preset = _cookie_preset(cookies, 'mendwright.open()')
    driver = wrap(page)
    store, coord, timeout_ms = _setup(
        driver, provider=provider, store=store, store_path=store_path, guard=guard,
        dry_run=dry_run, destructive_keys=destructive_keys, guardrails=guardrails,
        journal=journal, rate_per_key_per_hour=rate_per_key_per_hour,
        rate_global_per_hour=rate_global_per_hour, timeout_ms=timeout_ms,
        heal_across_pages=heal_across_pages, fingerprint=fingerprint)
    healer = Healer(page, driver, store, coord, timeout_ms=timeout_ms)
    if preset is not None:
        healer._interruptions[preset.key] = preset
    return healer


def _cookie_preset(cookies, entry):
    """The Interruption for open(cookies=...), or None. Raises ValueError for a value
    other than None, 'reject' and 'accept'."""
    if cookies is None:
        return None
    if not isinstance(cookies, str) or cookies.strip().lower() not in COOKIE_CHOICES:
        raise ValueError(cookie_choice_error(cookies, entry))
    return cookie_preset(cookies.strip().lower())


def _setup(driver, *, provider, store, store_path, guard, dry_run, destructive_keys,
           guardrails, journal, rate_per_key_per_hour, rate_global_per_hour, timeout_ms,
           entry='mendwright.open()', heal_across_pages=False, fingerprint=None):
    """Everything open() does once the driver is known: settings from the arguments and the
    environment, the provider, the store, the journal, the rate limit, and a
    HealCoordinator whose live gate runs through ``driver``. Returns
    (store, coordinator, timeout_ms). mendwright.aio.open() shares it."""
    if store_path is None:
        store_path = os.environ.get('MENDWRIGHT_STORE') or DEFAULT_STORE_PATH
    store_path = os.path.expanduser(os.fspath(store_path))
    if timeout_ms is None:
        timeout_ms = _env_timeout()
    if timeout_ms is None:
        timeout_ms = DEFAULT_TIMEOUT_MS
    if dry_run is None:
        dry_run = bool(_env_flag('MENDWRIGHT_DRY_RUN'))
    fingerprint = fingerprint_setting(fingerprint)

    if _env_flag('MENDWRIGHT_DISABLE'):
        provider = _NullProvider(
            'healing is turned off by MENDWRIGHT_DISABLE=1, so the selector was not repaired. '
            'Unset MENDWRIGHT_DISABLE to let mendwright heal again.')
    elif provider is None:
        try:
            from .providers import auto_provider
            provider = auto_provider()
        except Exception as exc:
            # Keep resolution working, and carry the actual diagnosis so a later miss
            # explains the real fix: auto_provider()'s error already names the missing
            # piece (for example ANTHROPIC_MODEL when the key is set but the model is not).
            reason = str(exc).strip() or (
                'no model provider is configured (set a provider key such as '
                'ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY)')
            if 'provider=' not in reason:
                reason = reason.rstrip('.') + '. Alternatively, pass provider=... to mendwright.open().'
            if fingerprint:
                logger.warning(entry + ': no model provider is available, so a broken '
                               'selector is repaired only when its fingerprint finds the '
                               'element with confidence (no model call). %s', reason)
            else:
                logger.warning(entry + ': no model provider is available, so a broken '
                               'selector will not be repaired. %s', reason)
            provider = _NullProvider(reason, summary=(
                'no model provider is configured (see the ' + entry + ' warning, or run '
                'mendwright doctor)'))

    if store is None:
        # No seed, plus any previously-healed overrides at store_path.
        store = load_store(None, store_path, destructive_keys)
    store_file = os.fspath(getattr(store, '_override_path', None) or store_path)

    if journal is None:
        journal = Journal(os.path.join(os.path.dirname(store_file) or '.', 'journal.jsonl'))
    if guardrails is None and not isinstance(provider, (GatewayProvider, _NullProvider)):
        # No gateway in front of a direct provider, so meter it here.
        guardrails = Guardrails(_shared_limiter(store_file, rate_per_key_per_hour,
                                                rate_global_per_hour))

    coord = HealCoordinator(
        store, provider=provider, driver=driver,     # driver=> the LIVE gate runs pre-commit
        html_provider=lambda: _outer_html(driver),
        inventory_provider=lambda: driver_inventory(driver),
        guard=guard or (lambda: True), dry_run=dry_run,
        guardrails=guardrails, journal=journal, heal_across_pages=heal_across_pages,
        entry=entry, fingerprint=fingerprint)
    # MENDWRIGHT_DISABLE=1 swaps in a provider that declines, which the coordinator's heal
    # path reads; the flag also stops a key being followed into an iframe.
    coord.disabled = _env_flag('MENDWRIGHT_DISABLE')
    return store, coord, timeout_ms


def _healed_target_error(key, xpath, locked):
    """The AttentionRequired for a stored heal on a key that is marked destructive."""
    fix = ('Fix the selector in your code or seed file.' if locked else
           'Fix the selector in your code, or pass destructive=False to heal.locator() if '
           'this control is safe to act on.')
    return AttentionRequired(
        "mendwright: '" + key + "' matched " + xpath + ', a selector an earlier heal saved, '
        "but '" + key + "' is marked as a destructive control, and a destructive control "
        'never acts through a healed selector. mendwright quarantined that selector and did '
        'not act on it. ' + fix)


def _bare_url(url):
    """A frame URL without user info, query or fragment, for a message."""
    try:
        parts = urlsplit(url)
        netloc = (parts.hostname or '') + (':' + str(parts.port) if parts.port else '')
        return urlunsplit((parts.scheme, netloc, parts.path, '', ''))
    except Exception:
        return ''


def _frame_message(key, selector, frames, opener, why=None, selenium=False):
    """Why a key whose selector matches only inside an iframe was not followed there."""
    frame = frames[0]
    name, url = frame.get('name') or '', _bare_url(frame.get('url') or '')
    if selenium:
        found = ('driver.find_element(By.CSS_SELECTOR, ' + repr(frame['selector']) + ')'
                 if frame.get('selector') else 'the iframe element')
        where = ('the iframe named ' + repr(name) if name else
                 'the iframe at ' + url if url else 'an iframe')
        if len(frames) > 1:
            where += ' (and ' + str(len(frames) - 1) + ' more)'
        because = why or 'nothing tells that iframe apart from the others on the page'
        return ("'" + key + "' is inside an iframe: " + selector + ' matches nothing in the '
                'page mendwright was opened on, but matches inside ' + where + '. mendwright '
                'did not look for it there, because ' + because + ', and did not ask a model '
                'to heal it. Switch into it and open mendwright there instead: '
                'driver.switch_to.frame(' + found + '), then heal = mendwright.open(driver).')
    if name:
        where, how = 'the iframe named ' + repr(name), 'page.frame(name=' + repr(name) + ')'
    elif url:
        where, how = 'the iframe at ' + url, 'page.frame(url=' + repr(url) + ')'
    else:
        where, how = 'an iframe', 'the Frame from page.frames'
    if len(frames) > 1:
        where += ' (and ' + str(len(frames) - 1) + ' more)'
    because = why or 'nothing tells that iframe apart from the others on the page'
    return ("'" + key + "' is inside an iframe: " + selector + ' matches nothing in the page '
            'mendwright was opened on, but matches inside ' + where + '. mendwright did not '
            'look for it there, because ' + because + ', and did not ask a model to heal '
            'it. Open mendwright on that Frame instead: heal = ' + opener + '(' + how + ').')


def frame_spelling(selector):
    """The key's selector as it is saved for a lookup inside an iframe: with its 'xpath=' or
    'css=' prefix written out, so the store keeps it apart from the same selector in the page
    (the store tells candidates apart by their text). None for a Playwright engine selector."""
    try:
        kind = classify(selector)
    except TypeError:
        return None
    if kind not in (XPATH, CSS):
        return None
    body = strip_prefix(selector)
    alt = ('xpath=' if kind == XPATH else 'css=') + body
    if alt == normalize(selector):
        if kind != XPATH:
            return None
        alt = 'xpath=(' + body + ')'
    return alt


def latest_success(store, key):
    """The key's active candidate that matched most recently, in any run, or None."""
    try:
        found = [c for c in store.candidates(key) if c.get('lastSucceededAt')]
    except Exception:
        return None
    return max(found, key=lambda c: str(c['lastSucceededAt'])) if found else None


_NOT_FOLLOWED = object()


def _locator_args(key, selector, role, inline_xpath, intent=None):
    """locator()'s argument checks, which touch neither the page nor the store: returns
    (roles, code), the accepted roles for role= and the selectors from _code_selectors().
    Raises TypeError for an intent= that is not a non-empty string."""
    if intent is not None and (not isinstance(intent, str) or not intent.strip()):
        raise TypeError('mendwright: intent= for ' + repr(key) + ' takes a description of the '
                        'element in words, got ' + repr(intent) + ". For example, intent='the "
                        "terms checkbox in the signup form'.")
    if inline_xpath is not None:
        if selector is not None:
            raise TypeError('mendwright: locator() got both selector and inline_xpath. '
                            'Pass the selector once, as selector=.')
        selector = inline_xpath
    roles = role_choices(role)
    return roles, _code_selectors(key, selector)


def _code_selectors(key, selector):
    """The selector(s) passed to locator() as a list of stored spellings, or None.

    Each is checked with mendwright.selectors.check_syntax(): a non-string (for example a
    Playwright Locator) raises TypeError, an empty or malformed one InvalidSelector."""
    if selector is None:
        return None
    items = list(selector) if isinstance(selector, (list, tuple)) else [selector]
    if not items:
        raise ValueError('mendwright: locator(' + repr(key) + ', []) got an empty list. '
                         "Pass at least one selector, for example ['#save', \"//button[@id='save']\"].")
    out = []
    for item in items:
        try:
            check_syntax(item)
        except InvalidSelector as exc:
            raise exc.for_key(key) from None
        out.append(normalize(item))
    return list(dict.fromkeys(out))


class Healer:
    """Resolves selector keys on one page, heals them when they break, and hands back an
    ActionProxy. Made by mendwright.open()."""

    def __init__(self, page: Any, driver: Any, store: Any, coordinator: HealCoordinator, *,
                 timeout_ms: int = 10000) -> None:
        self._page = page
        self._driver = driver
        self.store = store
        self.coordinator = coordinator
        self.timeout_ms = timeout_ms
        self._role_checked: set = set()
        # Interruptions by key, in the order they were registered (see interrupt()).
        self._interruptions: dict = {}
        # key -> the step an interruption was first handled before, in this process.
        self._handled: dict = {}
        self._interrupt_log: list = []
        # (key, xpath) pairs whose fingerprint this Healer has read already.
        self._fingerprinted: set = set()
        self._proxies: dict = {}
        _register(self)

    @property
    def page(self) -> Any:
        """The page to use in place of your own, so an existing script heals by changing
        one line: page = mendwright.open(page).page.

        For a Playwright page or frame it is a HealingPage: page.locator(selector) and the
        page shortcuts that take a selector (click, dblclick, fill, check, uncheck,
        select_option, press, type, hover, is_checked, text_content, inner_text,
        get_attribute) go through heal.locator(selector, selector), so the selector is the
        key its heals are saved under. The reads (get_attribute, text_content, ...),
        set_input_files and focus work on hidden elements in Playwright, so for those a
        selector that matches a hidden element or several, with no saved heal that finds
        the element, goes to the real page as written; only one that matches nothing is
        healed. is_visible(selector) answers at once and never heals. Everything else, and
        a locator built with options or chained from another one, is the real page's, not
        healed. For any other page or driver it is the object mendwright was opened on."""
        if 'page' not in self._proxies:
            from .drivers.playwright_sync import PlaywrightSyncDriver
            from .proxies import HealingPage
            healing = isinstance(self._driver, PlaywrightSyncDriver)
            self._proxies['page'] = HealingPage(self, self._page) if healing else self._page
        return self._proxies['page']

    @property
    def driver(self) -> Any:
        """For a Selenium WebDriver, the driver to use in place of your own:
        driver = mendwright.open(driver).driver. Its find_element(by, value) goes through
        heal.locator(f'{by}={value}', ...) for By.XPATH, By.CSS_SELECTOR, By.ID and By.NAME
        and returns the WebElement, healing only a selector that matches nothing (a hidden
        element, or the first of several, comes from the real driver as before);
        everything else is the real driver's. For any other
        page it is mendwright's own Driver for it, as before."""
        if 'driver' not in self._proxies:
            from .drivers.selenium import SeleniumDriver
            from .proxies import HealingDriver
            healing = isinstance(self._driver, SeleniumDriver)
            self._proxies['driver'] = HealingDriver(self, self._page) if healing else self._driver
        return self._proxies['driver']

    def _passes_through(self, key, selector):
        """For heal.driver's find_element and heal.page's reads: True when the call should
        go to the real page or driver as written. That is when selector matches something
        on the page now, but not the one visible element mendwright looks for, and none of
        key's saved selectors finds one either. Playwright's reads (get_attribute,
        text_content, ...) work on a hidden element, and Selenium's find_element takes the
        first of several matches, where mendwright would wait and then try to heal."""
        driver = self._driver
        try:
            count = driver.count(selector)
        except Exception:
            return False        # heal.locator() raises InvalidSelector for a bad one
        if not count:
            return False
        if count == 1:
            try:
                if driver.is_visible(selector):
                    return False
            except Exception:
                return False
        try:
            return not self.present(key)
        except Exception:
            return True

    def _knows(self, key):
        """True when the store has an active selector for key."""
        try:
            return bool(self.store.candidates(key))
        except Exception:
            return False

    def _peek_visible(self, key):
        """For heal.page's is_visible(): True when the first of key's stored selectors that
        matches one element shows it visible right now. Like Playwright's is_visible(): no
        wait, no heal, no scrolling, and nothing recorded in the store."""
        from .resolver import _match_handle, _require_visible, _split_runnable
        require_visible = _require_visible(self.store, key)
        runnable, _errors = _split_runnable(key, self.store.candidates(key))
        for candidate in runnable:
            try:
                native = _match_handle(self._driver, candidate, require_visible)
            except Exception:
                continue
            if native is not None:
                try:
                    return bool(driver_act_on(self._driver, native, 'is_visible'))
                except Exception:
                    return False
        return False

    def interrupt(self, key: str, selector: Selector, *, action: str = 'click',
                  args: Sequence[Any] = (), role: Role | None = None) -> None:
        """Register a step that may appear at any point of the flow: a cookie banner, a
        newsletter modal, an "are you still there?" dialog.

        selector finds the element to act on (one selector, or a list tried in order), and
        action with args is what to do with it (a click by default; for example
        action='press', args=('Escape',)). role=, when given, is the role the element
        must have before mendwright acts on it. Use a selector that matches one element:
        an XPath that matches several is narrowed to the first visible match, and any
        other kind that matches several is skipped.

        mendwright looks for it only when a step needs help: when a locator() misses
        (before any heal or model call), before an action while the page shows it, and when
        an action fails because something covered the element. If the element is there and
        visible, mendwright performs the action, logs one WARNING line ("handled
        interruption 'cookies' before 'email'"), writes a flow.interrupt journal event and
        retries the step. A failed action is retried once, and only after an interruption
        was handled. At most 3 interruptions are handled for one step.

        An interruption is never healed by a model. A later heal.locator(key, ...) for the
        same key is skipped (no heal, no error, its actions do nothing) when the element is
        not there: it was handled earlier, or it did not appear this time. Registering a
        key again replaces it. Raises TypeError, ValueError or InvalidSelector for
        arguments that can never work."""
        item = make_interruption(key, selector, action=action, args=args, role=role,
                                 actions=ACTIONS)
        self._interruptions.pop(item.key, None)
        self._interruptions[item.key] = item

    @property
    def handled_interruptions(self) -> list:
        """The interruptions this Healer handled, oldest first, as
        InterruptionEvent(key, before): before is the step it was handled for."""
        return list(self._interrupt_log)

    def locator(self, key: str, selector: Selector | None = None, *, role: Role | None = None,
                text: str | Sequence[str] | None = None, destructive: bool | None = None,
                timeout_ms: int | None = None, poll_ms: int = 200,
                inline_xpath: Selector | None = None, optional: bool = False,
                intent: str | None = None) -> ActionProxy:
        """Resolve `key`, healing + retrying inline if its candidates miss, and return an
        ActionProxy wrapping the native locator. timeout_ms bounds how long each resolution
        polls for the element before it's treated as broken (and a heal is attempted).

        selector is what your code says the key is: an XPath, CSS or Playwright selector
        string, or a list of them to try in order before any model is asked. Your code
        wins: these are tried before anything healed earlier, and when you change them,
        the old ones are retired (kept in the store with status 'superseded'). Heals stay
        as fallbacks. Leave selector out to use only what the store already has.

        role is the kind of element the key points at, in the gate's words (button,
        checkbox, radio, link, input, combobox, or a tag name such as textarea), or an
        ARIA alias such as textbox or switch. When it is omitted, mendwright reads it
        from the element the first time the selector matches, and saves it.

        text is the element's expected visible text (a string or a list of accepted ones).

        destructive=True marks a control mendwright must never heal, such as a pay, submit
        or delete button. It is saved, so later runs keep it. destructive=False clears a
        flag saved that way. None leaves the saved value alone. A key is also destructive
        when the seed or open(destructive_keys=...) says so, which nothing can undo.
        mendwright does not guess this from the key or the element's text: mark each
        control that must not be healed.

        optional=True marks a step that may not be there, such as a promotion banner. If
        its element does not appear within the timeout, it is not healed: you get a proxy
        whose .present is False and whose actions do nothing and return None, and one INFO
        line is logged. An optional step is never healed.

        A key registered with interrupt() is never healed either, and is skipped the same
        way when its element is not there. Registered interruptions are handled before
        this step when its element is missing or covered (see interrupt()).

        intent is what the element is for, in words, such as 'the terms checkbox in the
        signup form'. It is saved with the key and the model gets it when it heals the key.
        With intent= and role= but no selector, and nothing stored for the key yet, the
        model looks for the element from the intent on first use; what it finds passes
        the same checks as a heal, and is saved and used from then on.

        inline_xpath is the old name of selector and still works as a keyword.

        Raises AttentionRequired when the element cannot be found or healed (the message
        says why and what to do), InvalidSelector or TypeError for a selector that can
        never work, and ValueError for an unknown role, or for intent= with nothing to go
        on but no role=.
        """
        __tracebackhide__ = True  # pytest shows the caller
        roles, code = _locator_args(key, selector, role, inline_xpath, intent)
        self._prime(key, roles, code, text, destructive, intent)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        first_wait = self._first_wait(key, timeout)
        store = self.store

        def attempt(wait=timeout):
            return resolve(store, self._page, key, timeout_ms=wait, poll_ms=poll_ms)

        try:
            native = self._resolve_step(key, lambda: attempt(first_wait), Budget(), timeout,
                                        poll_ms)
        except InterruptionLoop:
            raise   # the page is blocked: a heal would not help
        except AttentionRequired as exc:
            skip = self._skip_reason(key, optional)
            if skip:
                self._record_skip(key, skip)
                return ActionProxy(self, key, None, present=False)
            native = self._follow_into_frame(key, exc, attempt)
            if native is _NOT_FOLLOWED:
                native = self._heal(key, exc, attempt)
        self._check_healed_target(key)
        self._learn(key, roles)
        self._remember_page(key)
        target = self._fingerprint_target(key)
        if target is not None:
            self._keep_fingerprint(key, target, driver_fingerprint(
                self._driver, target, self._winner_target(key)[1]))
        return ActionProxy(self, key, native, timeout_ms=timeout, poll_ms=poll_ms)

    def present(self, key: str) -> bool:
        """True if one of key's stored selectors matches a single element right now.
        No waiting and no healing."""
        from .resolver import present
        return present(self.store, self._page, key)

    # --- heals awaiting confirmation (hooks for test runners) ------------------------

    def pending(self) -> dict:
        """{key: healed xpath} for heals whose canary has not reported yet. A copy."""
        return dict(self.coordinator.pending)

    def confirm_all(self, success: bool) -> int:
        """Report every pending heal as confirmed (True) or failed (False, which
        quarantines it), and return how many there were. A test runner can call this
        with the test's outcome."""
        keys = list(self.coordinator.pending)
        for key in keys:
            self.coordinator.confirm_pending(key, bool(success))
        return len(keys)

    @property
    def heals(self) -> list:
        """The heals this Healer applied, oldest first, as HealEvent objects."""
        return [HealEvent(h['key'], tuple(h['old']), h['new'], h['confirmed'],
                          h.get('source', 'model'))
                for h in self.coordinator.heals]

    # --- metadata ------------------------------------------------------------------

    def _prime(self, key, roles, code, text, destructive, intent=None):
        """Store updates locator() makes before it looks at the page: the code's
        selectors go first, and role/text/destructive/intent are applied. Raises
        AttentionRequired when the key has no selector at all and no intent=, and
        ValueError for intent= with no selector and no role (see _check_intent)."""
        __tracebackhide__ = True  # pytest shows the caller
        store = self.store
        if code:
            store.set_code_candidates(key, code)
        elif not store.candidates(key):
            if intent is None:
                raise AttentionRequired(
                    'mendwright: there is no stored selector for "' + key + '" yet, and none '
                    'was passed. Pass a starting selector on the first call, for example '
                    'heal.locator("' + key + '", "//your/xpath", role="checkbox"), or describe '
                    'the element with intent= and role= so a model finds it on first use.')
            self._check_intent(key, code, roles, intent)
        self._apply_meta(key, roles, text, destructive, intent)

    def _check_intent(self, key, code, roles, intent):
        """Raise ValueError when key will be looked for from intent= alone (no selector
        passed, none stored) and mendwright does not know its role, which it needs to check
        what the model finds."""
        if intent is None or code or roles or self.store.candidates(key):
            return
        role = None
        try:
            if not self.store.role_is_guess(key):
                role = self.store.meta(key)['expectedRole']
        except Exception:
            role = None
        if role and role != 'unknown':
            return
        raise ValueError(
            "mendwright: '" + key + "' has no selector yet, so a model will look for it from "
            'intent=, and mendwright needs role= to check what it finds. Pass role=, for '
            "example heal.locator('" + key + "', intent=" + repr(intent) + ", role='button').")

    def _apply_meta(self, key, roles, text, destructive, intent=None):
        store = self.store
        if intent is not None:
            store.set_meta(key, intent=intent)
        if roles:
            current = store.meta(key)['expectedRole']
            if len(roles) == 1:
                if current != roles[0] or store.role_is_guess(key):
                    store.set_meta(key, role=roles[0])
            elif current not in roles or store.role_is_guess(key):
                # An alias with several meanings: the live element picks one later.
                store.guess_role(key, roles[0], replace=True)
        if text is not None or destructive is not None:
            store.set_meta(key, text=text, destructive=destructive)
        if not roles and store.role_is_guess(key):
            top = store.candidates(key)[:1]
            guess = infer_role(top[0]['xpath']) if top else 'unknown'
            if guess != 'unknown':
                store.guess_role(key, guess)

    def _winner_target(self, key):
        """(selector, frame_chain) of the candidate that just matched, or None."""
        winner = self.store.last_success(key)
        if not winner:
            return None
        frames = (winner['frameXPath'],) if winner.get('frameXPath') else ()
        return winner['xpath'], frames

    def _describe_winner(self, key):
        """driver.describe() for the candidate that just matched, or None."""
        target = self._winner_target(key)
        if target is None:
            return None
        try:
            return driver_describe(self._driver, *target)
        except Exception:
            return None

    # --- a stored heal on a key marked destructive -----------------------------------

    def _destructive_heal(self, key):
        """The xpath that just matched for key when it came from a heal and the key is
        marked destructive, else None. Selectors from your code or a seed file are never
        second-guessed."""
        winner = self.store.last_success(key)
        if not winner or winner.get('source') != 'heal':
            return None
        return winner['xpath'] if self.store.meta(key)['destructive'] else None

    def _locked(self, key):
        locked = getattr(self.store, 'is_locked_destructive', None)
        try:
            return bool(callable(locked) and locked(key))
        except Exception:
            return False

    def _check_healed_target(self, key):
        """Refuse a match that came from a heal when the key is marked destructive: the
        heal is quarantined and AttentionRequired is raised, so nothing acts on it. The
        coordinator never heals such a key, so this covers a heal saved before the key was
        marked."""
        __tracebackhide__ = True  # pytest shows the caller
        xpath = self._destructive_heal(key)
        if xpath is None:
            return
        coordinator = self.coordinator
        if coordinator.pending.get(key) == xpath:
            coordinator.confirm_pending(key, False)   # a heal from earlier in this process
        else:
            self.store.quarantine(key, xpath)
            coordinator.journal.quarantine(key, xpath=xpath)
        raise _healed_target_error(key, xpath, self._locked(key))

    def _learn(self, key, roles):
        """After a match, learn from the live element (one describe() at most):
        - with role= omitted, save the role the element has;
        - with a single role= given, warn once if the element disagrees."""
        todo = self._learn_todo(key, roles)
        if todo is None:
            return
        self._learn_from(key, roles, todo, self._describe_winner(key))

    def _learn_todo(self, key, roles):
        """What _learn() has to find out for key, as (need_role, check_role), or None when
        there is nothing to learn (and so no describe() round trip)."""
        user_role = roles[0] if roles and len(roles) == 1 else None
        need_role = user_role is None and self.store.role_is_guess(key)
        check_role = user_role is not None and key not in self._role_checked
        if not (need_role or check_role):
            return None
        return need_role, check_role

    def _learn_from(self, key, roles, todo, described):
        """The store side of _learn(): ``described`` is the matched element's describe()
        result, or None."""
        need_role, check_role = todo
        user_role = roles[0] if roles and len(roles) == 1 else None
        if need_role:
            self._learn_role(key, roles, described)
        if check_role:
            self._role_checked.add(key)
            live = (described or {}).get('role')
            if live and live != user_role:
                logger.warning(
                    'mendwright: role=%r was given for %r, but the element it matched is a %r '
                    "to mendwright's gate, so a heal for it would be rejected. Pass role=%r, or "
                    'leave role out and mendwright reads it from the page.',
                    user_role, key, live, live)

    def _learn_role(self, key, roles, described):
        store = self.store
        if described is None:
            if not callable(getattr(self._driver, 'describe', None)):
                # This driver cannot describe elements: keep the guess from the selector.
                guess = store.meta(key)['expectedRole']
                if guess and guess != 'unknown':
                    store.set_meta(key, role=guess)
            return
        live = described.get('role') or ''
        if roles and live and live not in roles:
            # role= was an alias with several meanings, and the element is none of them.
            # The user's role still wins, as with a single role; say what the gate saw.
            logger.warning(
                "mendwright: role= for %r means one of %s, but the element it matched is a %r "
                "to mendwright's gate, so a heal for it would be rejected. Pass role=%r, or "
                'leave role out and mendwright reads it from the page.',
                key, '/'.join(roles), live, live)
            live = roles[0]
        elif roles and not live:
            live = roles[0]
        if live:
            store.set_meta(key, role=live)

    # --- flow: interruptions, skipped steps and pages ----------------------------------

    def _others(self, before):
        """The registered interruptions other than the step ``before`` itself."""
        return [item for key, item in self._interruptions.items() if key != before]

    def _first_wait(self, key, timeout):
        """How long the first look for key waits: not at all for an interruption that
        was already handled (its element is expected to be gone), else the timeout."""
        return 0 if key in self._interruptions and key in self._handled else timeout

    def _resolve_step(self, key, attempt, budget, timeout, poll_ms):
        """attempt(), and after a miss, handle the registered interruptions and try again
        while one was handled. Raises the last miss when none is left to handle."""
        __tracebackhide__ = True
        try:
            return attempt()
        except AttentionRequired as exc:
            missed = exc
        while self._interrupt(key, budget, timeout, poll_ms):
            try:
                return attempt()
            except AttentionRequired as exc:
                missed = exc
        raise missed

    def _skip_reason(self, key, optional):
        """Why a step whose element is missing is skipped instead of healed, or None."""
        if key in self._interruptions:
            return 'interruption handled' if key in self._handled else 'interruption absent'
        return 'optional' if optional else None

    def _record_skip(self, key, reason):
        self.coordinator._emit(logging.INFO, skip_message(key, reason, self._handled.get(key)))
        journal_event(self.coordinator.journal, 'flow.skip', key, reason=reason)

    def _interrupt(self, before, budget, timeout_ms, poll_ms, driver=None):
        """Handle every registered interruption (other than ``before``) that the page
        shows now. True if at least one was handled. Raises AttentionRequired when the
        step's budget is used up and one is still there. driver is the driver to use (the
        async healer passes its SyncBridge, from a worker thread)."""
        __tracebackhide__ = True
        others = self._others(before)
        if not others:
            return False
        driver = driver or self._driver
        handled = False
        for item in others:
            target = self._interruption_target(item, driver)
            if target is None:
                continue
            if budget.used >= MAX_INTERRUPTIONS_PER_STEP:
                raise InterruptionLoop(loop_message(before, item.key, budget.used))
            budget.used += 1
            if self._run_interruption(item, target, before, timeout_ms, poll_ms, driver):
                budget.handled.append(item.key)
                handled = True
        return handled

    def _interruption_target(self, item, driver):
        """A selector that points at one visible element of the interruption right now
        (and of its role, when it has one), or None. Any error reads as not there."""
        selectors = item.selectors
        if item.probe:
            try:
                if not driver.count(item.probe):
                    selectors = [s for s in selectors if classify(s) != XPATH]
            except Exception:
                pass
        for selector in selectors:
            try:
                count = driver.count(selector)
            except Exception:
                continue
            for target in nth_targets(selector, count):
                try:
                    if driver.is_visible(target) and self._role_fits(item, target, driver):
                        return target
                except Exception:
                    continue
        return None

    def _role_fits(self, item, target, driver):
        if not item.roles:
            return True
        if not callable(getattr(driver, 'describe', None)):
            return True     # the driver cannot read elements: the selector is all we have
        described = driver_describe(driver, target)
        return bool(described) and described.get('role') in item.roles

    def _run_interruption(self, item, target, before, timeout_ms, poll_ms, driver):
        """Perform the interruption's action on target, wait for it to go away, and record
        it. False (with a WARNING) when the action failed in the browser."""
        try:
            native = driver.handle(target)
            driver_act_on(driver, native, item.action, *item.args)
        except _USAGE_ERRORS:
            raise   # a wrong action or arguments in heal.interrupt(): the caller's to fix
        except Exception as exc:
            self.coordinator._emit(logging.WARNING, unhandled_message(item.key, before, exc))
            return False
        self._settle(item, timeout_ms, poll_ms, driver)
        self._handled.setdefault(item.key, before)
        self._interrupt_log.append(InterruptionEvent(item.key, before))
        self.coordinator._emit(logging.WARNING, handled_message(item.key, before))
        journal_event(self.coordinator.journal, 'flow.interrupt', item.key, before=before,
                      action=item.action, preset=item.preset,
                      selector=None if item.preset else target)
        return True

    def _settle(self, item, timeout_ms, poll_ms, driver):
        """Wait, up to SETTLE_MS (or the step's timeout when shorter), for the
        interruption to go away, for example while a banner fades out."""
        limit = min(max(timeout_ms or 0, 0), SETTLE_MS) / 1000.0
        if limit <= 0:
            return
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and self._interruption_target(item, driver) is not None:
            time.sleep(max(poll_ms, 20) / 1000.0)

    def _before_action(self, key, timeout_ms, poll_ms):
        """Handle what the page shows before an action on key. Returns the step's budget,
        which a retry after an interception error shares."""
        __tracebackhide__ = True
        budget = Budget()
        self._interrupt(key, budget, timeout_ms, poll_ms)
        return budget

    def _report_nothing_matched(self, key):
        """After an action on key was intercepted and no registered interruption matched a
        visible element: say so, because the likeliest cause is an interruption whose
        selector no longer matches (its own step, if any, was skipped with an INFO line),
        or one mendwright was never told about."""
        others = self._others(key)
        if others:
            mine = [item.key for item in others if not item.preset]
            preset = any(item.preset for item in others)
            self.coordinator._emit(logging.WARNING, nothing_matched_message(key, mine, preset))

    def _url(self):
        try:
            return self._driver.url() or ''
        except Exception:
            return ''

    def _remember_page(self, key, url=None):
        """Save the page key's element was just found on (see mendwright.flow.page_path)."""
        setter = getattr(self.store, 'set_last_page', None)
        if not callable(setter):
            return
        path = page_path(self._url() if url is None else url)
        if path:
            setter(key, path)

    def _fingerprint_target(self, key):
        """The selector to read key's fingerprint through after a match, or None when there
        is nothing to record: the fingerprint tier is off, it came from a heal made in this
        process that is not confirmed yet, the key is marked destructive, or this Healer
        already read it for that selector. A match inside an iframe is read there."""
        if not getattr(self.coordinator, 'fingerprint', False):
            return None
        winner = self.store.last_success(key)
        if not winner or not isinstance(winner.get('xpath'), str):
            return None
        xpath = winner['xpath']
        if (key, xpath) in self._fingerprinted or self.coordinator.pending.get(key) == xpath:
            return None
        try:
            if self.store.meta(key)['destructive']:
                return None
        except Exception:
            return None
        return xpath

    def _keep_fingerprint(self, key, xpath, fp):
        """Save fp as key's fingerprint (the store writes the file only when it changed)."""
        self._fingerprinted.add((key, xpath))
        if fp is None:
            return
        try:
            self.store.set_meta(key, fingerprint=fp)
        except Exception:
            logger.debug('mendwright: could not save the fingerprint of %r', key, exc_info=True)

    def _flow_context(self, key, url):
        """The heal context: the URL, and the pages the page guard compares."""
        try:
            meta = self.store.meta(key)
        except Exception:
            meta = {}
        return {'url': url, 'page': page_path(url), 'lastPage': meta.get('lastPage'),
                'pages': list(meta.get('pages') or ())}

    # --- healing -------------------------------------------------------------------

    def _frame_candidates(self, key):
        """The selectors of key that are looked up in the page itself (no frameXPath)."""
        return [c['xpath'] for c in self.store.candidates(key)
                if not c.get('frameXPath') and isinstance(c.get('xpath'), str)]

    def _frame_stop(self, key, exc, selector, frames, opener, why=None):
        """Record and raise the AttentionRequired for a key found only inside an iframe
        that mendwright did not follow it into."""
        __tracebackhide__ = True
        selenium = type(self._driver).__name__ == 'SeleniumDriver'
        reason = _frame_message(key, selector, frames, opener, why, selenium)
        self.coordinator._decline(key, reason)
        raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason) from exc

    def _frame_plan(self, key, selector, frames):
        """(iframe selector, selector to save) when key can be followed into the one iframe
        its selector matches in (see _follow_into_frame), else (None, why not)."""
        coord = self.coordinator
        if getattr(coord, 'disabled', False):
            return None, 'healing is turned off by MENDWRIGHT_DISABLE=1'
        if coord.dry_run:
            return None, 'dry-run mode is on'
        try:
            destructive = bool(self.store.meta(key).get('destructive'))
        except Exception:
            destructive = False
        if destructive:
            return None, "'" + key + "' is marked as a destructive control"
        if len(frames) > 1:
            return None, 'it matches in more than one iframe'
        frame = frames[0]
        if frame.get('nested'):
            return None, 'that iframe is inside another iframe'
        if frame.get('count', 1) != 1:
            return None, 'it matches ' + str(frame.get('count')) + ' elements there'
        if not frame.get('selector'):
            return None, None
        alt = frame_spelling(selector)
        if alt is None:
            return None, 'a Playwright engine selector is not followed into an iframe'
        status = _stored_status(self.store, key, alt)
        if status == 'quarantined':
            return None, ('it was looked up in an iframe before and an action on it failed '
                          'there, so that lookup is quarantined')
        if status == 'superseded':
            return None, 'it was looked up in an iframe before and that lookup was superseded'
        return frame['selector'], alt

    def _pick_frame(self, key, exc, matches, opener):
        """From [(selector, frames)], the key's selectors that match inside an iframe, the
        first one that can be followed, as (selector, frames, iframe selector, selector to
        save). Raises the AttentionRequired for the first refusal when none can."""
        __tracebackhide__ = True
        refused = None
        for selector, frames in matches:
            frame, alt = self._frame_plan(key, selector, frames)
            if frame is not None:
                return selector, frames, frame, alt
            if refused is None:
                refused = (selector, frames, alt)
        self._frame_stop(key, exc, refused[0], refused[1], opener, refused[2])

    def _frame_guard(self, key, exc, url):
        """Raise like the page guard when key was never found on this page."""
        __tracebackhide__ = True
        moved = self.coordinator._flow_change(key, self._flow_context(key, url))
        if moved:
            self.coordinator._decline(key, moved)
            raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + moved) from exc

    def _frame_follow(self, key, selector, frame, alt, driver):
        """Check the guard, then ``alt`` inside the iframe ``frame`` on the live page
        (exactly one visible element of the key's role), then save it there and record it.
        Returns '' when the key was followed, else why not."""
        if not self.coordinator.guard():
            return 'a transaction guard is currently blocking heals'
        reason = live_reason(driver, alt, self.store.meta(key), (frame,))
        if reason:
            return "its element there failed mendwright's checks (" + reason + ')'
        if not add_heal(self.store, key, alt, frame_xpath=frame):
            return 'this store cannot save a selector inside an iframe'
        self.coordinator.record_frame(key, selector, alt, frame)
        self.coordinator.pending[key] = alt
        return ''

    def _follow_into_frame(self, key, exc, attempt):
        """After key missed on the page: if one of its own selectors matches one element
        inside one iframe of the page (a driver with frame_matches(), as the built-in ones
        have), look for it there from now on and return the retried step. No model is
        asked: the selector is the key's own, and only the place changed. It counts as a
        heal with source 'frame' (strict mode fails on it, the canary checks it).

        Not followed, with AttentionRequired saying why: dry run, MENDWRIGHT_DISABLE=1 or a
        guard that blocks heals, a key marked destructive, a match in several iframes or
        several elements, an iframe inside another iframe, an iframe nothing tells apart
        from the others, a Playwright engine selector, a lookup quarantined before, a page
        the key was never found on (the page guard), or an element that fails the live
        check. Returns _NOT_FOLLOWED when no selector of the key matches in an iframe."""
        __tracebackhide__ = True
        finder = getattr(self._driver, 'frame_matches', None)
        if not callable(finder):
            return _NOT_FOLLOWED
        matches = []
        for selector in self._frame_candidates(key):
            try:
                frames = finder(selector)
            except Exception:
                continue
            if frames:
                matches.append((selector, frames))
        if not matches:
            return _NOT_FOLLOWED
        selector, frames, frame, alt = self._pick_frame(key, exc, matches, 'mendwright.open')
        self._frame_guard(key, exc, self._url())
        why = self._frame_follow(key, selector, frame, alt, self._driver)
        if why:
            self._frame_stop(key, exc, selector, frames, 'mendwright.open', why)
        return attempt()

    def _heal_scope(self, key):
        """(iframe selector, driver for that iframe) when key was last found inside an
        iframe that is still on the page, so the heal runs there; else None."""
        best = latest_success(self.store, key)
        frame = best.get('frameXPath') if best else None
        if not isinstance(frame, str) or not frame:
            return None
        framed = driver_framed(self._driver, frame)
        return (frame, framed) if framed is not None else None

    def _heal(self, key, exc, attempt):
        """Heal through the coordinator after attempt() raised exc, then retry attempt()."""
        __tracebackhide__ = True  # pytest shows the caller
        pending = [exc]

        def resolve_fn():
            if pending:
                raise pending.pop()
            return attempt()

        context = self._flow_context(key, self._url())
        scope = self._heal_scope(key)
        if scope is None:
            return resolve_with_heal(resolve_fn, self.coordinator, key, self.store.meta(key),
                                     context)
        # The key was last found inside an iframe: capture, check and save the heal there.
        with self.coordinator.scoped(scope[1], scope[0]):
            return resolve_with_heal(resolve_fn, self.coordinator, key, self.store.meta(key),
                                     context)


# Actions a proxy runs through the driver with canary handling: Playwright Locator verbs
# plus Selenium's send_keys and submit. Everything else (inner_text, get_attribute, text,
# is_checked, ...) is a read and falls through to the native object unchanged.
ACTIONS = ('click', 'dblclick', 'fill', 'type', 'press', 'press_sequentially', 'check',
           'uncheck', 'set_checked', 'select_option', 'hover', 'tap', 'clear', 'focus',
           'set_input_files', 'drag_to', 'send_keys', 'submit')

# Errors raised before an action reaches the browser: a verb the element does not have,
# or arguments it does not take. They say nothing about the selector, so they never
# quarantine a heal.
_USAGE_ERRORS = (AttributeError, TypeError, ValueError)


def _unwrap(value):
    return value._native if isinstance(value, ActionProxy) else value


class ActionProxy:
    """The element a Healer resolved, with the heal canary attached.

    Actions (see ACTIONS) run through the driver, so Playwright verbs such as fill() and
    check() also work on Selenium. Keyword arguments pass through (click(timeout=5000)),
    and the return value is whatever the native call returned. If the action itself fails
    in the browser, a selector healed for this step is quarantined. A usage error
    (AttributeError, TypeError, ValueError) is raised as is and quarantines nothing.

    A successful action does not confirm a heal on its own. Pass expect=<callable
    returning bool> to check the result, or call confirm(). Reads such as inner_text()
    or get_attribute() fall through to the native object. Use .native where a library
    wants the real object, for example Playwright's expect(proxy.native).

    Before an action, registered interruptions the page shows are handled (see
    Healer.interrupt). An action that fails because something covered the element is
    retried once, and only after an interruption was handled.

    .present is False for a step that was skipped (an optional step, or an interruption,
    whose element was not there). Its actions then do nothing and return None.
    """

    def __init__(self, healer: Healer, key: str, native: Any, *, present: bool = True,
                 timeout_ms: int | None = None, poll_ms: int = 200) -> None:
        self._healer = healer
        self._key = key
        self._native = native
        self._present = bool(present)
        self._timeout_ms = healer.timeout_ms if timeout_ms is None else timeout_ms
        self._poll_ms = poll_ms

    @property
    def native(self) -> Any:
        """The native object: a Playwright Locator, or a Selenium WebElement (a FramedElement
        inside an iframe). Pass this to APIs that check the type, such as
        playwright.sync_api.expect(proxy.native).to_be_visible().

        A FramedElement switches into its frame itself, so do not call switch_to.frame()
        before using it. It is not a WebElement, so execute_script(..., el) and
        ActionChains reject it: run those through in_frame(), for example
        proxy.native.in_frame(lambda el: driver.execute_script('arguments[0].click()', el)).

        None for a skipped step (.present is False)."""
        return self._native

    @property
    def key(self) -> str:
        return self._key

    @property
    def present(self) -> bool:
        """False when the step was skipped because its element was not there (an
        optional step, or an interruption), else True."""
        return self._present

    def _act(self, verb: str, args: tuple, kwargs: dict, expect: Expect | None) -> Any:
        __tracebackhide__ = True  # pytest shows the caller
        if not self._present:
            return None   # a skipped step: nothing to act on
        args = tuple(_unwrap(a) for a in args)
        kwargs = {k: _unwrap(v) for k, v in kwargs.items()}
        act_on = getattr(self._healer._driver, 'act_on', None)
        if callable(act_on):
            # The built-in drivers raise AttributeError/TypeError/ValueError for a bad verb
            # or argument before anything reaches the browser.
            def run(*a, **kw):
                return act_on(self._native, verb, *a, **kw)
        else:
            run = getattr(self._native, verb)   # AttributeError here quarantines nothing
        healer = self._healer
        coordinator = healer.coordinator
        budget = healer._before_action(self._key, self._timeout_ms, self._poll_ms)
        try:
            result = run(*args, **kwargs)
        except _USAGE_ERRORS:
            raise
        except Exception as exc:
            intercepted, used = is_interception_error(exc), budget.used
            if not (intercepted
                    and healer._interrupt(self._key, budget, self._timeout_ms, self._poll_ms)):
                if intercepted and budget.used == used:
                    healer._report_nothing_matched(self._key)
                coordinator.confirm_pending(self._key, False)  # the browser action failed
                raise
            try:
                result = run(*args, **kwargs)   # once, after an interruption was handled
            except _USAGE_ERRORS:
                raise
            except Exception:
                coordinator.confirm_pending(self._key, False)
                raise
        if expect is not None:
            try:
                ok = bool(expect())
            except Exception:
                ok = False
            coordinator.confirm_pending(self._key, ok)
        return result

    def click(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('click', args, kwargs, expect)

    def dblclick(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('dblclick', args, kwargs, expect)

    def fill(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('fill', args, kwargs, expect)

    def type(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('type', args, kwargs, expect)

    def press(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('press', args, kwargs, expect)

    def press_sequentially(self, *args: Any, expect: Expect | None = None,
                           **kwargs: Any) -> Any:
        return self._act('press_sequentially', args, kwargs, expect)

    def check(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('check', args, kwargs, expect)

    def uncheck(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('uncheck', args, kwargs, expect)

    def set_checked(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('set_checked', args, kwargs, expect)

    def select_option(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('select_option', args, kwargs, expect)

    def hover(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('hover', args, kwargs, expect)

    def tap(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('tap', args, kwargs, expect)

    def clear(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('clear', args, kwargs, expect)

    def focus(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('focus', args, kwargs, expect)

    def set_input_files(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('set_input_files', args, kwargs, expect)

    def drag_to(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        """Drag to another element. The target may be another ActionProxy."""
        return self._act('drag_to', args, kwargs, expect)

    def send_keys(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('send_keys', args, kwargs, expect)

    def submit(self, *args: Any, expect: Expect | None = None, **kwargs: Any) -> Any:
        return self._act('submit', args, kwargs, expect)

    def confirm(self, success: bool) -> ActionProxy:
        """Confirm (True) or quarantine (False) a heal for this key, from a check of your own."""
        self._healer.coordinator.confirm_pending(self._key, bool(success))
        return self

    def __getattr__(self, name: str) -> Any:
        # Reads and anything else not listed in ACTIONS go to the native object unchanged.
        if not self.__dict__.get('_present', True) and not name.startswith('__'):
            raise AttributeError(absent_message(self._key))
        return getattr(self._native, name)

    def __repr__(self) -> str:
        if not self._present:
            return '<ActionProxy key=' + repr(self._key) + ' skipped: not on the page>'
        return '<ActionProxy key=' + repr(self._key) + ' native=' + repr(self._native) + '>'
