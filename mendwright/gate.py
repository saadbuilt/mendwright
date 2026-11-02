"""The live check. A proposed selector, from a direct provider or a gateway, is saved to
the store only after two checks (client.apply runs both):

  1. Static validation against the captured snapshot (validation.validate): not a pure
     absolute-index path, exactly one match, the expected role, and the exact text for
     destructive controls.
  2. A live check against the current page through the Driver (live_reason below): it
     matches exactly one element at that moment, and, for a driver with describe(), that
     element is read again: it must have the expected role (and the expected text for a
     destructive key). For the key of a heal.page.get_by_role() call (see mendwright.getby)
     Playwright itself must give the element the call's ARIA role, and level= when the call
     has one (driver.role_match): the gate's role is mostly the tag, and a
     <button role="tab"> that lost its role is still a button to it. A driver that cannot
     tell refuses the heal. The element must also be visible unless its role is checkbox
     (a hidden native checkbox behind a styled label is common).

The snapshot is taken before the model call, which takes seconds, so the page can change
in between (a single-page app re-rendering). Reading the element again on the live page
is what makes the element that is saved and acted on the one that was checked. When the
live element cannot be read, the proposal is refused (fail closed). A driver with no
describe() gets only the count and visibility checks.

A selector that matched once in a captured string is not enough; it must be uniquely
present on the live page when it is saved. Keys marked destructive never get this far: the
coordinator refuses them first.

A list found for heal.locator_all(..., nonempty=True) (see mendwright.lists) has a live check
of its own, list_live_reason(): two or more elements on the live page, each of the items'
role, and at least one of them visible. It is never used, only proposed, but a person reads
it as a list that is on the page.

A selector saved by a heal or a seed file, rather than passed by the code, is checked again on
the page each time a lookup would use it (reuse_reason(), run by mendwright.resolver once the
selector matches one element, visible unless the key is a checkbox): the element's role
against the key's role (the one locator(role=) passed or the store holds, never a guess) when
either is a control's, its text when the key is destructive and has expected text, and, for
the key of a get_by_role() call, its ARIA role. A page variant can put the element a heal was
made for and another control under the same selector: //input[@name='terms'], saved on a
checkbox, also matches an <input type="submit" name="terms">."""

import inspect

from .drivers.base import driver_role_match
from .getby import ROLE_UNREADABLE, role_mismatch
from .roles import CONTROL_ROLES
from .validation import _norm

NOT_LIVE = 'passed static validation but not uniquely present on the live page'
UNREADABLE = 'could not read the live element, so its role could not be checked'


def _element_reason(live, meta, reuse=False):
    """'' when the element driver.describe() read (live) has the key's role, and for a
    destructive key one of its expected texts, else the reason it does not. A role that is
    unknown is not compared.

    reuse=True is the reuse check's version: a role counts only when the key's role or the
    element's is a control's (roles.CONTROL_ROLES), since a key that is only read may find its
    span turned into a div, and a destructive key with no expected text has no text checked
    (a heal of such a key fails that check)."""
    want = meta.get('expectedRole')
    got = live.get('role')
    if want and want != 'unknown' and got != want and (
            not reuse or want in CONTROL_ROLES or got in CONTROL_ROLES):
        return 'live element role mismatch: got ' + str(got) + ' want ' + str(want)
    if meta.get('destructive'):
        texts = {_norm(t) for t in (meta.get('expectedText') or ())}
        if (texts or not reuse) and _norm(live.get('text')) not in texts:
            return 'live action text mismatch: ' + _norm(live.get('text'))
    return ''


def live_reason(driver, xpath, meta, frame_chain=(), call=None):
    """'' when xpath passes the live check, else the reason it does not (apply() passes it
    to the re-prompt). Any driver error counts as a failure (fail closed). With no live
    driver (the legacy low-level path) only the static gate applies, so this is ''.

    call is the GetByCall of the key when the key is a get_by_* call (see
    mendwright.getby): for get_by_role(), Playwright must give the element its role."""
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
            reason = _element_reason(live, meta)
            if reason:
                return reason
        if call is not None and call.method == 'get_by_role':
            ok = driver_role_match(driver, xpath, call.value, call.level, frame_chain)
            if ok is None:
                return ROLE_UNREADABLE
            if not ok:
                return role_mismatch(call)
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


def reuse_reason(driver, selector, meta, frame_chain=(), call=None):
    """The check of a selector a heal or a seed file saved, once it matches one element on
    the live page (visible unless the key's role is checkbox), before a lookup uses it: ''
    when the element may be used, else the reason it is not used on this page.

    The element is read with driver.describe() (one round trip): it must have the key's role
    in meta (pass 'unknown' while the role is only a guess) and, for a destructive key with
    expected text, one of those texts. For the key of a get_by_role() call (call), the browser
    must also give it the call's ARIA role (driver.role_match). Only an answer that says no
    refuses it: an element the driver cannot read (no describe() or role_match(), a read that
    fails, an element that went away meanwhile) is used as it was before this check, on its
    count and visibility, so a driver that cannot describe elements keeps working."""
    meta = meta or {}
    try:
        if callable(getattr(driver, 'describe', None)):
            live = driver.describe(selector, frame_chain)
            if isinstance(live, dict):
                reason = _element_reason(live, meta, reuse=True)
                if reason:
                    return reason
        if call is not None and call.method == 'get_by_role':
            if driver_role_match(driver, selector, call.value, call.level, frame_chain) is False:
                return role_mismatch(call)
        return ''
    except Exception:
        return ''


async def _awaited(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def reuse_reason_async(driver, selector, meta, frame_chain=(), call=None):
    """reuse_reason() for an async driver (mendwright.aio), with each read awaited."""
    meta = meta or {}
    try:
        describe = getattr(driver, 'describe', None)
        if callable(describe):
            live = await _awaited(describe(selector, frame_chain))
            if isinstance(live, dict):
                reason = _element_reason(live, meta, reuse=True)
                if reason:
                    return reason
        if call is not None and call.method == 'get_by_role':
            method = getattr(driver, 'role_match', None)
            if callable(method):
                try:
                    ok = await _awaited(method(selector, call.value, call.level, frame_chain))
                except Exception:
                    ok = None
                if ok is False:
                    return role_mismatch(call)
        return ''
    except Exception:
        return ''


# list_live_reason()'s reason when the live page could not be read (fail closed).
LIST_UNREADABLE = 'the live page could not be read'


def list_live_reason(driver, xpath, meta):
    """'' when the list xpath passes the live check, else the reason it does not. The
    elements are read in one round trip (list_items(), at most the first 200 of them): two
    or more, each of the expected role, at least one visible. A driver without
    list_items(), no live driver and any driver error fail (fail closed)."""
    from .drivers.base import driver_list_items
    want = (meta or {}).get('expectedRole')
    try:
        items = driver_list_items(driver, xpath) if driver is not None else None
    except Exception:
        return LIST_UNREADABLE
    if not isinstance(items, list):
        return LIST_UNREADABLE
    if len(items) < 2:
        return _too_few(len(items))
    for item in items:
        if want and want != 'unknown' and item.get('role') != want:
            return 'live element role mismatch: got ' + str(item.get('role')) + ' want ' + str(want)
    if not any(item.get('visible') for item in items):
        return 'none of its ' + str(len(items)) + ' elements is visible on the live page'
    return ''


def _too_few(count):
    return ('it matches ' + str(count) + (' element' if count == 1 else ' elements')
            + ' on the live page, and a list heal needs 2 or more')
