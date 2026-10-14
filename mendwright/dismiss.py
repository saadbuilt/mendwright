"""Popups mendwright was never told about: mendwright.open(page, dismiss=True).

With dismiss=True, mendwright dismisses a popup that blocks an action and that no
interruption registered with heal.interrupt() or cookies= accounts for: a newsletter or
discount modal, a prompt for an address or a location that can be skipped, an app-install
banner, a survey, a chat invite, a consent banner when cookies= is not set. It is off by
default, and then mendwright makes no browser call for it.

It acts only on evidence, and does nothing when unsure:

- The action's element is covered. Before an action, once the registered interruptions
  the page shows are handled, the driver looks at the point in the middle of the element,
  when it is inside the window, and asks the browser which element is on top there
  (document.elementFromPoint, into open shadow roots). The element is covered when that is
  neither the element, nor inside it, nor one of its ancestors. After an action failed
  because something covered its element (flow.is_interception_error), and no registered
  interruption is on the page, it looks the same way.
- The covering element belongs to a popup (popup_of): the nearest dialog it is in, or
  else the outermost fixed or sticky element it is in that takes pointer events and
  covers at least a quarter of the window. A popup that holds the action's element is
  being used, and is left alone.
- It tries the least risky step first and looks again after each: Escape, when the
  keyboard focus is on the page's body or in the popup (Escape goes to the focused
  element), then a close button of the popup, then a decline button (classify). It never
  clicks a button whose label says accept, subscribe, sign up, yes, continue and the like
  (NEVER), a link that goes somewhere, a button that submits a form or one in a label,
  and it never types, checks or selects anything.

The drivers report what the page shows (mendwright.drivers.base.POPUP_PROBE_JS and the
scripts after it); the rules are here, so they are the same on Playwright and Selenium.
Healer._dismiss runs them.
"""

import re
import unicodedata
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

# What may follow "continue without": what the button leaves out. "Continue without
# accepting" declines; "Continue without delay" is not a decline button.
WITHOUT = ('accepting', 'accepting all', 'accepting cookies', 'agreeing', 'an account',
           'account', 'signing in', 'signing up', 'logging in', 'registering', 'subscribing',
           'cookies')

# The phrases a decline button's label says. "No thank you" is "no thanks" spelled
# otherwise. 'close' counts only as the whole label, so "Close account" never does.
DECLINE = (('no thanks', 'no thank you', 'not now', 'maybe later', 'later', 'skip',
            'skip for now', 'dismiss', 'reject', 'reject all', 'decline', 'not interested')
           + tuple('continue without ' + w for w in WITHOUT))

# The words that may come with a decline phrase in a label, at most MAX_FILLER of them:
# "No thanks, I'd rather not", "Remind me later", "Not now, thanks". Any other word keeps
# the label from being a decline: "Skip the line", "Thanks, I'm in", "No-risk trial".
FILLER = frozenset(('i', 'im', 'id', 'ill', 'ive', 'm', 'd', 'll', 've', 'am', 'me', 'you',
                    'it', 'this', 'that', 'the', 'a', 'for', 'now', 'today', 'time', 'right',
                    'just', 'all', 'please', 'thanks', 'thank', 'no', 'not', 'but', 'rather',
                    'really', 'maybe', 'later', 'again', 'good', 'fine', 'browsing', 'remind',
                    'ask', 'step', 'tour', 'intro', 'survey', 'offer', 'question',
                    'questions'))
MAX_FILLER = 4

# Words that keep a button from ever being clicked, anywhere in its label (its text,
# hidden text included, value, aria-label and title): it may take an action, spend, send,
# or leave the page. Each matches as a word stem: 'pay' also catches "payment" and
# "paying", 'subscribe' catches "subscribed" (see _stem). 'continue' is one too, unless the
# label says 'continue without' and what it leaves out (WITHOUT).
NEVER = ('accept', 'agree', 'allow', 'subscribe', 'sign up', 'sign in', 'log in', 'join',
         'register', 'yes', 'ok', 'confirm', 'buy', 'pay', 'order', 'save', 'submit',
         'delete', 'remove', 'install', 'enable', 'notify', 'start', 'get', 'claim', 'try',
         # The same, spelled otherwise, and others like them.
         'signup', 'signin', 'login', 'unsubscribe', 'subscription', 'registration',
         'submission', 'signing up', 'signing in', 'logging in', 'trial', 'tried', 'tries',
         'bought', 'paid', 'sent', 'discard', 'create', 'purchase', 'checkout', 'cancel',
         'send', 'share', 'download', 'upgrade', 'unlock', 'redeem', 'apply', 'book',
         'donate', 'follow', 'open', 'next', 'proceed', 'verify', 'activate', 'connect',
         'log out', 'logout', 'sign out', 'leave')

