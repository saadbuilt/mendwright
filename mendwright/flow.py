"""Flow changes: what mendwright does when the steps of a script, not the selectors on a
page, have changed.

- page_path(): the normalized path of a page, which the store keeps per key ('lastPage'
  and 'pages'). A heal on a page where the key's element was never found is declined
  before any model call (see flow_change_message), because a step that moved to another
  page looks like a missing element and a model may still find a similar one.
- is_interception_error(): whether an action failed because something covered the
  element (a cookie banner, a modal). mendwright then looks for a registered interruption
  and retries the action once, only after it handled one.
- is_stale_error(): whether an action failed because the page replaced the element after
  it was found. That says nothing about a heal, so it quarantines none.
- UNROUTED_STEP: what an interruption is logged as handled before when it showed up
  during a Playwright action mendwright did not route.
- DISMISS_KEY: the key a popup that open(dismiss=True) dismissed is listed under (see
  mendwright.dismiss).
- The texts mendwright logs or raises for interruptions, dismissed popups, skipped steps
  and the page guard. Sync and async healers use the same ones.
"""

import re
from urllib.parse import urlsplit

from .exceptions import AttentionRequired

# How many interruptions mendwright handles for one step (one locator() call, or one
# action) before it stops and raises AttentionRequired: an interruption that comes back
# every time it is handled would otherwise loop forever.
MAX_INTERRUPTIONS_PER_STEP = 3

# After an interruption's action, how long mendwright waits for it to go away (a banner
# that fades out), at most. The step's own timeout_ms is used when it is shorter.
SETTLE_MS = 2000

# The step an interruption is handled before when Playwright's overlay handler (see
# Healer._watch) ran for an action mendwright did not route, such as
# heal.page.get_by_role(...).click(): mendwright never sees that action, so it has no key
# to name it by. The angle brackets keep it from reading as a key in the log and journal.
UNROUTED_STEP = '<page action>'

# The key handled_interruptions lists a popup under that open(dismiss=True) dismissed (see
# mendwright.dismiss): nobody registered it, so it has no key of its own.
DISMISS_KEY = '<popup>'

_UUID = re.compile(r'^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$')
# Hex of 16 or more characters: database ids such as MongoDB's 24, hashes.
_HEX = re.compile(r'^[0-9a-f]{16,}$')
# A long token of letters, digits, '-' and '_': ULIDs, nanoids, many generated ids.
_TOKEN = re.compile(r'^[a-z0-9_-]{16,}$')
_NO_PATH = ('about:', 'data:', 'blob:', 'javascript:')


def _is_id(part):
    """True for a path segment that names one record rather than a page: all digits, a
    uuid, 16 or more hex characters, or a token of 16 or more letters and digits with at
    least two digits. Short segments such as 'v2', '2fa' or 'step-3' are page names."""
    if part.isdigit() or _UUID.match(part) or _HEX.match(part):
        return True
    return (bool(_TOKEN.match(part)) and sum(c.isdigit() for c in part) >= 2
            and any(c.isalpha() for c in part))


class InterruptionLoop(AttentionRequired):
    """Raised when a step has handled MAX_INTERRUPTIONS_PER_STEP interruptions and one is
    still on the page. It is an AttentionRequired, and no heal is tried after it."""


def page_path(url):
    """The page part of ``url`` that mendwright compares between runs: the path in
    lower case, without scheme, host, query or fragment, without empty segments (so a
    trailing or doubled slash changes nothing), and with segments that look like record
    ids (see _is_id) replaced by '*'. None for about:blank, data: URLs and pages with no
    path (the site root included)."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if text.lower().startswith(_NO_PATH):
        return None
    try:
        path = urlsplit(text).path
    except ValueError:
        return None
    segments = ['*' if _is_id(part) else part for part in path.lower().split('/') if part]
    if not segments:
        return None
    return '/' + '/'.join(segments)


def is_interception_error(exc):
    """True when an action failed because another element covered the target or the
    target could not be interacted with: Playwright's "intercepts pointer events" and
    "element is not visible", Selenium's ElementClickInterceptedException and
    ElementNotInteractableException. Matched by name and text, so selenium and playwright
    need not be importable.

    False when Playwright's call log shows the action reached the page ("click action
    done", then "waiting for scheduled navigations to finish"), even if an earlier attempt
    in the same log was intercepted: such an action may have taken effect, for example a
    click that placed an order and then timed out waiting for the next page, so it must
    never be run again."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {'ElementClickInterceptedException', 'ElementNotInteractableException'}:
        return True
    text = str(exc).lower()
    if _ACTION_RAN.search(text):
        return False
    return 'intercepts pointer events' in text or 'element is not visible' in text


