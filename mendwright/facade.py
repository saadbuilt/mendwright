"""The zero-config facade: mendwright.open(page) gives a Healer, and Healer.locator(...)
resolves a key, heals and retries it when it breaks, and arms the canary, in one call.

It is a layer over the low-level core (resolve, resolve_with_heal, HealCoordinator,
SelectorStore), which keeps working as before. The facade adds the conveniences that need
a live page: the selector in your code wins over stored ones, the role is read from the
element, and actions run through the driver so the same verbs work on Playwright and
Selenium.

It also handles changes in a script's flow (see mendwright.flow): interruptions that can
appear at any point (heal.interrupt(), and the cookies= preset of open()), popups nobody
registered (open(dismiss=True), see mendwright.dismiss), optional steps
(locator(optional=True)), and a guard that does not heal a key on a page it was never
found on (open(heal_across_pages=...)). Steps the site shows in another order, on one page
or not at all run through heal.step() and heal.run_steps() (see mendwright.steps).

Lists of elements, such as the cards of a product grid, are looked up with
heal.locator_all() (see mendwright.lists), and expect_value= checks what a read through a heal
returns (see mendwright.values). With open(heal_get_by=True), Playwright's get_by_* calls on
heal.page are looked up and healed too, under keys written as the calls (see mendwright.getby
and Healer._get_by_locator).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import inspect
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Sequence, Union
from urllib.parse import urlsplit, urlunsplit

from . import getby
from .client import _stored_status, add_heal
from .coordinator import DISABLED_REASON, HealCoordinator, _canary_call, resolve_with_heal
from .dismiss import (NOT_LOOKED_AT, asks_answer, asks_consent, control_text, expect_control,
                      expect_of, is_consent, parse_probe, pick, popup_of)
from .drivers import (driver_act_on, driver_describe, driver_fingerprint, driver_framed,
                      driver_inventory, wrap)
from .drivers.base import (UNSURE, _member, css_pierces, driver_handles,
                           driver_list_fingerprint, driver_popup_click, driver_popup_controls,
                           driver_popup_probe, driver_popup_release, driver_popup_root,
                           driver_press_escape, driver_visible_match)
from .exceptions import AttentionRequired
from .flow import (MAX_INTERRUPTIONS_PER_STEP, SETTLE_MS, UNROUTED_STEP, InterruptionLoop,
                   absent_message, handled_message, is_interception_error, is_stale_error,
                   journal_event, loop_message, nothing_matched_message, page_path, page_url,
                   rejected_selector_message, run_cancelled_message, run_failed_message,
                   several_message, skip_message, stuck_message, unhandled_message)
from .flow import (DISMISS_KEY, alert_message, consent_message, dismiss_loop_message,
                   dismissed_message, lost_message, step_text, undismissed_message)
from .gate import live_reason
from .interruptions import (COOKIE_BANNER_KEY, COOKIE_CHOICES, Budget, InterruptionEvent,
                            cookie_choice_error, cookie_preset, function_name,
                            make_interruption, nth_targets, precise_selectors)
from .journal import Journal, run_id, run_id_problem
from .limits import Guardrails, RateLimiter
from .lists import item_role, placed as placed_list, valid as valid_list
from .providers import GatewayProvider
from .resolver import (THROUGH, _no_match_message, _require_visible, _split_runnable,
                       present_get_by, resolve, resolve_get_by)
from .roles import infer_role, role_choices
from .selectors import (CSS, XPATH, InvalidSelector, check_syntax, classify, frame_spelling,
                        normalize, strip_prefix)
from .shadow import (MISSING, ShadowHost, as_document, driver_shadowed, hosts_of, place_text,
                     saved_hosts, shadow_text, take_hosts)
from . import steps as _steps
from .store import load_store
from .values import READS as VALUE_READS, expectation, expectations, refusal_reason, refused_message

logger = logging.getLogger('mendwright')

# The step run_steps() runs now, as (healer, step name, page path), in this thread or asyncio
# task only (see Healer._in_step), or None. A context variable rather than an attribute, so
# a coroutine gathered next to run_steps() on the same healer is not taken for part of the
# step.
_RUNNING_STEP = contextvars.ContextVar('mendwright_running_step', default=None)

# The run= functions of heal.interrupt() that run now in this thread, asyncio task or
# greenlet, outermost first, as _Run records (see Healer._run_function). A function's own
# actions do not handle its own interruption again, nor dismiss any popup: the one it
# answers is still on the page while it fills it in. A context variable, so that on the
# async healer the function's task, which the step that called it waits for, is told apart
# from a coroutine gathered next to that step. Healer._runs has the same records, from
# every context: Playwright's overlay handler runs in a greenlet of its own (see
# Healer._await_runs).
_IN_RUN = contextvars.ContextVar('mendwright_in_run', default=())


class _Run:
    """One call of a run= function of heal.interrupt(), while it runs: the healer and the
    interruption's key."""

    __slots__ = ('healer', 'key')

    def __init__(self, healer, key):
        self.healer, self.key = healer, key


def _raised_by_run(exc):
    """Whether exc came out of a run= function (see Healer._run_function): it is raised as
    it is, and an AttentionRequired is never taken for a miss of the step the interruption
    was handled for."""
    return getattr(exc, '_mendwright_run', False) is True


def _batched(store):
    """store.batch() (see SelectorStore.batch), or a block that holds nothing back for a
    store without it."""
    batch = getattr(store, 'batch', None)
    return batch() if callable(batch) else contextlib.nullcontext()


def _reraise(budget):
    """Raise what a run= function raised in Playwright's overlay handler while the step of
    budget ran (see Healer._overlay_run), if it did."""
    if budget.raised is not None:
        raise budget.raised

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
    that had no selector, found by a model from its intent=), 'frame' (the key's own
    selector, which matched only inside an iframe and is looked up there now), or 'shadow'
    (the key's own selector, which matched only inside an open shadow root, see
    mendwright.shadow)."""
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


def _fingerprint_off_by(fingerprint=None):
    """What turns the fingerprint tier off, in the words a message uses, with the precedence
    of fingerprint_setting(): MENDWRIGHT_DISABLE=1, fingerprint=False, or
    MENDWRIGHT_FINGERPRINT=0."""
    if _env_flag('MENDWRIGHT_DISABLE'):
        return 'MENDWRIGHT_DISABLE=1'
    if fingerprint is not None:
        return 'fingerprint=False'
    return 'MENDWRIGHT_FINGERPRINT=0'


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


def get_by_setting(heal_get_by=None):
    """Whether heal.page's get_by_* calls are looked up and healed (see
    Healer._get_by_locator): the heal_get_by= argument when it is not None, else
    MENDWRIGHT_HEAL_GET_BY (1 or 0), else off. MENDWRIGHT_DISABLE=1 leaves it as it is: the
    calls are looked up, and nothing heals. Raises ValueError for a MENDWRIGHT_HEAL_GET_BY
    that is not a flag."""
    if heal_get_by is not None:
        return bool(heal_get_by)
    return bool(_env_flag('MENDWRIGHT_HEAL_GET_BY'))


def _get_by_choice(heal_get_by, entry):
    """Raise TypeError for a heal_get_by= other than True, False or None."""
    if heal_get_by is not None and not isinstance(heal_get_by, bool):
        raise TypeError(entry + ' takes heal_get_by=True, heal_get_by=False or heal_get_by=None, '
                        'got heal_get_by=' + repr(heal_get_by) + '.')


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
         heal_across_pages: bool = False, fingerprint: bool | None = None,
         dismiss: bool = False, expect_values: dict | None = None,
         heal_get_by: bool | None = None) -> Healer:
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
    - dismiss=True dismisses a popup nobody registered when it covers the element of an
      action: a newsletter modal, a survey, a chat invite, a consent banner when cookies=
      is not set. Registered interruptions come first: before each action, once those the
      page shows are handled, mendwright asks the browser what is on top at the middle of
      the element (one round trip), and after an action failed because something covered
      it, it looks again when none of them is on the page. It acts only on a dialog, or a
      fixed element that covers at least a quarter of the window, that does not hold the
      element. It presses Escape (only while the keyboard focus is on the page's body or
      in the popup, and not in an iframe), then clicks a close button of the popup, then a
      button that declines (No thanks, Not now, Skip, Reject, ...), and looks again after
      each. It never clicks a button whose label says accept, subscribe, sign up, yes,
      continue (but for "continue without ...") or the like, a link that goes somewhere,
      or a button that submits a form, and never types, checks or selects. A popup it
      cannot dismiss is named in one WARNING, and the action raises its own error. Each
      dismissal logs one WARNING, writes a flow.dismiss journal event and is listed in
      handled_interruptions under '<popup>'. With dismiss=True, heal.page's get_by_*
      locators, locators built with options, chained locators, .first, .last, .nth() and
      .filter() run the same check before their actions, without healing. False (the
      default) makes no browser call for it. See mendwright.dismiss.
    - expect_values={key: check} sets expect_value= (see Healer.locator) for those keys on
      every lookup, also on those heal.page and heal.driver make, whose key is the selector
      (by=value for heal.driver). See mendwright.values.
    - heal_get_by=True (or MENDWRIGHT_HEAL_GET_BY=1) looks up Playwright's get_by_role()
      and the other get_by_* calls on heal.page too, under a key written as the call, such
      as 'get_by_role("button", name="Sign in")' (mendwright.get_by_key() builds one for
      destructive_keys= and expect_values=). A call that matches nothing for timeout_ms is
      healed from the fingerprint mendwright recorded when it last matched, never by a
      model, and the miss raises an AttentionRequired that is also Playwright's
      TimeoutError. A call that matches several elements, or one hidden element, goes to
      Playwright as written. Off by default (None reads the environment), and then
      heal.page's get_by_* are the real ones. On Selenium it does nothing. See
      Healer._get_by_locator.

    Settings an argument leaves at None come from the environment:
    MENDWRIGHT_STORE (store_path, default ./.mendwright/overrides.json),
    MENDWRIGHT_TIMEOUT_MS (timeout_ms, default 10000), MENDWRIGHT_DRY_RUN=1 (dry_run) and
    MENDWRIGHT_HEAL_GET_BY=1 (heal_get_by). MENDWRIGHT_DISABLE=1 turns healing off:
    selectors still resolve, nothing is healed and no provider is called, even one passed
    as provider=.

    Heals are written to journal.jsonl next to the store. A direct in-process provider
    (not GatewayProvider, which meters server-side) also gets an hourly rate limit, shared
    by every Healer on the same store file in this process.
    """
    from .proxies import unwrap
    page = unwrap(page)    # heal.page or heal.driver of another Healer: use the real one
    preset = _cookie_preset(cookies, 'mendwright.open()')
    _dismiss_choice(dismiss, 'mendwright.open()')
    _get_by_choice(heal_get_by, 'mendwright.open()')
    checks = expectations(expect_values)
    heal_get_by = get_by_setting(heal_get_by)
    driver = wrap(page)
    store, coord, timeout_ms = _setup(
        driver, provider=provider, store=store, store_path=store_path, guard=guard,
        dry_run=dry_run, destructive_keys=destructive_keys, guardrails=guardrails,
        journal=journal, rate_per_key_per_hour=rate_per_key_per_hour,
        rate_global_per_hour=rate_global_per_hour, timeout_ms=timeout_ms,
        heal_across_pages=heal_across_pages, fingerprint=fingerprint)
    healer = Healer(page, driver, store, coord, timeout_ms=timeout_ms)
    healer._dismiss_on = dismiss
    healer._expect_values = checks
    healer._heal_get_by = heal_get_by
    if preset is not None:
        healer._interruptions[preset.key] = preset
        healer._watch(preset)
    return healer


def _dismiss_choice(dismiss, entry):
    """Raise TypeError for a dismiss= other than True or False: a string such as 'no' would
    otherwise turn on a feature that clicks."""
    if not isinstance(dismiss, bool):
        raise TypeError(entry + ' takes dismiss=True or dismiss=False, got dismiss='
                        + repr(dismiss) + '.')


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
    off_by = _fingerprint_off_by(fingerprint)
    fingerprint = fingerprint_setting(fingerprint)
    # A run id only groups journal lines, so a MENDWRIGHT_RUN_ID that is not one must not
    # fail the tests: it is ignored, and said once per open().
    bad_run_id = run_id()[2]
    if bad_run_id is not None:
        logger.warning('mendwright: ' + run_id_problem(bad_run_id))

    if _env_flag('MENDWRIGHT_DISABLE'):
        provider = _NullProvider(DISABLED_REASON)
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
    # What turned the fingerprint tier off, for the refusal of a list looked up with
    # nonempty=True (see Healer._nonempty_blocked).
    coord.fingerprint_off_by = None if fingerprint else off_by
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
    """Why a key whose selector matches only inside an iframe was not followed there. For
    the key of a get_by_* call, the advice passes heal_get_by=True."""
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
    if getby.is_key(key):
        how += ', heal_get_by=True'     # else heal.page.get_by_*() there would not heal
    because = why or 'nothing tells that iframe apart from the others on the page'
    return ("'" + key + "' is inside an iframe: " + selector + ' matches nothing in the page '
            'mendwright was opened on, but matches inside ' + where + '. mendwright did not '
            'look for it there, because ' + because + ', and did not ask a model to heal '
            'it. Open mendwright on that Frame instead: heal = ' + opener + '(' + how + ').')


def latest_success(store, key):
    """The key's active candidate that matched most recently, in any run, or None."""
    try:
        found = [c for c in store.candidates(key) if c.get('lastSucceededAt')]
    except Exception:
        return None
    return max(found, key=lambda c: str(c['lastSucceededAt'])) if found else None


# --- where a heal runs: the page, an iframe or an open shadow root ------------------------------

@dataclass(frozen=True)
class HealScope:
    """Where a heal of a key runs when not on the page (see heal_place and
    Healer._heal_scope): inside the iframe ``frame``, or inside the open shadow root that
    ``hosts`` lead to, through ``driver`` (a driver for the iframe's document, or a
    shadow.ShadowDriver); or nowhere, with ``why``."""
    frame: str | None = None
    hosts: tuple = ()
    driver: Any = None
    why: str = ''

    def entered(self, coordinator):
        """The coordinator, scoped to this place for a with block (or refusing every heal
        with why)."""
        if self.why:
            return coordinator.refusing(self.why)
        return coordinator.scoped(self.driver, self.frame, shadow_hosts=self.hosts or None)


def heal_place(store, key):
    """(frame, hosts, why): where a heal of key runs. frame is the iframe its latest match
    was in ('' for none); hosts the shadow hosts of the open shadow root its element was last
    found in: those its latest match runs inside, else the key's shadowHosts (see
    mendwright.shadow), () for none; why says why no heal may run anywhere, '' else: an
    element inside a shadow root inside an iframe, or behind a host nothing tells apart. A
    key last found inside a shadow root is never healed on the page outside it."""
    best = latest_success(store, key)
    frame = best.get('frameXPath') if best else None
    frame = frame if isinstance(frame, str) and frame else ''
    hosts = hosts_of(best) if best else ()
    if not hosts:
        recorded = getattr(store, 'shadow_hosts', None)
        try:
            hosts = saved_hosts(recorded(key)) if callable(recorded) else ()
        except Exception:
            hosts = ()
    if hosts and frame:
        return frame, hosts, (
            "'" + key + "' was last found inside a shadow root in the iframe " + frame + ', and '
            'mendwright heals a key inside a shadow root only in the page or frame it was '
            'opened on, so it did not heal it, there or anywhere else. Open mendwright on that '
            'frame to heal it there.')
    if any(h is None for h in hosts):
        return '', hosts, (
            "'" + key + "' was last found inside a shadow root whose host nothing tells apart "
            'from the elements around it (it has no test id, id or name that no other element '
            'of its tag there has, and is not the only element of its custom tag), so '
            'mendwright cannot find that shadow root again, and did not heal the key on the page '
            'outside it either. Give the host an id or a test id, or fix the selector by hand.')
    return frame, hosts, ''


