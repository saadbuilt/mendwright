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

The provider here is ScriptedProvider: it builds the real prompt and parses a canned
reply with the real parser, but makes no API call. The page is StandInPage, or a real
Playwright page with --browser. With --live, the provider is the one auto_provider()
builds from the environment, and the accept_terms heal is a real API call.
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
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
    model_calls: int = 0
    store_path: str = ''
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return (self.v1_terms and self.v1_pay and self.v2_terms_missed and self.retried
                and self.confirmed is True and self.pay_refused and self.pay_model_calls == 0
                and self.banner_handled and not self.problems)


# --- output ----------------------------------------------------------------------------

_COLORS = {'green': '32', 'red': '31', 'yellow': '33', 'bold': '1', 'dim': '2'}


class Style:
    """ANSI colour when on, plain text when off."""

    def __init__(self, on):
        self.on = bool(on)

    def __call__(self, text, color):
        if not self.on:
            return text
        return '\x1b[' + _COLORS[color] + 'm' + text + '\x1b[0m'


def want_color(stream):
    """Colour only for a terminal, and never when NO_COLOR is set (https://no-color.org)."""
    if os.environ.get('NO_COLOR'):
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    return _enable_windows_ansi()


def _enable_windows_ansi():
    """Turn on ANSI escape handling in a Windows console. False if that fails."""
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def write_line(text, stream=None):
    """Print one line, replacing characters the stream's encoding cannot show."""
    stream = stream or sys.stdout
    try:
        stream.write(text + '\n')
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'ascii'
        stream.write(text.encode(encoding, 'replace').decode(encoding) + '\n')
    stream.flush()


# --- the story -------------------------------------------------------------------------