# Playwright's call-log lines once a pointer action was performed (see is_interception_error).
_ACTION_RAN = re.compile(r'\baction done\b|waiting for scheduled navigations')
# What a browser library says when the page went away under a call: a click that started
# a navigation, say.
_PAGE_WENT = re.compile(r'navigat|context was destroyed|target closed|has been closed|'
                        r'detached', re.I)


def click_may_have_run(exc):
    """Whether a click that raised exc may have reached the page all the same: Playwright's
    call log says the click action was done (it then waited for a navigation, say, and
    timed out), or the page navigated or closed under the call. False for an error that
    came before the click, such as a timeout waiting for the element to be visible or
    another element that intercepted it."""
    text = str(exc)
    return bool(_ACTION_RAN.search(text.lower()) or _PAGE_WENT.search(text))


def is_stale_error(exc):
    """True when an action failed because the element it had was removed or replaced
    after it was found: Selenium's StaleElementReferenceException, when the page
    re-rendered the element between find_element() and the action. The element was the
    right one when it was found, so the failure says nothing about a heal, and the heal
    canary is not told. Matched by name, as is_interception_error() is."""
    return 'StaleElementReferenceException' in {cls.__name__ for cls in type(exc).__mro__}


# --- messages ------------------------------------------------------------------------------

def flow_change_message(key, last, here, entry='mendwright.open()'):
    """Why a heal was declined because the page changed since the key was last found."""
    return ("'" + key + "' was last found on " + last + ', and this page is ' + here + '. '
            'That looks like a change in the flow, not a renamed element, so mendwright did '
            'not heal it. If the step moved, update your script. If the URL itself changed '
            'in a redesign, pass heal_across_pages=True to ' + entry + '.')


def handled_message(key, before, function=None):
    """The WARNING for an interruption mendwright handled. function names the run=
    function of heal.interrupt() that handled it, if any."""
    text = 'handled interruption ' + repr(key) + ' before ' + repr(before)
    return text + (' with ' + function + '()' if function else '')


def run_failed_message(key, before, function, error, overlay=False):
    """The WARNING for the run= function of interruption key, named function, that raised
    error. overlay is True when Playwright's overlay handler called it: the handler cannot
    raise into the action it runs for, so the step before raises it, when mendwright routes
    it, and otherwise nothing does."""
    first = (str(error).strip().splitlines() or [type(error).__name__])[0]
    text = (function + '(), the run= function of interruption ' + repr(key) + ', raised an '
            'error while mendwright handled it before ' + repr(before) + ' ('
            + type(error).__name__ + ': ' + first[:200] + ').')
    if not overlay:
        return text + ' The error is raised as it is.'
    left = ('leaves ' + repr(key) + ' alone until mendwright sees it gone or the page '
            'navigates.')
    if before != UNROUTED_STEP:
        return (text + " Playwright's overlay handler cannot raise it into the action, so "
                + repr(before) + ' raises it once Playwright hands back to it, and the handler '
                + left)
    return (text + " Playwright's overlay handler cannot raise it into the action, so it "
            + left + ' A step mendwright routes still handles ' + repr(key) + ', and raises '
            'what ' + function + '() raises.')


def run_cancelled_message(key, before, function):
    """The WARNING for the run= function of interruption key, named function, that was
    cancelled on mendwright.aio.open()'s healer because the step it ran for was."""
    return (function + '(), the run= function of interruption ' + repr(key) + ', was '
            'cancelled while mendwright handled it before ' + repr(before) + ', because that '
            'step was cancelled. ' + repr(key) + ' may still be on the page, partly filled '
            'in.')


def run_plain_message(key, function, result):
    """The TypeError text for a plain run= function on the async healer that returned no
    awaitable."""
    return ('mendwright: ' + function + '(), the run= function of interruption ' + repr(key)
            + ', returned ' + repr(result) + ", not an awaitable. On mendwright.aio.open()'s "
            "healer, heal.page's calls are coroutines, and a plain function that makes them "
            'without await does nothing on the page: make it async def and await them, or '
            'return the awaitable.')