def shadow_unreachable(key, hosts, reaches, entered):
    """Why a heal of key, last found inside the open shadow root that hosts lead to, cannot
    run there now, or '': the driver does not reach into shadow roots (reaches False), or the
    hosts lead to no open shadow root on the page now (entered False)."""
    where = place_text(hosts)
    if not reaches:
        return ("'" + key + "' was last found inside " + where + ', and this driver does not '
                'reach into shadow roots, so mendwright did not heal it, there or on the page.')
    if not entered:
        return ("'" + key + "' was last found inside " + where + ', and mendwright cannot enter '
                'that shadow root now: one of its hosts (' + ' >> '.join(hosts) + ') is not on '
                'the page, matches more than one element, or has a closed shadow root. '
                'mendwright heals the key only inside that shadow root, never on the page outside '
                'it, so it did not heal it. Fix the selector in your code.')
    return ''


def shadow_found(selector, roots):
    """The open shadow roots (as driver.shadow_matches() gives them) that one of a key's own
    selectors matches in, for the follow (see Healer._follow_into_shadow): dicts of 'hosts',
    'count' (the elements it matches there) and 'forms', the CSS selectors to look it up with
    there: the selector itself for CSS, the CSS forms of its one element (css.css_selectors)
    for an XPath, which mendwright evaluates in the root's HTML itself."""
    from .css import css_selectors
    import lxml.html
    kind = classify(selector)
    body = strip_prefix(selector)
    out = []
    for root in roots:
        if kind == CSS:
            if root.get('count'):
                out.append({'hosts': root['hosts'], 'count': root['count'], 'forms': [body]})
            continue
        if kind != XPATH or root.get('html') is None:
            continue
        try:
            doc = lxml.html.fromstring(as_document(root['html']))
            hits = [h for h in doc.xpath(body) if isinstance(getattr(h, 'tag', None), str)]
        except Exception:
            continue
        if hits:
            out.append({'hosts': root['hosts'], 'count': len(hits),
                        'forms': css_selectors(hits[0], doc) if len(hits) == 1 else []})
    return out


def follow_allowed(store, key, found):
    """Whether key may be followed into the shadow roots of ``found`` (see shadow_found) by
    where its element was last found (heal_place): anywhere when mendwright has no record
    of that (no selector of the key matched, and no shadowHosts are saved); else only into
    the one shadow root it was last found in, when found is that one alone. A key last found
    on the page or in an iframe is healed there instead, and one last found in another
    shadow root, in that one, as any key is (see Healer._heal_scope)."""
    frame, hosts, why = heal_place(store, key)
    if frame or why:
        return False
    if hosts:
        return len(found) == 1 and tuple(found[0]['hosts']) == tuple(hosts)
    return latest_success(store, key) is None


def _matches_nothing(count, selector):
    """True when count(selector), a driver's count on the page, is 0; False for any other
    count and when it raises."""
    try:
        return count(selector) == 0
    except Exception:
        return False


def _shadow_message(key, selector, found, why):
    """Why a key whose selector matches only inside an open shadow root was not followed
    there."""
    where = place_text(found[0]['hosts'])
    if len(found) > 1:
        where += ' (and ' + str(len(found) - 1) + ' more)'
    return ("'" + key + "' is inside a shadow root: " + selector + ' matches nothing on the '
            'page, but matches inside ' + where + '. mendwright did not look for it there, '
            'because ' + why + ', and did not ask a model to heal it. Fix the selector in your '
            'code.')


_NOT_FOLLOWED = object()


def moved_message(key, hosts, verb):
    """Why an action or read of a proxy for an element found inside the shadow root that
    hosts lead to did not run: its Locator matches nothing there now, or something outside
    it too (see ActionProxy._wait_placed)."""
    return ("mendwright: '" + key + "' was found inside " + place_text(hosts) + ', but '
            'when it came to ' + verb + '() its selector no longer matched one element there '
            "and nothing outside it: the page changed after the lookup. Playwright's CSS "
            'reaches into every open shadow root, so mendwright did not run ' + verb + '(), '
            'which could have reached an element outside that shadow root. Look the element '
            'up again with heal.locator().')


def _locator_args(key, selector, role, inline_xpath, intent=None, entry='heal.locator()'):
    """locator()'s argument checks, which touch neither the page nor the store: returns
    (roles, code), the accepted roles for role= and the selectors from _code_selectors().
    Raises TypeError for an intent= that is not a non-empty string, and ValueError for a key
    written as a get_by_* call, which is how mendwright names a heal.page.get_by_*() call
    (entry is the method named in that message)."""
    if isinstance(key, str) and getby.is_key(key):
        raise ValueError(getby.reserved_key_message(key, entry))
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


def _nonempty_args(key, nonempty, optional):
    """locator_all()'s checks of nonempty=, which touch neither the page nor the store:
    TypeError for anything but True or False, ValueError for it with optional=True."""
    if type(nonempty) is not bool:
        raise TypeError('mendwright: heal.locator_all(' + repr(key) + ', ..., nonempty=...) takes '
                        'True or False, got ' + type(nonempty).__name__ + '.')
    if nonempty and optional:
        raise ValueError('mendwright: heal.locator_all(' + repr(key) + ', ..., nonempty=True, '
                         'optional=True) asks for two opposite things: nonempty=True says the '
                         'list always has items here, and optional=True says it may be empty. '
                         'Pass one of them.')


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


def _popup_state(probe):
    """What a look at an element after a step on the popup over it says: 'gone' when the
    popup's root was removed or hidden, or the element is on top again ('free'); 'lost'
    when the element could not be looked at (it matches none or several now) or is hidden
    or outside the window now, so mendwright cannot tell what the step did; 'there' when
    the popup still covers it."""
    if probe is None:
        return 'lost'
    if probe.gone or (not probe.covered and probe.why == 'free'):
        return 'gone'
    return 'lost' if not probe.covered else 'there'


# --- Playwright's overlay handler (page.add_locator_handler) ---------------------------------

def _overlay_locator(page, selectors):
    """The Locator a Playwright overlay handler watches for one interruption: any of its
    selectors, narrowed to the first visible match.

    Before each action Playwright asks whether this locator is visible, in strict mode and
    in the main frame (Page._performLocatorHandlersCheckpoint calls
    mainFrame().isVisibleInternal(selector, {strict: true})). A locator that matched two
    elements, such as a banner's Reject and Close buttons, would fail every action on the
    page with a strict mode violation, so it is narrowed to one."""
    from .drivers.playwright_sync import playwright_selector
    found = None
    for selector in selectors:
        one = page.locator(playwright_selector(selector))
        found = one if found is None else found.or_(one)   # Locator.or_: Playwright 1.33
    return found.locator('visible=true').first


def _overlay_options(add_locator_handler):
    """The options mendwright passes to page.add_locator_handler: no_wait_after=True where
    it takes it (Playwright 1.44 and later; before that Playwright never waited).

    By default Playwright waits after the handler until the overlay is hidden, for as long
    as the action's timeout. mendwright's handler does not always act (see
    Healer._overlay_wanted): it stays out while mendwright clicks the banner itself, whose
    click runs Playwright's check too, and it gives up on an interruption that comes back
    every time. Waiting then would hold the action until it times out. The handler waits
    for what it handled to go away itself (Healer._settle)."""
    try:
        params = inspect.signature(add_locator_handler).parameters
    except (TypeError, ValueError):
        return {}
    return {'no_wait_after': True} if 'no_wait_after' in params else {}


class _PageOverlays:
    """What the healers of one page share about Playwright's overlay handlers (see
    Healer._watch).

    handlers maps an interruption key to (selectors, healer refs, token) for the one
    handler registered on the page for it. Playwright checks every handler's locator before
    each locator action, so a page that outlives many healers keeps one handler per key,
    not one from each healer: a healer that registers the same selectors joins the one
    there, which acts for the newest of its healers still alive. It keeps the selectors,
    not the Locator, which would keep the page alive.

    busy is above 0 while a healer of the page handles interruptions itself, and while it
    looks up a key: its own action on the banner, and the lookup's scroll, run Playwright's
    check too, and the page's handlers must stay out of them.

    navigations counts the page's main-frame navigations and loads (goto(), a link,
    set_content(), a history change in a single-page app) since its first handler was
    registered. An interruption the handler gave up on is handled again after one (see
    Healer._overlay_wanted).

    parsed holds the selectors the browser has parsed on this page (see Healer._parsed),
    and registering is the asyncio.Lock the async healers of the page hold while they
    change its handlers (see AsyncHealer._watch), made on first use."""

    def __init__(self):
        self.handlers: dict = {}
        self.busy = 0
        self.navigations = 0
        self.parsed: set = set()
        self.registering = None
        self._listening = False
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def handling(self):
        with self._lock:
            self.busy += 1
        try:
            yield
        finally:
            with self._lock:
                self.busy -= 1

    def listen(self, page):
        """Count page's main-frame navigations and loads from now on, once per page.
        framenavigated covers a history change, which loads nothing, and load covers
        set_content(), which navigates nowhere. The listeners keep no healer alive."""
        on = getattr(page, 'on', None)
        if self._listening or not callable(on):
            return

        def navigated(frame):
            if getattr(frame, 'parent_frame', None) is None:   # the main frame
                self.navigations += 1

        def loaded(_page):
            self.navigations += 1

        try:
            on('framenavigated', navigated)
            on('load', loaded)
            self._listening = True
        except Exception:
            pass


# {native page: _PageOverlays}
_OVERLAYS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_OVERLAYS_LOCK = threading.Lock()


def _page_overlays(page):
    """page's _PageOverlays, or one of its own for a page that cannot be weakly
    referenced."""
    try:
        with _OVERLAYS_LOCK:
            found = _OVERLAYS.get(page)
            if found is None:
                found = _OVERLAYS[page] = _PageOverlays()
            return found
    except TypeError:
        return _PageOverlays()


def _overlay_owner(overlays, key, token):
    """The healer the overlay handler registered with ``token`` for key acts for: the
    newest healer that registered key's selectors on the page and is still alive. None
    when that handler was replaced, or none of its healers is left."""
    entry = overlays.get(key)
    if entry is None or entry[2] is not token:
        return None
    for ref in reversed(entry[1]):
        healer = ref()
        if healer is not None:
            return healer
    return None


def _join_overlay(overlays, key, selectors, healer):
    """When key's handler on the page watches these selectors already, add healer to the
    healers it acts for and return True; else False."""
    entry = overlays.get(key)
    if entry is None or entry[0] != selectors:
        return False
    refs = [r for r in entry[1] if r() is not None and r() is not healer]
    overlays[key] = (entry[0], tuple(refs) + (weakref.ref(healer),), entry[2])
    return True


def _leave_overlay(overlays, key, healer):
    """Take healer off key's handler on the page. Returns the handler's selectors when no
    healer is left on it, so it can be removed from the page, else None."""
    entry = overlays.get(key)
    if entry is None or not any(r() is healer for r in entry[1]):
        return None
    refs = tuple(r for r in entry[1] if r() is not None and r() is not healer)
    if refs:
        overlays[key] = (entry[0], refs, entry[2])
        return None
    del overlays[key]
    return entry[0]


