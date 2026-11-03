"""The story `mendwright demo` tells, written against the public facade.

    heal = mendwright.open(page, provider=..., store_path=<temp dir>)
    heal.interrupt('cookies', '#cookie-accept')
    heal.locator('accept_terms', '#accept-terms').check(...)
    heal.locator('pay', '#pay', destructive=True)

v1 of a checkout page matches both selectors. v2 (a redesign) removed both ids, and a
cookie banner now opens first and covers the form. The checkbox misses; before any heal,
mendwright finds the banner the story registered with heal.interrupt() and clicks it
away. The checkbox still misses, so the provider proposes an XPath, mendwright's local
checks pass it, the step is retried and the box is checked. The pay button misses too,
and is refused without a model call, because its key is marked destructive=True and
mendwright never heals a key marked destructive.

The provider is the one auto_provider() builds from the environment, the one `mendwright
doctor` shows, so the accept_terms heal is a real model call. With --scripted it is
ScriptedProvider: it builds the real prompt and parses a canned reply with the real
parser, but makes no API call. The page is StandInPage, or a real Playwright page with
--browser. What the command prints is mendwright's own log lines from the run, the ones
your own script logs through the 'mendwright' logger, between a line that says what the
demo is and a one-line summary.
"""

import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import resources

from ..exceptions import AttentionRequired
from ..facade import open as _open_healer
from ..prompt import build_prompt, parse_proposal
from ..selectors import InvalidSelector

TERMS_KEY = 'accept_terms'
TERMS_SELECTOR = '#accept-terms'
PAY_KEY = 'pay'
PAY_SELECTOR = '#pay'
COOKIE_KEY = 'cookies'
COOKIE_SELECTOR = '#cookie-accept'

# The XPath the scripted reply proposes: anchored on the checkbox and its visible label,
# both of which survived the redesign.
HEALED_XPATH = "//label[contains(normalize-space(.),'accept the terms')]//input[@type='checkbox']"

# A reply in the shape a model gives, parsed by mendwright.prompt.parse_proposal().
SCRIPTED_REPLY = json.dumps({
    'xpath': HEALED_XPATH,
    'fallbacks': ["//ds-checkbox//input[@type='checkbox']"],
    'reasoning': 'The id accept-terms is gone. Anchor on the checkbox inside the label '
                 "that reads 'I accept the terms and conditions', which did not change.",
})

BROWSERS = ('chromium', 'firefox', 'webkit')

# How long a real browser polls a selector before it counts as broken. The pages are
# static, so this only has to cover rendering.
BROWSER_TIMEOUT_MS = 250
BROWSER_POLL_MS = 50


def page_html(name):
    """The HTML of a demo page: 'v1' or 'v2'. The files ship inside the package."""
    if name not in ('v1', 'v2'):
        raise ValueError("mendwright.demo: there is no page named " + repr(name)
                         + ". Use 'v1' or 'v2'.")
    return resources.files(__package__).joinpath(name + '.html').read_text(encoding='utf-8')


class ScriptedProvider:
    """A model provider that makes no API call.

    propose() does what every built-in provider does around its API call: it builds the
    prompt from the capture bundle with mendwright.prompt.build_prompt(), and parses the
    reply with parse_proposal(). The reply is canned, one per key. A key with no reply
    gets None and a last_error, like a failed call."""

    def __init__(self, replies):
        self.replies = dict(replies)
        self.calls = []        # the key of every propose() call
        self.prompts = []      # the prompts a real model would have received
        self.proposals = []    # (key, parsed proposal)
        self.outcomes = []     # (key, xpath, success) canary reports
        self.last_error = None

    def propose(self, bundle):
        self.last_error = None
        key = bundle.get('key')
        self.calls.append(key)
        self.prompts.append(build_prompt(bundle))
        reply = self.replies.get(key)
        if reply is None:
            self.last_error = 'the scripted provider has no reply for ' + repr(key) + '.'
            return None
        proposal = parse_proposal(reply)
        self.proposals.append((key, proposal))
        return proposal

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, bool(success)))