def several_message(what, name, selector, count, looked, found, *, visible=1,
                    playwright=False):
    """The WARNING for an interruption (what='interruption') or a step's landmark
    ('landmark') that mendwright skipped because its selector matches count elements:
    looked is how many of them it looked at (0: a CSS or Playwright selector that matches
    several is not acted on; 1: Selenium asks about the first match of a CSS landmark;
    more: the first ones), none visible. found are selectors that match one visible
    element on this page (see interruptions.precise_selectors), visible is how many of the
    matches are visible (0, 1, or 2 for two or more; None: the driver cannot tell), and
    playwright whether the generic advice may name Playwright's own selectors. With two or
    more visible, the caller passes no found: a selector built from one of them could
    name the wrong one."""
    if what == 'interruption':
        text = ('interruption ' + repr(name) + ' was skipped: its selector ' + repr(selector)
                + ' matches ' + str(count) + ' elements, and ')
        if looked == 0:
            text += ('mendwright acts on a CSS or Playwright selector only when it matches one '
                     '(an XPath is narrowed to its first visible match).')
        else:
            text += ('mendwright looks at the first ' + str(looked) + ' of them only, and none '
                     'of those is visible.')
        use = 'Use a selector that matches only the element to act on'
    else:
        text = ('step ' + repr(name) + ' did not show: its landmark ' + repr(selector)
                + ' matches ' + str(count) + ' elements, and ')
        if looked == 1:
            text += ('on Selenium mendwright asks only whether the first of them is visible, '
                     'for a CSS selector, and it is not.')
        else:
            text += ('mendwright looks at the first ' + str(looked) + ' of them only, and none '
                     'of those is visible.')
        use = 'Use a landmark that matches one element'
    if found and visible == 1:
        return (text + ' ' + use + ', such as ' + ' or '.join(repr(s) for s in found)
                + (', which matches' if len(found) == 1 else ', which each match')
                + ' one visible element on this page.')
    advice = ('its id or test id, '
              + ('its role and name, as in role=button[name="Accept all"], its whole text, as '
                 'in text="Accept all", ' if playwright else '')
              + "or an XPath of its tag and text, as in //button[normalize-space()='Accept all'].")
    if visible is not None and visible >= 2:
        text += (' More than one of them is visible, so mendwright cannot tell which one is '
                 'meant.')
    elif visible is not None and visible <= 0:
        text += ' None of them is visible now.'
    return text + ' ' + use + ': ' + advice


def unhandled_message(key, before, error):
    first = (str(error).strip().splitlines() or [type(error).__name__])[0]
    return ('could not handle interruption ' + repr(key) + ' before ' + repr(before) + ' ('
            + type(error).__name__ + ': ' + first[:200] + '). Check the selector and action '
            'passed to heal.interrupt(' + repr(key) + ', ...).')


def loop_message(before, key, count, function=None):
    """The InterruptionLoop text. function names the run= function of heal.interrupt()
    that handled key, if any."""
    return ('mendwright: ' + str(count) + ' interruptions were handled for ' + repr(before)
            + ' and ' + repr(key) + ' is still on the page, so mendwright stopped instead of '
            'handling it again. ' + _check_closes(key, function))


def stuck_message(key, count, function=None):
    """The WARNING for an interruption that stayed on the page after its action, when
    Playwright's overlay handler stops handling it (see Healer._overlay_wanted)."""
    return (repr(key) + ' was still on the page after mendwright handled it ' + str(count)
            + ' times in a row, so Playwright\'s overlay handler leaves it alone until '
            'mendwright sees it gone or the page navigates. ' + _check_closes(key, function))


def _check_closes(key, function):
    """What loop_message() and stuck_message() tell the user to check: the action of
    interruption key, or its run= function."""
    if function:
        return ('Check that ' + function + '(), the run= function registered with '
                'heal.interrupt(' + repr(key) + ', ...), really closes it.')
    return ('Check that the action registered with heal.interrupt(' + repr(key) + ', ...) '
            'really closes it, for example with action="press", args=("Escape",) or a '
            'selector for its close button.')


