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
"""
import time

from .drivers import wrap
from .exceptions import AttentionRequired
from .selectors import InvalidSelector, check_syntax


def _frame_chain(candidate):
    fx = candidate.get('frameXPath')
    return (fx,) if fx else ()


def _require_visible(store, key):
    # Checkboxes / switches are commonly hidden inputs toggled via their label, so matching
    # them on uniqueness alone (not visibility) mirrors the old checkbox() behaviour.
    try:
        return store.meta(key).get('expectedRole') != 'checkbox'
    except Exception:
        return True


def _split_runnable(key, candidates):
    """(runnable, errors): candidates whose selector and frame selector pass
    check_syntax, and the errors for the ones that do not. A get_by_* call is a candidate
    like any selector (calls=True); the driver runs it, or refuses it."""
    runnable, errors = [], []
    for candidate in candidates:
        try:
            check_syntax(candidate.get('xpath'), calls=True)
            if candidate.get('frameXPath'):
                check_syntax(candidate['frameXPath'])
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
    selector, fc = candidate['xpath'], _frame_chain(candidate)
    try:
        if driver.count(selector, fc) == 1 and (not require_visible or driver.is_visible(selector, fc)):
            return driver.handle(selector, fc)
    except InvalidSelector:
        raise
    except Exception:
        return None
    return None


def _no_match_message(key, candidates, errors):
    message = ('No matching element for: ' + key + ' (tried '
               + str(len(candidates)) + ' candidate(s)).')
    if errors:
        first = getattr(errors[0], 'reason', None) or str(errors[0])
        message += (' ' + str(len(errors)) + ' of them could not run: ' + first)
    return message


def resolve(store, page, key, *, on_overlay=None, timeout_ms=20000, poll_ms=200,
            monotonic=time.monotonic, sleep=time.sleep):
    driver = wrap(page)
    candidates = store.candidates(key)
    if not candidates:
        raise AttentionRequired('No selector candidates for: ' + key)
    runnable, errors = _split_runnable(key, candidates)
    if not runnable:
        raise errors[0]
    require_visible = _require_visible(store, key)
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
            if handle is not None:
                try:
                    driver.scroll_into_view(candidate['xpath'], _frame_chain(candidate))
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
        if monotonic() >= deadline:
            raise AttentionRequired(_no_match_message(key, candidates, errors))
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
                   monotonic=time.monotonic, sleep=time.sleep):
    """resolve() for the key of a heal.page.get_by_*() call (see mendwright.getby). Each poll:

    1. The call itself (the key): one visible element, or one of role checkbox, is scrolled
       into view, recorded and returned. One element or more otherwise, or a call the
       browser rejects: THROUGH, at once.
    2. Only when the call matches nothing: the key's other candidates (saved heals, seed
       rows), in store order, the first that matches one (visible) element. One that cannot
       run is dropped with its error.
    3. At the deadline: AttentionRequired, as resolve() raises it.

    first is (count, visible) of the call, already read by the caller, used in the first
    poll instead of asking the page again."""
    driver = wrap(page)
    require_visible = _require_visible(store, key)
    candidates = store.candidates(key)
    runnable, errors = _split_runnable(key, [c for c in candidates if c.get('xpath') != key])
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
            if handle is not None:
                try:
                    driver.scroll_into_view(candidate['xpath'], _frame_chain(candidate))
                except Exception:
                    pass
                store.record_success(key, candidate['xpath'])
                return handle
        if monotonic() >= deadline:
            raise AttentionRequired(_no_match_message(key, candidates, errors))
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
                                              if c.get('xpath') != key])
    for candidate in runnable:
        try:
            if _match_handle(driver, candidate, require_visible) is not None:
                return True
        except InvalidSelector:
            continue
    return False


def present(store, page, key):
    """True if any candidate for key matches exactly one (visible) element at this moment.
    No polling. Raises InvalidSelector only when none of the candidates can run."""
    driver = wrap(page)
    require_visible = _require_visible(store, key)
    candidates = store.candidates(key)
    runnable, errors = _split_runnable(key, candidates)
    for candidate in runnable:
        try:
            if _match_handle(driver, candidate, require_visible) is not None:
                return True
        except InvalidSelector as exc:
            errors.append(exc.for_key(key))
    if candidates and len(errors) == len(candidates):
        raise errors[0]
    return False