@contextlib.contextmanager
def _healing_on():
    """MENDWRIGHT_DISABLE=1 would turn healing off even with provider= passed. The demo
    exists to show a heal, so it sets the variable aside while it opens the Healer."""
    saved = os.environ.pop('MENDWRIGHT_DISABLE', None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ['MENDWRIGHT_DISABLE'] = saved


def _silent(_message):
    pass


def _recorded(provider):
    """(calls, proposals) lists that fill as ``provider`` is asked, for a provider that
    does not keep them itself. propose() is wrapped on the instance, so the coordinator
    still sees the provider's own type, model and last_error."""
    calls, proposals = [], []
    real = provider.propose

    def propose(bundle):
        key = bundle.get('key')
        calls.append(key)
        proposal = real(bundle)
        if proposal:
            proposals.append((key, proposal))
        return proposal

    provider.propose = propose
    return calls, proposals


def story(page, store_path, *, say, style=None, timeout_ms=0, poll_ms=0, provider=None):
    """Run the story on ``page`` (StandInPage or a real sync Playwright page) with the
    override store at ``store_path``. ``say`` receives each line. Returns a StoryResult.
    timeout_ms and poll_ms are passed to every heal.locator() call. ``provider`` is the
    model provider to ask (mendwright demo --live); None uses the scripted reply."""
    style = style or Style(False)
    result = StoryResult(store_path=store_path)
    if provider is None:
        provider = ScriptedProvider({TERMS_KEY: SCRIPTED_REPLY})
        asked, proposals = provider.calls, provider.proposals
        source = '(scripted reply, no API call)'
    else:
        asked, proposals = _recorded(provider)
        source = '(' + type(provider).__name__ + ', a real API call)'
    # fingerprint=False: the story shows a model's heal. With the fingerprint tier on (the
    # default), the checkbox would heal from its recorded fingerprint before any model call.
    with _healing_on():
        heal = _open_healer(page, provider=provider, store_path=store_path, dry_run=False,
                            timeout_ms=timeout_ms, fingerprint=False)
    # The story prints its own lines. mendwright's log line for each heal applied or
    # refused, or interruption handled, would repeat them on stderr.
    heal.coordinator.log = _silent
    # A step that may now come at any point: registered once, handled wherever it shows up.
    heal.interrupt(COOKIE_KEY, COOKIE_SELECTOR)
    width = max(len(TERMS_KEY), len(PAY_KEY), len(COOKIE_KEY))
    sel_width = max(len(TERMS_SELECTOR), len(PAY_SELECTOR), len(COOKIE_SELECTOR))

    def row(version, key, selector, status, color, note):
        return ('  ' + version + '  ' + key.ljust(width) + '  ' + selector.ljust(sel_width)
                + '  ' + style(status.ljust(5), color) + '  ' + note)

    def detail(label, text):
        return '      ' + label.ljust(8) + ' ' + text

    # v1: both selectors match.
    page.set_content(page_html('v1'))
    box = heal.locator(TERMS_KEY, TERMS_SELECTOR, poll_ms=poll_ms)
    box.check(expect=box.is_checked)
    result.v1_terms = bool(box.is_checked())
    say(row('v1', TERMS_KEY, TERMS_SELECTOR, 'ok' if result.v1_terms else 'FAIL',
            'green' if result.v1_terms else 'red', 'matched, box checked'))
    heal.locator(PAY_KEY, PAY_SELECTOR, destructive=True, poll_ms=poll_ms)
    result.v1_pay = True
    say(row('v1', PAY_KEY, PAY_SELECTOR, 'ok', 'green', 'matched (not clicked)'))

    # v2: a redesign removed both ids, and a cookie banner now covers the form.
    page.set_content(page_html('v2'))
    say(style('      -- v2: a redesign ships, both ids are gone, and a cookie banner opens first --',
              'dim'))
    failure = None
    try:
        box = heal.locator(TERMS_KEY, TERMS_SELECTOR, poll_ms=poll_ms)
    except (AttentionRequired, InvalidSelector) as exc:
        failure, box = exc, None
    banner = [e for e in heal.handled_interruptions if e.key == COOKIE_KEY]
    result.banner_handled = bool(banner)
    if banner:
        say(row('v2', COOKIE_KEY, COOKIE_SELECTOR, 'ok', 'green',
                'banner covered the form, clicked it (heal.interrupt)'))
    else:
        result.problems.append('the cookie banner on v2 was not handled.')
    if failure is not None:
        say(row('v2', TERMS_KEY, TERMS_SELECTOR, 'MISS', 'red', 'not healed'))
        result.problems.append(TERMS_KEY + ' was not healed (the reason is above).')
        for line in str(failure).splitlines():
            say(detail('', line.strip()))
    event = heal.heals[-1] if heal.heals else None
    if box is not None and event is not None and event.key == TERMS_KEY:
        result.v2_terms_missed = TERMS_SELECTOR in event.old
        result.healed_xpath = event.new
        proposal = next((p for k, p in reversed(proposals) if k == TERMS_KEY), None)
        say(row('v2', TERMS_KEY, TERMS_SELECTOR, 'MISS', 'red', 'no element matches'))
        say(detail('model', 'proposed ' + str((proposal or {}).get('xpath'))
                   + ' ' + style(source, 'yellow')))
        role = heal.store.meta(TERMS_KEY).get('expectedRole') or 'element'
        live = '1 match on the live page' + ('' if role == 'checkbox' else ', visible')
        say(detail('checks', style('pass', 'green') + ': 1 ' + role + ' in the captured HTML, '
                   + live))
        box.check(expect=box.is_checked)
        result.retried = bool(box.is_checked())
        event = heal.heals[-1]
        result.confirmed = event.confirmed
        if result.retried and event.confirmed:
            say(detail('retry', style('ok', 'green')
                       + ': box checked with the healed selector, heal confirmed'))
        else:
            say(detail('retry', style('FAIL', 'red') + ': the retried step did not check the box'))
    elif box is not None:
        result.problems.append('#accept-terms still matched on v2, so nothing was healed.')
        say(row('v2', TERMS_KEY, TERMS_SELECTOR, 'ok', 'yellow', 'still matched (no heal needed)'))

    # v2: the pay button broke too. It is marked destructive=True, so it is never healed.
    calls_before = len(asked)
    try:
        heal.locator(PAY_KEY, PAY_SELECTOR, destructive=True, poll_ms=poll_ms)
    except AttentionRequired as exc:
        reason = heal.coordinator.explain(PAY_KEY) or str(exc)
        result.pay_refused = 'is marked as a destructive control' in reason
        result.pay_model_calls = len(asked) - calls_before
        say(row('v2', PAY_KEY, PAY_SELECTOR, 'MISS', 'red', 'no element matches'))
        if result.pay_refused:
            calls = ('no model call' if result.pay_model_calls == 0
                     else str(result.pay_model_calls) + ' model call(s)')
            say(detail(style('refused', 'yellow'), "'" + PAY_KEY + "' is marked destructive=True. "
                       'Not healed, ' + calls + '.'))
        else:
            say(detail('', reason))
    else:
        result.problems.append('#pay still matched on v2.')
        say(row('v2', PAY_KEY, PAY_SELECTOR, 'ok', 'yellow', 'still matched'))

    result.model_calls = len(asked)
    return result


def _summary(result, style):
    applied = 1 if result.healed_xpath else 0
    confirmed = ' and confirmed' if result.confirmed else ''
    refused = 1 if result.pay_refused else 0
    banner = 1 if result.banner_handled else 0
    return (style('Done:', 'bold') + ' ' + _count(applied, 'heal') + ' applied' + confirmed
            + ', ' + str(refused) + ' refused, ' + _count(banner, 'interruption') + ' handled, '
            + _count(result.model_calls, 'model call') + ' in total.')


def _count(n, noun):
    return str(n) + ' ' + noun + ('' if n == 1 else 's')


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


def run(*, browser=None, keep=False, slow=False, color=None, stream=None,
        err_stream=None, sleep=time.sleep, live=False):
    """`mendwright demo`. Returns the exit code: 0 when the story ran red -> green, the
    cookie banner was handled and the pay heal was refused, 1 otherwise (with the reason
    on stderr). live=True asks the
    provider from the environment (auto_provider()) instead of the scripted reply."""
    stream = stream or sys.stdout
    err_stream = err_stream or sys.stderr
    style = Style(want_color(stream) if color is None else color)
    if browser is not None and browser not in BROWSERS:
        write_line('mendwright demo: unknown browser ' + repr(browser) + '. Use one of: '
                   + ', '.join(BROWSERS) + '.', err_stream)
        return 2
    provider = None
    if live:
        provider, why = _live_provider()
        if provider is None:
            write_line('mendwright demo --live: no model provider is configured, so there is '
                       'nothing to call. ' + why, err_stream)
            write_line('mendwright demo --live: set a provider (mendwright doctor shows what it '
                       'finds), or run mendwright demo without --live for the scripted reply.',
                       err_stream)
            return 1
    first = [True]

    def say(text):
        if slow and not first[0]:
            sleep(0.4)
        first[0] = False
        write_line(text, stream)

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

        if browser:
            where = 'Real headless ' + browser + ' (pages loaded with page.set_content)'
        elif live:
            where = 'A stand-in page (lxml, no browser)'
        else:
            where = 'Offline: a stand-in page (lxml, no browser)'
        say(style('mendwright demo: a redesign breaks two selectors on a checkout page and adds '
                  'a cookie banner.', 'bold'))
        if live:
            say(where + ' and a live model: ' + _provider_name(provider)
                + ' (real API calls).')
        else:
            say(where + ' and a scripted model reply (no API call).')
        if os.environ.get('MENDWRIGHT_DISABLE'):
            say('Note: MENDWRIGHT_DISABLE is set. The demo ignores it; your own runs do not.')
        say('')

        folder = tempfile.mkdtemp(prefix='mendwright-demo-')
        if not keep:
            stack.callback(shutil.rmtree, folder, ignore_errors=True)
        store_path = os.path.join(folder, 'overrides.json')
        result = story(page, store_path, say=say, style=style,
                       timeout_ms=BROWSER_TIMEOUT_MS if browser else 0,
                       poll_ms=BROWSER_POLL_MS if browser else 0, provider=provider)
        say('')
        say(_summary(result, style))
        if keep:
            say('Kept the store at ' + store_path + ' (journal.jsonl is next to it).')
            say('See it with: mendwright report --store "' + store_path + '"')
        if not result.ok:
            for problem in result.problems or ['the story did not reach the expected end.']:
                write_line('mendwright demo: ' + problem, err_stream)
            if live:
                write_line('mendwright demo --live: with a live model the result depends on '
                           'the model. The reason is above; try a stronger model, or run '
                           'mendwright demo without --live for the scripted reply.', err_stream)
            else:
                write_line('mendwright demo: this is a bug. Please report it with the output '
                           'above at https://github.com/saadbuilt/mendwright/issues', err_stream)
            return 1
        return 0