def _removable(overlays, selectors):
    """Whether the page's handler for selectors may be removed. Playwright's
    remove_locator_handler removes every handler whose locator is the same, so not while
    the handler of another key watches the same selectors."""
    return bool(selectors) and not any(entry[0] == selectors for entry in overlays.values())


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
        # open(dismiss=True): dismiss popups nobody registered (see _dismiss).
        self._dismiss_on = False
        # key -> (how many times in a row an interruption was still on the page after its
        # action, the page's navigation count then). The overlay handler stops acting on
        # one at MAX_INTERRUPTIONS_PER_STEP, until the page navigates (see _stuck_count).
        self._stuck: dict = {}
        # Playwright's overlay handlers on this page, shared with the other healers of the
        # page (see _PageOverlays and _watch).
        self._overlays = _page_overlays(page)
        # The steps running now, as (key, budget, timeout_ms, poll_ms), so the overlay
        # handler counts what it handles in the middle of one against that step.
        self._steps: list = []
        # (key, xpath) pairs whose fingerprint this Healer has read already.
        self._fingerprinted: set = set()
        self._proxies: dict = {}
        # Steps registered with step(), by name in the order registered (see mendwright.steps).
        self._step_book: dict = {}
        # How a run= function of interrupt() is called: None calls it here with heal.page or
        # heal.driver; the async healer sets its own, which awaits it on the event loop.
        self._runner = None
        # The run= functions of this healer that run now, in any context (see _await_runs).
        self._runs: list = []
        # (what, name, page) of each interruption and landmark named in the WARNING for a
        # selector that matches several elements (see _several), so it comes once per page.
        self._several_seen: set = set()
        # open(expect_values=...): key -> the check a read through a heal must pass.
        self._expect_values: dict = {}
        # open(heal_get_by=True): heal.page's get_by_* calls are looked up and healed (see
        # _get_by_locator). The calls that are never keys, logged once each (_note_get_by).
        self._heal_get_by = False
        self._get_by_noted: set = set()
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
        healed. is_visible(selector) answers at once and never heals. With
        open(heal_get_by=True), get_by_role() and the other get_by_* calls on it are looked
        up and healed too (see _get_by_locator); otherwise they are the real page's.
        Everything else, and a locator built with options or chained from another one, is
        the real page's, not healed. Interruptions (interrupt(), open(cookies=...)) are handled before the
        actions of those locators all the same, on Playwright 1.42 and later, but not
        before page.mouse, page.keyboard or an action with force=True: see interrupt().
        For any other page or driver it is the object mendwright was opened on."""
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
        heal.locator(f'{by}={value}', ...) for By.XPATH, By.CSS_SELECTOR, By.ID and By.NAME,
        healing only a selector that matches nothing (a hidden element, or the first of
        several, comes from the real driver as before). find_element, find_elements and an
        element's own find_element(s) return the element wrapped (see
        mendwright.proxies.HealingElement): it passes isinstance(el, WebElement), and its
        click, send_keys, clear and submit handle interruptions (interrupt(),
        open(cookies=...)) as heal.locator()'s actions do. Everything else is the real
        driver's. For any other page it is mendwright's own Driver for it, as before."""
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
        runnable, _errors = _split_runnable(key, self.store.candidates(key), self._driver)
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

    def interrupt(self, key: str, selector: Selector | None, *, action: str | None = None,
                  args: Sequence[Any] = (), role: Role | None = None,
                  run: Callable[[Any], Any] | None = None) -> None:
        """Register a step that may appear at any point of the flow: a cookie banner, a
        newsletter modal, an "are you still there?" dialog.

        selector finds the element to act on (one selector, or a list tried in order), and
        action with args is what to do with it (a click by default; for example
        action='press', args=('Escape',)). role=, when given, is the role the element
        must have before mendwright acts on it. Use a selector that matches one element:
        an XPath that matches several is narrowed to the first visible match, and any
        other kind that matches several is skipped, with one WARNING per page that names
        a selector that matches one, when exactly one of its matches is visible.

        run= is a function that handles the interruption itself, in place of action and
        args, for a popup that needs a form: a session-expired sign-in, an address, two
        fields to confirm. It gets heal.page (heal.driver on Selenium), so its selectors
        heal as usual, and mendwright calls it when selector matches one visible element,
        then waits for that element to go away as after an action. Its own actions never
        handle this interruption, so it never runs itself, but they handle the others, and
        no popup is dismissed while it runs. What it raises is raised as it is, after one
        WARNING and a flow.interrupt journal event with outcome 'failed'.

        mendwright looks for it only when a step needs help: when a locator() misses
        (before any heal or model call), before an action while the page shows it, and when
        an action fails because something covered the element. If the element is there and
        visible, mendwright performs the action, logs one WARNING line ("handled
        interruption 'cookies' before 'email'"), writes a flow.interrupt journal event and
        retries the step. A failed action is retried once, and only after an interruption
        was handled. At most 3 interruptions are handled for one step.

        That covers the actions of heal.locator(), of heal.page's selector shortcuts and
        the click, send_keys, clear and submit of the elements heal.driver finds. On
        Selenium the check before an action is skipped when nothing covers the element, so
        an action on the interruption's own buttons goes ahead and does not find them gone.
        On a Playwright Page (Playwright 1.42 and later) mendwright also registers it with
        page.add_locator_handler, so Playwright itself hands over to mendwright when the
        element shows up before a locator or selector action on the page (click, fill,
        check, hover, ...) or while one waits: a get_by_* or chained locator of heal.page,
        or of the page itself. Not before page.mouse, page.keyboard, dispatch_event() or an
        action with force=True. Such an action is logged as before '<page action>'. An
        interruption still on the page after 3 such actions in a row is left alone (one
        WARNING) until mendwright sees it gone or the page navigates. Playwright does the
        same before auto-waiting assertions, wait_for_selector() and Locator.wait_for(), so
        an expect() or a wait on the interruption's own element, or a get_by_* step that
        clicks it, finds it closed: drop such a step, or write it as heal.locator(key, ...).
        A selector the browser cannot parse is left out of that handler, with a WARNING.

        An interruption is never healed by a model. A later heal.locator(key, ...) for the
        same key is skipped (no heal, no error, its actions do nothing) when the element is
        not there: it was handled earlier, or it did not appear this time. Registering a
        key again replaces it. interrupt(key, None) removes it from this healer, and its
        overlay handler from the page once no other healer of the page has registered it.
        Raises TypeError, ValueError or InvalidSelector for arguments that can never
        work, run= with action= or args= among them."""
        if selector is None:
            if run is not None:
                raise TypeError('mendwright: heal.interrupt(' + repr(key) + ', None) removes an '
                                'interruption. To register one with run=, pass the selector of '
                                "what shows it, such as '#session-expired'.")
            self._interruptions.pop(key, None)
            self._stuck.pop(key, None)
            self._unwatch(key)
            return
        item = make_interruption(key, selector, action=action, args=args, role=role, run=run,
                                 actions=ACTIONS, awaits=self._runner is not None)
        self._interruptions.pop(item.key, None)
        self._interruptions[item.key] = item
        self._stuck.pop(item.key, None)
        self._watch(item)

    @property
    def handled_interruptions(self) -> list:
        """The interruptions this Healer handled, oldest first, as
        InterruptionEvent(key, before): before is the step it was handled for. A popup that
        open(dismiss=True) dismissed is listed under the key '<popup>', with its popup and
        how fields set."""
        return list(self._interrupt_log)

    def locator(self, key: str, selector: Selector | None = None, *, role: Role | None = None,
                text: str | Sequence[str] | None = None, destructive: bool | None = None,
                timeout_ms: int | None = None, poll_ms: int = 200,
                inline_xpath: Selector | None = None, optional: bool = False,
                intent: str | None = None, expect_value: Any = None) -> ActionProxy:
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

        expect_value is what a read of the element must return when the element was found
        through a selector a heal saved: 'money', 'number', 'integer', 'percent', 'date',
        'email' or 'url', a compiled regular expression, or a callable that takes the value
        and returns True when it is right. The reads are inner_text(), text_content(),
        input_value() and get_attribute(), and Selenium's text, get_attribute() and
        get_dom_attribute(). A value that passes confirms a heal made in this process, as
        expect= on an action does; one that fails quarantines the heal, and the read raises
        AttentionRequired with the key, the healed selector and the value. A read through
        any other selector is never checked. Without it, the check open(expect_values=...)
        has for key applies. See mendwright.values.

        inline_xpath is the old name of selector and still works as a keyword.

        Raises AttentionRequired when the element cannot be found or healed (the message
        says why and what to do), InvalidSelector or TypeError for a selector that can
        never work, and ValueError for an unknown role, or for intent= with nothing to go
        on but no role=. An expect_value= that is none of those raises TypeError or
        ValueError, before anything touches the page.
        """
        __tracebackhide__ = True  # pytest shows the caller
        roles, code = _locator_args(key, selector, role, inline_xpath, intent)
        expect = expectation(expect_value, key) or self._expect_values.get(key)
        self._prime(key, roles, code, text, destructive, intent)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        first_wait = self._first_wait(key, timeout)
        store = self.store

        def attempt(wait=timeout):
            # Playwright's overlay handlers stay out of the lookup, whose scroll runs their
            # check: the step handles what the page shows itself, before its action or read
            # (see ActionProxy._before_read). A run= function called inside the handler
            # would have only the scroll's timeout.
            with self._overlays.handling():
                return resolve(store, self._page, key, timeout_ms=wait, poll_ms=poll_ms)

        budget = Budget()
        with self._running(key, budget, timeout, poll_ms):
            try:
                native = self._resolve_step(key, lambda: attempt(first_wait), budget, timeout,
                                            poll_ms)
            except InterruptionLoop:
                raise   # the page is blocked: a heal would not help
            except AttentionRequired as exc:
                if _raised_by_run(exc):
                    raise   # a run= function's own, not a miss of this step
                skip = self._skip_reason(key, optional)
                if skip:
                    self._record_skip(key, skip)
                    return ActionProxy(self, key, None, present=False)
                native = self._follow_into_frame(key, exc, attempt)
                if native is _NOT_FOLLOWED:
                    native = self._follow_into_shadow(key, exc, attempt)
                if native is _NOT_FOLLOWED:
                    native = self._heal(key, exc, attempt)
            # One the overlay handler still runs, for an earlier action that gave up on it
            self._await_runs(budget, poll_ms)
        self._check_healed_target(key)
        self._learn(key, roles)
        self._remember_page(key)
        target = self._fingerprint_target(key)
        if target is not None:
            self._keep_fingerprint(key, target, driver_fingerprint(
                self._driver, *self._winner_target(key), hosts=True))
        proxy = ActionProxy(self, key, native, timeout_ms=timeout, poll_ms=poll_ms,
                            healed=self._healed(key), expect=expect)
        proxy._hosts = self._shadow_place(key)
        return proxy

    def present(self, key: str) -> bool:
        """True if one of key's stored selectors matches a single element right now.
        No waiting and no healing. For the key of a get_by_* call (mendwright.get_by_key()),
        the call itself first: True when it matches one visible element, False when it
        matches several or one hidden one, and its saved heals only while it matches
        nothing (see resolver.present_get_by). A call written in another spelling, such as
        "get_by_role('button', name='Pay')", is read as the call's key."""
        from .resolver import present
        if getby.is_key(key):
            return present_get_by(self.store, self._driver, getby.canonical_key(key))
        return present(self.store, self._page, key)

    # --- heal.page.get_by_*(), with open(heal_get_by=True) (see mendwright.getby) -------

    def _get_by_locator(self, call, target, *, timeout_ms=None, poll_ms=200):
        """The ActionProxy an action or a waiting read of a heal.page.get_by_*() call runs
        on: call is its GetByCall, target the real Locator. The key is the call
        (getby.canonical), such as 'get_by_role("button", name="Sign in")'.

        A. One look first, before anything is written: a call the browser rejects (level=
           on a button), or that matches several elements or one hidden element (unless the
           key's role is checkbox), is not broken: it goes to Playwright as written
           (_get_by_through), with nothing saved and no saved heal used. A call that
           matches nothing has the registered interruptions handled now, before the wait: a
           modal cookie banner can hide the rest of the page from the accessibility tree.
        B. The lookup (resolver.resolve_get_by): the call itself first, as the key's code
           selector, and a saved heal only while the call matches nothing. A miss handles
           the registered interruptions and tries again. A call that starts to match
           several elements or a hidden one during the wait goes to Playwright at once.
        C. After a miss: a call that matches only inside an iframe is not followed there,
           and no heal of it is either; else the coordinator heals it from its fingerprint
           only (HealCoordinator._heal_get_by). What it cannot find or heal is raised as a
           getby.GetByNotFound, which is also Playwright's TimeoutError, so an except
           TimeoutError around an optional step still works. InterruptionLoop, what a run=
           function raised and the refusal of a heal on a key marked destructive are raised
           as they are.
        D. After a match, as locator() does: the role, the page and the fingerprint are
           saved. An action on the element the call itself found reports nothing to the
           heal canary; one on a heal's element does."""
        __tracebackhide__ = True  # pytest shows the caller
        key = getby.canonical(call)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        budget = Budget()
        with self._running(key, budget, timeout, poll_ms):
            state, why = self._get_by_state(key)
            if state is None:
                return self._get_by_through(key, target, timeout, why)
            early = state[0] == 0 and bool(self._others(key)) and self._interrupt(
                key, budget, timeout, poll_ms)
            # The page changed when an interruption was handled: look at the call again.
            first = [None if early else state]
            self._prime(key, None, [key], None, None)

            def attempt(wait=timeout):
                # Playwright's overlay handlers stay out of the lookup, as in locator().
                with self._overlays.handling():
                    return resolve_get_by(self.store, self._driver, key, timeout_ms=wait,
                                          poll_ms=poll_ms, first=first.pop() if first else None)

            try:
                try:
                    native = self._resolve_step(key, attempt, budget, timeout, poll_ms)
                except InterruptionLoop:
                    raise
                except AttentionRequired as exc:
                    if _raised_by_run(exc):
                        raise
                    self._get_by_frames(key, exc, 'mendwright.open')
                    native = self._heal(key, exc, attempt)
            except InterruptionLoop:
                raise   # the page is blocked: a heal would not help
            except AttentionRequired as exc:
                if _raised_by_run(exc):
                    raise   # a run= function's own, not a miss of this step
                raise getby.not_found_error(exc) from exc
            if native is THROUGH:
                return self._get_by_through(key, target, timeout, getby.STARTED_TO_MATCH)
            self._await_runs(budget, poll_ms)
        self._check_healed_target(key)
        self._learn(key, None)
        self._remember_page(key)
        fp_target = self._fingerprint_target(key)
        if fp_target is not None:
            self._keep_fingerprint(key, fp_target, driver_fingerprint(
                self._driver, *self._winner_target(key), hosts=True))
        proxy = ActionProxy(self, key, native, timeout_ms=timeout, poll_ms=poll_ms,
                            healed=self._healed(key), expect=self._expect_values.get(key))
        proxy._canary = proxy._healed is not None
        proxy._hosts = self._shadow_place(key)
        return proxy

    def _get_by_state(self, key):
        """Step A of _get_by_locator: ((count, visible), '') of the call key on the page now,
        or (None, why) when it goes to Playwright as written. One round trip, two when it
        matches one element."""
        require_visible = _require_visible(self.store, key)
        try:
            count = self._driver.count(key)
            visible = None
            if count == 1 and require_visible:
                visible = bool(self._driver.is_visible(key))
        except InvalidSelector:
            return None, getby.REJECTED
        if count >= 2:
            return None, 'matches ' + str(count) + ' elements'
        if count == 1 and require_visible and not visible:
            return None, getby.HIDDEN
        return (count, visible), ''

    def _get_by_through(self, key, target, timeout_ms, why):
        """An ActionProxy on the real Locator of a get_by_* call that goes to Playwright as
        written: its action handles the registered interruptions and, with
        open(dismiss=True), a popup over the element first, and Playwright raises its own
        error (a strict mode violation, its wait and timeout, a selector error). Nothing is
        saved, no saved heal is used, and it reports nothing to the heal canary."""
        logger.debug('mendwright: ' + getby.through_message(key, why))
        proxy = ActionProxy(self, key, target, timeout_ms=timeout_ms)
        proxy._canary = False
        return proxy

    def _get_by_frames(self, key, exc, opener):
        """Step C1 of _get_by_locator: when the call itself matches inside an iframe (a
        driver with frame_matches()), raise the AttentionRequired that says mendwright does
        not follow a get_by_* call there. Only the call is looked for, not its heals or seed
        rows: nothing of a call key is followed into an iframe."""
        __tracebackhide__ = True
        finder = getattr(self._driver, 'frame_matches', None)
        if not callable(finder):
            return
        try:
            frames = finder(key)
        except Exception:
            return
        if frames:
            self._frame_stop(key, exc, key, frames, opener, getby.NOT_FOLLOWED)

    def _note_get_by(self, step, why):
        """Log, once per healer and call, that heal.page's get_by_* call ``step`` is never a
        key (see getby.call_of) and goes to Playwright as written."""
        if step in self._get_by_noted:
            return
        self._get_by_noted.add(step)
        self.coordinator._emit(logging.INFO, getby.not_healed_message(step, why))

    def drift(self, keys: Iterable[str] | None = None, *, timeout_ms: int | None = None,
              poll_ms: int = 200) -> Any:
        """Check, on the page this healer is on, whether the selectors saved for keys still
        find their elements, and what a run would do about the ones that don't, without
        acting on the page, saving anything or asking a model. Returns a
        mendwright.drift.DriftReport: one KeyDrift per key, with a verdict ('ok', 'ok-heal',
        'wrong-role', 'would-heal', 'needs-model', 'refused', or one that says why the key
        could not be judged) and the reason, in the run's own words.

        keys=None checks the keys found on this page before (its page is their lastPage or
        one of their pages; on the site root, the keys whose lastUrl is a root of this host,
        whatever its port, which _remember_page saves only for a key found on no other
        page). keys=[...] checks those wherever they were found: a key never
        found on this page gets the page guard's refusal, as in a run, unless
        heal_across_pages=True. timeout_ms (default: the healer's) is one wait for the whole
        page, polled every poll_ms.

        It writes nothing: no store write, no journal line, no fingerprint, no quarantine,
        no canary. It changes nothing in heals, pending() or handled_interruptions, performs
        no action, handles no interruption and dismisses no popup, and Playwright's overlay
        handlers of this healer stay out of it. It never calls the provider. It logs one
        INFO line. TypeError or ValueError for arguments that can never work (a single
        string as keys, keys the store doesn't have), before anything touches the page."""
        from . import drift as _drift
        wanted = _drift.drift_keys(self.store, keys, timeout_ms, poll_ms)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        skips, canaries = _drift.journal_evidence(_drift.store_journal(self.store))
        with self._overlays.handling():
            url = self._url()
            if wanted is None:
                wanted = _drift.keys_on_page(self.store, url)
            report = _drift.probe_page(self, wanted, timeout_ms=timeout, poll_ms=poll_ms,
                                       skips=skips, canaries=canaries,
                                       optional=set(self._interruptions), url=url)
        logger.info('mendwright: ' + report.summary())
        return report

    # --- lists (see mendwright.lists) ------------------------------------------------

    def locator_all(self, key: str, selector: Selector | None = None, *,
                    role: Role | None = None, timeout_ms: int | None = None, poll_ms: int = 200,
                    optional: bool = False, nonempty: bool = False) -> list:
        """The elements of a list, such as the cards of a product grid: an ActionProxy for
        each element key's selector matches, in page order. mendwright never gives a list
        that the key's own selectors don't match, those in your code or in a seed file.

        selector, role (the items' role, read from the first item when it is left out),
        timeout_ms and poll_ms mean what they mean for locator(). The first of key's
        selectors that matches one element or more gives the list, as written: the selector
        in your code first, then those of a seed file. A heal saved under the same key is
        skipped: a heal of one element, such as by heal.driver.find_element(), is no heal of
        the list, and no list heal is ever used. Each time one matches two or more,
        mendwright records what the items look like (a list fingerprint, once per Healer and
        selector), which says the list had items. When none matches:

        - a list mendwright has not seen with two or more items, or one with optional=True,
          gives [] at once, as Locator.all() and find_elements() do, and so does any list
          while the fingerprint tier is off (fingerprint=False, MENDWRIGHT_FINGERPRINT=0 or
          MENDWRIGHT_DISABLE=1), which records no list fingerprint;
        - any other waits up to timeout_ms, handling registered interruptions, and raises
          AttentionRequired, which says that either the list is empty at this point or its
          selector broke. mendwright can't tell the two apart: the page after a cart's items
          all moved to Saved for later can be the page after a redesign but for a class
          name. So it looks for no other list, asks no model and saves nothing, and a
          selector that broke is fixed in your code.

        nonempty=True says the list always has items at this point, such as the rows of a
        cart the test just filled. Such a lookup records where the list sits and its heading
        with its list fingerprint, and so does every later lookup of the key. When none of
        its selectors matches, it always waits timeout_ms, then looks for the list on the
        page (see mendwright.lists) and never uses what it finds: it saves it in the store as
        a proposed list heal and raises AttentionRequired with the new selector, where the
        list was and where the group found is, and the commands to check it (mendwright
        lists), accept it (mendwright lists accept KEY ID) and write it into your code
        (mendwright fix --apply). Only then does the list come from it, as your selector.
        When it finds nothing it can propose, AttentionRequired says why. A list heal you
        accepted is quarantined once the selector it replaced matches again, in any lookup
        of the key.

        The proxies' actions handle interruptions and popups as locator()'s do, and confirm
        or quarantine nothing, as their confirm() does. Raises InvalidSelector, TypeError or
        ValueError for arguments that can never work (nonempty= other than True or False,
        or nonempty=True with optional=True), before anything touches the page."""
        __tracebackhide__ = True  # pytest shows the caller
        roles, code = _locator_args(key, selector, role, None, entry='heal.locator_all()')
        _nonempty_args(key, nonempty, optional)
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        natives = self._list(key, roles, code, timeout, poll_ms, optional, nonempty)
        due = [True]    # one check of the interruptions for the reads of the whole list
        hosts = self._shadow_place(key) if natives else None
        return [self._item(key, native, timeout, poll_ms, due, hosts) for native in natives]

    def _item(self, key, native, timeout_ms, poll_ms, due=None, hosts=None):
        """An ActionProxy for one element of list key, with the heal canary off: its actions
        and its confirm() report nothing, since no list is found through a heal. due, when
        given, is shared by the elements of one lookup (see ActionProxy._before_read), and
        hosts are those of the shadow root the list was found in (see _shadow_place)."""
        proxy = ActionProxy(self, key, native, timeout_ms=timeout_ms, poll_ms=poll_ms)
        proxy._canary = False
        proxy._hosts = hosts
        if due is not None:
            proxy._reads_due = due
        return proxy

    def _list_known(self, key):
        """True when the store has a list fingerprint for key: it matched two or more
        elements before."""
        try:
            return valid_list(self.store.meta(key).get('listFingerprint'))
        except Exception:
            return False

    def _list_waits(self, key):
        """True when a list lookup of key that matches nothing waits and raises: key has a
        list fingerprint and the fingerprint tier is on. With fingerprint=False,
        MENDWRIGHT_FINGERPRINT=0 or MENDWRIGHT_DISABLE=1 such a lookup gives [] at once, as
        the real page or driver does."""
        return bool(getattr(self.coordinator, 'fingerprint', False)) and self._list_known(key)

    def _list_runnable(self, key):
        """key's candidates a list lookup tries: those on the page (lists are not looked for
        inside an iframe, nor through a row saved inside a shadow root) that can run, without
        heals. A list key can also be the key of
        locator(), of a heal.page shortcut or of heal.driver's find_element(), and a heal of
        the first card is no heal of the cards. A list heal (saved with "list": true by a
        build of 0.8.0 before its release) is not used either: it can be another list that
        holds the items of an emptied one. Raises the first InvalidSelector when none can."""
        candidates = [c for c in self.store.candidates(key) if not c.get('frameXPath')
                      and not hosts_of(c) and c.get('source') != 'heal']
        if not candidates:
            raise AttentionRequired('No selector candidates for: ' + key)
        runnable, errors = _split_runnable(key, candidates)
        if not runnable:
            raise errors[0]
        return runnable

    def _list_look(self, key, runnable):
        """(selector, count) for the first of runnable that matches one element or more now,
        recorded as the key's success, or None."""
        for candidate in runnable:
            try:
                count = self._driver.count(candidate['xpath'])
            except InvalidSelector:
                raise
            except Exception:
                count = 0
            if count:
                self.store.record_success(key, candidate['xpath'])
                return candidate['xpath'], count
        return None

    def _list_attempt(self, key, wait, poll_ms):
        """_list_look() until one matches or wait (ms) runs out; AttentionRequired then."""
        runnable = self._list_runnable(key)
        deadline = time.monotonic() + wait / 1000.0
        while True:
            found = self._list_look(key, runnable)
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                raise AttentionRequired(_no_match_message(key, runnable, []))
            time.sleep(poll_ms / 1000.0)

    def _list(self, key, roles, code, timeout, poll_ms, optional=False, nonempty=False):
        """The native handles of list key (see locator_all), with code primed first (see
        _prime). What a lookup that matches at once saves (the code's selectors, the first
        success, the page, the list fingerprint, a list heal it retires) costs one write of
        the store."""
        __tracebackhide__ = True  # pytest shows the caller
        with _batched(self.store):
            self._prime(key, roles, code, None, None)
            found = self._list_look(key, self._list_runnable(key))
            if found is not None:
                return self._list_found(key, roles, found, nonempty)
        if not nonempty and (optional or not self._list_waits(key)):
            return []
        budget = Budget()
        with self._running(key, budget, timeout, poll_ms):
            try:
                found = self._resolve_step(
                    key, lambda: self._list_attempt(key, timeout, poll_ms), budget, timeout,
                    poll_ms)
            except InterruptionLoop:
                raise   # the page is blocked
            except AttentionRequired as exc:
                if _raised_by_run(exc):
                    raise   # a run= function's own, not a miss of this list
                if nonempty:
                    return self._list_nonempty_missing(key, roles, exc)
                raise self._list_missing(key, exc) from exc
        with _batched(self.store):
            return self._list_found(key, roles, found, nonempty)

    def _list_found(self, key, roles, found, place=False):
        """The native handles of what found, (selector, count), matched for list key, with
        the page saved and, when it should be read (see _list_target), the list
        fingerprint: with where the list sits when place (nonempty=True) or when the stored
        one already holds it. The key's list heals that selector was replaced by are retired
        (see _retire_lists)."""
        selector, count = found
        self._remember_page(key)
        placed = bool(place) or self._list_placed(key)
        if self._list_target(key, selector, count, placed):
            self._keep_list(key, selector, roles,
                            driver_list_fingerprint(self._driver, selector, place=placed), placed)
        self._retire_lists(key, selector, count)
        return driver_handles(self._driver, selector)

    def _list_placed(self, key):
        """True when key's stored list fingerprint holds where the list sits."""
        try:
            return placed_list(self.store.meta(key).get('listFingerprint'))
        except Exception:
            return False

    def _list_rows(self, key):
        """key's list heals that wait (store.list_heals), or [] for a store without
        list_heals() or retire_list_heals()."""
        store = self.store
        if not (callable(getattr(store, 'list_heals', None))
                and callable(getattr(store, 'retire_list_heals', None))):
            return []
        try:
            return list(store.list_heals(key))
        except Exception:
            return []

    def _reload_lists(self, key):
        """Take in what mendwright lists accept or reject did to key's list heals since the
        store was read (SelectorStore.reload_list_heals), so a Healer that lives across an
        accept or a reject doesn't propose that list heal again. Nothing for a store without
        reload_list_heals()."""
        method = getattr(self.store, 'reload_list_heals', None)
        if not callable(method):
            return
        try:
            method(key)
        except Exception:
            logger.debug('mendwright: could not reload the list heals of %r', key, exc_info=True)

    def _retire_lists(self, key, selector, count):
        """End key's list heals that selector, which just matched count elements, replaced
        (SelectorStore.retire_list_heals): a proposed one is no longer proposed, an accepted
        one is quarantined, with a WARNING and a heal.quarantine journal event. Nothing for a
        key with none, which no 0.8.0 store has."""
        rows = self._list_rows(key)
        if not rows:
            return
        try:
            changes = self.store.retire_list_heals(key, selector)
        except Exception:
            logger.debug('mendwright: could not retire the list heals of %r', key, exc_info=True)
            return
        if changes:
            self.coordinator.retired_lists(key, selector, count, changes, rows)

    def _list_nonempty_missing(self, key, roles, exc):
        """What a lookup of list key with nonempty=True does when none of its selectors
        matched (exc): the native handles of a list that came while mendwright looked for it,
        or AttentionRequired with exc's text and why: the fingerprint tier is off, a list
        heal you accepted waits for mendwright fix, or what HealCoordinator.propose_list()
        proposed or refused."""
        __tracebackhide__ = True  # pytest shows the caller
        reason = self._nonempty_blocked(key)
        if reason:
            raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason) from exc
        self._reload_lists(key)
        for row in self._list_rows(key):
            if row.get('status') == 'accepted':
                count = self._count_live(row.get('xpath'))
                if count:
                    raise self._waits_for_fix(key, exc, row, count) from exc
        outcome = self._propose_list(key, exc, self._url())
        if outcome.kind == 'came':
            with _batched(self.store):
                found = self._list_look(key, self._list_runnable(key))
                if found is not None:
                    self.coordinator.came(key, found[0])
                    return self._list_found(key, roles, found, True)
        raise self._list_outcome_error(key, exc, outcome) from exc

    def _nonempty_blocked(self, key):
        """The refusal of a list looked up with nonempty=True while the fingerprint tier is
        off, naming what turned it off, else ''."""
        coordinator = self.coordinator
        if getattr(coordinator, 'fingerprint', False):
            return ''
        off_by = getattr(coordinator, 'fingerprint_off_by', None)
        if not isinstance(off_by, str) or not off_by:
            off_by = "the HealCoordinator's fingerprint=False"
        return coordinator.decline_nonempty(
            key, 'mendwright looks for a list only while the fingerprint tier is on, and '
            + off_by + ' turns it off.')

    def _count_live(self, selector):
        """How many elements selector matches on the page now; 0 on any error."""
        try:
            return int(self._driver.count(selector)) if isinstance(selector, str) else 0
        except Exception:
            return 0

    def _waits_for_fix(self, key, exc, row, count):
        """The AttentionRequired for list key, when a list heal you accepted for it (row)
        matches count elements and waits for mendwright fix."""
        reason = self.coordinator.waits_for_fix(key, self.store.meta(key), row, count)
        return AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason)

    def _propose_list(self, key, exc, url):
        """HealCoordinator.propose_list() for list key, which nonempty=True says has items
        and whose lookup raised exc, on the page at url. Runs in a worker thread on the async
        healer, where the coordinator reaches the page through SyncBridge."""
        return self.coordinator.propose_list(key, self.store.meta(key),
                                             self._list_runnable(key),
                                             self._flow_context(key, url))

    def _list_outcome_error(self, key, exc, outcome):
        """The AttentionRequired for what propose_list() did (see ListOutcome), but a list
        that came and is still there: exc's text and the reason."""
        if outcome.kind == 'came':
            reason = self.coordinator.came_and_went(key, self.store.meta(key), outcome.xpath,
                                                    outcome.reason)
        elif outcome.kind == 'accepted':
            row = outcome.row or {'xpath': outcome.xpath}
            reason = self.coordinator.waits_for_fix(key, self.store.meta(key), row,
                                                    outcome.count)
        else:
            reason = outcome.reason
        return AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason)

    def _list_missing(self, key, exc):
        """The AttentionRequired for list key, whose lookup raised exc when none of its
        selectors matched: exc's text and why the list is not healed
        (HealCoordinator.decline_list), which the log gets too."""
        reason = self.coordinator.decline_list(key, self.store.meta(key))
        return AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason)

    def _list_target(self, key, selector, count, place=False):
        """True when the list fingerprint of what selector matched for key should be read:
        the fingerprint tier is on, it matched two or more, this Healer has not read it for
        that selector yet (with where it sits, when place), and the key is not marked
        destructive."""
        if not getattr(self.coordinator, 'fingerprint', False) or count < 2:
            return False
        if ('list', key, selector, bool(place)) in self._fingerprinted:
            return False
        try:
            return not self.store.meta(key)['destructive']
        except Exception:
            return False

    def _keep_list(self, key, selector, roles, lfp, place=False):
        """Save lfp as key's list fingerprint, and the items' role when role= did not say.
        When place (where the list sits) was asked for and lfp has none, a stored list
        fingerprint that holds one is left as it is: a failed read never removes a place."""
        self._fingerprinted.add(('list', key, selector, bool(place)))
        if place and not placed_list(lfp) and self._list_placed(key):
            return
        if lfp is None:
            return
        try:
            self.store.set_meta(key, list_fingerprint=lfp)
            if not roles and self.store.role_is_guess(key):
                self.store.set_meta(key, role=item_role(lfp))
        except Exception:
            logger.debug('mendwright: could not save the list fingerprint of %r', key,
                         exc_info=True)

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
            _canary_call(self.coordinator.confirm_pending, key, bool(success), by='confirm')
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
        """(selector, frame_chain) the candidate that just matched runs with, or None: for
        one inside a shadow root, its CSS, and a chain that ends in its shadow hosts (see
        shadow.run_target)."""
        from .shadow import chain_of, run_target
        winner = self.store.last_success(key)
        if not winner:
            return None
        try:
            return run_target(winner)
        except InvalidSelector:
            return winner['xpath'], chain_of(winner)    # no driver runs it: reads give None

    def _shadow_place(self, key, driver=None):
        """The CSS of the shadow hosts of the candidate key just matched through, when it
        runs inside a shadow root on a driver whose CSS pierces shadow roots and that can
        tell where a Locator's matches are (placed()), else None. Playwright resolves the
        Locator again for each action and read, so the proxy checks it is still there right
        before each one (see ActionProxy._wait_placed). driver is this healer's unless
        given (the async healer passes its own)."""
        driver = self._driver if driver is None else driver
        if not (css_pierces(driver) and callable(_member(driver, 'placed'))):
            return None
        target = self._winner_target(key)
        hosts = tuple(str(s) for s in (target[1] if target else ()) if isinstance(s, ShadowHost))
        return hosts or None

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

    def _healed(self, key):
        """The selector that just matched for key when a heal saved it (in this process or
        an earlier one), else None."""
        winner = self.store.last_success(key)
        if not winner or winner.get('source') != 'heal':
            return None
        return winner['xpath'] if isinstance(winner.get('xpath'), str) else None

    def _check_value(self, key, healed, verb, args, value, expect):
        """value, which the read verb(*args) returned for key, when it passes expect (see
        mendwright.values) or key was not found through a heal (healed is None). A value
        that passes confirms a heal of key made in this process. One that fails quarantines
        the heal, and AttentionRequired is raised with the key, the heal and the value."""
        __tracebackhide__ = True  # pytest shows the caller
        if healed is None or expect is None:
            return value
        coordinator = self.coordinator
        ok = expect.passes(value)
        reason = None if ok else refusal_reason(verb, args, expect)
        if coordinator.is_pending(key, healed):
            _canary_call(coordinator.confirm_pending, key, ok, reason=reason, xpath=healed,
                         by='expect_value')
        elif not ok:
            self.store.quarantine(key, healed)
            coordinator.journal.quarantine(key, xpath=healed, reason=reason)
        if ok:
            return value
        message = refused_message(key, healed, verb, args, value, expect)
        coordinator._emit(logging.WARNING, message)
        raise AttentionRequired('mendwright: ' + message)

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
        if coordinator.is_pending(key, xpath):
            # A heal from earlier in this process.
            _canary_call(coordinator.confirm_pending, key, False, xpath=xpath, by='destructive')
        else:
            # A heal saved before the key was marked: by says why, so mendwright notify
            # does not call the repair wrong.
            self.store.quarantine(key, xpath)
            coordinator.journal.quarantine(key, xpath=xpath, by='destructive')
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
        """The registered interruptions other than the step ``before`` itself, and other
        than those whose run= function is running in this context: the actions of a run=
        function do not handle its own interruption, which it is filling in, but they do
        handle another one, such as a cookie banner over its form."""
        mine = {run.key for run in _IN_RUN.get() if run.healer is self}
        return [item for key, item in self._interruptions.items()
                if key != before and key not in mine]

    def _in_run(self):
        """Whether a run= function of this healer runs now, in this thread, asyncio task or
        greenlet (see _run_function): its own actions dismiss no popup, and on the async
        healer they skip the lock (see AsyncHealer._turn)."""
        return any(run.healer is self for run in _IN_RUN.get())

    def _await_runs(self, budget, poll_ms):
        """Wait for the run= functions of this healer that run in another context, then
        raise what one of them raised for the step of budget (_reraise).

        On sync Playwright that context is the greenlet in which Playwright's overlay
        handler called the function. When the function takes longer than the action's
        timeout, the action gives up and the step goes on while the function is still
        filling in its form, in the same thread. The step must not act on the page then,
        nor call the function a second time, so it waits here, through the page
        (page.wait_for_timeout), which lets the handler's greenlet go on: time.sleep would
        hold both. It waits with no limit, as a step that calls the function itself does.
        Elsewhere, such as another thread, it waits with time.sleep."""
        __tracebackhide__ = True
        while True:
            mine = _IN_RUN.get()
            if not any(run not in mine for run in self._runs):
                break
            self._pause(poll_ms)
        _reraise(budget)

    def _pause(self, poll_ms):
        """Wait one poll while a run= function runs elsewhere (see _await_runs)."""
        from .drivers.playwright_sync import PlaywrightSyncDriver
        wait = getattr(self._page, 'wait_for_timeout', None)
        if isinstance(self._driver, PlaywrightSyncDriver) and callable(wait):
            wait(max(poll_ms, 20))
        else:
            time.sleep(max(poll_ms, 20) / 1000.0)

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

    def _interrupt(self, before, budget, timeout_ms, poll_ms, driver=None, only=None,
                   uncovered=None, overlay=False):
        """Handle every registered interruption (other than ``before``) that the page
        shows now, or only the one registered as ``only``. True if at least one was
        handled. Raises AttentionRequired when the step's budget is used up and one is
        still there, and what a run= function raised. driver is the driver to use (the
        async healer passes its SyncBridge, from a worker thread). overlay is True when
        Playwright's overlay handler runs this (see _overlay_run).

        uncovered, when given, is called once the page shows one: when it returns True,
        nothing covers the element the step acts on, and none is handled (see
        ActionProxy._act)."""
        __tracebackhide__ = True
        # A run= function the overlay handler is still running, then its error: no second call
        self._await_runs(budget, poll_ms)
        others = [item for item in self._others(before) if only is None or item.key == only]
        if not others:
            return False
        driver = driver or self._driver
        handled = False
        with self._overlays.handling():     # Playwright's handlers stay out of it
            for item in others:
                target = self._interruption_target(item, driver)
                if target is None:
                    self._stuck.pop(item.key, None)    # gone: it may be handled again
                    continue
                if uncovered is not None:
                    if uncovered():
                        return False    # not in the step's way; it may be part of the step
                    uncovered = None
                if budget.used >= MAX_INTERRUPTIONS_PER_STEP:
                    journal_event(self.coordinator.journal, 'flow.stop', item.key,
                                  code='interruption_loop', before=before)
                    raise InterruptionLoop(loop_message(
                        before, item.key, budget.used,
                        function_name(item.run) if item.run is not None else None))
                budget.used += 1
                if self._run_interruption(item, target, before, timeout_ms, poll_ms, driver,
                                          overlay, budget):
                    budget.handled.append(item.key)
                    handled = True
        return handled

    def _interruption_target(self, item, driver, warn=True):
        """A selector that points at one visible element of the interruption right now
        (and of its role, when it has one), or None. Any error reads as not there. When
        none of its selectors gives one, the first of the user's that matches several
        elements, so that it was skipped (or, for an XPath, looked at in its first matches
        only), is named in one WARNING per page (see _several), unless warn is False: not
        while mendwright waits for one it handled to go away (_settle)."""
        selectors = item.selectors
        if item.probe:
            try:
                if not driver.count(item.probe):
                    selectors = [s for s in selectors if classify(s) != XPATH]
            except Exception:
                pass
        several = None
        for selector in selectors:
            try:
                count = driver.count(selector)
            except Exception:
                continue
            targets = nth_targets(selector, count)
            for target in targets:
                try:
                    if driver.is_visible(target) and self._role_fits(item, target, driver):
                        return target
                except Exception:
                    continue
            if count > len(targets) and several is None:
                several = (selector, count, len(targets))
        if several is not None and warn and item.preset is None:
            self._several('interruption', item.key, *several, driver)
        return None

    def _several(self, what, name, selector, count, looked, driver, url=None):
        """The WARNING for an interruption (what='interruption') or a step's landmark
        ('landmark') that mendwright skipped because its selector matches count elements
        and it looked at only the first ``looked`` of them (see flow.several_message),
        once per name and page. When exactly one of its matches is visible, it names
        selectors that match one visible element on this page, built from that match (see
        interruptions.precise_selectors) and checked on the page: a few round trips, once.
        When more than one is, or the driver cannot say how many (see
        drivers.base.driver_visible_match), it names none, since one built from the first
        could be the wrong element. url is the page's address, when the caller has read
        it."""
        if url is None:
            try:
                url = driver.url() or ''
            except Exception:
                url = ''
        seen = (what, name, page_path(url))
        if seen in self._several_seen:
            return
        self._several_seen.add(seen)
        summary = driver_visible_match(driver, selector)
        if not callable(getattr(driver, 'visible_match', None)):
            visible = None                  # the driver cannot tell
        elif summary is None:
            visible = 0
        else:
            visible = summary.get('visible')
            if not isinstance(visible, int) or isinstance(visible, bool) or visible < 1:
                visible = None              # a driver that does not count them
        playwright = _steps._playwright(self._driver)
        found, css = [], False
        for candidate in precise_selectors(summary, playwright) if visible == 1 else ():
            if css and classify(candidate) == CSS:
                continue    # one CSS form is enough; the others say the same
            try:
                if driver.count(candidate) == 1 and driver.is_visible(candidate):
                    found.append(candidate)
                    css = css or classify(candidate) == CSS
            except Exception:
                continue
            if len(found) == 2:
                break
        self.coordinator._emit(logging.WARNING, several_message(
            what, name, selector, count, looked, found, visible=visible,
            playwright=playwright))

    def _role_fits(self, item, target, driver):
        if not item.roles:
            return True
        if not callable(getattr(driver, 'describe', None)):
            return True     # the driver cannot read elements: the selector is all we have
        described = driver_describe(driver, target)
        return bool(described) and described.get('role') in item.roles

    def _run_interruption(self, item, target, before, timeout_ms, poll_ms, driver,
                          overlay=False, budget=None):
        """Perform the interruption's action on target, or call its run= function (see
        _run_function), wait for it to go away, and record it. False (with a WARNING) when
        the action failed in the browser; what a run= function raised is raised."""
        function = None
        if item.run is not None:
            function = function_name(item.run)
            self._run_function(item, target, before, driver, overlay, budget)
        else:
            try:
                native = driver.handle(target)
                driver_act_on(driver, native, item.action, *item.args)
            except _USAGE_ERRORS:
                raise   # a wrong action or arguments in heal.interrupt(): the caller's to fix
            except Exception as exc:
                self.coordinator._emit(logging.WARNING, unhandled_message(item.key, before, exc))
                return False
        if self._settle(item, timeout_ms, poll_ms, driver):
            self._stuck.pop(item.key, None)
        else:
            self._stuck[item.key] = (self._stuck_count(item.key) + 1,
                                     self._overlays.navigations)
        self._handled.setdefault(item.key, before)
        self._interrupt_log.append(InterruptionEvent(item.key, before))
        self.coordinator._emit(logging.WARNING, handled_message(item.key, before, function))
        journal_event(self.coordinator.journal, 'flow.interrupt', item.key, before=before,
                      action=item.action, preset=item.preset,
                      selector=None if item.preset else target, function=function)
        return True

    def _run_function(self, item, target, before, driver, overlay, budget=None):
        """Call interruption item's run= function with heal.page (heal.driver on Selenium),
        or, on the async healer, have _runner await it on the event loop. While it runs
        (_IN_RUN, _runs), its own actions do not handle item again and dismiss no popup, so
        it never calls itself, and Playwright's overlay handlers stay out of them as they
        stay out of mendwright's own clicks (the caller holds _PageOverlays.handling()). The
        other registered interruptions are still handled before its actions. A step in
        another context waits for it (see _await_runs). It is not part of a step
        run_steps() runs, whose page its keys are not saved for. What it raises gets one
        WARNING and a flow.interrupt event with outcome 'failed', and is raised as it is,
        marked (_raised_by_run): the step it was handled for does not take an
        AttentionRequired from it for a miss of its own. When Playwright's overlay handler
        called it (overlay), the error goes into budget, the step's, before the run ends:
        on the async healer the step waits for the run in another thread (see
        aio.AsyncHealer._await_runs), and must find the error once the run is over. On the
        async healer, a step that is cancelled cancels it (see aio._in_thread)."""
        record = _Run(self, item.key)
        running, step = _IN_RUN.set(_IN_RUN.get() + (record,)), _RUNNING_STEP.set(None)
        self._runs.append(record)
        try:
            if self._runner is not None:
                self._runner(item, driver)
            else:
                item.run(self._step_page())
        except Exception as exc:
            function = function_name(item.run)
            cancelled = isinstance(exc, concurrent.futures.CancelledError)
            self.coordinator._emit(logging.WARNING, (
                run_cancelled_message(item.key, before, function) if cancelled
                else run_failed_message(item.key, before, function, exc, overlay)))
            journal_event(self.coordinator.journal, 'flow.interrupt', item.key, before=before,
                          action=item.action, selector=target, function=function,
                          outcome='failed', error=type(exc).__name__)
            try:
                exc._mendwright_run = True
            except Exception:
                pass        # an exception class with __slots__: it is raised unmarked
            if overlay and budget is not None and _raised_by_run(exc):
                budget.raised = exc     # the step raises it (see _overlay_run)
            raise
        finally:
            self._runs.remove(record)
            _RUNNING_STEP.reset(step)
            _IN_RUN.reset(running)

    def _settle(self, item, timeout_ms, poll_ms, driver):
        """Wait, up to SETTLE_MS (or the step's timeout when shorter), for the
        interruption to go away, for example while a banner fades out. False when it was
        still there at the end; True when it went away, or with no time to wait."""
        limit = min(max(timeout_ms or 0, 0), SETTLE_MS) / 1000.0
        if limit <= 0:
            return True
        deadline = time.monotonic() + limit
        while self._interruption_target(item, driver, warn=False) is not None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(poll_ms, 20) / 1000.0)
        return True

    def _before_action(self, key, timeout_ms, poll_ms, uncovered=None):
        """Handle what the page shows before an action on key, unless uncovered() says
        nothing covers the element (see _interrupt). Returns the step's budget, which a
        retry after an interception error shares."""
        __tracebackhide__ = True
        budget = Budget()
        self._interrupt(key, budget, timeout_ms, poll_ms, uncovered=uncovered)
        return budget

    def _handles_reads(self):
        """Whether the reads of heal.locator()'s proxies handle the registered
        interruptions first (see ActionProxy._before_read): on sync Playwright."""
        from .drivers.playwright_sync import PlaywrightSyncDriver
        return isinstance(self._driver, PlaywrightSyncDriver)

    @contextlib.contextmanager
    def _running(self, key, budget, timeout_ms, poll_ms):
        """Mark key's step as running for the block, so an interruption the overlay
        handler handles in the middle of it is logged before key and counts against the
        step's budget."""
        entry = (key, budget, timeout_ms, poll_ms)
        self._steps.append(entry)
        try:
            yield
        finally:
            self._steps = [e for e in self._steps if e is not entry]

    # --- Playwright's overlay handler ------------------------------------------------

    def _watch(self, item):
        """Register interruption ``item`` with page.add_locator_handler (Playwright 1.42 and
        later), so Playwright hands over to mendwright when the page shows it before a
        locator or selector action on the page, or while one waits. That covers the actions
        mendwright does not route (get_by_* and chained locators, the page's own methods)
        and a banner that opens after an action began. Only a sync Playwright Page has it;
        for a Frame or another driver this does nothing, and the checks of the steps
        mendwright routes stay as they are.

        The page keeps one handler per key (see _OVERLAYS). One with the same selectors is
        joined. One with other selectors, from this healer or another, is replaced (see
        _remove_overlay). A selector the browser cannot parse is left out (see _parsed)."""
        from .drivers.playwright_sync import PlaywrightSyncDriver
        page, overlays, key = self._page, self._overlays.handlers, item.key
        if not isinstance(self._driver, PlaywrightSyncDriver) or not callable(
                getattr(page, 'add_locator_handler', None)):
            return
        self._overlays.listen(page)
        selectors = self._parsed(item)
        if selectors and _join_overlay(overlays, key, selectors, self):
            return
        old = overlays.pop(key, None)
        if old is not None:
            self._remove_overlay(old[0])
        if not selectors:
            return
        token = object()

        def handler(*_locator):
            healer = _overlay_owner(overlays, key, token)   # the page keeps no healer alive
            if healer is not None:
                healer._overlay(key)

        try:
            locator = _overlay_locator(page, selectors)
            overlays[key] = (selectors, (weakref.ref(self),), token)
            page.add_locator_handler(locator, handler,
                                     **_overlay_options(page.add_locator_handler))
        except Exception:
            if overlays.get(key, (None, None, None))[2] is token:
                overlays.pop(key, None)
            logger.debug('mendwright: could not register an overlay handler for %r', key,
                         exc_info=True)

    def _unwatch(self, key):
        """Take this healer off key's overlay handler (interrupt(key, None)), and remove
        the handler from the page once no healer of the page is left on it."""
        from .drivers.playwright_sync import PlaywrightSyncDriver
        if isinstance(self._driver, PlaywrightSyncDriver):
            self._remove_overlay(_leave_overlay(self._overlays.handlers, key, self))

    def _remove_overlay(self, selectors):
        """Remove the page's handler for selectors, with Playwright 1.44 and later. Before
        that it stays on the page, and does nothing once its entry is gone. It also stays
        while the handler of another key watches the same selectors, because Playwright
        would remove that one with it (see _removable)."""
        remove = getattr(self._page, 'remove_locator_handler', None)
        if not callable(remove) or not _removable(self._overlays.handlers, selectors):
            return
        try:
            remove(_overlay_locator(self._page, selectors))
        except Exception:
            pass    # left on the page, it does nothing: its token is gone

    def _parsed(self, item):
        """The selectors of interruption item that the browser can parse, for Playwright's
        overlay handler.

        Playwright's check before an action re-raises a selector the browser rejects, so
        one such selector in the handler would fail every action on the page with a parse
        error. check_syntax() checks XPath without a browser but leaves CSS and engine
        selectors to the browser (a 'button:contains("Close")' passes it), so each of those
        is counted once on the page here, and one the browser rejects is left out, with a
        WARNING. Any other error keeps it."""
        from .drivers.playwright_sync import browser_rejected, playwright_selector
        parsed, kept = self._overlays.parsed, []
        for selector in item.selectors:
            if classify(selector) != XPATH and selector not in parsed:
                try:
                    self._page.locator(playwright_selector(selector)).count()
                except Exception as exc:
                    if browser_rejected(exc):
                        self.coordinator._emit(logging.WARNING, rejected_selector_message(
                            item.key, selector, exc))
                        continue
                else:
                    parsed.add(selector)
            kept.append(selector)
        return tuple(kept)

    def _overlay(self, key):
        """What Playwright runs, on this thread, when the page shows interruption key's
        element before an action (see _watch)."""
        if self._overlay_wanted(key):
            self._overlay_run(key, self._driver)

    def _overlay_wanted(self, key):
        """Whether the overlay handler for key should look at the page now. Not while a
        healer of the page handles interruptions itself or looks up a key (its own action
        on the banner and the lookup's scroll run Playwright's check too: the step handles
        what the page shows before its action or read), and not for an interruption that was
        still on the page after MAX_INTERRUPTIONS_PER_STEP actions in a row, until
        mendwright sees it gone or the page navigates (see _stuck_count)."""
        return (not self._overlays.busy and key in self._interruptions
                and self._stuck_count(key) < MAX_INTERRUPTIONS_PER_STEP)

    def _stuck_count(self, key):
        """How many times in a row interruption key was still on the page after its action,
        on the page as it is now. A navigation or load of the page since then (see
        _PageOverlays) starts again at 0: the overlay handler is called only while the
        interruption shows, so it never sees one go away on a script that acts only
        through locators mendwright does not route, and a navigation is its sign that the
        page changed."""
        count, navigations = self._stuck.get(key, (0, None))
        return count if navigations == self._overlays.navigations else 0

    def _overlay_run(self, key, driver):
        """Handle interruption key if the page shows it, for the step that is running when
        exactly one is, else for an action mendwright did not route (UNROUTED_STEP, with a
        budget of its own). Playwright calls each handler whose element shows in turn, so
        this handles key's alone. driver is the driver to use, as for _interrupt(). Never
        raises: Playwright runs this in the middle of an action, which goes on either way.

        What a run= function raises here (one WARNING already says so) cannot reach the
        action, which goes on. The step that is running, if any, keeps it in its budget and
        raises it once that action ends (_reraise), when it goes through or its timeout runs
        out, without calling the function again. And the interruption is left alone, as one
        still there after 3 times in a row is, until mendwright sees it gone or the page
        navigates: before every action mendwright does not route, it would only fail again."""
        steps = self._steps
        if len(steps) == 1:
            before, budget, timeout_ms, poll_ms = steps[0]
        else:
            before, budget, timeout_ms, poll_ms = UNROUTED_STEP, Budget(), self.timeout_ms, 200
        if budget.used >= MAX_INTERRUPTIONS_PER_STEP or budget.raised is not None:
            return      # the step itself raises InterruptionLoop, or the function's error
        stuck = self._stuck_count(key)
        try:
            self._interrupt(before, budget, timeout_ms, poll_ms, driver, only=key, overlay=True)
        except Exception as exc:
            if _raised_by_run(exc):
                self._stuck[key] = (MAX_INTERRUPTIONS_PER_STEP, self._overlays.navigations)
                budget.raised = exc
            elif not isinstance(exc, InterruptionLoop):
                self.coordinator._emit(logging.WARNING, unhandled_message(key, before, exc))
            return
        now = self._stuck_count(key)
        if stuck < MAX_INTERRUPTIONS_PER_STEP <= now:
            item = self._interruptions.get(key)
            run = getattr(item, 'run', None)
            self.coordinator._emit(logging.WARNING, stuck_message(
                key, now, function_name(run) if run is not None else None))

    # --- popups nobody registered: open(dismiss=True) ---------------------------------

    def _dismiss_before(self, key, native, verb, budget, timeout_ms, poll_ms):
        """Before an action on native, after _before_action() handled the registered
        interruptions the page shows: with open(dismiss=True), dismiss a popup that covers
        its element (see _dismiss). Not before focus() or set_input_files(), which work on a
        covered element. With dismiss off it makes no browser call."""
        __tracebackhide__ = True
        if not self._dismiss_on or verb in NOT_LOOKED_AT:
            return False
        return self._dismiss(key, native, budget, timeout_ms, poll_ms)

    def _unblock(self, key, native, budget, timeout_ms, poll_ms):
        """After an action on key failed because something covered native's element:
        handle the registered interruptions the page shows, else, with open(dismiss=True),
        dismiss a popup over the element. True when one was handled or dismissed."""
        __tracebackhide__ = True
        return (self._interrupt(key, budget, timeout_ms, poll_ms)
                or self._dismiss(key, native, budget, timeout_ms, poll_ms))

    def _dismiss(self, before, native, budget, timeout_ms, poll_ms, driver=None):
        """With open(dismiss=True), dismiss each popup that covers native's element, while
        the step's budget lasts. True when at least one was dismissed.

        What counts as covered and as a popup is mendwright.dismiss's to say: it never
        acts on a popup that holds the element or on a small fixed bar. Nothing is
        dismissed while an interruption the step handled is still on the page (one that
        fades out slowly may be the popup over the element), nor a role="alertdialog",
        which asks for an answer (asks_answer), nor, with open(cookies="accept"), a popup
        that looks like a consent banner or has a button that says accept, agree or allow
        (is_consent, asks_consent), nor while a run= function of interrupt() runs, in this
        context or another (_runs): it may be filling in the very popup. Each popup counts
        once against the budget, whatever was tried on it, and InterruptionLoop is raised
        when the budget is used up and one is still in the way (one that comes back each
        time, say). A popup that could not be dismissed is named in one WARNING and not
        looked at again in this step (budget.gave_up). driver is the driver to use (the
        async healer passes its SyncBridge, from a worker thread)."""
        __tracebackhide__ = True
        if not self._dismiss_on or native is None or budget.gave_up or self._runs:
            return False
        driver = driver or self._driver
        dismissed = again = False
        with self._overlays_out():
            if self._interruption_left(budget, driver):
                return False
            probe = parse_probe(driver_popup_probe(driver, native, None, True))
            while True:
                popup = popup_of(probe)
                if popup is None:
                    return dismissed
                if asks_answer(popup):
                    self.coordinator._emit(logging.WARNING, alert_message(before, popup.summary))
                    budget.gave_up = popup.summary
                    return dismissed
                if self._accepts_consent() and is_consent(popup):
                    self.coordinator._emit(logging.WARNING,
                                           consent_message(before, popup.summary))
                    budget.gave_up = popup.summary
                    return dismissed
                if budget.used >= MAX_INTERRUPTIONS_PER_STEP:
                    # No popup text in the event: the code and the step it was in front of.
                    journal_event(self.coordinator.journal, 'flow.stop', DISMISS_KEY,
                                  code='popup_loop', before=before)
                    raise InterruptionLoop(dismiss_loop_message(
                        before, popup.summary, budget.used, DISMISS_KEY in budget.handled))
                root = driver_popup_root(driver, native, expect_of(popup))
                if root is None:
                    if not again:
                        again = True    # it changed between two looks: look once more
                        probe = parse_probe(driver_popup_probe(driver, native, None, True))
                        continue
                    self.coordinator._emit(logging.WARNING, undismissed_message(
                        before, popup.summary, [], changed=True))
                    budget.gave_up = popup.summary
                    return dismissed
                again = False
                if self._accepts_consent() and asks_consent(
                        driver_popup_controls(driver, native, root)):
                    driver_popup_release(driver, root)
                    self.coordinator._emit(logging.WARNING,
                                           consent_message(before, popup.summary))
                    budget.gave_up = popup.summary
                    return dismissed
                budget.used += 1
                done, probe = self._dismiss_popup(before, native, popup, root, timeout_ms,
                                                  poll_ms, driver)
                if not done:
                    budget.gave_up = popup.summary
                    return dismissed
                dismissed = True
                budget.handled.append(DISMISS_KEY)

    def _interruption_left(self, budget, driver):
        """Whether an interruption the step handled is still on the page: the popup over
        the element may be that one, a banner that fades out slowly, say, and declining it
        could undo what its registered action did."""
        for key in budget.handled:
            item = self._interruptions.get(key)
            if item is not None and self._interruption_target(item, driver) is not None:
                return True
        return False

    def _accepts_consent(self):
        """Whether open(cookies="accept") registered its preset."""
        item = self._interruptions.get(COOKIE_BANNER_KEY)
        return item is not None and item.preset == 'accept'

    @contextlib.contextmanager
    def _overlays_out(self):
        """Run the block with Playwright's overlay handlers left out, as _interrupt() does:
        mendwright's own Escape and clicks on a popup run Playwright's check too."""
        with self._overlays.handling():
            yield

    def _dismiss_popup(self, before, native, popup, root, timeout_ms, poll_ms, driver):
        """Press Escape (when the keyboard focus lets the key reach the popup, see
        driver_press_escape, and never with open(cookies="accept"): every key listener of
        the page gets the key, a consent banner's too), then click the popup's close
        button, then a decline button, and look again after each (_popup_settle). root is
        the handle on the popup that popup_root() gave, which this lets go of. (True, the
        last probe) once one worked; else (False, None) after the WARNING that names the
        popup. When the element can no longer be looked at after a step (it left the page or
        was hidden, say), or a click raised an error after it may have reached the page,
        mendwright cannot tell what that step did: it stops there and does not count the
        popup as dismissed."""
        limit = min(max(timeout_ms or 0, 0), SETTLE_MS)
        done = []      # (how, label) of each step that did not close it, in order
        escaped = False
        consent = self._accepts_consent()
        try:
            if not consent and driver_press_escape(driver, native, root):
                escaped = True
                outcome = self._after_step(before, native, popup, root, limit, poll_ms,
                                           driver, ('escape', None), done)
                if outcome is not None:
                    return outcome
            for kind in ('close', 'decline'):
                chosen = pick(driver_popup_controls(driver, native, root), popup).get(kind)
                if chosen is None:
                    continue
                index, control = chosen
                clicked = driver_popup_click(driver, native, root,
                                             expect_control(index, control),
                                             max(limit, 1000))   # 0 is no limit to Playwright
                if not clicked:
                    continue     # it changed since it was listed, and was not clicked
                step = (kind, control_text(control))
                if clicked == UNSURE:
                    done.append(step)
                    self.coordinator._emit(logging.WARNING, lost_message(
                        before, popup.summary, 'clicked ' + step_text(*step), raised=True))
                    self._journal_dismiss(before, popup, 'lost', None, None, done)
                    return False, None
                outcome = self._after_step(before, native, popup, root, limit, poll_ms,
                                           driver, step, done)
                if outcome is not None:
                    return outcome
            if done:
                # One last look: a popup that closes with an animation may be gone by now.
                probe = parse_probe(driver_popup_probe(driver, native, root))
                if _popup_state(probe) == 'gone':
                    how, label = done.pop()
                    self._record_dismissal(before, popup, how, label, done)
                    return True, probe
        finally:
            driver_popup_release(driver, root)
        self.coordinator._emit(logging.WARNING, undismissed_message(
            before, popup.summary, [step_text(*step) for step in done], escaped,
            consent=consent))
        if done:
            self._journal_dismiss(before, popup, 'failed', None, None, done)
        return False, None

    def _after_step(self, before, native, popup, root, limit_ms, poll_ms, driver, step, done):
        """Look again after one step, (how, label), on the popup. (True, probe) once it was
        recorded as dismissed; (False, None) after the WARNING for an element that can no
        longer be looked at; None when the popup is still there, with step added to done."""
        probe, state = self._popup_settle(native, root, limit_ms, poll_ms, driver)
        if state == 'gone':
            self._record_dismissal(before, popup, step[0], step[1], done)
            return True, probe
        done.append(step)
        if state == 'lost':
            how, label = step
            did = 'pressed Escape' if how == 'escape' else 'clicked ' + step_text(how, label)
            self.coordinator._emit(logging.WARNING, lost_message(before, popup.summary, did))
            self._journal_dismiss(before, popup, 'lost', None, None, done)
            return False, None
        return None

    def _popup_settle(self, native, root, limit_ms, poll_ms, driver):
        """Look at native's element again until the popup's root is gone or hidden, or the
        element is no longer covered, for up to limit_ms. Looks at least once, even with no
        time to wait. Returns (probe, state): the last probe, and _popup_state() of it:
        'gone', 'lost' or, at the end, 'there'."""
        deadline = time.monotonic() + limit_ms / 1000.0
        while True:
            probe = parse_probe(driver_popup_probe(driver, native, root))
            state = _popup_state(probe)
            if state != 'there' or time.monotonic() >= deadline:
                return probe, state
            time.sleep(max(poll_ms, 20) / 1000.0)

    def _record_dismissal(self, before, popup, how, label, done):
        """One WARNING, a flow.dismiss journal event and an entry in handled_interruptions
        for a popup that was dismissed with how ('escape', 'close' or 'decline'; label is
        the button's) after the steps in done did not close it."""
        self._interrupt_log.append(InterruptionEvent(DISMISS_KEY, before, popup=popup.summary,
                                                     how=how, label=label))
        self.coordinator._emit(logging.WARNING, dismissed_message(
            before, popup.summary, step_text(how, label), [step_text(*s) for s in done]))
        self._journal_dismiss(before, popup, 'dismissed', how, label, done)

    def _journal_dismiss(self, before, popup, outcome, how, label, done):
        """The flow.dismiss journal event: outcome is 'dismissed' (how and label say with
        what), 'failed' or 'lost'; tried lists each step before it that did not close the
        popup, so every key mendwright pressed and button it clicked is on record."""
        tried = [{'how': h} if lab is None else {'how': h, 'label': lab} for h, lab in done]
        journal_event(self.coordinator.journal, 'flow.dismiss', DISMISS_KEY, before=before,
                      popup=popup.summary, outcome=outcome, how=how, label=label, tried=tried)

    def _report_nothing_matched(self, key):
        """After an action on key was intercepted and no registered interruption matched a
        visible element: say so, because the likeliest cause is an interruption whose
        selector no longer matches (its own step, if any, was skipped with an INFO line),
        or one mendwright was never told about. On a Selenium driver that the script
        switched into a frame, the page around the frame is out of reach too, and the
        message says so."""
        others = self._others(key)
        if others:
            mine = [item.key for item in others if not item.preset]
            preset = any(item.preset for item in others)
            inside = getattr(self._driver, 'inside_frame', None)
            framed = callable(inside) and bool(inside())
            self.coordinator._emit(logging.WARNING,
                                   nothing_matched_message(key, mine, preset, framed))

    def _url(self):
        try:
            return self._driver.url() or ''
        except Exception:
            return ''

    def _remember_page(self, key, url=None):
        """Save the page key's element was just found on (see mendwright.flow.page_path),
        its address (see _remember_url), and the step run_steps() runs now, if any (see
        _remember_step), with at most one write of the store for all three."""
        url = self._url() if url is None else url
        path = page_path(url)
        with _batched(self.store):
            self._remember_step(key, path)
            setter = getattr(self.store, 'set_last_page', None)
            if callable(setter) and path:
                setter(key, path)
            # The site root has no page: its address is saved only for a key found on no
            # page yet, so that lastUrl stays an address of lastPage.
            if path or not self._saved_page(key):
                self._remember_url(key, url)

    def _saved_page(self, key):
        try:
            return self.store.meta(key).get('lastPage')
        except Exception:
            return None

    def _remember_url(self, key, url):
        """Save the address of the page key's element was just found on, with its record
        ids masked (see mendwright.flow.page_url), so that `mendwright check` can load the
        page again. Nothing for a page with no such address (about:blank, a data: URL, a
        file:// page), nor for one inside an iframe (see _top_level), nor on a custom store
        without set_last_url(). An address the store holds already costs no browser call
        and no write."""
        setter = getattr(self.store, 'set_last_url', None)
        if not callable(setter):
            return
        address = page_url(url)
        if address is None:
            return
        getter = getattr(self.store, 'last_url', None)
        try:
            if callable(getter) and getter(key) == address:
                return
        except Exception:
            pass
        if not self._top_level():
            return
        setter(key, address)

    def _top_level(self):
        """Whether the address url() gives is the top-level page's: on Playwright, when
        mendwright was opened on a Page (a Frame's address is the frame's own); on Selenium,
        when the driver is not switched into a frame (current_url is the top-level page's
        then, whatever frame the element is in: one round trip). Any other driver's url()
        is taken as given."""
        from .drivers.playwright_async import AsyncPlaywrightDriver
        from .drivers.playwright_sync import PlaywrightSyncDriver
        from .drivers.selenium import SeleniumDriver
        driver = self._driver
        if isinstance(driver, (PlaywrightSyncDriver, AsyncPlaywrightDriver)):
            return isinstance(getattr(self._page, 'frames', None), list)
        if isinstance(driver, SeleniumDriver):
            return not driver.inside_frame()
        return True

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
        """Save fp as key's fingerprint, and the shadow hosts the read gave as its
        shadowHosts, [] removing them for an element on the page (see mendwright.shadow). The
        store writes the file only when one of them changed. A read without hosts (a driver
        that does not read where an element is) leaves shadowHosts as it is."""
        self._fingerprinted.add((key, xpath))
        fp, hosts = take_hosts(fp)
        if fp is None:
            return
        try:
            if hosts is MISSING:
                self.store.set_meta(key, fingerprint=fp)
            else:
                self.store.set_meta(key, fingerprint=fp, shadow_hosts=hosts)
        except TypeError:
            self.store.set_meta(key, fingerprint=fp)     # a store without shadow_hosts=
        except Exception:
            logger.debug('mendwright: could not save the fingerprint of %r', key, exc_info=True)

    def _flow_context(self, key, url, *, quiet=False):
        """The heal context: the URL, and the pages the page guard compares (this page among
        them while a step that key was found during runs, see _step_pages). quiet=True
        leaves out the INFO line _step_pages() logs (heal.drift() logs one line only)."""
        try:
            meta = self.store.meta(key)
        except Exception:
            meta = {}
        return {'url': url, 'page': page_path(url), 'lastPage': meta.get('lastPage'),
                'pages': self._step_pages(key, meta, page_path(url), quiet=quiet)}

    # --- healing -------------------------------------------------------------------

    def _frame_candidates(self, key):
        """The selectors of key that are looked up in the page itself (no frameXPath and no
        shadowHosts)."""
        return [c['xpath'] for c in self.store.candidates(key)
                if not c.get('frameXPath') and not hosts_of(c) and isinstance(c.get('xpath'), str)]

    def _frame_stop(self, key, exc, selector, frames, opener, why=None):
        """Record and raise the AttentionRequired for a key found only inside an iframe
        that mendwright did not follow it into."""
        __tracebackhide__ = True
        selenium = type(self._driver).__name__ == 'SeleniumDriver'
        reason = _frame_message(key, selector, frames, opener, why, selenium)
        coordinator = self.coordinator
        code = ('disabled' if getattr(coordinator, 'disabled', False)
                else 'dry_run' if coordinator.dry_run else 'frame')
        coordinator._decline(key, reason, code=code)
        raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason) from exc

    def _frame_plan(self, key, selector, frames, coordinator=None):
        """(iframe selector, selector to save) when key can be followed into the one iframe
        its selector matches in (see _follow_into_frame), else (None, why not). coordinator,
        when given, is the one whose disabled and dry_run settings count (heal.drift()'s
        preview passes its own); else this healer's."""
        coord = self.coordinator if coordinator is None else coordinator
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
        try:
            alt = frame_spelling(selector)
        except TypeError:
            alt = None
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
            self.coordinator._decline(key, moved, code='page_guard')
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
        if not add_heal(self.store, key, alt, frame_xpath=frame, shadow_hosts=[]):
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
        """The HealScope of a heal of key (see heal_place), or None for the page: inside the
        iframe it was last found in when that iframe is still on the page; inside the open
        shadow root it was last found in, through a shadow.ShadowDriver; or, for an element
        last found inside a shadow root that cannot be entered now, nowhere, with the reason.
        Such a key is never healed on the page outside its shadow root."""
        frame, hosts, why = heal_place(self.store, key)
        if why:
            return HealScope(hosts=hosts, why=why)
        if hosts:
            shadowed = driver_shadowed(self._driver, hosts)
            why = shadow_unreachable(key, hosts, shadowed is not None,
                                     shadowed is not None and shadowed.inner_html() is not None)
            return HealScope(hosts=hosts, why=why) if why else HealScope(hosts=hosts,
                                                                          driver=shadowed)
        if not frame:
            return None
        framed = driver_framed(self._driver, frame)
        return HealScope(frame=frame, driver=framed) if framed is not None else None

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
        # The key was last found inside an iframe or a shadow root: capture, check and save
        # the heal there, or refuse it.
        with scope.entered(self.coordinator):
            return resolve_with_heal(resolve_fn, self.coordinator, key, self.store.meta(key),
                                     context)

    # --- following a key's own selector into an open shadow root -----------------------

    def _shadow_plan(self, key, selector, found, cut, coordinator=None):
        """(root, why): the one shadow root of ``found`` (see shadow_found) that key can be
        followed into with ``selector``, its own selector, else (None, why not). The same
        refusals as _frame_plan, and: several shadow roots or elements, a host on the way that
        nothing tells apart, an XPath whose element no CSS selector tells apart, and, for an
        XPath, a page with more shadow roots than mendwright reads (cut)."""
        coord = self.coordinator if coordinator is None else coordinator
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
        if len(found) > 1:
            return None, 'it matches in more than one shadow root'
        if cut and classify(selector) == XPATH:
            return None, ('the page has more shadow roots than mendwright reads, so it cannot '
                          'tell that the selector matches in that one only')
        root = found[0]
        if root['count'] != 1:
            return None, 'it matches ' + str(root['count']) + ' elements there'
        if any(h is None for h in root['hosts']):
            return None, ('nothing tells one of the shadow hosts on the way apart from the '
                          'elements around it (no test id, id or name of its own there, and not '
                          'the only element of its custom tag)')
        if not root['forms']:
            return None, ('no CSS selector tells its element apart there, and only CSS runs '
                          'inside a shadow root')
        statuses = [_stored_status(self.store, key, shadow_text(root['hosts'], f))
                    for f in root['forms']]
        if all(s == 'quarantined' for s in statuses):
            return None, ('it was looked up in that shadow root before and an action on it '
                          'failed there, so that lookup is quarantined')
        if all(s in ('quarantined', 'superseded') for s in statuses):
            return None, ('it was looked up in that shadow root before and that lookup was '
                          'superseded')
        return root, None

    def _pick_shadow(self, key, exc, matches):
        """From [(selector, found, cut)], the key's selectors that match inside shadow roots,
        the first one that can be followed, as (selector, found, root). Raises the
        AttentionRequired for the first refusal when none can."""
        __tracebackhide__ = True
        refused = None
        for selector, found, cut in matches:
            root, why = self._shadow_plan(key, selector, found, cut)
            if root is not None:
                return selector, found, root
            if refused is None:
                refused = (selector, found, why)
        self._shadow_stop(key, exc, *refused)

    def _shadow_stop(self, key, exc, selector, found, why):
        """Record and raise the AttentionRequired for a key found only inside a shadow root
        that mendwright did not follow it into."""
        __tracebackhide__ = True
        reason = _shadow_message(key, selector, found, why)
        coordinator = self.coordinator
        code = ('disabled' if getattr(coordinator, 'disabled', False)
                else 'dry_run' if coordinator.dry_run else 'shadow')
        coordinator._decline(key, reason, code=code)
        raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + reason) from exc

    def _shadow_follow(self, key, selector, root, driver):
        """Check the guard, then each CSS form of ``root`` inside its shadow root on the live
        page (exactly one visible element of the key's role, not retired), and save the
        first that passes there, as the chain of the hosts and the CSS (shadow.shadow_text)
        with the hosts as its shadowHosts, and record it. Returns '' when the key was
        followed, else why not."""
        if not self.coordinator.guard():
            return 'a transaction guard is currently blocking heals'
        shadowed = driver_shadowed(driver, root['hosts'])
        if shadowed is None:
            return 'this driver does not reach into shadow roots'
        meta = self.store.meta(key)
        why = 'no CSS selector for it passed'
        for css in root['forms']:
            saved = shadow_text(root['hosts'], css)
            if _stored_status(self.store, key, saved) in ('quarantined', 'superseded'):
                continue
            reason = live_reason(shadowed, css, meta)
            if reason:
                why = "its element there failed mendwright's checks (" + reason + ')'
                continue
            if not add_heal(self.store, key, saved, frame_xpath='', shadow_hosts=root['hosts']):
                return 'this store cannot save a selector inside a shadow root'
            self.coordinator.record_shadow(key, selector, saved, root['hosts'])
            self.coordinator.pending[key] = saved
            return ''
        return why

    def _shadow_matches(self, key, finder, driver=None):
        """[(selector, found, cut)] for the key's own selectors on the page that a follow may
        take into open shadow roots (see _follow_into_shadow): each matches inside them (see
        shadow_found), through finder (driver.shadow_matches), where the key may be followed
        (follow_allowed). On a driver whose CSS pierces shadow roots (base.css_pierces), a
        CSS selector is never one: what it matches there, it matches on the page. [] when
        any of the key's selectors on the page matches something there (driver.count; one
        that raises counts as a match): the key's element may be that one. driver is this
        healer's unless given (heal.drift()'s preview passes its own)."""
        driver = self._driver if driver is None else driver
        pierces = css_pierces(driver)
        selectors = self._frame_candidates(key)
        matches = []
        for selector in selectors:
            if pierces and classify(selector) == CSS:
                continue
            try:
                roots, cut = finder(selector)
            except Exception:
                continue
            found = shadow_found(selector, roots)
            if found and follow_allowed(self.store, key, found):
                matches.append((selector, found, cut))
        if matches and not all(_matches_nothing(driver.count, s) for s in selectors):
            return []
        return matches

    def _follow_into_shadow(self, key, exc, attempt):
        """After key missed on the page, and in no iframe: if none of its own selectors
        matches anything on the page, and one of them matches one element inside one open
        shadow root of the page (a driver with shadow_matches(), as the built-in ones have),
        look for it there from now on and return the retried step. A CSS selector is looked
        up there as it is; an XPath, which runs inside no shadow root, is evaluated in the
        root's HTML and its element looked up through a CSS form of it. No model is asked:
        the selector is the key's own, and only the place changed. It counts as a heal with
        source 'shadow'.

        Playwright's CSS pierces open shadow roots by itself, so there only an XPath is
        followed. A key one of whose selectors matches something on the page (several
        elements, or a hidden one) is not followed, and neither is a key last found on the
        page, in an iframe or in another shadow root (follow_allowed): it heals where it was
        last found, as any key does. Not followed, with AttentionRequired saying why: the
        refusals of _shadow_plan, a page the key was never found on (the page guard), a
        guard that blocks heals, or an element that fails the live check. Returns
        _NOT_FOLLOWED when no selector of the key may be followed into a shadow root (see
        _shadow_matches)."""
        __tracebackhide__ = True
        finder = getattr(self._driver, 'shadow_matches', None)
        if not callable(finder):
            return _NOT_FOLLOWED
        matches = self._shadow_matches(key, finder)
        if not matches:
            return _NOT_FOLLOWED
        selector, found, root = self._pick_shadow(key, exc, matches)
        self._frame_guard(key, exc, self._url())
        why = self._shadow_follow(key, selector, root, self._driver)
        if why:
            self._shadow_stop(key, exc, selector, found, why)
        return attempt()

    # --- steps in any order: step() and run_steps() (see mendwright.steps) -------------

    def step(self, name: str, when: Selector | None = None,
             fn: Callable[[Any], Any] | None = None, *, url: str | None = None,
             last: bool = False, optional: bool = False) -> Any:
        """Register a step of a flow whose steps the site may show in another order, on one
        page, or not at all, for run_steps(). Use it as a decorator,
        @heal.step('payment', '#card-number'), or pass the function:
        heal.step('payment', '#card-number', payment). Returns the function.

        when is what shows the step is on the page: one selector or a list, and any visible
        match counts (a landmark: a heading's text, a field's name). It is never healed, so
        pick one that stays. url is a pattern for the page's path, such as
        '/checkout/payment' or '*/payment*', normalized as the page guard normalizes paths:
        instead of when=, or as one more condition with it. last=True marks the step that
        ends the flow, such as placing the order; a flow has at most one, and one that ends
        in a submit should have it. optional=True marks one that may not show at all, such
        as an upsell.

        The function gets heal.page on Playwright and heal.driver on Selenium, so its
        selectors heal as usual. Registering a name again replaces its step, which keeps its
        place in the order. Steps stay on the healer for every later run_steps() call: for
        another flow, open a new healer. Raises TypeError, ValueError or InvalidSelector for
        arguments that can never work, before anything touches the page."""
        return _steps.add(self, name, when, fn, url=url, last=last, optional=optional)

    def run_steps(self, *, timeout_ms: int | None = None, poll_ms: int = 200) -> list:
        """Run the steps registered with step() in the order the site shows them, each
        once, until the last=True step ran (or, with no last step, every step that is not
        optional ran), and return their names in the order they ran.

        Each time, it waits up to timeout_ms (default: the healer's) for a step that has not
        run to show, then runs it. Steps that show at the same time run in the order they
        were registered, and the last step only when no other shows. Registered
        interruptions the page shows meanwhile are handled.

        While a step runs, a key found during that step before heals on the page the step
        showed on, whatever its address, where the page guard would decline a page the key
        was never found on. Once the step's function goes to another page, such as with its
        own submit, and outside run_steps(), the guard is as before.

        Raises AttentionRequired when a step that ran is on the page again with no new step
        for the whole timeout (its submit probably failed, or the site added a step that
        shows part of it: it is never run twice), when no step shows (the message names the
        page and every landmark), and after 3 times as many step runs as there are steps.
        An error from a step's function is raised as it is, after the flow.step journal
        event records it. Each run logs one INFO line, a step run after one registered
        later logs a WARNING, and so does each step that is not optional and never showed.
        See mendwright.steps."""
        __tracebackhide__ = True  # pytest shows the caller
        return _steps.run(self, timeout_ms, poll_ms)

    def _step_page(self):
        """What a step's function gets: heal.driver on Selenium, heal.page otherwise."""
        from .drivers.selenium import SeleniumDriver
        return self.driver if isinstance(self._driver, SeleniumDriver) else self.page

    @contextlib.contextmanager
    def _in_step(self, name, path):
        """Mark step ``name``, which showed on the page at ``path`` (see
        mendwright.flow.page_path), as the one running for the block, in this thread or
        asyncio task only (see _step_now)."""
        token = _RUNNING_STEP.set((self, name, path))
        try:
            yield
        finally:
            _RUNNING_STEP.reset(token)

    def _step_now(self):
        """(name, page path) of the step run_steps() runs now on this healer, or None. Only
        in the thread or asyncio task that runs it, and in the asyncio tasks and
        asyncio.to_thread() workers it starts, which copy its context: a coroutine gathered
        next to run_steps() is not part of the step."""
        running = _RUNNING_STEP.get()
        if running is None or running[0] is not self:
            return None
        return running[1], running[2]

    def _remember_step(self, key, path):
        """While run_steps() runs a step, and key was found on the page that step showed on
        (path), save the step's name with key (SelectorStore.set_last_step), so a later run
        lets key heal wherever that step shows. Not for a key found after the step's
        function went to another page: that page may be the next step's."""
        running = self._step_now()
        setter = getattr(self.store, 'set_last_step', None)
        if running is not None and path and path == running[1] and callable(setter):
            setter(key, running[0])

    def _step_pages(self, key, meta, here, quiet=False):
        """The pages the page guard compares for key: the ones saved with it, and, while
        run_steps() runs a step that key was found during before, this page too when it is
        the page that step showed on (its landmark, its url=) when it started. So a site
        that moved the step to another page or another place in the flow still heals the
        key there, and one INFO line says so when the page is new to the key. Once the
        step's function went to another page, such as with its own submit, that page may be
        the next step's, and nothing changes; nor outside run_steps(), or for a key never
        found during the step that runs."""
        pages = list(meta.get('pages') or ())
        running, steps = self._step_now(), meta.get('steps') or ()
        if running is None or not here or here != running[1] or running[0] not in steps:
            return pages
        step = running[0]
        last = meta.get('lastPage')
        if (here not in pages and here != last and (pages or last) and not quiet
                and not self.coordinator.heal_across_pages):
            self.coordinator._emit(logging.INFO, _steps.scope_message(key, step, here, last))
        return pages + [here]


