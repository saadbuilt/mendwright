"""Resolve a selector key to a unique visible element by trying its candidate selectors in
recency order, polling while dismissing overlays. The browser library is abstracted behind
a Driver (drivers/): resolve() auto-wraps a raw page, drives count/is_visible/scroll through
the Driver, and returns the native locator or element (driver.handle; on Selenium, a
FramedElement for a candidate inside an iframe, which switches into the frame itself and
is not a WebElement, so pass execute_script() and ActionChains calls through its
in_frame()). The AI healer is only invoked by the caller (resolve_with_heal) when this
raises.

Candidates may be XPath, CSS or Playwright engine selectors (see mendwright.selectors),
or get_by_* calls, which only the Playwright drivers run. A candidate that can never run
(bad XPath, a CSS string the browser rejects, an engine selector or a call on Selenium) is
dropped from the poll. If no runnable candidate is left, InvalidSelector is raised at once
instead of waiting out the timeout. It is not an AttentionRequired, so the heal loop does
not spend a model call on it.

The key of a heal.page.get_by_*() call (see mendwright.getby) is looked up by
resolve_get_by() instead, whose rule differs: the call itself first, and its saved heals
only while the call matches nothing. A call that matches several elements, or one hidden
element, is not broken, and goes to Playwright as written (THROUGH).

A candidate saved inside an open shadow root (its shadowHosts, see mendwright.shadow) runs
with a ShadowHost step per host at the end of its frame chain, and only on a driver that
reaches into shadow roots (drivers.base.reaches_shadow). On any other driver it cannot run,
as an engine selector cannot run on Selenium: a driver that knows nothing of shadow roots
could run it on the page, outside its host.

A candidate the code did not pass (a heal, a seed file's row) is used only after its element
passes the live check of gate.reuse_reason() on this page: the key's role, its text when the
key is destructive, and the ARIA role of a get_by_role() call. One that fails is left out for
the rest of the lookup, which goes on with the other candidates (or, when none is left, gives
up at once so the heal path can run), and refused=, when given, is called with it. It is not
quarantined: it may be right on other pages. The code's own selectors are used as they are.
"""
import time

from . import getby
from .drivers import wrap
from .drivers.base import reaches_shadow
from .exceptions import AttentionRequired
from .gate import reuse_reason
from .selectors import InvalidSelector, check_syntax
from .shadow import chain_of, hosts_of, run_target


def _frame_chain(candidate):
    """The frame chain a candidate runs in: its iframe, then its shadow hosts."""
    return chain_of(candidate)


def _target(candidate):
    """(selector, frame_chain) a candidate runs with (see shadow.run_target): for one inside
    a shadow root, the CSS its text ends in. Call only for a candidate _split_runnable()
    kept."""
    return run_target(candidate)


def _no_shadow(candidate):
    selector = candidate.get('xpath')
    return InvalidSelector.build(
        selector, 'css', repr(selector) + ' was saved inside a shadow root, and this driver '
        'does not reach into shadow roots.', 'Use a Playwright page or a Selenium driver, or '
        "give the driver shadow_html() and ShadowHost steps (see mendwright.drivers.base).")


def _require_visible(store, key):
    # Checkboxes / switches are commonly hidden inputs toggled via their label, so matching
    # them on uniqueness alone (not visibility) mirrors the old checkbox() behaviour.
    try:
        return store.meta(key).get('expectedRole') != 'checkbox'
    except Exception:
        return True


def _split_runnable(key, candidates, driver=None):
    """(runnable, errors): candidates whose selector and frame selector pass
    check_syntax, and the errors for the ones that do not. A get_by_* call is a candidate
    like any selector (calls=True); the driver runs it, or refuses it. A candidate inside a
    shadow root needs CSS, hosts saved as CSS selectors, and, when driver is given, a driver
    that reaches into shadow roots."""
    runnable, errors = [], []
    for candidate in candidates:
        try:
            check_syntax(candidate.get('xpath'), calls=True)
            if candidate.get('frameXPath'):
                check_syntax(candidate['frameXPath'])
            if hosts_of(candidate):
                run_target(candidate)
                if driver is not None and not reaches_shadow(driver):
                    raise _no_shadow(candidate)
        except InvalidSelector as exc:
            errors.append(exc.for_key(key))
        except TypeError as exc:
            errors.append(exc)
        else:
            runnable.append(candidate)
    return runnable, errors


