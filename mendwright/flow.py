"""Flow changes: what mendwright does when the steps of a script, not the selectors on a
page, have changed.

- page_path(): the normalized path of a page, which the store keeps per key ('lastPage'
  and 'pages'). A heal on a page where the key's element was never found is declined
  before any model call (see flow_change_message), because a step that moved to another
  page looks like a missing element and a model may still find a similar one.
- is_interception_error(): whether an action failed because something covered the
  element (a cookie banner, a modal). mendwright then looks for a registered interruption
  and retries the action once, only after it handled one.
- UNROUTED_STEP: what an interruption is logged as handled before when it showed up
  during a Playwright action mendwright did not route.
- The texts mendwright logs or raises for interruptions, skipped steps and the page
  guard. Sync and async healers use the same ones.
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


# --- messages ------------------------------------------------------------------------------

def flow_change_message(key, last, here, entry='mendwright.open()'):
    """Why a heal was declined because the page changed since the key was last found."""
    return ("'" + key + "' was last found on " + last + ', and this page is ' + here + '. '
            'That looks like a change in the flow, not a renamed element, so mendwright did '
            'not heal it. If the step moved, update your script. If the URL itself changed '
            'in a redesign, pass heal_across_pages=True to ' + entry + '.')


def handled_message(key, before):
    return 'handled interruption ' + repr(key) + ' before ' + repr(before)


def unhandled_message(key, before, error):
    first = (str(error).strip().splitlines() or [type(error).__name__])[0]
    return ('could not handle interruption ' + repr(key) + ' before ' + repr(before) + ' ('
            + type(error).__name__ + ': ' + first[:200] + '). Check the selector and action '
            'passed to heal.interrupt(' + repr(key) + ', ...).')


def loop_message(before, key, count):
    return ('mendwright: ' + str(count) + ' interruptions were handled for ' + repr(before)
            + ' and ' + repr(key) + ' is still on the page, so mendwright stopped instead of '
            'handling it again. Check that the action registered with heal.interrupt('
            + repr(key) + ', ...) really closes it, for example with action="press", '
            'args=("Escape",) or a selector for its close button.')


def stuck_message(key, count):
    """The WARNING for an interruption that stayed on the page after its action, when
    Playwright's overlay handler stops handling it (see Healer._overlay_wanted)."""
    return (repr(key) + ' was still on the page after mendwright handled it ' + str(count)
            + ' times in a row, so Playwright\'s overlay handler leaves it alone until it is '
            'gone. Check that the action registered with heal.interrupt(' + repr(key)
            + ', ...) really closes it, for example with action="press", args=("Escape",) or '
            'a selector for its close button.')


def nothing_matched_message(before, keys, preset=False):
    """The WARNING for an action that failed because something covered its element or it
    could not be reached, when no interruption matched a visible element. keys are the
    ones registered with heal.interrupt(); preset is True when open(cookies=...) added
    its own."""
    looked = []
    if keys:
        looked.append('heal.interrupt(): ' + ', '.join(repr(k) for k in keys))
    if preset:
        looked.append('the cookies= preset')
    text = ('the action on ' + repr(before) + ' failed because something covered its '
            'element or it could not be reached, and none of the interruptions mendwright '
            'looks for (' + '; '.join(looked) + ') matched a visible element, so it did not '
            'try the action again.')
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
                    'flow.page_guard': 'page_guard'}


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