def _listed(items):
    items = list(items)
    if len(items) < 2:
        return ''.join(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def step_text(how, label=None):
    """How a log line names one step on a popup: 'Escape', or a button's label in quotes,
    as people read buttons."""
    if how == 'escape':
        return 'Escape'
    return '“' + label + '”' if label else 'an unlabelled button'


def _did(tried):
    """What mendwright did, from step_text() results: 'pressed Escape and clicked “Close”'."""
    return _listed('pressed Escape' if t == 'Escape' else 'clicked ' + t for t in tried)


def dismissed_message(before, popup, how, tried):
    """The WARNING for a popup open(dismiss=True) dismissed. popup is how
    mendwright.dismiss.summary() names it, how is step_text() of the step that closed it,
    and tried lists the steps before that did not close it."""
    text = 'dismissed a popup in the way of ' + repr(before) + ': ' + popup + ' with ' + how
    if tried:
        text += ', after ' + _listed(tried) + ' did not close it'
    return text


_REGISTER = ('Register the button that closes it with heal.interrupt("<name>", "<selector of '
             'the button>"). If it needs data first, register its field before the button: '
             'heal.interrupt("<name>_field", "<selector of the field>", action="fill", '
             'args=("<value>",)).')


def undismissed_message(before, popup, tried, escaped=True, changed=False, consent=False):
    """The WARNING for a popup in the way of a step that open(dismiss=True) found and could
    not dismiss. tried lists the steps it took, in order (step_text()). escaped=False says
    it did not press Escape, because the keyboard focus was outside the popup or the popup
    is in an iframe, or with consent=True because cookies="accept" is set. changed=True
    says the popup changed before mendwright could act."""
    if changed:
        did = 'It changed before mendwright could act on it, so mendwright left it alone'
    elif tried:
        did = ('mendwright ' + _did(tried) + ', which did not close it, and it has no '
               + ('other ' if tried != ['Escape'] else '') + 'close or decline button that '
               'mendwright may click')
    else:
        did = ('It has no close or decline button that mendwright may click, so mendwright '
               'left it alone')
    if not escaped and not changed and consent:
        did += ('. mendwright did not press Escape: with cookies="accept" it never does, '
                'because every key listener of the page gets the key, a consent banner\'s '
                'too, which may take it as a refusal')
    elif not escaped and not changed:
        did += ('. mendwright did not press Escape, which goes to the element that has the '
                'keyboard focus: that was outside the popup, or the popup is in an iframe')
    return ('could not dismiss a popup in the way of ' + repr(before) + ': ' + popup + '. '
            + did + '. The action goes ahead, and fails with the browser\'s own error if the '
            'popup blocks it. ' + _REGISTER)


def lost_message(before, popup, did, raised=False):
    """The WARNING for an element that could not be looked at any more once mendwright did
    ``did`` ('pressed Escape', 'clicked “No thanks”') on a popup in its way, or, with
    raised=True, for a click that raised an error after it may have reached the page."""
    if raised:
        what = (', and the click raised an error after it may have reached the page (a '
                'navigation it started did not finish in time, say), so mendwright cannot '
                'tell what it did. mendwright')
    else:
        what = (', and then could not look at the element of ' + repr(before) + ' as '
                'before (it matches no element or several now, it is hidden or outside the '
                'window, or it left the page). That step may have changed the page, so '
                'mendwright')
    return (did[0].upper() + did[1:] + ' on a popup in the way of ' + repr(before) + ': '
            + popup + what + ' does not count the popup as dismissed and does not try a '
            'failed action again.')


def consent_message(before, popup):
    """The WARNING for a popup that looks like a consent banner, with open(cookies="accept")
    and dismiss=True: dismissing it would turn the consent down."""
    return ('left a popup in the way of ' + repr(before) + ' alone: ' + popup + '. It looks '
            'like a consent banner the cookies="accept" preset does not know, and Escape, a '
            'close or a decline button could turn the consent down, so mendwright does not '
            'dismiss it. The action goes ahead, and fails with the browser\'s own error if the '
            'banner blocks it. Register its accept button with heal.interrupt("<name>", '
            '"<selector of the button>").')


def alert_message(before, popup):
    """The WARNING for a popup that is a role="alertdialog": it asks for an answer, and
    Escape or any of its buttons would give one."""
    return ('left a popup in the way of ' + repr(before) + ' alone: ' + popup + '. It is an '
            'alert dialog (role="alertdialog"), which asks for an answer, such as "Delete '
            'this project?", and Escape or any of its buttons would give one, so mendwright '
            'does not press or click anything on it. The action goes ahead, and fails with '
            'the browser\'s own error if the dialog blocks it. If your script should answer '
            'it, register the button with heal.interrupt("<name>", "<selector of the '
            'button>").')


def dismiss_loop_message(before, popup, count, again=True):
    """The AttentionRequired text for a step whose budget is used up while a popup still
    covers its element. again=True: a popup was dismissed in this step already, so it is
    likely one that comes back each time; False: the budget went to interruptions."""
    if again:
        return ('mendwright: ' + str(count) + ' interruptions and popups were handled for '
                + repr(before) + ' and a popup is still in the way: ' + popup + '. It comes '
                'back each time it is dismissed, so mendwright stopped instead of dismissing it '
                'again. Register the button that closes it for good with heal.interrupt(), or '
                'find out what opens it again.')
    return ('mendwright: ' + str(count) + ' interruptions were handled for ' + repr(before)
            + ' and a popup is still in the way: ' + popup + '. A step handles at most '
            + str(MAX_INTERRUPTIONS_PER_STEP) + ', so mendwright stopped instead of '
            'dismissing it. Find out what keeps opening them, or register the button that '
            'closes the popup with heal.interrupt().')


def rejected_selector_message(key, selector, error):
    """The WARNING for a selector of interruption key that the browser cannot parse,
    which Playwright's overlay handler leaves out (see Healer._watch)."""
    first = (str(error).strip().splitlines() or [type(error).__name__])[0]
    return ('the browser rejected the selector ' + repr(selector) + ' of interruption '
            + repr(key) + ' (' + first[:200] + '), so mendwright never finds the interruption '
            'with it, and Playwright\'s overlay handler leaves it out. Fix the selector, or use '
            'an XPath, which heal.interrupt() checks without a browser.')


def nothing_matched_message(before, keys, preset=False, framed=False):
    """The WARNING for an action that failed because something covered its element or it
    could not be reached, when no interruption matched a visible element. keys are the
    ones registered with heal.interrupt(); preset is True when open(cookies=...) added
    its own. framed is True when the Selenium driver was inside a frame the script had
    switched into, from where mendwright cannot see the page around the frame."""
    looked = []
    if keys:
        looked.append('heal.interrupt(): ' + ', '.join(repr(k) for k in keys))
    if preset:
        looked.append('the cookies= preset')
    text = ('the action on ' + repr(before) + ' failed because something covered its '
            'element or it could not be reached, and none of the interruptions mendwright '
            'looks for (' + '; '.join(looked) + ') matched a visible element, so it did not '
            'try the action again.')
    if framed:
        text += (' The driver is inside a frame the script switched into, so mendwright looked '
                 'for them in that frame only, and one on the page around the frame, such as a '
                 'cookie banner over it, is out of reach from there. Handle that one before '
                 'switch_to.frame(), or find the element from the page without switching: '
                 'heal.driver follows a selector into an iframe one level down.')
        if keys:
            text += (' If the element in the way is in the frame and is one of those registered '
                     'with heal.interrupt(), its selector no longer matches: update it.')
        return text
    if keys:
        return (text + ' If the element in the way is one of those registered with '
                'heal.interrupt(), its selector no longer matches: update it. Otherwise '
                'register the button that closes the element in the way with heal.interrupt()'
                + (', for example for a cookie banner the cookies= preset does not know.'
                   if preset else '.'))
    return (text + ' If the element in the way is a cookie banner the preset does not know, '
            'or anything else, register the button that closes it with heal.interrupt().')


def skip_message(key, reason, before=None):
    """The INFO line for a step that was skipped instead of healed."""
    if reason == 'interruption handled':
        where = (' before ' + repr(before)) if before else ''
        return ('skipped ' + repr(key) + ': it was already handled as an interruption' + where
                + ', and it is not on the page now.')
    if reason == 'interruption absent':
        return ('skipped ' + repr(key) + ': it is registered with heal.interrupt() and is not '
                'on the page, so there was nothing to do.')
    return 'skipped ' + repr(key) + ': it is optional (optional=True) and is not on the page.'


def absent_message(key):
    """The AttributeError text for reading from a step that was skipped."""
    return ("mendwright: '" + key + "' was not on the page, so this step was skipped and "
            'there is no element to read. Check proxy.present before reading from an '
            'optional step.')


_JOURNAL_HELPERS = {'flow.interrupt': 'interrupted', 'flow.skip': 'skipped',
                    'flow.page_guard': 'page_guard', 'flow.dismiss': 'dismissed',
                    'flow.step': 'stepped'}


def journal_event(journal, name, key, **fields):
    """Write one flow.* event to ``journal``. A journal passed to open() that predates
    these events (it has event() but not the named helper) still gets the line; one that
    has neither is skipped. Never raises, like every journal write."""
    try:
        helper = getattr(journal, _JOURNAL_HELPERS.get(name, ''), None)
        if callable(helper):
            helper(key, **fields)
            return
        event = getattr(journal, 'event', None)
        if callable(event):
            event(name, key=key, **fields)
    except Exception:
        pass