def _match_handle(driver, candidate, require_visible):
    """The native handle if the candidate matches exactly one (visible) element, else None.
    InvalidSelector propagates; any other driver error counts as no match."""
    selector, fc = _target(candidate)
    try:
        if driver.count(selector, fc) == 1 and (not require_visible or driver.is_visible(selector, fc)):
            return driver.handle(selector, fc)
    except InvalidSelector:
        raise
    except Exception:
        return None
    return None


def _no_match_message(key, candidates, errors, refusals=()):
    message = ('No matching element for: ' + key + ' (tried '
               + str(len(candidates)) + ' candidate(s)).')
    if errors:
        first = getattr(errors[0], 'reason', None) or str(errors[0])
        message += (' ' + str(len(errors)) + ' of them could not run: ' + first)
    if refusals:
        message += (' ' + str(len(refusals)) + ' of them matched an element that failed the '
                    'live check here, so it was not used: ' + refusals[0])
    return message


# The sources of a candidate the code did not pass: those pass the reuse check (see Reuse)
# before a lookup uses them. 'code', and a candidate a custom store gives with no source, are
# used as they are.
SAVED_SOURCES = ('heal', 'seed')


def from_code(candidate):
    """True for a selector a lookup uses as it is: one the caller's code passed (source
    'code'), or one a custom store gives with no source of mendwright's. A heal and a seed
    file's row pass the reuse check first (see Reuse)."""
    return candidate.get('source') not in SAVED_SOURCES


def reuse_meta(store, key):
    """What the reuse check compares a saved selector's element with: the key's meta, with
    its role 'unknown' while that role is only a guess (store.role_is_guess), since a role
    guessed from a selector's text says nothing of the element. {} when the store can't say."""
    try:
        meta = dict(store.meta(key))
    except Exception:
        return {}
    guess = getattr(store, 'role_is_guess', None)
    try:
        if callable(guess) and guess(key):
            meta['expectedRole'] = 'unknown'
    except Exception:
        meta['expectedRole'] = 'unknown'
    return meta


class Reuse:
    """The reuse check of one lookup of key (see gate.reuse_reason). check(driver, candidate)
    gives '' for a candidate that may be used, else the reason it is not used here. refuse()
    records a refusal and tells refused(key, candidate, reason), when given; reasons lists
    them, for the miss's message."""

    def __init__(self, store, key, refused=None):
        self.store, self.key, self.refused = store, key, refused
        self.call = getby.parse(key)
        self.reasons = []
        self.xpaths = set()     # the selectors refused in this lookup

    def check(self, driver, candidate):
        if from_code(candidate):
            return ''
        selector, frame_chain = _target(candidate)
        return reuse_reason(driver, selector, reuse_meta(self.store, self.key), frame_chain,
                            call=self.call)

    def refuse(self, candidate, reason):
        self.reasons.append(reason)
        self.xpaths.add(candidate.get('xpath'))
        if callable(self.refused):
            try:
                self.refused(self.key, candidate, reason)
            except Exception:
                pass    # a broken log line or journal must not break a lookup


def refused_message(key, candidate, page, reason):
    """The WARNING for a saved selector of key whose element failed the reuse check on the
    page ``page`` (a page path, or None)."""
    source = candidate.get('source')
    what = ('a heal saved' if source == 'heal' else 'a seed file holds' if source == 'seed'
            else 'the store holds')
    where = ' on ' + page if page else ' on this page'
    return ('did not use ' + str(candidate.get('xpath')) + " for '" + key + "'" + where + ': '
            + what + ' that selector, and the element it matches here failed the live check ('
            + reason + '). mendwright left it in the store for the pages where it fits and '
            'went on without it.')


def refresh_key(store, key, max_age=0.0):
    """store.refresh(key, max_age=max_age) for a store that has it (SelectorStore does): True
    when what other workers saved for key changed it in memory. Never raises."""
    method = getattr(store, 'refresh', None)
    if not callable(method):
        return False
    try:
        return bool(method(key, max_age=max_age))
    except Exception:
        return False


def refresh_every(store):
    """How often, in seconds, a lookup looks at the store's file again (see refresh_key)."""
    every = getattr(store, 'refresh_every', None)
    return every if isinstance(every, (int, float)) and not isinstance(every, bool) \
        and every > 0 else 1.0


def _gone(store, key, candidate):
    """True when the store no longer has candidate active: another worker quarantined it (or
    superseded it) and refresh() took that in."""
    status = getattr(store, 'status', None)
    try:
        return callable(status) and status(key, candidate.get('xpath')) not in (None, 'active')
    except Exception:
        return False