# Actions a proxy runs through the driver with canary handling: Playwright Locator verbs
# plus Selenium's send_keys and submit. Everything else (inner_text, get_attribute, text,
# is_checked, ...) is a read and falls through to the native object, on Playwright once
# the registered interruptions the page shows are handled (see LOCATOR_READS).
ACTIONS = ('click', 'dblclick', 'fill', 'type', 'press', 'press_sequentially', 'check',
           'uncheck', 'set_checked', 'select_option', 'hover', 'tap', 'clear', 'focus',
           'set_input_files', 'drag_to', 'send_keys', 'submit')

# The reads of a Playwright Locator that look at the page as it is now. On Playwright, the
# registered interruptions the page shows are handled before the first of them after a
# lookup, as before an action (see ActionProxy._before_read).
LOCATOR_READS = frozenset(('text_content', 'inner_text', 'inner_html', 'get_attribute',
                           'input_value', 'is_visible', 'is_hidden', 'is_checked',
                           'is_enabled', 'is_disabled', 'is_editable'))

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
    (AttributeError, TypeError, ValueError) is raised as is and quarantines nothing, and
    so does Selenium's StaleElementReferenceException: the page re-rendered an element
    that was found right.

    A successful action does not confirm a heal on its own. Pass expect=<callable
    returning bool> to check the result, or call confirm(). Reads such as inner_text()
    or get_attribute() fall through to the native object, on Playwright once the
    registered interruptions the page shows are handled, before the first read. Use
    .native where a library wants the real object, for example Playwright's
    expect(proxy.native).

    Before an action, registered interruptions the page shows are handled (see
    Healer.interrupt); on Selenium, only when something covers the element. On
    Playwright they are handled before the first read of the Locator too (LOCATOR_READS),
    unless an action came first: once per lookup (see _before_read). With
    open(dismiss=True) a popup nobody registered that covers the element is
    dismissed (see mendwright.dismiss). An action that fails because something
    covered the element is retried once, and only after an interruption was
    handled or a popup dismissed.

    .present is False for a step that was skipped (an optional step, or an interruption,
    whose element was not there). Its actions then do nothing and return None.

    For an element found inside a shadow root on Playwright, each action and each read of
    LOCATOR_READS first waits until the Locator matches one element inside that shadow
    root and nothing outside it, and raises AttentionRequired when it never does (see
    _wait_placed).

    With expect_value= (see Healer.locator), the value a read returns for an element found
    through a heal is checked, and one that fails raises AttentionRequired instead.
    """

    # Whether an action's outcome goes to the heal canary. heal.driver turns it off for an
    # element mendwright did not resolve itself (see mendwright.proxies.HealingElement):
    # no heal found that element, so its failure says nothing about one.
    _canary = True
    # The CSS of the shadow hosts of the shadow root the element was found in, on a driver
    # whose CSS pierces shadow roots (see Healer._shadow_place), else None.
    _hosts = None

    def __init__(self, healer: Healer, key: str, native: Any, *, present: bool = True,
                 timeout_ms: int | None = None, poll_ms: int = 200, healed: str | None = None,
                 expect: Any = None) -> None:
        self._healer = healer
        self._key = key
        self._native = native
        self._present = bool(present)
        self._timeout_ms = healer.timeout_ms if timeout_ms is None else timeout_ms
        self._poll_ms = poll_ms
        # The heal the element was found through, and the check its reads must pass.
        self._healed = healed
        self._expect = expect
        # Whether a read still handles the registered interruptions first: until a read or
        # an action of the element did, once per lookup (see _before_read). The elements of
        # one heal.locator_all() share it.
        self._reads_due = [True]

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
        budget = healer._before_action(self._key, self._timeout_ms, self._poll_ms,
                                       self._uncovered())
        self._reads_due[0] = False      # its reads need not look again (see _before_read)
        healer._dismiss_before(self._key, self._native, verb, budget, self._timeout_ms,
                               self._poll_ms)
        with healer._running(self._key, budget, self._timeout_ms, self._poll_ms):
            self._wait_placed(verb)
            try:
                result = run(*args, **kwargs)
            except _USAGE_ERRORS:
                raise
            except Exception as exc:
                # A run= function the overlay handler ran meanwhile: wait for it, and raise
                # what it raised.
                healer._await_runs(budget, self._poll_ms)
                intercepted, used = is_interception_error(exc), budget.used
                if not (intercepted and healer._unblock(self._key, self._native, budget,
                                                        self._timeout_ms, self._poll_ms)):
                    if intercepted and budget.used == used and not budget.gave_up:
                        healer._report_nothing_matched(self._key)
                    self._failed(exc)  # the browser action failed
                    raise
                self._wait_placed(verb)
                try:
                    result = run(*args, **kwargs)   # once, after an interruption was handled
                except _USAGE_ERRORS:
                    raise
                except Exception as again:
                    healer._await_runs(budget, self._poll_ms)
                    self._failed(again)
                    raise
            healer._await_runs(budget, self._poll_ms)
        if expect is not None:
            try:
                ok = bool(expect())
            except Exception:
                ok = False
            self._confirm(ok, 'expect')
        return result

    def _wait_placed(self, verb):
        """For an element found inside a shadow root on Playwright (_hosts), right before an
        action or a read: wait, up to the proxy's timeout, until its Locator matches one
        element and that one inside the shadow root (driver.placed()). Playwright resolves
        the Locator again for each action and read, and its CSS pierces every open shadow
        root, so a re-render after the lookup could otherwise send it to an element outside
        that shadow root. Raises AttentionRequired when it never does, and reports nothing
        to the heal canary: the page changed after the lookup, which says nothing about the
        heal."""
        __tracebackhide__ = True  # pytest shows the caller
        hosts = self._hosts
        if not hosts:
            return
        driver = self._healer._driver
        deadline = time.monotonic() + self._timeout_ms / 1000.0
        while True:
            try:
                if driver.placed(self._native, hosts):
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise AttentionRequired(moved_message(self._key, hosts, verb))
            time.sleep(max(self._poll_ms, 20) / 1000.0)

    def _uncovered(self):
        """For the check before an action: a callable that says True when nothing covers
        the element, on a driver that can tell (Selenium's, see SeleniumDriver.covered),
        else None. Registered interruptions the page shows are then left alone: none is in
        the way, and the element may be one of their own, such as a banner's Reject
        button, which handling the banner first would remove. If the action fails because
        something covered it after all, they are handled and it is tried once more."""
        covered = getattr(self._healer._driver, 'covered', None)
        if not callable(covered):
            return None
        native = self._native
        return lambda: covered(native) is False

    def _failed(self, error):
        """Report a failed action to the heal canary, unless error says nothing about the
        heal: a stale element (see mendwright.flow.is_stale_error) was found, then
        re-rendered by the page."""
        if not is_stale_error(error):
            self._confirm(False, 'action')

    def _confirm(self, success, by=None):
        """Report the action's outcome to the heal canary, unless _canary is off. by says
        what decided it, for the journal ('expect' or 'action')."""
        if self._canary:
            _canary_call(self._healer.coordinator.confirm_pending, self._key, success, by=by)

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
        """Confirm (True) or quarantine (False) a heal for this key, from a check of your own.
        An element of heal.locator_all() reports nothing: no list is found through a heal, and
        a heal of one element under the same key is not this element's."""
        if self._canary:
            _canary_call(self._healer.coordinator.confirm_pending, self._key, bool(success),
                         by='confirm')
        return self

    def __getattr__(self, name: str) -> Any:
        # Reads and anything else not listed in ACTIONS go to the native object unchanged,
        # but for the reads expect_value= checks on an element found through a heal, and
        # for a Locator's reads on sync Playwright, the first of which handles interruptions
        # first (see _before_read).
        if not self.__dict__.get('_present', True) and not name.startswith('__'):
            raise AttributeError(absent_message(self._key))
        value = getattr(self._native, name)
        checked = (name in VALUE_READS and self.__dict__.get('_expect') is not None
                   and self.__dict__.get('_healed') is not None)
        handles = name in LOCATOR_READS and callable(value) and self._healer._handles_reads()
        placed = name in LOCATOR_READS and callable(value) and bool(self.__dict__.get('_hosts'))
        if not (checked or handles or placed):
            return value
        healer, key, healed, expect = self._healer, self._key, self._healed, self._expect
        if not callable(value):
            return healer._check_value(key, healed, name, (), value, expect)  # Selenium's text

        def read(*args, **kwargs):
            __tracebackhide__ = True  # pytest shows the caller
            if handles:
                self._before_read()
            if placed:
                self._wait_placed(name)
            result = value(*args, **kwargs)
            if not checked:
                return result
            return healer._check_value(key, healed, name, args, result, expect)
        read.__name__ = name
        return read

    def _before_read(self):
        """Before a read of the Locator (LOCATOR_READS) on sync Playwright: handle the
        registered interruptions the page shows, as before an action (see
        Healer._before_action), logged and journaled before this key, with no time limit
        for a run= function. Playwright runs no overlay handler before a read, and its
        handlers stay out of the lookup's scroll (see Healer.locator), so a cookie banner
        would otherwise stay over the page after a step that only reads.

        The check queries the page for each registered interruption, so it runs once per
        lookup, as the lookup's scroll did on 0.7.0: on the first read, unless an action
        of the element came first, and once for all the elements of a list. A banner that
        opens later is left to the next action or lookup. A check that raised (a run=
        function's error, InterruptionLoop) runs again on the next read."""
        __tracebackhide__ = True
        due = self._reads_due
        if due[0]:
            self._healer._before_action(self._key, self._timeout_ms, self._poll_ms)
            due[0] = False

    def __repr__(self) -> str:
        if not self._present:
            return '<ActionProxy key=' + repr(self._key) + ' skipped: not on the page>'
        return '<ActionProxy key=' + repr(self._key) + ' native=' + repr(self._native) + '>'