@dataclass
class StoryResult:
    """What happened in one run of the story. ok is the red -> green arc plus the refusal."""
    v1_terms: bool = False          # #accept-terms matched v1 and the box was checked
    v1_pay: bool = False            # #pay matched v1
    v2_terms_missed: bool = False   # #accept-terms stopped matching on v2
    healed_xpath: str | None = None  # what mendwright applied for accept_terms
    retried: bool = False           # the retried step checked the box on v2
    confirmed: bool | None = None   # the canary result for the heal
    pay_refused: bool = False       # the pay heal was refused: the key is marked destructive
    pay_model_calls: int = 0        # model calls spent on pay (must stay 0)
    banner_handled: bool = False    # the v2 cookie banner was handled as an interruption
    model_calls: int = 0            # the provider's propose() calls
    scripted: bool = False          # the provider was ScriptedProvider: its calls ask no model
    store_path: str = ''
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return (self.v1_terms and self.v1_pay and self.v2_terms_missed and self.retried
                and self.confirmed is True and self.pay_refused and self.pay_model_calls == 0
                and self.banner_handled and not self.problems)




# --- output ----------------------------------------------------------------------------

def write_line(text, stream=None):
    """Print one line, replacing characters the stream's encoding cannot show."""
    stream = stream or sys.stdout
    try:
        stream.write(text + '\n')
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'ascii'
        stream.write(text.encode(encoding, 'replace').decode(encoding) + '\n')
    stream.flush()


class _LineHandler(logging.Handler):
    """Writes each record of the 'mendwright' logger as one line, its level first: what a
    script that configures logging with format='%(levelname)s %(message)s' shows."""

    def __init__(self, stream):
        super().__init__(logging.INFO)
        self.stream = stream
        self.setFormatter(logging.Formatter('%(levelname)s %(message)s'))

    def emit(self, record):
        try:
            write_line(self.format(record), self.stream)
        except Exception:
            self.handleError(record)


@contextlib.contextmanager
def _log_lines(stream):
    """Show mendwright's own INFO and WARNING lines on ``stream`` while the story runs, and
    only there (not also through a root handler the caller configured). The logger's level
    and propagation are put back afterwards, and the handler removed."""
    log = logging.getLogger('mendwright')
    handler = _LineHandler(stream)
    level, propagate = log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        yield
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
        log.propagate = propagate


# --- the story -------------------------------------------------------------------------

