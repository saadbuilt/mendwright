"""Popups mendwright was never told about: mendwright.open(page, dismiss=True).

With dismiss=True, mendwright dismisses a popup that blocks an action and that no
interruption registered with heal.interrupt() or cookies= accounts for: a newsletter or
discount modal, a prompt for an address or a location that can be put off, an app-install
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
  element) and cookies="accept" is not set, then a close button of the popup, then a
  decline button (classify). It never clicks a button whose label says accept,
  subscribe, sign up, yes, continue and the like (NEVER), a link that goes somewhere, a
  button that submits a form or one in a label, a radio, a checkbox, a switch, a tab or
  an option, and it never types, checks or selects anything.
- It leaves alone a popup that asks a question: role="alertdialog" (nothing is pressed
  or clicked), and one with a button that says cancel or keep (Escape only). A close or
  decline button whose verb the popup's own text says ("Close your account?" with
  Close, "Skip next week's delivery?" with Skip) is not one (Setting). A bare Skip or
  Dismiss is one only in a popup whose own text reads as a promotion or onboarding
  (Setting.promo): "New sign-in detected" with Dismiss and Review is left alone.

The drivers report what the page shows (mendwright.drivers.base.POPUP_PROBE_JS and the
scripts after it); the rules are here, so they are the same on Playwright and Selenium.
Healer._dismiss runs them.
"""

import re
import unicodedata
from dataclasses import dataclass

from .capture import _scrub
from .interruptions import CLOSE_NAMES, _PUNCT

# The verbs whose element is not looked at before they run: focus() and set_input_files()
# work on an element whatever is on top of it, and a file input is often hidden.
NOT_LOOKED_AT = frozenset(('focus', 'set_input_files'))

# The least part of the window a fixed or sticky element must cover to count as a popup.
# A header, a footer or a toolbar covers less, and is never dismissed.
MIN_AREA = 0.25

# What may follow "continue without": what the button leaves out. "Continue without
# accepting" declines; "Continue without delay" is not a decline button.
WITHOUT = ('accepting', 'accepting all', 'accepting cookies', 'agreeing', 'an account',
           'account', 'signing in', 'signing up', 'logging in', 'registering', 'subscribing',
           'cookies')

# The phrases a decline button's label says, in any popup but those of PROMO_ONLY. "No
# thank you" is "no thanks" spelled otherwise. Each only puts something off, it answers
# nothing.
DECLINE = (('no thanks', 'no thank you', 'not now', 'maybe later', 'later', 'skip',
            'skip for now', 'dismiss', 'not interested')
           + tuple('continue without ' + w for w in WITHOUT))

# The DECLINE phrases that decline only in a popup whose own text reads as a promotion or
# onboarding (Setting.promo). Elsewhere a bare Skip or Dismiss may acknowledge something
# that matters: "New sign-in detected. [Dismiss] [Review]", "Update your payment method
# [Skip] [Update]".
PROMO_ONLY = ('skip', 'skip for now', 'dismiss')

# What a popup's own text (Setting.prose, as words()) says when it is a promotion or an
# onboarding step, as whole words and phrases: a newsletter, an offer, a sale, a coupon,
# an app to get, a prompt to turn on notifications, a survey, a tour, what's new, a
# welcome, a chat invite. Not stems: "Your subscription payment failed" is not
# "subscribe". Not "notifications" alone: in a notification centre "Dismiss all" clears
# its alerts. Not "welcome back", which a sign-in notice says. A part off ("10% off"),
# getting the app and cookie or consent text count too (_promo_text).
PROMO = ('newsletter', 'newsletters', 'subscribe', 'subscribing', 'sign up for', 'offer',
         'offers', 'discount', 'discounts', 'sale', 'deal', 'deals', 'coupon', 'coupons',
         'promo', 'promos', 'promo code', 'promotion', 'promotions', 'promotional',
         'free shipping', 'turn on notifications', 'enable notifications',
         'allow notifications', 'push notifications', 'get notifications', 'survey',
         'surveys', 'feedback', 'rate us', 'tour', 'tours', 'tips', 'what s new', 'whats new',
         'welcome', 'getting started', 'chat with us')
