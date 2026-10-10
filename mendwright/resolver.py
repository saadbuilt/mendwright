"""Resolve a selector key to a unique visible element by trying its candidate selectors in
recency order, polling while dismissing overlays. The browser library is abstracted behind
a Driver (drivers/): resolve() auto-wraps a raw page, drives count/is_visible/scroll through
the Driver, and returns the native locator or element (driver.handle; on Selenium, a
FramedElement for a candidate inside an iframe, which switches into the frame itself and
is not a WebElement, so pass execute_script() and ActionChains calls through its
in_frame()). The AI healer is only invoked by the caller (resolve_with_heal) when this
raises.

Candidates may be XPath, CSS or Playwright engine selectors (see mendwright.selectors).
A candidate that can never run (bad XPath, a CSS string the browser rejects, an engine
selector on Selenium) is dropped from the poll. If no runnable candidate is left,
InvalidSelector is raised at once instead of waiting out the timeout. It is not an
AttentionRequired, so the heal loop does not spend a model call on it.
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
    check_syntax, and the errors for the ones that do not."""
    runnable, errors = [], []
    for candidate in candidates:
        try:
            check_syntax(candidate.get('xpath'))
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
