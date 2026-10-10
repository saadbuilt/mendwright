"""The zero-config facade: mendwright.open(page) gives a Healer, and Healer.locator(...)
resolves a key, heals and retries it when it breaks, and arms the canary, in one call.

It is a layer over the low-level core (resolve, resolve_with_heal, HealCoordinator,
SelectorStore), which keeps working as before. The facade adds the conveniences that need
a live page: the selector in your code wins over stored ones, the role is read from the
element, pay/submit/confirm-like keys are treated as destructive, and actions run through
the driver so the same verbs work on Playwright and Selenium.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Sequence, Union
from urllib.parse import urlsplit, urlunsplit

from .coordinator import HealCoordinator, resolve_with_heal
from .destructive import (DESTRUCTIVE_PHRASES, DESTRUCTIVE_WORDS,  # noqa: F401  (re-exported)
                          destructive_hit as _destructive_hit, text_words as _text_words)
from .drivers import driver_describe, driver_inventory, wrap
from .drivers.base import described_destructive_label
from .exceptions import AttentionRequired
from .journal import Journal
from .limits import Guardrails, RateLimiter
from .providers import GatewayProvider
from .resolver import resolve
from .roles import infer_role, role_choices
from .selectors import InvalidSelector, check_syntax, normalize
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
    """Used when no provider is configured: resolution still works, but a heal is a no-op.

    It advertises an actionable ``unavailable_reason`` that the coordinator surfaces on an
    AttentionRequired, so a broken selector with no model yields "configure a provider"
    (with the exact next step) instead of a bare "no matching element"."""

    _DEFAULT = (
        'no model provider is configured, so mendwright cannot repair a broken selector. '
        'Set a provider key and model (for example ANTHROPIC_API_KEY and ANTHROPIC_MODEL), '
        'or pass provider=... to mendwright.open(). Run mendwright doctor to see what it finds.')

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
    new xpath, and the canary result (None while unconfirmed, then True or False)."""
    key: str
    old: tuple
    new: str
    confirmed: bool | None = None


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
         timeout_ms: int | None = None) -> Healer:
    """Wrap a live page or driver into a Healer.

    - The driver is detected from the object (sync Playwright, camoufox, patchright,
      Selenium, or one added with register_driver).
    - provider defaults to auto_provider(), which reads the environment. If none can be
      built, resolution still works, heals are declined with the reason, and one WARNING
      is logged.
    - store defaults to a JSON override file at store_path (no seed needed).
    - guard is a callable returning False while healing must wait (default: always True).
    - timeout_ms is how long locator() polls before treating a selector as broken.

    Settings an argument leaves at None come from the environment:
    MENDWRIGHT_STORE (store_path, default ./.mendwright/overrides.json),
    MENDWRIGHT_TIMEOUT_MS (timeout_ms, default 10000) and MENDWRIGHT_DRY_RUN=1 (dry_run).
    MENDWRIGHT_DISABLE=1 turns healing off: selectors still resolve, nothing is healed and
    no provider is called, even one passed as provider=.

    Heals are written to journal.jsonl next to the store. A direct in-process provider
    (not GatewayProvider, which meters server-side) also gets an hourly rate limit, shared
    by every Healer on the same store file in this process.
    """
    driver = wrap(page)
    store, coord, timeout_ms = _setup(
        driver, provider=provider, store=store, store_path=store_path, guard=guard,
        dry_run=dry_run, destructive_keys=destructive_keys, guardrails=guardrails,
        journal=journal, rate_per_key_per_hour=rate_per_key_per_hour,
        rate_global_per_hour=rate_global_per_hour, timeout_ms=timeout_ms)
    return Healer(page, driver, store, coord, timeout_ms=timeout_ms)


def _setup(driver, *, provider, store, store_path, guard, dry_run, destructive_keys,
           guardrails, journal, rate_per_key_per_hour, rate_global_per_hour, timeout_ms,
           entry='mendwright.open()'):
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
        guardrails=guardrails, journal=journal)
    return store, coord, timeout_ms


