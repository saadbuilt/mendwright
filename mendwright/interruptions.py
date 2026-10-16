"""Interruptions: steps that can show up at any point of a flow, such as a cookie banner,
a newsletter modal or an "are you still there?" dialog.

heal.interrupt(key, selector) registers one. mendwright looks for it only when a step
needs help: when a locator() misses, before an action while the page shows it, and when an
action fails because something covered the element. When it is there and visible,
mendwright performs its action (a click by default), or calls its run= function for one
that needs a whole form, and goes on with the step. The actions
are those of heal.locator(), heal.page's selector shortcuts and the click, send_keys,
clear and submit of the elements heal.driver finds; on a Playwright Page it is also
registered with page.add_locator_handler, so Playwright hands over before the other
locator and selector actions on the page too, though not before page.mouse,
page.keyboard or an action with force=True (see Healer._watch).

mendwright.open(page, cookies='reject' | 'accept') registers a ready-made one for the
consent banners of common consent managers. This module holds its selectors:

- OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, the
  cookieconsent library (Osano's and orestbida's), CookieYes (and the older Cookie Law
  Info plugin) and Complianz, by the ids and classes of their buttons;
- a generic fallback: a button or link whose own text, aria-label or title says reject,
  decline, deny or necessary only ('reject'), or accept, allow or agree ('accept'),
  inside a banner. A Close button counts for 'reject' only when its label is just Close
  (or an icon whose aria-label or title says close). The banner is one element that
  meets all of these: its id, class or aria label mentions cookie or consent, its text
  mentions cookies, its text is at most 1500 characters, and it holds no main or nav
  element and no text field. It is never html, body or main. So a consent screen that is
  not about cookies (an OAuth "Allow", a terms step), or a page-wide wrapper with a class
  such as "cookie-consent-given", is not a banner, and its buttons are never clicked.

The selectors are XPath, so they also run on Selenium. Usercentrics draws its banner
inside a shadow root, which XPath cannot reach; a CSS selector for its buttons is tried as
well, which Playwright's CSS engine can reach and Selenium's cannot. Playwright's overlay
handler watches for all of them, the CSS one included.
"""

import inspect
import re
from dataclasses import dataclass, field
from typing import Callable

import lxml.html

from . import getby
from .candidates import xpath_literal
from .css import css_selectors, css_string
from .roles import role_choices
from .selectors import XPATH, InvalidSelector, check_syntax, classify, normalize, strip_prefix

# The key the cookies= preset is registered under.
COOKIE_BANNER_KEY = 'cookie_banner'

COOKIE_CHOICES = ('reject', 'accept')


@dataclass(frozen=True)
class Interruption:
    """One registered interruption: the key it is logged under, the selectors that find
    the element to act on (tried in order), the action and its arguments, and the roles
    the element may have (empty: any). probe is an XPath that matches when any of the
    XPath selectors could match, checked first so a page without the interruption costs
    one query. run is the function heal.interrupt(run=...) got, which handles the
    interruption itself (action is then 'run'); the selectors only show it is there."""
    key: str
    selectors: tuple
    action: str = 'click'
    args: tuple = ()
    roles: tuple = ()
    probe: str | None = None
    preset: str | None = None
    run: Callable | None = None


@dataclass(frozen=True)
class InterruptionEvent:
    """One interruption a healer handled: its key, and the step it was handled for.

    A popup that open(dismiss=True) dismissed is listed too, under the key
    flow.DISMISS_KEY ('<popup>'), with popup (how the log line names it, as
    mendwright.dismiss.summary() does), how ('escape', 'close' or 'decline', as in the
    flow.dismiss journal event) and label (the label of the button that closed it, None
    for Escape). All three are None for a registered interruption."""
    key: str
    before: str
    popup: str | None = None
    how: str | None = None
    label: str | None = None


@dataclass
class Budget:
    """How many interruptions one step has handled (see flow.MAX_INTERRUPTIONS_PER_STEP).
    gave_up names a popup open(dismiss=True) could not dismiss in this step, so it is
    tried once per step and named in one WARNING. raised is what a run= function raised in
    Playwright's overlay handler while the step ran, which cannot raise into the action:
    the step raises it instead (see Healer._overlay_run)."""
    used: int = 0
    handled: list = field(default_factory=list)
    gave_up: str | None = None
    raised: BaseException | None = None