_PROMO_TEXT = re.compile(
    r'(?<!\S)(?:' + '|'.join(re.escape(p) for p in sorted(set(PROMO) - {'welcome'},
                                                            key=len, reverse=True))
    + r'|welcome(?! back(?!\S))'                    # "Welcome back" greets a sign-in
    # The app: download, install or get the app, our app, the Acme app, our free Acme app.
    + r'|(?:download|install|get) (?:the|our)(?: \S+){0,2} app)(?!\S)'
    # A part off, "10% off" (words() keeps the %).
    + r'|% ?off(?!\S)')

# The phrases that decline only in a popup whose text speaks of cookies (Setting.cookies).
# Elsewhere they are the answer to a request: reject an expense, decline an invitation or
# new terms.
COOKIE_DECLINE = ('reject', 'reject all', 'decline')

# The words that may come with a decline phrase in a label, at most MAX_FILLER of them:
# "No thanks, I'd rather not", "Remind me later", "Not now, thanks". Any other word keeps
# the label from being a decline: "Skip the line", "Thanks, I'm in", "No-risk trial",
# "Decline offer".
FILLER = frozenset(('i', 'im', 'id', 'ill', 'ive', 'm', 'd', 'll', 've', 'am', 'me', 'you',
                    'it', 'this', 'that', 'the', 'a', 'for', 'now', 'today', 'time', 'right',
                    'just', 'all', 'please', 'thanks', 'thank', 'no', 'not', 'but', 'rather',
                    'really', 'maybe', 'later', 'again', 'good', 'fine', 'browsing', 'remind',
                    'ask', 'step', 'tour', 'intro', 'survey', 'question', 'questions'))
MAX_FILLER = 4

# The verbs of a close or decline button that a popup may ask about, with the forms its
# text may say them in: "Close your account?" with a Close button, "Skip next week's
# delivery?" with Skip. Such a button may be the answer, so it is not clicked.
ECHO = {'close': ('close', 'closes', 'closed', 'closing', 'closure'),
        'skip': ('skip', 'skips', 'skipped', 'skipping'),
        'dismiss': ('dismiss', 'dismisses', 'dismissed', 'dismissing', 'dismissal')}

# A control whose label has a word that starts with one of these makes the popup a
# question: "Cancel", "Keep it", "Keep open", "Keep my plan". Then its other buttons may
# be the answer (Close, Skip, No thanks), and none of them is clicked.
ASKS = ('cancel', 'keep')

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

_TRANSLATE = {ord(c): ' ' for c in _PUNCT + '−'}     # and the minus sign
# What a label is read for: its text, the text it hides, its value, aria-label, title,
# and the text of the elements its aria-labelledby names.
_LABEL_KEYS = ('text', 'all', 'value', 'aria', 'title', 'labelledby')


def _plain(c):
    """One character of a label as words() reads it: a dash of any kind is punctuation,
    and a format character (a soft hyphen, a zero-width space, a byte order mark) is
    nothing, so "sub­scribe" reads as subscribe and "Sign‑up" as sign up."""
    kind = unicodedata.category(c)
    if kind == 'Cf':
        return ''
    return ' ' if kind == 'Pd' else c