_TRANSLATE = {ord(c): ' ' for c in _PUNCT}
_LABEL_KEYS = ('text', 'all', 'value', 'aria', 'title')


def words(text):
    """text the way labels are compared (see interruptions._LABEL): in lower case, with
    punctuation as spaces and the spaces normalized."""
    return ' '.join(str(text or '').lower().translate(_TRANSLATE).split())


def _says(label, phrase):
    return (' ' + phrase + ' ') in (' ' + label + ' ')


def _stem(word):
    """How a NEVER word matches: as the start of a word, without a last e or y in a word of
    more than three letters, so 'save' catches "saving" and 'notify' "notification"."""
    return word[:-1] if len(word) > 3 and word[-1] in 'ey' else word


def _entry(word):
    parts = word.split()
    return word, parts[:-1], _stem(parts[-1])


_NEVER_STEMS = tuple(_entry(w) for w in NEVER)
_CONTINUE = _entry('continue')


def _matches(found, entry):
    """Whether the words found say a NEVER entry: its words before the last one exactly,
    then a word that starts with the last one's stem."""
    _, head, stem = entry
    n = len(head)
    return any(found[i:i + n] == head and found[i + n].startswith(stem)
               for i in range(len(found) - n))


_LEFT_OUT = re.compile(r'(?<!\S)(?:' + '|'.join(
    re.escape('continue without ' + w) for w in sorted(WITHOUT, key=len, reverse=True))
    + r')(?!\S)')


def _left_out(label):
    """label, already words(), with each "continue without <what it leaves out>" taken
    out."""
    return ' '.join(_LEFT_OUT.sub(' ', label).split())


def forbidden(control):
    """The NEVER word a control's label says (see _stem), or None. The label is its text,
    the text it hides, its value, aria-label and title."""
    label = words(' '.join(str(control.get(k) or '') for k in _LABEL_KEYS))
    found = _left_out(label).split()
    if _matches(found, _CONTINUE):
        return 'continue'
    return next((entry[0] for entry in _NEVER_STEMS if _matches(found, entry)), None)


def priced(control):
    """Whether a control's label has a digit or a currency sign in it, as in "Skip the
    line - $9.99" or "No, I want 10% off": such a button may cost or decide something, so
    it is never a close or decline button."""
    text = ' '.join(str(control.get(k) or '') for k in _LABEL_KEYS)
    return any(c.isdigit() or unicodedata.category(c) == 'Sc' for c in text)


def is_control(control):
    """A control mendwright may click: a button, an element with role=button, an input of
    type button or submit, or a link that goes nowhere (no href, '#' or javascript:).

    Never one that is disabled; a link with a real href, whatever its role; one inside a
    link that goes somewhere or that holds one (a click on it follows the link); one in a
    <label>, or a label itself (a click on it checks the label's box); one that holds a
    field (a click on it may land on the field); or one that submits or resets its form (a
    <button> in a form whose type is missing, submit or reset, or an input of type submit
    in a form), unless the form has method="dialog", which only closes its dialog."""
    if any(control.get(k) for k in ('disabled', 'link', 'label', 'field', 'submits')):
        return False
    tag, role = control.get('tag'), control.get('role')
    if tag == 'a':
        href = control.get('href')
        return href is None or href.strip() == '#' or href.strip().lower().startswith(
            'javascript:')
    if tag == 'input':
        return control.get('type') in ('button', 'submit')
    if tag == 'label':
        return False
    return tag == 'button' or role == 'button'


def _declines(label):
    """Whether one label, already words(), is a decline: a DECLINE phrase in it, and no
    other word than FILLER, at most MAX_FILLER of them; or exactly 'close'."""
    if label == 'close':
        return True
    found = label.split()
    for phrase in DECLINE:
        size = len(phrase.split())
        for i in range(len(found) - size + 1):
            if ' '.join(found[i:i + size]) != phrase:
                continue
            rest = found[:i] + found[i + size:]
            if len(rest) <= MAX_FILLER and all(w in FILLER for w in rest):
                return True
    return False


def _shown(control):
    """What a control shows: its rendered text (text hidden with display: none left out),
    and that with an input's value, as words()."""
    raw = ' '.join(str(control.get('text') or '').split())
    return raw, words(raw + ' ' + str(control.get('value') or ''))


def _decline_label(control):
    """The label a control declines by: what it shows, or, when that is one character or
    none (an icon), its aria-label or title."""
    raw, shown = _shown(control)
    if len(raw) > 1 or shown:
        return shown
    return words(control.get('aria')) or words(control.get('title'))


def whole_decline(control):
    """Whether a decline control's label (_decline_label) is a DECLINE phrase and nothing
    else, such as "No thanks". pick() prefers those."""
    label = _decline_label(control)
    return label in DECLINE or label == 'close'