def make_interruption(key, selector, *, action=None, args=(), role=None, run=None, actions=(),
                      awaits=False):
    """Check heal.interrupt()'s arguments and build the Interruption.

    selector is one selector string or a list of them. action must be one of ``actions``
    (the proxy verbs); None is a click. run is a function that handles the interruption
    itself, instead of action and args. awaits=False (the sync healer) rejects an async
    function, which it could not await. Raises TypeError, ValueError or InvalidSelector,
    each naming the fix, before anything touches the page. A key written as a get_by_*
    call is how mendwright names a heal.page.get_by_*() call, and is refused."""
    if not isinstance(key, str) or not key.strip():
        raise TypeError('mendwright: heal.interrupt() needs a key, a non-empty name such as '
                        "'cookies', got " + repr(key) + '.')
    if getby.is_key(key):
        raise ValueError(getby.reserved_key_message(key, 'heal.interrupt()'))
    items = list(selector) if isinstance(selector, (list, tuple)) else [selector]
    if not items:
        raise ValueError('mendwright: heal.interrupt(' + repr(key) + ', []) got an empty list. '
                         "Pass the selector of the element to act on, for example '#cookie-accept'.")
    selectors = []
    for item in items:
        try:
            check_syntax(item)
        except InvalidSelector as exc:
            raise exc.for_key(key) from None
        selectors.append(normalize(item))
    roles = tuple(role_choices(role)) if role is not None else ()
    if run is not None:
        _check_run(key, run, action, args, awaits)
        return Interruption(key=key, selectors=tuple(dict.fromkeys(selectors)), action='run',
                            roles=roles, run=run)
    action = 'click' if action is None else action
    if not isinstance(action, str) or action not in actions:
        raise ValueError('mendwright: heal.interrupt(' + repr(key) + ', ...) got action='
                         + repr(action) + ', which mendwright cannot run. Use one of: '
                         + ', '.join(actions) + ', or run= with a function that handles the '
                         'interruption itself.')
    if isinstance(args, (str, bytes)) or not isinstance(args, (list, tuple)):
        raise TypeError('mendwright: heal.interrupt(' + repr(key) + ', ...) takes args= as a '
                        "tuple, for example action='press', args=('Escape',). Got "
                        + repr(args) + '.')
    return Interruption(key=key, selectors=tuple(dict.fromkeys(selectors)), action=action,
                        args=tuple(args), roles=roles)


def _check_run(key, run, action, args, awaits):
    """heal.interrupt(run=...)'s checks: a function, without action= or args=, and not an
    async one on the sync healer."""
    if not callable(run):
        raise TypeError('mendwright: heal.interrupt(' + repr(key) + ', ..., run=...) takes the '
                        'function that handles the interruption, which gets the page, such as '
                        'def relogin(page): ..., got ' + repr(run) + '.')
    given = []
    if action is not None:
        given.append('action=' + repr(action))
    if not isinstance(args, (list, tuple)) or args:
        given.append('args=' + repr(args))
    if given:
        raise TypeError('mendwright: heal.interrupt(' + repr(key) + ', ...) got run= and '
                        + ' and '.join(given) + '. run= does the whole interruption itself, so '
                        'it takes no action= or args=: drop them, or drop run=.')
    if not awaits and (inspect.iscoroutinefunction(run)
                       or inspect.iscoroutinefunction(type(run).__call__)):
        raise TypeError('mendwright: heal.interrupt(' + repr(key) + ', ..., run=...) got an '
                        'async function, which the healer of mendwright.open() cannot await. '
                        'Make it a plain function, or use mendwright.aio.open() with async '
                        'Playwright.')


def function_name(fn):
    """How the log and the journal name a run= function: its __name__ ('relogin',
    '<lambda>'), else its type's."""
    name = getattr(fn, '__name__', None)
    return name if isinstance(name, str) and name else type(fn).__name__


def nth_targets(selector, count, limit=5):
    """The selectors that each point at one of ``count`` matches of ``selector``: the
    selector itself when it matches once, '(xpath)[n]' for an XPath that matches several,
    and nothing for another kind that matches several (it cannot be narrowed on every
    driver)."""
    if count == 1:
        return [selector]
    if count < 1 or classify(selector) != XPATH:
        return []
    body = strip_prefix(selector)
    return ['xpath=(' + body + ')[' + str(i) + ']' for i in range(1, min(count, limit) + 1)]


# The roles of mendwright's summary of an element (drivers.base.element_summary) whose
# accessible name, for Playwright's role= engine, is their text.
_NAMED_BY_TEXT = ('button', 'link')
# The longest text a suggested selector is built from: a button's or a heading's, not the
# whole text of a dialog.
SUGGEST_TEXT = 60
_TAG = re.compile(r'^[a-z][a-z0-9-]*$')