def _used(store, key, candidate):
    """Before a lookup uses candidate, a selector a heal or a seed file saved: look at the
    store's file again for key (at most once per refresh_every seconds), so a quarantine
    another worker made since reaches this one. True when candidate may still be used."""
    if from_code(candidate):
        return True
    return not (refresh_key(store, key, refresh_every(store)) and _gone(store, key, candidate))


def _again(store, key, driver, reuse, keep=None):
    """(candidates, runnable, errors) after refresh() changed key: its active candidates
    now, without the ones this lookup refused, those keep() leaves out (resolve_get_by
    leaves out the call itself), and those that cannot run."""
    candidates = store.candidates(key)
    rows = [c for c in candidates if c.get('xpath') not in reuse.xpaths
            and (keep is None or keep(c))]
    runnable, errors = _split_runnable(key, rows, driver)
    return candidates, runnable, errors


def resolve(store, page, key, *, on_overlay=None, timeout_ms=20000, poll_ms=200,
            monotonic=time.monotonic, sleep=time.sleep, refused=None):
    """The native handle of key's element. Each poll tries key's active candidates in order
    until one matches exactly one element (visible unless the key is a checkbox), scrolls it
    into view, records it with store.record_success() and returns it. A candidate the code
    did not pass must also pass the reuse check (see Reuse); refused(key, candidate, reason)
    is called for one that does not. Raises AttentionRequired at the deadline, or at once when
    every candidate was refused, and InvalidSelector when none can run.

    Read-through (a store with refresh(), see SelectorStore.refresh): before it uses a
    selector a heal or a seed file saved, and after each poll that matched nothing, the
    lookup looks at the store's file again for key, at most once per store.refresh_every
    seconds. A heal another worker saved meanwhile is tried at once, without waiting out the
    timeout, and one another worker quarantined is not used."""
    driver = wrap(page)
    candidates = store.candidates(key)
    if not candidates:
        raise AttentionRequired('No selector candidates for: ' + key)
    runnable, errors = _split_runnable(key, candidates, driver)
    if not runnable:
        raise errors[0]
    require_visible = _require_visible(store, key)
    reuse = Reuse(store, key, refused)
    every = refresh_every(store)
    deadline = monotonic() + timeout_ms / 1000.0
    overlay_tried = False
    while True:
        for candidate in list(runnable):
            try:
                handle = _match_handle(driver, candidate, require_visible)
            except InvalidSelector as exc:
                runnable.remove(candidate)
                errors.append(exc.for_key(key))
                if not runnable:
                    raise errors[0] from None
                continue
            if handle is None:
                continue
            verdict = reuse.check(driver, candidate)
            if verdict:
                runnable.remove(candidate)
                reuse.refuse(candidate, verdict)
                if not runnable:
                    raise AttentionRequired(_no_match_message(key, candidates, errors,
                                                              reuse.reasons))
                continue
            if not _used(store, key, candidate):
                runnable.remove(candidate)
                if not runnable:
                    break
                continue
            try:
                driver.scroll_into_view(*_target(candidate))
            except Exception:
                pass
            store.record_success(key, candidate['xpath'])
            return handle
        if not overlay_tried and on_overlay:
            try:
                on_overlay()
            except Exception:
                pass
            overlay_tried = True
        if refresh_key(store, key, every):
            candidates, runnable, errors = _again(store, key, driver, reuse)
            if runnable:
                continue        # what another worker saved: try it now
        if not runnable:
            if errors and not reuse.reasons:
                raise errors[0]
            raise AttentionRequired(_no_match_message(key, candidates, errors, reuse.reasons))
        if monotonic() >= deadline:
            raise AttentionRequired(_no_match_message(key, candidates, errors, reuse.reasons))
        sleep(poll_ms / 1000.0)


# What resolve_get_by() returns when the call matches something other than one usable
# element (several, one hidden one), or the browser rejects it: Playwright runs the call as
# written, and no saved heal is used.
THROUGH = object()


def _call_state(driver, key, require_visible):
    """(count, visible) of the call key itself on the page, visible None when it was not
    asked. InvalidSelector propagates (the browser rejected the call)."""
    count = driver.count(key)
    visible = None
    if count == 1 and require_visible:
        visible = bool(driver.is_visible(key))
    return count, visible


def _call_usable(state, require_visible):
    """True when (count, visible) is one element mendwright can use: visible, unless the
    key's role is checkbox."""
    count, visible = state
    return count == 1 and (not require_visible or bool(visible))