# DESTRUCTIVE_WORDS and DESTRUCTIVE_PHRASES (from mendwright.destructive) make the facade
# treat a key as destructive unless destructive=False is passed: in the key itself, in
# its expected text, or in the label of the element it matched.

_CAMEL_WORDS = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+')
_INPUT_BUTTON = re.compile(r"""^<input\b[^>]*\btype\s*=\s*["']?(submit|button|reset)\b""",
                           re.IGNORECASE)


def _key_words(key):
    """'placeOrder_btn-2' -> ['place', 'order', 'btn', '2']"""
    words = []
    for part in re.split(r'[_\-.\s]+', str(key)):
        words.extend(w.lower() for w in _CAMEL_WORDS.findall(part))
    return words


def _destructive_reason(key, expected_text):
    """Why the facade treats key as destructive, or None."""
    hit = _destructive_hit(_key_words(key))
    if hit:
        return 'its key contains ' + repr(hit)
    for text in expected_text or ():
        hit = _destructive_hit(text)
        if hit:
            return 'its text ' + repr(text) + ' contains ' + repr(hit)
    return None


def _label_source(described):
    """(text, read_value) for a described element: its visible text, and whether the
    label is instead the value of an <input type=submit|button|reset>, which describe()
    leaves out and the caller must read from the element."""
    text = (described.get('text') or '').strip()
    if text:
        return text, False
    return '', bool(_INPUT_BUTTON.match(described.get('outer_html') or ''))


def _value_label(value):
    """An input button's value attribute as a label: whitespace collapsed, capped."""
    return ' '.join(value.split())[:200] if isinstance(value, str) else ''


def _input_value(described, native):
    """The value attribute of an <input type=submit|button|reset> as a label, read from the
    element because describe() strips value=. '' for anything else."""
    if not _INPUT_BUTTON.match((described or {}).get('outer_html') or ''):
        return ''
    try:
        return _value_label(native.get_attribute('value'))
    except Exception:
        return ''


# describe() not called yet (see Healer._learn).
_UNREAD = object()


def _healed_target_error(key, xpath, why, locked):
    """The AttentionRequired for a stored heal that points at a destructive control."""
    fix = ('Fix the selector in your code or seed file.' if locked else
           'Fix the selector in your code, or pass destructive=False to heal.locator() if '
           'this control is safe to act on.')
    return AttentionRequired(
        "mendwright: '" + key + "' matched " + xpath + ', a selector an earlier heal saved, '
        'but ' + why + '. mendwright quarantined that selector and did not act on it. ' + fix)


def _pick_label(text, described):
    """The label to remember for a described element: the first of its labels that looks
    destructive, else its visible label ``text``. The labels are the text and the
    aria-label, title and image alt text read from describe()'s outer_html, each checked on
    its own: an aria-label replaces the text as the accessible name, so
    <button aria-label="Delete account">x</button> is remembered as 'Delete account'."""
    found = described_destructive_label(described, [text])
    return found[0] if found else text


def _live_label(described, native):
    """The label of a described element: see _pick_label. The visible label is its text,
    or the value of an <input type=submit|button|reset>, which describe() leaves out."""
    text, read_value = _label_source(described)
    if read_value:
        try:
            text = _value_label(native.get_attribute('value'))
        except Exception:
            text = ''
    return _pick_label(text, described)


def _bare_url(url):
    """A frame URL without user info, query or fragment, for a message."""
    try:
        parts = urlsplit(url)
        netloc = (parts.hostname or '') + (':' + str(parts.port) if parts.port else '')
        return urlunsplit((parts.scheme, netloc, parts.path, '', ''))
    except Exception:
        return ''