def precise_selectors(summary, playwright=False):
    """Selectors that may match the element ``summary`` describes (describe()'s result for
    the first visible match of a selector that matches several) and nothing else, best
    first: CSS from its own attributes (mendwright.css.css_selectors: its id or test id
    first), then, for a text of at most SUGGEST_TEXT characters, role=button[name="..."]
    and text="..." on Playwright, and an XPath of its tag and whole text. The caller keeps
    those that match one visible element on the live page."""
    out = []
    try:
        el = lxml.html.fragment_fromstring(summary.get('outer_html') or '')
    except Exception:
        el = None
    if el is not None and isinstance(el.tag, str):
        out.extend(css_selectors(el, limit=3))
    text, tag = summary.get('text') or '', (summary.get('tag') or '').lower()
    if 0 < len(text) <= SUGGEST_TEXT:
        if playwright:
            if summary.get('role') in _NAMED_BY_TEXT:
                out.append('role=' + summary['role'] + '[name=' + css_string(text) + ']')
            out.append('text=' + css_string(text))
        if _TAG.match(tag):
            out.append('//' + tag + '[normalize-space()=' + xpath_literal(text) + ']')
    return list(dict.fromkeys(out))


# --- the cookies= preset --------------------------------------------------------------------

_UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_LOWER = 'abcdefghijklmnopqrstuvwxyz'
# Punctuation in a label, including both apostrophes, so "Don't allow" reads as 'don t allow'.
_PUNCT = ".,:;!?()[]'’×-_/"


def _cls(name):
    """XPath predicate: the class attribute has the class ``name``."""
    return "contains(concat(' ', normalize-space(@class), ' '), ' " + name + " ')"


def _ids(*ids):
    return ' or '.join("@id='" + i + "'" for i in ids)


def _union(parts):
    return ' | '.join(parts)


# The words a button's label is checked for: its text, value, aria-label and title, in
# lower case, with punctuation turned into spaces and a space at each end, so that
# ' close ' matches "Close" and "Close banner" but not "Disclosure".
_LABEL = ("concat(' ', translate(concat(normalize-space(.), ' ', @value, ' ', @aria-label, ' ', "
          '@title), "' + _UPPER + _PUNCT + '", "' + _LOWER + ' ' * len(_PUNCT) + "\"), ' ')")
_BUTTONISH = ("(self::button or self::a or @role='button' or "
              "(self::input and (@type='button' or @type='submit')))")

# The longest text a banner may have. A cookie banner has a sentence or two and its
# buttons; an element with more text than this is the page, not a banner.
BANNER_MAX_TEXT = 1500

_ATTRS = ("translate(concat(@id, ' ', @class, ' ', @aria-label, ' ', @aria-labelledby, ' ', "
          "@data-testid), '" + _UPPER + "', '" + _LOWER + "')")
# A field you type into: a page has them, a cookie banner does not.
_TEXT_FIELD = (".//textarea or .//input[not(@type) or @type='text' or @type='email' or "
               "@type='password' or @type='search' or @type='tel' or @type='url' or "
               "@type='number']")
# The banner a fallback button must be in. Every condition is on the same element.
_CONTAINER = ("ancestor-or-self::*[not(self::html or self::body or self::main)]"
              "[contains(" + _ATTRS + ", 'cookie') or contains(" + _ATTRS + ", 'consent')]"
              "[contains(translate(., '" + _UPPER + "', '" + _LOWER + "'), 'cookie')]"
              "[string-length(normalize-space(.)) <= " + str(BANNER_MAX_TEXT) + "]"
              "[not(.//main or .//nav or " + _TEXT_FIELD + ")]")


def _says(words):
    return '(' + ' or '.join('contains(' + _LABEL + ", ' " + w + " ')" for w in words) + ')'


def _says_none(words):
    return 'not(' + _says(words) + ')'


# A button's visible text (or an input's value), in lower case, with punctuation turned
# into spaces and the spaces normalized: "Close!" reads as 'close', "×" as ''.
_SHOWN = ("normalize-space(translate(concat(normalize-space(.), ' ', self::input/@value), \""
          + _UPPER + _PUNCT + '", "' + _LOWER + ' ' * len(_PUNCT) + '"))')
