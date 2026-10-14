"""Popups mendwright was never told about: mendwright.open(page, dismiss=True).

With dismiss=True, mendwright dismisses a popup that blocks an action when no interruption
registered with heal.interrupt() or cookies= was handled for it: a newsletter or discount
modal, a prompt for an address or a location that can be skipped, an app-install banner,
a survey, a chat invite, a consent banner when cookies= is not set. It is off by default,
and then mendwright makes no browser call for it.

It acts only on evidence, and does nothing when unsure:

- The action's element is covered. Before an action, the driver looks at the point in the
  middle of the element, when it is inside the window, and asks the browser which element
  is on top there (document.elementFromPoint, into open shadow roots). The element is
  covered when that is neither the element, nor inside it, nor one of its ancestors. After
  an action failed because something covered its element (flow.is_interception_error),
  it looks the same way.
- The covering element belongs to a popup (popup_of): the nearest dialog it is in, or
  else the outermost fixed or sticky element it is in that covers at least a quarter of
  the window. A popup that holds the action's element is being used, and is left alone.
- It tries the least risky step first and looks again after each: Escape, then a close
  button of the popup, then a decline button (classify). It never clicks a button whose
  label says accept, subscribe, sign up, yes, continue and the like (NEVER), never a link
  that goes somewhere, and it never types, checks or selects anything.

The drivers report what the page shows (mendwright.drivers.base.POPUP_PROBE_JS and the
scripts after it); the rules are here, so they are the same on Playwright and Selenium.
Healer._dismiss runs them.
"""

from dataclasses import dataclass

from .capture import _scrub
from .interruptions import _CLOSE_PHRASES, _PUNCT

# The verbs whose element is not looked at before they run: focus() and set_input_files()
# work on an element whatever is on top of it, and a file input is often hidden.
NOT_LOOKED_AT = frozenset(('focus', 'set_input_files'))

# The least part of the window a fixed or sticky element must cover to count as a popup.
# A header, a footer or a toolbar covers less, and is never dismissed.
MIN_AREA = 0.25

# A button that shows one of these, and no label that says something else, closes.
CLOSE_GLYPHS = ('x', '×', '✕', '✖')

# What a decline button's label says: all of it, or how it starts or ends ("No thanks, I'd
# rather not", "Remind me later"). 'close' counts only as the whole label, so "Close
# account" never does.
DECLINE = ('no thanks', 'no', 'thanks', 'not now', 'maybe later', 'later', 'skip',
           'skip for now', 'dismiss', 'reject', 'reject all', 'decline', 'not interested',
           'continue without')

# Words that keep a button from ever being clicked, anywhere in its label (its text,
# value, aria-label and title): it may take an action, spend, send, or leave the page.
# 'continue' is one too, unless the label says 'continue without'.
NEVER = ('accept', 'agree', 'allow', 'subscribe', 'sign up', 'sign in', 'log in', 'join',
         'register', 'yes', 'ok', 'confirm', 'buy', 'pay', 'order', 'save', 'submit',
         'delete', 'remove', 'install', 'enable', 'notify', 'start', 'get', 'claim', 'try',
         # The same, spelled otherwise, and others like them.
         'signup', 'signin', 'login', 'okay', 'unsubscribe', 'saving', 'discard', 'create',
         'purchase', 'checkout', 'cancel', 'send', 'share', 'download', 'upgrade', 'unlock',
         'redeem', 'apply', 'book', 'donate', 'follow', 'open', 'next', 'proceed', 'verify',
         'activate', 'connect', 'log out', 'logout', 'sign out', 'leave')

_TRANSLATE = {ord(c): ' ' for c in _PUNCT}


def words(text):
    """text the way labels are compared (see interruptions._LABEL): in lower case, with
    punctuation as spaces and the spaces normalized."""
    return ' '.join(str(text or '').lower().translate(_TRANSLATE).split())


def _says(label, phrase):
    return (' ' + phrase + ' ') in (' ' + label + ' ')


def _declines(label):
    """Whether one label, already words(), is a decline: a DECLINE phrase as the whole
    label, or at its start or end, or exactly 'close'."""
    if label == 'close':
        return True
    return any(label == p or label.startswith(p + ' ') or label.endswith(' ' + p)
               for p in DECLINE)


def forbidden(control):
    """The NEVER word a control's label says, or None."""
    label = words(' '.join((control.get('text') or '', control.get('value') or '',
                            control.get('aria') or '', control.get('title') or '')))
    if _says(label, 'continue') and not _says(label, 'continue without'):
        return 'continue'
    return next((w for w in NEVER if _says(label, w)), None)


def is_control(control):
    """A control mendwright may click: a button, an element with role=button, an input of
    type button or submit, or a link that goes nowhere (no href, '#' or javascript:).
    Never one that is disabled, and never a link with a real href, whatever its role."""
    if control.get('disabled'):
        return False
    tag, role = control.get('tag'), control.get('role')
    if tag == 'a':
        href = control.get('href')
        return href is None or href.strip() == '#' or href.strip().lower().startswith(
            'javascript:')
    if tag == 'input':
        return control.get('type') in ('button', 'submit')
    return tag == 'button' or role == 'button'