def resolve_get_by(store, page, key, *, timeout_ms, poll_ms=200, first=None,
                   monotonic=time.monotonic, sleep=time.sleep, refused=None):
    """resolve() for the key of a heal.page.get_by_*() call (see mendwright.getby). Each poll:

    1. The call itself (the key): one visible element, or one of role checkbox, is scrolled
       into view, recorded and returned. One element or more otherwise, or a call the
       browser rejects: THROUGH, at once.
    2. Only when the call matches nothing: the key's other candidates (saved heals, seed
       rows), in store order, the first that matches one (visible) element. One that cannot
       run is dropped with its error.
    3. At the deadline: AttentionRequired, as resolve() raises it.

    A saved heal or seed row must also pass the reuse check (see Reuse), the call's ARIA role
    included for a get_by_role() call. One that does not is left out for the rest of the
    lookup, and refused(key, candidate, reason) is called with it. The store's file is looked
    at again as resolve() does (read-through).

    first is (count, visible) of the call, already read by the caller, used in the first
    poll instead of asking the page again."""
    driver = wrap(page)
    require_visible = _require_visible(store, key)
    candidates = store.candidates(key)
    runnable, errors = _split_runnable(key, [c for c in candidates if c.get('xpath') != key],
                                       driver)
    reuse = Reuse(store, key, refused)
    deadline = monotonic() + timeout_ms / 1000.0
    while True:
        try:
            state = first if first is not None else _call_state(driver, key, require_visible)
            if state[0] == 1 and require_visible and state[1] is None:
                state = _call_state(driver, key, require_visible)
        except InvalidSelector:
            return THROUGH
        first = None
        if _call_usable(state, require_visible):
            try:
                driver.scroll_into_view(key)
            except Exception:
                pass
            store.record_success(key, key)
            return driver.handle(key)
        if state[0] >= 1:
            return THROUGH
        for candidate in list(runnable):
            try:
                handle = _match_handle(driver, candidate, require_visible)
            except InvalidSelector as exc:
                runnable.remove(candidate)
                errors.append(exc.for_key(key))
                continue
            if handle is None:
                continue
            verdict = reuse.check(driver, candidate)
            if verdict:
                runnable.remove(candidate)
                reuse.refuse(candidate, verdict)
                continue
            if not _used(store, key, candidate):
                runnable.remove(candidate)
                continue
            try:
                driver.scroll_into_view(*_target(candidate))
            except Exception:
                pass
            store.record_success(key, candidate['xpath'])
            return handle
        if refresh_key(store, key, refresh_every(store)):
            candidates, runnable, errors = _again(store, key, driver, reuse,
                                                  keep=lambda c: c.get('xpath') != key)
            if runnable:
                continue        # what another worker saved: try it now (the call first)
        if monotonic() >= deadline:
            raise AttentionRequired(_no_match_message(key, candidates, errors, reuse.reasons))
        sleep(poll_ms / 1000.0)


def present_get_by(store, page, key):
    """present() for the key of a heal.page.get_by_*() call: one pass of resolve_get_by()'s
    rule, with no wait, no scrolling and nothing recorded. True when the call matches one
    usable element, or matches nothing and a saved selector of the key does; False when the
    call matches several elements or one hidden one, the browser rejects it, or nothing
    matches."""
    driver = wrap(page)
    require_visible = _require_visible(store, key)
    try:
        state = _call_state(driver, key, require_visible)
    except InvalidSelector:
        return False
    if state[0] >= 1:
        return _call_usable(state, require_visible)
    runnable, _errors = _split_runnable(key, [c for c in store.candidates(key)
                                              if c.get('xpath') != key], driver)
    reuse = Reuse(store, key)
    for candidate in runnable:
        try:
            if (_match_handle(driver, candidate, require_visible) is not None
                    and reuse.check(driver, candidate) == ''):
                return True
        except InvalidSelector:
            continue
    return False


def present(store, page, key):
    """True if any candidate for key matches exactly one (visible) element at this moment,
    one the code did not pass only when that element passes the reuse check (see Reuse). No
    polling, and nothing is logged. Raises InvalidSelector only when none of the candidates
    can run."""
    driver = wrap(page)
    require_visible = _require_visible(store, key)
    candidates = store.candidates(key)
    runnable, errors = _split_runnable(key, candidates, driver)
    reuse = Reuse(store, key)
    for candidate in runnable:
        try:
            if (_match_handle(driver, candidate, require_visible) is not None
                    and reuse.check(driver, candidate) == ''):
                return True
        except InvalidSelector as exc:
            errors.append(exc.for_key(key))
    if candidates and len(errors) == len(candidates):
        raise errors[0]
    return False