def classify(control):
    """'close', 'decline' or None for one control of a popup (a dict that
    POPUP_CONTROLS_JS returns).

    close: the label it shows is exactly a close phrase ("Close", "Close dialog", ... as
    for the cookies= preset), or it shows one character or none (an icon) and its
    aria-label or title says close, or it shows x, ×, ✕ or ✖ and has no aria-label or title
    that says anything else. decline: what it shows (for an icon, its aria-label or title)
    says no thanks, not now, maybe later, skip, dismiss, reject, decline, not interested,
    continue without and what it leaves out (DECLINE), with no other words than a few
    harmless ones (FILLER), such as "Remind me later". Neither for a control mendwright
    never clicks (is_control, forbidden), or one whose label has a digit or a currency
    sign (priced)."""
    if not is_control(control) or forbidden(control) or priced(control):
        return None
    raw, shown = _shown(control)
    named = words((control.get('aria') or '') + ' ' + (control.get('title') or ''))
    if shown in _CLOSE_PHRASES:
        return 'close'
    if (shown == '' or len(raw) <= 1) and _says(named, 'close'):
        return 'close'
    if raw.lower() in CLOSE_GLYPHS and (not named or _says(named, 'close')):
        return 'close'
    label = _decline_label(control)
    if label and _declines(label):
        return 'decline'
    return None


def pick(controls):
    """{'close': (index, control), 'decline': (index, control)}: the first close control
    in the order the page lists them, and the first decline control whose label is a
    decline phrase and nothing else ("No thanks"), else the first decline control."""
    out, loose = {}, None
    for index, control in enumerate(controls or ()):
        if not isinstance(control, dict):
            continue
        kind = classify(control)
        if kind == 'close' and 'close' not in out:
            out['close'] = (index, control)
        elif kind == 'decline' and 'decline' not in out:
            if whole_decline(control):
                out['decline'] = (index, control)
            elif loose is None:
                loose = (index, control)
    if 'decline' not in out and loose is not None:
        out['decline'] = loose
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
_WHY = ('free', 'hidden', 'outside')


@dataclass(frozen=True)
class Probe:
    """What POPUP_PROBE_JS saw at the middle of an element: covered, the elements from the
    one on top up to (not including) body when it is covered, and, when a popup was passed
    as known, whether it is gone (removed or hidden). gone is None when nothing was passed.
    why says why an element is not covered: 'free' (it, or part of it, is on top), 'hidden'
    (it has no size or is hidden) or 'outside' (its middle is outside the window); it is ''
    when the element is covered or the driver did not say."""
    covered: bool
    chain: tuple = ()
    gone: bool | None = None
    why: str = ''


def parse_probe(raw):
    """POPUP_PROBE_JS's result as a Probe, or None for anything else (the element matched
    none or several, or a driver answered something mendwright does not know), which
    counts as not covered."""
    if not isinstance(raw, dict) or not isinstance(raw.get('covered'), bool):
        return None
    gone = raw.get('gone')
    gone = gone if isinstance(gone, bool) else None
    if not raw['covered']:
        why = raw.get('why')
        return Probe(False, (), gone, why if why in _WHY else '')
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
    is not a popup: no dialog, and no fixed or sticky element outside the element's own
    containers that takes pointer events and covers at least MIN_AREA of the window. A
    sticky header is not one, nor the full-window layer with pointer-events: none that
    toast libraries put a small toast in."""
    if probe is None or not probe.covered:
        return None
    chain = probe.chain
    for depth, element in enumerate(chain):
        if is_dialog(element):
            return None if element['holds'] else Popup(depth, element, summary(element))
    fixed = [depth for depth, element in enumerate(chain)
             if element['pos'] in ('fixed', 'sticky') and not element['holds']
             and element.get('events') is not False]
    if not fixed:
        return None
    depth = fixed[-1]                    # the outermost
    element = chain[depth]
    try:
        big = float(element['area']) >= MIN_AREA
    except (TypeError, ValueError):
        big = False
    return Popup(depth, element, summary(element)) if big else None


_CONSENT_TEXT = re.compile(r'cookie|consent', re.I)
_CONSENT_NAME = re.compile(r'cookie|consent|gdpr|privacy', re.I)


def is_consent(popup):
    """Whether a popup looks like a consent banner: its text says cookie or consent, or its
    id, class or aria-label says cookie, consent, gdpr or privacy."""
    element = popup.element
    name = ' '.join(str(element.get(k) or '') for k in ('id', 'cls', 'aria'))
    return bool(_CONSENT_TEXT.search(str(element.get('text') or ''))
                or _CONSENT_NAME.search(name))


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