# What a close button may say it closes, after "close", "close the" or "close this": the
# popup itself. "Close account", "Close position" and "Close ticket" do something else.
_CLOSE_WHAT = ('banner', 'notice', 'message', 'dialog', 'popup', 'pop up', 'modal', 'window',
               'overlay', 'panel', 'drawer', 'sheet', 'chat', 'survey', 'cart', 'menu',
               'sidebar', 'search', 'lightbox', 'tour', 'video', 'notification', 'tooltip',
               'ad', 'advertisement', 'promotion', 'promo', 'newsletter', 'announcement',
               'bar', 'cookie banner', 'cookie notice', 'cookie message', 'cookie bar',
               'cookies banner', 'cookies notice', 'consent banner', 'privacy notice')
# The whole label of a Close button, in words (lower case, punctuation as spaces): Close,
# Close banner, Close this dialog, ... mendwright.dismiss reads close buttons the same way.
CLOSE_NAMES = ('close',) + tuple('close ' + a + w for w in _CLOSE_WHAT
                                 for a in ('', 'the ', 'this '))
_CLOSE_LIST = "'|" + '|'.join(CLOSE_NAMES) + "|'"


def _named(expr):
    """XPath: expr, in words as _SHOWN reads a label (with | as a space too), is one of
    CLOSE_NAMES."""
    norm = ('normalize-space(translate(' + expr + ', "' + _UPPER + _PUNCT + '|", "' + _LOWER
            + ' ' * (len(_PUNCT) + 1) + '"))')
    return '(' + norm + " != '' and contains(" + _CLOSE_LIST + ", concat('|', " + norm + ", '|')))"


# A Close button: its whole label is a close name, or it shows one glyph or none (an icon)
# and its aria-label (or, without one, its title) is one. "Close account" is not a Close
# button, as text or as an icon's name.
_CLOSE = ('(' + _named("concat(normalize-space(.), ' ', self::input/@value)") + ' or ('
          + _SHOWN + " = '' or string-length(normalize-space(.)) <= 1) and ("
          "(normalize-space(@aria-label) != '' and " + _named('@aria-label') + ') or '
          "(normalize-space(@aria-label) = '' and " + _named('@title') + ')))')


_REJECT_WORDS = ('reject', 'decline', 'deny', 'disagree', 'necessary only', 'only necessary',
                 'necessary cookies only', 'essential only', 'only essential',
                 'without accepting', 'without agreeing')
_ACCEPT_WORDS = ('accept', 'allow', 'agree')
_NOT_ACCEPT = ('accept', 'allow', 'agree')
_NOT_REJECT = ('reject', 'decline', 'deny', 'disagree', 'necessary', 'essential', 'not',
               'don t', 'without')


def generic_xpath(choice):
    """The fallback for consent banners no listed manager made (see the module docs).

    The banner check comes before the label checks: XPath stops at the first false
    operand of 'and', and on a page without a banner the attribute test fails at once for
    each button, where the label tests would read every button's text a dozen times.
    Playwright runs this at least twice per action on the page (see Healer._watch)."""
    if choice == 'reject':
        says = ('(' + _says(_REJECT_WORDS) + ' or ' + _CLOSE + ') and '
                + _says_none(_NOT_ACCEPT))
    else:
        says = _says(_ACCEPT_WORDS) + ' and ' + _says_none(_NOT_REJECT)
    return ('//*[' + _BUTTONISH + ' and ' + _CONTAINER
            + ' and string-length(normalize-space(.)) <= 40 and ' + says + ']')