@contextlib.contextmanager
def _healing_on():
    """The variables of your own runs that mendwright.open() reads and takes no argument for,
    set aside while the demo opens its Healer: MENDWRIGHT_DISABLE=1 would turn healing off
    even with provider= passed, and the demo exists to show a heal; MENDWRIGHT_STORE_READ_ONLY
    would keep its heal out of its store, and MENDWRIGHT_DECLINE_TTL_SECONDS, which can't
    change what the demo does, stops open() when it is not a number."""
    saved = {name: os.environ.pop(name) for name in _SET_ASIDE if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


_SET_ASIDE = ('MENDWRIGHT_DISABLE', 'MENDWRIGHT_STORE_READ_ONLY',
              'MENDWRIGHT_DECLINE_TTL_SECONDS')


def _recorded(provider):
    """A list that fills with the key of each propose() call to ``provider``. propose() is
    wrapped on the instance, so the coordinator still sees the provider's own type, model
    and last_error."""
    calls = []
    real = provider.propose

    def propose(bundle):
        calls.append(bundle.get('key'))
        return real(bundle)

    provider.propose = propose
    return calls


def _first_line(error):
    return (str(error).strip().splitlines() or [type(error).__name__])[0]


def story(page, store_path, *, timeout_ms=0, poll_ms=0, provider=None):
    """Run the story on ``page`` (StandInPage or a real sync Playwright page) with the
    override store at ``store_path``, and return a StoryResult. It prints nothing itself:
    mendwright logs each interruption it handles and each heal it applies or refuses to the
    'mendwright' logger, as in any run. timeout_ms and poll_ms are passed to every
    heal.locator() call. ``provider`` is the model provider to ask; None uses
    ScriptedProvider with the canned reply."""
    result = StoryResult(store_path=store_path)
    if provider is None:
        provider = ScriptedProvider({TERMS_KEY: SCRIPTED_REPLY})
    result.scripted = isinstance(provider, ScriptedProvider)
    asked = _recorded(provider)
    # fingerprint=False: the story shows a model's heal. With the fingerprint tier on (the
    # default), the checkbox would heal from its recorded fingerprint before any model call.
    # Every other setting is the demo's own too, whatever your environment says: no rate
    # limit (MENDWRIGHT_RATE_PER_KEY_PER_HOUR=0 would refuse the heal), no get_by routing and
    # no twins.
    with _healing_on():
        try:
            heal = _open_healer(page, provider=provider, store_path=store_path, dry_run=False,
                                timeout_ms=timeout_ms, fingerprint=False,
                                rate_per_key_per_hour=None, rate_global_per_hour=None,
                                heal_get_by=False, suggest_get_by=False)
        except ValueError as exc:     # a setting mendwright.open() refuses
            text = str(exc)
            raise SetupError(text[len('mendwright: '):] if text.startswith('mendwright: ')
                             else text) from None
    # A step that may now come at any point: registered once, handled wherever it shows up.
    heal.interrupt(COOKIE_KEY, COOKIE_SELECTOR)

    # v1: both selectors match.
    page.set_content(page_html('v1'))
    box = heal.locator(TERMS_KEY, TERMS_SELECTOR, poll_ms=poll_ms)
    box.check(expect=box.is_checked)
    result.v1_terms = bool(box.is_checked())
    heal.locator(PAY_KEY, PAY_SELECTOR, destructive=True, poll_ms=poll_ms)
    result.v1_pay = True

    # v2: a redesign removed both ids, and a cookie banner now covers the form.
    page.set_content(page_html('v2'))
    failure = None
    try:
        box = heal.locator(TERMS_KEY, TERMS_SELECTOR, poll_ms=poll_ms)
    except (AttentionRequired, InvalidSelector) as exc:
        failure, box = exc, None
    result.banner_handled = any(e.key == COOKIE_KEY for e in heal.handled_interruptions)
    if not result.banner_handled:
        result.problems.append('the cookie banner on v2 was not handled.')
    if failure is not None:
        result.problems.append(TERMS_KEY + ' was not repaired: ' + _first_line(failure))
    event = heal.heals[-1] if heal.heals else None
    if box is not None and event is not None and event.key == TERMS_KEY:
        result.v2_terms_missed = TERMS_SELECTOR in event.old
        result.healed_xpath = event.new
        box.check(expect=box.is_checked)
        result.retried = bool(box.is_checked())
        result.confirmed = heal.heals[-1].confirmed
        if not (result.retried and result.confirmed):
            result.problems.append('the retried step did not check the box with the repaired '
                                   'selector.')
    elif box is not None:
        result.problems.append(TERMS_SELECTOR + ' still matched on v2, so nothing was '
                               'repaired.')

    # v2: the pay button broke too. It is marked destructive=True, so it is never healed.
    calls_before = len(asked)
    try:
        heal.locator(PAY_KEY, PAY_SELECTOR, destructive=True, poll_ms=poll_ms)
    except AttentionRequired as exc:
        reason = heal.coordinator.explain(PAY_KEY) or str(exc)
        result.pay_refused = 'is marked as a destructive control' in reason
        result.pay_model_calls = len(asked) - calls_before
        if not result.pay_refused:
            result.problems.append(PAY_KEY + ' was not refused as destructive: '
                                   + _first_line(reason))
        elif result.pay_model_calls:
            result.problems.append(PAY_KEY + ' is marked destructive, but the model was asked '
                                   'about it.')
    else:
        result.problems.append(PAY_SELECTOR + ' still matched on v2.')

    result.model_calls = len(asked)
    return result


def _summary(result):
    if result.healed_xpath and result.confirmed:
        repaired = '1 repaired (checked on the live page and confirmed by the retried step)'
    elif result.healed_xpath:
        repaired = '1 repaired (checked on the live page, not confirmed by the retried step)'
    else:
        repaired = '0 repaired'
    # A ScriptedProvider's reply is canned: the header line said no model is called.
    asked = (_count(result.model_calls, 'scripted reply', 'scripted replies') if result.scripted
             else _count(result.model_calls, 'model call'))
    return ('Done: ' + repaired + ', ' + str(1 if result.pay_refused else 0) + ' refused, '
            + _count(1 if result.banner_handled else 0, 'interruption') + ' handled, '
            + asked + '.')


def _count(n, noun, nouns=None):
    return str(n) + ' ' + (noun if n == 1 else nouns or noun + 's')


class SetupError(Exception):
    """The demo could not start (for example, --browser without Playwright)."""


def _launch(pw, name):
    try:
        return getattr(pw, name).launch(headless=True)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or 'playwright install' in text:
            raise SetupError("Playwright's " + name + ' browser is not installed. Install it '
                             'with: python -m playwright install ' + name) from None
        first = (text.strip().splitlines() or [type(exc).__name__])[0]
        raise SetupError('could not start ' + name + ' (' + first + '). Check that it runs, or '
                         'reinstall it with: python -m playwright install ' + name) from None


@contextlib.contextmanager
def browser_page(name):
    """A page in a real headless Playwright browser. Raises SetupError, with the install
    command, when Playwright or the browser is missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SetupError('--browser needs Playwright, which is not installed. Install it with: '
                         'pip install "mendwright[playwright]", then: python -m playwright '
                         'install ' + name) from None
    try:
        manager = sync_playwright().start()
    except Exception as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise SetupError('could not start Playwright (' + first + '). Run it outside an '
                         'asyncio event loop, or reinstall it with: pip install -U playwright'
                         ) from None
    try:
        browser = _launch(manager, name)
        try:
            yield browser.new_page()
        finally:
            browser.close()
    finally:
        manager.stop()


def _live_provider():
    """The provider auto_provider() builds from the environment, or (None, the reason)."""
    from ..providers import auto_provider
    try:
        return auto_provider(), None
    except Exception as exc:
        return None, str(exc).strip() or type(exc).__name__


def _provider_name(provider):
    model = getattr(provider, 'model', None)
    name = type(provider).__name__
    return name + (', model ' + model if isinstance(model, str) and model else '')


NO_PROVIDER_HELP = (
    'mendwright demo: set a provider up and check it with mendwright doctor, or install '
    "mendwright's local model with mendwright model install (it runs in Ollama on your "
    'machine). To run the demo with no model, use mendwright demo --scripted.')


def run(*, browser=None, keep=False, scripted=False, stream=None, err_stream=None):
    """`mendwright demo`. Returns the exit code: 0 when the story ran red -> green, the
    cookie banner was handled and the pay heal was refused, 1 otherwise (with the reason on
    stderr), and 1 when no model provider can be set up from the environment, with what to
    do instead. scripted=True uses ScriptedProvider's canned reply, with no model call."""
    stream = stream or sys.stdout
    err_stream = err_stream or sys.stderr
    if browser is not None and browser not in BROWSERS:
        write_line('mendwright demo: unknown browser ' + repr(browser) + '. Use one of: '
                   + ', '.join(BROWSERS) + '.', err_stream)
        return 2
    if scripted:
        provider = ScriptedProvider({TERMS_KEY: SCRIPTED_REPLY})
    else:
        provider, why = _live_provider()
        if provider is None:
            write_line('mendwright demo: no model provider could be set up from your '
                       'environment. ' + why, err_stream)
            write_line(NO_PROVIDER_HELP, err_stream)
            return 1

    with contextlib.ExitStack() as stack:
        # Start the browser before printing anything, so a missing install is one clear
        # message instead of a story that stops halfway.
        try:
            if browser:
                page = stack.enter_context(browser_page(browser))
            else:
                from ._page import StandInPage
                page = StandInPage()
        except SetupError as exc:
            write_line('mendwright demo: ' + str(exc), err_stream)
            return 1

        write_line('mendwright demo: a checkout page is redesigned and two selectors break'
                   + (', in headless ' + browser if browser else '') + '.', stream)
        if scripted:
            write_line('Provider: ScriptedProvider, a canned reply (--scripted): no model call, '
                       'nothing sent.', stream)
        else:
            write_line('Provider: ' + _provider_name(provider) + ', from your environment '
                       '(mendwright doctor shows it). Only the demo page is sent.', stream)
        if os.environ.get('MENDWRIGHT_DISABLE'):
            write_line('Note: MENDWRIGHT_DISABLE is set. The demo ignores it; your own runs '
                       'do not.', stream)

        folder = tempfile.mkdtemp(prefix='mendwright-demo-')
        if not keep:
            stack.callback(shutil.rmtree, folder, ignore_errors=True)
        store_path = os.path.join(folder, 'overrides.json')
        with _log_lines(stream):
            try:
                result = story(page, store_path,
                               timeout_ms=BROWSER_TIMEOUT_MS if browser else 0,
                               poll_ms=BROWSER_POLL_MS if browser else 0, provider=provider)
            except SetupError as exc:
                write_line('mendwright demo: ' + str(exc), err_stream)
                return 1
        write_line(_summary(result), stream)
        if keep:
            write_line('Kept the store at ' + store_path + ' (journal.jsonl is next to it).',
                       stream)
            write_line('See it with: mendwright report --store "' + store_path + '"', stream)
        if not result.ok:
            for problem in result.problems or ['the story did not reach the expected end.']:
                write_line('mendwright demo: ' + problem, err_stream)
            if scripted:
                write_line('mendwright demo: this is a bug. Please report it with the output '
                           'above at https://github.com/saadbuilt/mendwright/issues', err_stream)
            else:
                write_line('mendwright demo: with a live model the result depends on the '
                           'model. The reason is above; try a stronger model, or run '
                           'mendwright demo --scripted for the canned reply.', err_stream)
            return 1
        return 0