def _frame_message(key, selector, frames, opener):
    """Why a key whose selector matches only inside an iframe was not healed."""
    frame = frames[0]
    name, url = frame.get('name') or '', _bare_url(frame.get('url') or '')
    if name:
        where, how = 'the iframe named ' + repr(name), 'page.frame(name=' + repr(name) + ')'
    elif url:
        where, how = 'the iframe at ' + url, 'page.frame(url=' + repr(url) + ')'
    else:
        where, how = 'an iframe', 'the Frame from page.frames'
    if len(frames) > 1:
        where += ' (and ' + str(len(frames) - 1) + ' more)'
    return ("'" + key + "' is inside an iframe: " + selector + ' matches nothing in the page '
            'mendwright was opened on, but matches inside ' + where + '. mendwright looks '
            'only in the page or frame it was opened on, so it did not ask a model to heal '
            'it. Open mendwright on that Frame instead: heal = ' + opener + '(' + how + ').')


def _locator_args(key, selector, role, inline_xpath):
    """locator()'s argument checks, which touch neither the page nor the store: returns
    (roles, code), the accepted roles for role= and the selectors from _code_selectors()."""
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
        self.page = page
        self.driver = driver
        self.store = store
        self.coordinator = coordinator
        self.timeout_ms = timeout_ms
        self._role_checked: set = set()
        self._label_checked: set = set()
        _register(self)

    def locator(self, key: str, selector: Selector | None = None, *, role: Role | None = None,
                text: str | Sequence[str] | None = None, destructive: bool | None = None,
                timeout_ms: int | None = None, poll_ms: int = 200,
                inline_xpath: Selector | None = None) -> ActionProxy:
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

        destructive marks a control mendwright must never heal (pay, submit, confirm).
        None leaves the saved value alone. A key is also treated as destructive when the
        seed or open(destructive_keys=...) says so, which nothing can undo, or when a word
        from DESTRUCTIVE_WORDS / DESTRUCTIVE_PHRASES appears in the key, its text or its
        button label. Pass destructive=False to opt a key out of that word check.

        inline_xpath is the old name of selector and still works as a keyword.

        Raises AttentionRequired when the element cannot be found or healed (the message
        says why and what to do), InvalidSelector or TypeError for a selector that can
        never work, and ValueError for an unknown role.
        """
        __tracebackhide__ = True  # pytest shows the caller
        roles, code = _locator_args(key, selector, role, inline_xpath)
        self._prime(key, roles, code, text, destructive)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        store = self.store

        def attempt():
            return resolve(store, self.page, key, timeout_ms=timeout, poll_ms=poll_ms)

        try:
            native = attempt()
        except AttentionRequired as exc:
            self._stop_if_in_a_frame(key, exc)
            native = self._heal(key, exc, attempt)
        described = self._check_healed_target(key, native)
        self._learn(key, roles, native, text_given=text is not None, described=described)
        return ActionProxy(self, key, native)

    def present(self, key: str) -> bool:
        """True if one of key's stored selectors matches a single element right now.
        No waiting and no healing."""
        from .resolver import present
        return present(self.store, self.page, key)

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
        return [HealEvent(h['key'], tuple(h['old']), h['new'], h['confirmed'])
                for h in self.coordinator.heals]

    # --- metadata ------------------------------------------------------------------

    def _prime(self, key, roles, code, text, destructive):
        """Store updates locator() makes before it looks at the page: the code's
        selectors go first, and role/text/destructive are applied. Raises
        AttentionRequired when the key has no selector at all."""
        __tracebackhide__ = True  # pytest shows the caller
        store = self.store
        if code:
            store.set_code_candidates(key, code)
        elif not store.candidates(key):
            raise AttentionRequired(
                'mendwright: there is no stored selector for "' + key + '" yet, and none was '
                'passed. Pass a starting selector on the first call, for example '
                'heal.locator("' + key + '", "//your/xpath", role="checkbox").')
        self._apply_meta(key, roles, text, destructive)

    def _apply_meta(self, key, roles, text, destructive):
        store = self.store
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
            return driver_describe(self.driver, *target)
        except Exception:
            return None

    # --- a stored heal that points at a destructive control ---------------------------

    def _healed_winner(self, key):
        """(xpath, heal meta) when the candidate that just matched came from a heal, else
        None. Selectors from your code or a seed file are never second-guessed."""
        winner = self.store.last_success(key)
        if not winner or winner.get('source') != 'heal':
            return None
        return winner['xpath'], self._heal_meta(key)

    def _healed_target_problem(self, key, meta, described, extra_labels=()):
        """Why a heal-sourced match must not be acted on, or None. A key treated as
        destructive never acts through a healed selector; any other key (unless opted out
        with destructive=False) not on an element whose label looks destructive."""
        if meta['destructive']:
            why = meta.get('destructiveReason')
            if isinstance(why, str) and why:
                return ("'" + key + "' looks like a destructive control (" + why + '), and a '
                        'destructive control never acts through a healed selector')
            return ("'" + key + "' is marked as a destructive control, and a destructive "
                    'control never acts through a healed selector')
        if not meta.get('refuseDestructiveTarget'):
            return None
        found = described_destructive_label(described, extra_labels)
        if found:
            return ('the element looks destructive (its label ' + repr(found[0][:80])
                    + ' contains ' + repr(found[1]) + ')')
        return None

    def _locked(self, key):
        locked = getattr(self.store, 'is_locked_destructive', None)
        try:
            return bool(callable(locked) and locked(key))
        except Exception:
            return False

    def _check_healed_target(self, key, native):
        """Refuse a match that came from a heal and points at a destructive control: the
        heal is quarantined and AttentionRequired is raised, so nothing acts on it. The
        gate refuses such heals, so this covers heals saved before that check (or a page
        that changed since). Returns the describe() result it read, or _UNREAD."""
        __tracebackhide__ = True  # pytest shows the caller
        healed = self._healed_winner(key)
        if healed is None:
            return _UNREAD
        xpath, meta = healed
        described = None
        if not meta['destructive'] and meta.get('refuseDestructiveTarget'):
            described = self._describe_winner(key)
        why = self._healed_target_problem(key, meta, described,
                                          [_input_value(described, native)])
        if why:
            coordinator = self.coordinator
            if coordinator.pending.get(key) == xpath:
                coordinator.confirm_pending(key, False)   # this call's own heal
            else:
                self.store.quarantine(key, xpath)
                coordinator.journal.quarantine(key, xpath=xpath)
            raise _healed_target_error(key, xpath, why, self._locked(key))
        return _UNREAD if described is None else described

    def _learn(self, key, roles, native, *, text_given, described=_UNREAD):
        """After a match, learn from the live element (one describe() at most):
        - with role= omitted, save the role the element has;
        - with a single role= given, warn once if the element disagrees;
        - with no expected text, save the label of a button or link whose label looks
          destructive, so a later heal of this key is refused even when the element can
          no longer be found.
        ``described`` is a describe() result already read for this match, if any."""
        todo = self._learn_todo(key, roles, text_given=text_given)
        if todo is None:
            return
        if described is _UNREAD:
            described = self._describe_winner(key)
        label = _live_label(described, native) if todo[2] and described is not None else ''
        self._learn_from(key, roles, todo, described, label)

    def _learn_todo(self, key, roles, *, text_given):
        """What _learn() has to find out for key, as (need_role, check_role, need_label),
        or None when there is nothing to learn (and so no describe() round trip)."""
        store = self.store
        user_role = roles[0] if roles and len(roles) == 1 else None
        need_role = user_role is None and store.role_is_guess(key)
        check_role = user_role is not None and key not in self._role_checked
        need_label = (not text_given and key not in self._label_checked
                      and not store.meta(key)['expectedText'])
        if not (need_role or check_role or need_label):
            return None
        return need_role, check_role, need_label

    def _learn_from(self, key, roles, todo, described, label):
        """The store side of _learn(): ``described`` is the matched element's describe()
        result (or None) and ``label`` its live label, read only when todo asks for it."""
        need_role, check_role, need_label = todo
        store = self.store
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
        if need_label:
            self._label_checked.add(key)
        if (need_label and described is not None and label
                and described.get('role') in ('button', 'link') and _destructive_hit(label)):
            store.set_meta(key, text=label)

    def _learn_role(self, key, roles, described):
        store = self.store
        if described is None:
            if not callable(getattr(self.driver, 'describe', None)):
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

    # --- healing -------------------------------------------------------------------

    def _frame_candidates(self, key):
        """The selectors of key that are looked up in the page itself (no frameXPath)."""
        return [c['xpath'] for c in self.store.candidates(key)
                if not c.get('frameXPath') and isinstance(c.get('xpath'), str)]

    def _frame_stop(self, key, exc, selector, frames, opener):
        """Record and raise the AttentionRequired for a key found only inside an iframe."""
        __tracebackhide__ = True
        reason = _frame_message(key, selector, frames, opener)
        self.coordinator._decline(key, reason)
        raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason) from exc

    def _stop_if_in_a_frame(self, key, exc):
        """After key missed on the page: if one of its selectors matches inside an iframe
        (a driver with frame_matches(), such as Playwright's), raise AttentionRequired
        saying so, before any model call. A model shown this page could not find it."""
        __tracebackhide__ = True
        finder = getattr(self.driver, 'frame_matches', None)
        if not callable(finder):
            return
        for selector in self._frame_candidates(key):
            try:
                frames = finder(selector)
            except Exception:
                continue
            if frames:
                self._frame_stop(key, exc, selector, frames, 'mendwright.open')

    def _heal(self, key, exc, attempt):
        """Heal through the coordinator after attempt() raised exc, then retry attempt()."""
        __tracebackhide__ = True  # pytest shows the caller
        pending = [exc]

        def resolve_fn():
            if pending:
                raise pending.pop()
            return attempt()

        return resolve_with_heal(resolve_fn, self.coordinator, key, self._heal_meta(key),
                                 {'url': self.driver.url()})

    def _heal_meta(self, key):
        """The key's metadata for a heal attempt, with the destructive word check applied.
        refuseDestructiveTarget tells the gate to refuse a heal onto an element whose label
        looks destructive; destructive=False for the key turns both checks off."""
        meta = self.store.meta(key)
        opted_out = self.store.destructive_choice(key) is False
        meta = dict(meta, refuseDestructiveTarget=not opted_out)
        if not meta['destructive'] and not opted_out:
            hit = _destructive_reason(key, meta.get('expectedText'))
            if hit:
                # Computed on every call and never saved: destructive=False opts out.
                meta = dict(meta, destructive=True, destructiveReason=hit)
        return meta


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
    """

    def __init__(self, healer: Healer, key: str, native: Any) -> None:
        self._healer = healer
        self._key = key
        self._native = native

    @property
    def native(self) -> Any:
        """The native object: a Playwright Locator, or a Selenium WebElement (a FramedElement
        inside an iframe). Pass this to APIs that check the type, such as
        playwright.sync_api.expect(proxy.native).to_be_visible().

        A FramedElement switches into its frame itself, so do not call switch_to.frame()
        before using it. It is not a WebElement, so execute_script(..., el) and
        ActionChains reject it: run those through in_frame(), for example
        proxy.native.in_frame(lambda el: driver.execute_script('arguments[0].click()', el))."""
        return self._native

    @property
    def key(self) -> str:
        return self._key

    def _act(self, verb: str, args: tuple, kwargs: dict, expect: Expect | None) -> Any:
        __tracebackhide__ = True  # pytest shows the caller
        args = tuple(_unwrap(a) for a in args)
        kwargs = {k: _unwrap(v) for k, v in kwargs.items()}
        act_on = getattr(self._healer.driver, 'act_on', None)
        if callable(act_on):
            # The built-in drivers raise AttributeError/TypeError/ValueError for a bad verb
            # or argument before anything reaches the browser.
            def run(*a, **kw):
                return act_on(self._native, verb, *a, **kw)
        else:
            run = getattr(self._native, verb)   # AttributeError here quarantines nothing
        coordinator = self._healer.coordinator
        try:
            result = run(*args, **kwargs)
        except _USAGE_ERRORS:
            raise
        except Exception:
            coordinator.confirm_pending(self._key, False)  # the browser action failed
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
        return getattr(self._native, name)

    def __repr__(self) -> str:
        return '<ActionProxy key=' + repr(self._key) + ' native=' + repr(self._native) + '>'