def classify(control):
    """'close', 'decline' or None for one control of a popup (a dict that
    POPUP_CONTROLS_JS returns).

    close: the label it shows is exactly a close phrase ("Close", "Close dialog", ... as
    for the cookies= preset), or it shows one character or none (an icon) and its
    aria-label or title says close, or it shows x, ×, ✕ or ✖ and has no aria-label or title
    that says anything else. decline: its text, aria-label or title says no thanks, not
    now, maybe later, skip, dismiss, reject, decline, not interested, continue without or
    the like (DECLINE). Neither for a control mendwright never clicks (is_control,
    forbidden)."""
    if not is_control(control) or forbidden(control):
        return None
    raw = ' '.join(str(control.get('text') or '').split())
    shown = words(raw + ' ' + (control.get('value') or ''))
    named = words((control.get('aria') or '') + ' ' + (control.get('title') or ''))
    if shown in _CLOSE_PHRASES:
        return 'close'
    if (shown == '' or len(raw) <= 1) and _says(named, 'close'):
        return 'close'
    if raw.lower() in CLOSE_GLYPHS and (not named or _says(named, 'close')):
        return 'close'
    parts = (shown, words(control.get('aria')), words(control.get('title')))
    if any(part and _declines(part) for part in parts):
        return 'decline'
    return None


def pick(controls):
    """{'close': (index, control), 'decline': (index, control)} for the first control of
    each kind, in the order the page lists them."""
    out = {}
    for index, control in enumerate(controls or ()):
        if not isinstance(control, dict):
            continue
        kind = classify(control)
        if kind and kind not in out:
            out[kind] = (index, control)
    return out


def control_text(control):
    """How a log line names a control: what it shows, else (for an icon that shows one
    character or none) its aria-label or title, cut to 40 characters."""
    texts = [' '.join(str(control.get(k) or '').split())
             for k in ('text', 'value', 'aria', 'title')]
    shown = texts[0] or texts[1]
    if len(shown) <= 1:
        shown = texts[2] or texts[3] or shown
    return _scrub(shown[:40])


# --- what the page showed --------------------------------------------------------------------

_ELEMENT_KEYS = ('tag', 'id', 'cls', 'role', 'modal', 'open', 'pos', 'area', 'holds', 'text')


@dataclass(frozen=True)
class Probe:
    """What POPUP_PROBE_JS saw at the middle of an element: covered, the elements from the
    one on top up to (not including) body when it is covered, and, when a popup was passed
    as known, whether it is gone (removed or hidden). gone is None when nothing was passed."""
    covered: bool
    chain: tuple = ()
    gone: bool | None = None


def parse_probe(raw):
    """POPUP_PROBE_JS's result as a Probe, or None for anything else (the element matched
    none or several, or a driver answered something mendwright does not know), which
    counts as not covered."""
    if not isinstance(raw, dict) or not isinstance(raw.get('covered'), bool):
        return None
    gone = raw.get('gone')
    gone = gone if isinstance(gone, bool) else None
    if not raw['covered']:
        return Probe(False, (), gone)
    chain = raw.get('chain')
    if not isinstance(chain, list) or not chain or not all(
            isinstance(el, dict) and all(k in el for k in _ELEMENT_KEYS) for el in chain):
        return None
    return Probe(True, tuple(chain), gone)


def is_dialog(element):
    """role=dialog or alertdialog, aria-modal=true, or an open <dialog>."""
    return (element.get('role') in ('dialog', 'alertdialog') or element.get('modal') is True
            or element.get('open') is True)


@dataclass(frozen=True)
class Popup:
    """The popup over an element: how many steps up from the element on top its root is,
    the root as the probe saw it, and how log lines name it (summary())."""
    depth: int
    element: dict
    summary: str


def popup_of(probe):
    """The Popup covering the element, or None: when it is not covered, when the element
    is inside the popup (the dialog that covers it holds it too), or when what covers it
    is not a popup (no dialog, and no fixed or sticky element outside the element's own
    containers that covers at least MIN_AREA of the window, such as a sticky header)."""
    if probe is None or not probe.covered:
        return None
    chain = probe.chain
    for depth, element in enumerate(chain):
        if is_dialog(element):
            return None if element['holds'] else Popup(depth, element, summary(element))
    fixed = [depth for depth, element in enumerate(chain)
             if element['pos'] in ('fixed', 'sticky') and not element['holds']]
    if not fixed:
        return None
    depth = fixed[-1]                    # the outermost
    element = chain[depth]
    try:
        big = float(element['area']) >= MIN_AREA
    except (TypeError, ValueError):
        big = False
    return Popup(depth, element, summary(element)) if big else None


def _attr(value, limit=60):
    value = ' '.join(str(value or '').split())[:limit]
    return value.replace('"', "'")


def summary(element):
    """How a log line names a popup: its tag with its id, role and first classes, and the
    first 60 characters of its text, with runs of 12 to 19 digits redacted. For example
    <div role="dialog" class="nl"> ("Get 10% off your first order…")."""
    head = '<' + _attr(element.get('tag'), 20)
    for name, value in (('id', element.get('id')), ('role', element.get('role')),
                        ('class', ' '.join(str(element.get('cls') or '').split()[:3]))):
        if value:
            head += ' ' + name + '="' + _attr(value) + '"'
    head += '>'
    text = _scrub(' '.join(str(element.get('text') or '').split()))
    if text:
        cut = text[:60].rstrip()
        head += ' ("' + cut + ('…' if len(text) > 60 else '') + '")'
    return head


def expect_of(popup):
    """What POPUP_ROOT_JS checks the popup's root against before it returns it."""
    return {'depth': popup.depth, 'tag': popup.element.get('tag') or '',
            'id': popup.element.get('id') or ''}


def expect_control(index, control):
    """What POPUP_CONTROL_JS checks a control against before it returns it for a click."""
    return {'index': index, 'tag': control.get('tag') or '', 'text': control.get('text') or '',
            'aria': control.get('aria') or ''}
