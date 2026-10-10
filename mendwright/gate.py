"""The live check. A proposed selector, from a direct provider or a gateway, is saved to
the store only after two checks (client.apply runs both):

  1. Static validation against the captured snapshot (validation.validate): not a pure
     absolute-index path, exactly one match, the expected role, and the exact text for
     destructive controls.
  2. A live check against the current page through the Driver (live_reason below): it
     matches exactly one element at that moment, and, for a driver with describe(), that
     element is read again: it must have the expected role (and the expected text for a
     destructive key). The element must also be visible unless its role is checkbox (a
     hidden native checkbox behind a styled label is common).

The snapshot is taken before the model call, which takes seconds, so the page can change
in between (a single-page app re-rendering). Reading the element again on the live page
is what makes the element that is saved and acted on the one that was checked. When the
live element cannot be read, the proposal is refused (fail closed). A driver with no
describe() gets only the count and visibility checks.

A selector that matched once in a captured string is not enough; it must be uniquely
present on the live page when it is saved. Keys marked destructive never get this far: the
coordinator refuses them first."""

from .validation import _norm

NOT_LIVE = 'passed static validation but not uniquely present on the live page'
UNREADABLE = 'could not read the live element, so its role could not be checked'


def live_reason(driver, xpath, meta, frame_chain=()):
    """'' when xpath passes the live check, else the reason it does not (apply() passes it
    to the re-prompt). Any driver error counts as a failure (fail closed). With no live
    driver (the legacy low-level path) only the static gate applies, so this is ''."""
    if driver is None:
        return ''
    meta = meta or {}
    try:
        if driver.count(xpath, frame_chain) != 1:
            return NOT_LIVE
        if callable(getattr(driver, 'describe', None)):
            live = driver.describe(xpath, frame_chain)
            if not isinstance(live, dict):
                return UNREADABLE
            want = meta.get('expectedRole')
            if want and want != 'unknown' and live.get('role') != want:
                return ('live element role mismatch: got ' + str(live.get('role'))
                        + ' want ' + str(want))
            if meta.get('destructive'):
                texts = {_norm(t) for t in (meta.get('expectedText') or ())}
                if _norm(live.get('text')) not in texts:
                    return 'live action text mismatch: ' + _norm(live.get('text'))
        if meta.get('expectedRole') == 'checkbox':
            return ''
        if not driver.is_visible(xpath, frame_chain):
            return NOT_LIVE
        return ''
    except Exception:
        return NOT_LIVE


def live_match(driver, xpath, meta, frame_chain=()):
    """True if xpath passes the live check (see live_reason)."""
    return not live_reason(driver, xpath, meta, frame_chain)