# Per manager: (reject buttons, close buttons, accept buttons), as XPath expressions.
MANAGERS = {
    'OneTrust': (
        ["//*[" + _ids('onetrust-reject-all-handler') + "]",
         "//*[" + _cls('ot-pc-refuse-all-handler') + "]"],
        ["//*[@id='onetrust-banner-sdk' or @id='onetrust-pc-sdk']//*["
         + _cls('onetrust-close-btn-handler') + "]"],
        ["//*[" + _ids('onetrust-accept-btn-handler', 'accept-recommended-btn-handler') + "]"],
    ),
    'Cookiebot': (
        ["//*[" + _ids('CybotCookiebotDialogBodyButtonDecline',
                       'CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll') + "]"],
        [],
        ["//*[" + _ids('CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                       'CybotCookiebotDialogBodyButtonAccept') + "]"],
    ),
    'Didomi': (
        ["//*[" + _ids('didomi-notice-disagree-button') + "]",
         "//*[@id='didomi-notice' or @id='didomi-popup' or @id='didomi-host']//*["
         + _cls('didomi-continue-without-agreeing') + "]"],
        [],
        ["//*[" + _ids('didomi-notice-agree-button') + "]"],
    ),
    'TrustArc': (
        ["//*[" + _ids('truste-consent-required') + "]"],
        [],
        ["//*[" + _ids('truste-consent-button') + "]"],
    ),
    'Usercentrics': (
        ["//*[" + _ids('uc-btn-deny-banner') + "]",
         "//*[@data-testid='uc-deny-all-button']"],
        [],
        ["//*[" + _ids('uc-btn-accept-banner') + "]",
         "//*[@data-testid='uc-accept-all-button']"],
    ),
    'Quantcast Choice': (
        ["//*[@id='qc-cmp2-ui' or " + _cls('qc-cmp2-container') + "]//button["
         + _says(('disagree', 'reject', 'reject all')) + "]"],
        [],
        ["//*[@id='qc-cmp2-ui' or " + _cls('qc-cmp2-container') + "]//button[@mode='primary' and "
         + _says(('agree', 'accept', 'accept all')) + ' and ' + _says_none(('disagree',)) + "]"],
    ),
    'Osano': (
        ["//*[" + _cls('osano-cm-denyAll') + "]",
         "//*[" + _cls('osano-cm-window') + "]//*[" + _cls('osano-cm-deny') + "]"],
        ["//*[" + _cls('osano-cm-window') + "]//*[" + _cls('osano-cm-dialog__close') + "]"],
        ["//*[" + _cls('osano-cm-accept-all') + "]",
         "//*[" + _cls('osano-cm-window') + "]//*[" + _cls('osano-cm-accept') + "]"],
    ),
    'cookieconsent': (
        ["//*[" + _cls('cc-window') + "]//*[" + _cls('cc-deny') + "]",
         "//*[@id='cc-main']//*[@data-role='necessary' or @data-role='accept-necessary']"],
        [],
        ["//*[" + _cls('cc-window') + "]//*[" + _cls('cc-allow') + " or " + _cls('cc-dismiss') + "]",
         "//*[@id='cc-main']//*[@data-role='all' or @data-role='accept-all']"],
    ),
    'CookieYes': (
        ["//*[" + _cls('cky-btn-reject') + " or @data-cky-tag='reject-button']",
         "//*[" + _ids('cookie_action_close_header_reject') + "]"],
        ["//*[" + _cls('cky-banner-btn-close') + " or @data-cky-tag='close-button']"],
        ["//*[" + _cls('cky-btn-accept') + " or @data-cky-tag='accept-button']",
         "//*[" + _ids('cookie_action_close_header') + "]"],
    ),
    'Complianz': (
        ["//*[" + _cls('cmplz-cookiebanner') + "]//*[" + _cls('cmplz-deny') + "]"],
        ["//*[" + _cls('cmplz-cookiebanner') + "]//*[" + _cls('cmplz-close') + "]"],
        ["//*[" + _cls('cmplz-cookiebanner') + "]//*[" + _cls('cmplz-accept') + "]"],
    ),
}

# Usercentrics' buttons inside its shadow root, for Playwright's CSS engine.
SHADOW_CSS = {
    'reject': '[data-testid="uc-deny-all-button"]',
    'accept': '[data-testid="uc-accept-all-button"]',
}


def preset_selectors(choice):
    """The selectors of the cookies= preset, in the order they are tried: the managers'
    reject (or accept) buttons, then their close buttons (reject only), then the generic
    fallback, then the Usercentrics shadow-root CSS."""
    if choice not in COOKIE_CHOICES:
        raise ValueError(cookie_choice_error(choice))
    if choice == 'reject':
        tiers = [[x for m in MANAGERS.values() for x in m[0]],
                 [x for m in MANAGERS.values() for x in m[1]]]
    else:
        tiers = [[x for m in MANAGERS.values() for x in m[2]]]
    out = ['xpath=' + _union(tier) for tier in tiers if tier]
    out.append('xpath=' + generic_xpath(choice))
    out.append(SHADOW_CSS[choice])
    return tuple(out)


def cookie_choice_error(choice, entry='mendwright.open()'):
    return (entry + ' got cookies=' + repr(choice) + '. Use cookies="reject" to close '
            'consent banners without accepting, cookies="accept" to accept them, or leave '
            'cookies out to click nothing you did not ask for.')


def cookie_preset(choice):
    """The Interruption that cookies='reject' or cookies='accept' registers."""
    selectors = preset_selectors(choice)
    probe = 'xpath=' + _union(strip_prefix(s) for s in selectors if classify(s) == XPATH)
    return Interruption(key=COOKIE_BANNER_KEY, selectors=selectors, action='click',
                        roles=(), probe=probe, preset=choice)