def words(text):
    """text the way labels are compared (see interruptions._LABEL): its compatibility form
    (NFKC: a ligature such as ﬁ is fi, a fullwidth letter is the letter), in lower
    case, with punctuation and dashes as spaces, format characters dropped and the spaces
    normalized."""
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = ''.join(_plain(c) for c in text)
    return ' '.join(text.lower().translate(_TRANSLATE).split())


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
    the text it hides, its value, aria-label, title and the text its aria-labelledby names,
    read as words() reads it."""
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


# A javascript: link whose code is one of these (spaces and a last ; left out) does nothing.
_NOWHERE = ('', 'void(0)', 'void0', 'void(null)')


def goes_nowhere(href):
    """Whether a link's href goes nowhere: none, '#', or javascript: code that does nothing
    (javascript:, javascript:void(0), javascript:;). Any other javascript: code runs, and
    may load a page: javascript:location.assign('/checkout')."""
    if href is None:
        return True
    href = str(href).strip().lower()
    if href == '#':
        return True
    if not href.startswith('javascript:'):
        return False
    code = ''.join(href[len('javascript:'):].split()).rstrip(';')
    return code in _NOWHERE


def is_control(control):
    """A control mendwright may click: a button, an element with role=button, an input of
    type button or submit, or a link that goes nowhere (goes_nowhere).

    Never one that is disabled; one with another role (a radio, checkbox, switch, tab,
    option, link, menu item, ...) or with aria-checked, aria-pressed or aria-selected,
    since a click on it chooses or toggles something; a link with a real href, whatever its
    role; one inside a link that goes somewhere or that holds one (a click on it follows
    the link); one in a <label>, or a label itself (a click on it checks the label's box);
    one that holds a field (a click on it may land on the field); or one that submits or
    resets its form (a <button> in a form whose type is missing, submit or reset, or an
    input of type submit in a form), whatever the form's method: a form with
    method="dialog" still fires its submit event, and the page's handlers of it run."""
    if any(control.get(k) for k in ('disabled', 'link', 'label', 'field', 'submits',
                                    'checked')):
        return False
    tag, role = control.get('tag'), control.get('role') or ''
    if role not in ('', 'button'):
        return False
    if tag == 'a':
        return goes_nowhere(control.get('href'))
    if tag == 'input':
        return control.get('type') in ('button', 'submit')
    if tag == 'label':
        return False
    return tag == 'button' or role == 'button'


def _decline_phrase(label, cookies=False):
    """The DECLINE phrase (or, with cookies=True, COOKIE_DECLINE phrase) one label, already
    words(), declines by, or None: the phrase in it, and no other word than FILLER, at
    most MAX_FILLER of them."""
    found = label.split()
    for phrase in DECLINE + (COOKIE_DECLINE if cookies else ()):
        size = len(phrase.split())
        for i in range(len(found) - size + 1):
            if ' '.join(found[i:i + size]) != phrase:
                continue
            rest = found[:i] + found[i + size:]
            if len(rest) <= MAX_FILLER and all(w in FILLER for w in rest):
                return phrase
    return None


def _shown(control):
    """What a control shows: its rendered text (text hidden with display: none left out),
    and that with an input's value, as words()."""
    raw = ' '.join(str(control.get('text') or '').split())
    return raw, words(raw + ' ' + str(control.get('value') or ''))


def _icon(control):
    """Whether a control is an icon: it shows one character or none, and has no value."""
    raw, shown = _shown(control)
    return shown == '' or (len(raw) <= 1 and not str(control.get('value') or '').strip())


def _name(control):
    """An icon's name, as words(): its aria-label, or without one its title."""
    return words(control.get('aria')) or words(control.get('title'))


def _decline_label(control):
    """The label a control declines by: what it shows, or for an icon its name (_name)."""
    return _name(control) if _icon(control) else _shown(control)[1]


def _promo_text(prose):
    """Whether a popup's own text (Setting.prose) reads as a promotion or onboarding: it
    says a PROMO word or phrase, get the app or the like, a part off ("10% off"), or a word
    that starts with cookie or consent."""
    return bool(_PROMO_TEXT.search(prose)
                or any(w.startswith(('cookie', 'consent')) for w in prose.split()))


@dataclass(frozen=True)
class Setting:
    """What a popup says around its controls, which changes what a control means there.

    cookies: its text speaks of cookies, so reject and decline are declines (COOKIE_DECLINE).
    prose: its own text outside its controls, and its aria-label, as words(): a close, skip
    or dismiss button whose verb it says (ECHO) may be the answer to it. asks: one of its
    controls says cancel or keep (ASKS), so it asks a question and no close or decline
    button of it counts. promo, read from cookies and prose: the popup reads as a
    promotion or onboarding (_promo_text), so a bare skip or dismiss declines there
    (PROMO_ONLY)."""
    cookies: bool = False
    prose: str = ''
    asks: bool = False

    @property
    def promo(self):
        return self.cookies or _promo_text(self.prose)


def _label_words(control):
    return words(' '.join(str(control.get(k) or '') for k in _LABEL_KEYS)).split()


def setting(popup, controls):
    """The Setting of a popup (a Popup, or None) with these controls (POPUP_CONTROLS_JS).
    A driver that does not say what the popup's own text is (prose) gets its whole text,
    its buttons' labels included, which only keeps more buttons from a click."""
    element = popup.element if popup is not None else {}
    own = element.get('prose')
    if not isinstance(own, str):
        own = element.get('text')
    prose = words(str(own or '') + ' ' + str(element.get('aria') or ''))
    text = words(element.get('text')) + ' ' + prose
    asks = any(w.startswith(ASKS) for control in controls or () if isinstance(control, dict)
               for w in _label_words(control))
    return Setting(cookies=any(w.startswith('cookie') for w in text.split()), prose=prose,
                   asks=asks)


def _echoes(prose, verb):
    """Whether a popup's own text (Setting.prose) says verb in one of its ECHO forms."""
    forms = ECHO.get(verb, ())
    return any(w in forms for w in prose.split())


def classify(control, where=None):
    """'close', 'decline' or None for one control of a popup (a dict that
    POPUP_CONTROLS_JS returns), in a popup that says what where (a Setting) says.

    close: the label it shows is exactly a close name ("Close", "Close dialog", "Close this
    banner", ... as for the cookies= preset, interruptions.CLOSE_NAMES), or it is an icon
    (it shows one character or none) whose aria-label, or without one its title, is one.
    A bare ×, or an icon named "Close position", is not one. decline: what it shows (for an
    icon, its name) says no thanks, not now, maybe later, later, skip, dismiss, not
    interested, continue without and what it leaves out (DECLINE), or in a popup that
    speaks of cookies reject or decline (COOKIE_DECLINE), with no other words than a few
    harmless ones (FILLER), such as "Remind me later". Skip, skip for now and dismiss
    (PROMO_ONLY) decline only in a popup that reads as a promotion or onboarding
    (Setting.promo): in "New sign-in detected" a Dismiss button acknowledges the sign-in.

    Neither for a control mendwright never clicks (is_control, forbidden), one whose label
    has a digit or a currency sign (priced), any control of a popup that asks (a control
    says cancel or keep), or a close, skip or dismiss button whose verb the popup's own
    text says (ECHO): in "Close your account?" a Close button is the answer."""
    if not is_control(control) or forbidden(control) or priced(control):
        return None
    where = where or Setting()
    if where.asks:
        return None
    if _shown(control)[1] in CLOSE_NAMES or (_icon(control) and _name(control) in CLOSE_NAMES):
        return None if _echoes(where.prose, 'close') else 'close'
    label = _decline_label(control)
    phrase = _decline_phrase(label, where.cookies) if label else None
    if phrase is None or (phrase in PROMO_ONLY and not where.promo):
        return None
    return None if _echoes(where.prose, phrase.split()[0]) else 'decline'


def whole_decline(control, where=None):
    """Whether a decline control's label (_decline_label) is a decline phrase and nothing
    else, such as "No thanks". pick() prefers those."""
    label = _decline_label(control)
    return label in DECLINE or bool(where and where.cookies and label in COOKIE_DECLINE)


def pick(controls, popup=None):
    """{'close': (index, control), 'decline': (index, control)}: the first close control
    in the order the page lists them, and the first decline control whose label is a
    decline phrase and nothing else ("No thanks"), else the first decline control. popup
    is the Popup they are in, whose text changes what they mean (setting())."""
    where = setting(popup, controls)
    out, loose = {}, None
    for index, control in enumerate(controls or ()):
        if not isinstance(control, dict):
            continue
        kind = classify(control, where)
        if kind == 'close' and 'close' not in out:
            out['close'] = (index, control)
        elif kind == 'decline' and 'decline' not in out:
            if whole_decline(control, where):
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
    the root as the probe saw it, and how log lines name it (summary()). alert: it is, or
    is inside, a role="alertdialog", which asks for an answer (see asks_answer())."""
    depth: int
    element: dict
    summary: str
    alert: bool = False


def asks_answer(popup):
    """Whether a popup is, or is inside, a role="alertdialog". ARIA's alert dialog is one
    that asks for the user's answer: "Close your position?", "Delete this project?". Escape
    or any of its buttons would answer it, so mendwright leaves it alone."""
    return bool(popup.alert or popup.element.get('role') == 'alertdialog')


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
    alert = any(element.get('role') == 'alertdialog' for element in chain)
    for depth, element in enumerate(chain):
        if is_dialog(element):
            return None if element['holds'] else Popup(depth, element, summary(element), alert)
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
    return Popup(depth, element, summary(element), alert) if big else None


# What a consent banner's text says, in English and in the words of the GDPR's languages
# that consent managers use: cookies, consent, "your privacy" as in "We value your
# privacy", the partners it shares data with, personal data, trackers (traceurs in the
# CNIL's French), data protection (Datenschutz).
_CONSENT_TEXT = re.compile(
    r'cookie|consent|value your privacy|privacy (?:settings|preferences|choices|center)'
    r'|partners|personal data|tracker|traceur|datenschutz|privacidad|confidentialit'
    r'|donn[ée]es personnelles|datos personales|dati personali|einwilligung', re.I)
# What a consent banner's id, class or aria-label says: those and the names of the GDPR
# (DSGVO, RGPD), of consent managers (CMP, TCF, Sourcepoint's sp_message, ...).
_CONSENT_NAME = re.compile(
    r'cookie|consent|gdpr|privacy|dsgvo|rgpd|datenschutz|cmp|tcf|sp_message|didomi'
    r'|onetrust|cybot|usercentrics|truste|quantcast|osano|cmplz|iubenda|axeptio|klaro'
    r'|tarteaucitron', re.I)
# What an accept button's label says, as the start of a word, in the languages consent
# managers use: accept (accepter, accepteren), agree, allow, consent, akzeptieren,
# zustimmen, einverstanden, erlauben, autoriser, accettare, aceptar, aceitar, permitir,
# toestaan, godkend, godta, hyväksy, akceptuj.
ACCEPT_STEMS = ('accept', 'agree', 'allow', 'consent', 'akzept', 'akcept', 'zustimm',
                'einverstanden', 'erlaub', 'autoris', 'accett', 'acept', 'aceit', 'permit',
                'toestaan', 'godkend', 'godta', 'hyväksy')


def is_consent(popup):
    """Whether a popup looks like a consent banner: its text says cookie, consent, your
    privacy, partners, personal data, trackers or the like (_CONSENT_TEXT), or its id,
    class or aria-label says cookie, consent, gdpr, privacy, dsgvo, rgpd, cmp, tcf or a
    consent manager's name (_CONSENT_NAME)."""
    element = popup.element
    name = ' '.join(str(element.get(k) or '') for k in ('id', 'cls', 'aria'))
    return bool(_CONSENT_TEXT.search(str(element.get('text') or ''))
                or _CONSENT_NAME.search(name))


def asks_consent(controls):
    """Whether one of a popup's controls (POPUP_CONTROLS_JS) says accept, agree, allow or
    consent, in English or another language (ACCEPT_STEMS): the popup asks for consent,
    whatever its text or name. With cookies="accept" mendwright leaves such a popup alone."""
    return any(w.startswith(ACCEPT_STEMS) for control in controls or ()
               if isinstance(control, dict) for w in _label_words(control))


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
    """What POPUP_CONTROL_JS checks a control against before it returns it for a click:
    its number, and every fact POPUP_CONTROLS_JS gave of it (its tag, type, role, href,
    text, value, aria-label, title, ...). A control that changed in any of them since it was
    listed, such as an input whose value is now "Yes, subscribe me", is not clicked."""
    return {'index': index, 'facts': dict(control)}
