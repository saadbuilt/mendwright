"""Steps in any order: a flow (a checkout, a signup, a booking) whose steps the site may
show in another order, put on one page, or leave out, run without a change to the script.

    heal = mendwright.open(page)

    @heal.step('shipping', when='#ship-name')
    def shipping(page): page.fill('#ship-name', 'Ada'); page.click('#next')

    @heal.step('payment', when='#card-number')
    def payment(page): page.fill('#card-number', '4242 4242 4242 4242'); page.click('#next')

    @heal.step('review', when='text=Place order', last=True)
    def review(page): page.click('text=Place order')

    heal.run_steps()        # ['shipping', 'payment', 'review'], or in the site's order

heal.step(name, when, fn) registers a step: what shows it is on the page, and the function
that does it. when= is a landmark, one selector or several, and any visible match shows
the step; url= is a pattern for the page's path (see url_pattern), instead of a landmark or
as one more condition. run_steps() then waits for a step that has not run yet to show,
runs it, and looks again, until the step marked last=True ran, or, with no last step,
until every step that is not optional ran. Each step runs at most once. Steps that show at
the same time (a one-page checkout) run in the order they were registered, and the last
step only once no other step that has not run shows. The function gets heal.page (on
Playwright) or heal.driver (on Selenium), so its selectors heal as usual. Steps stay on the
healer for every later run_steps() call, so the same flow can run again; another flow needs
a new healer.

Mark the step that ends the flow last=True, above all one that ends in a submit: a step the
site dropped is then named in one WARNING once the last step ran. With no last step,
run_steps() waits for every step that is not optional, so a dropped one stops it with
AttentionRequired, which says so.

What mendwright does not guess:
- A landmark is looked up with the driver's count() and is_visible() only. It is never
  healed and nothing is saved for it: a landmark that is missing is how a step that is not
  on the page looks. So a landmark must be stable, such as a heading's text or a field's
  name.
- A step that ran and is on the page again, with no new step for the whole timeout (the
  card was declined, a field was rejected, or the site added a step that shows part of
  it, such as its heading), stops the flow with AttentionRequired: it is never run twice.
  The message tells the two apart by the page, the one the step ran on or another. A page
  where no step shows stops the flow too, and the message names the page and every
  landmark run_steps() looked for. A function that raises stops the flow with its own
  error, after the journal records which step failed.
- run_steps() never runs more steps than 3 times the number registered when it started.
- On mendwright.aio.open(), a plain function that returns no awaitable stops the flow with
  TypeError, and the step counts as failed: heal.page's calls there are coroutines, and
  one that is not awaited does nothing on the page.

While run_steps() waits, the registered interruptions (heal.interrupt(), cookies=) the
page shows are handled, as when a locator() misses: up to 3 in each wait, as for one
action. One whose action fails is named in one WARNING and not tried again in that wait;
the others still are. Popups nobody registered (open(dismiss=True)) are dismissed by the
steps' own actions, as everywhere else: a popup over a landmark does not hide it.

While a step runs, a key's page is the step: a key found during that step in an earlier
run heals on the page the step showed on when it started, whatever its address, where the
page guard would decline a page it was never found on (Healer._step_pages). Once the
step's function goes to another page, such as with its own submit, the guard is as before:
that page may be the next step's. The store saves the steps each key was found during
('steps', next to 'pages'), for a key found on the page its step showed on.

Each step run logs one INFO line and writes a flow.step journal event. A step that runs
after steps registered behind it logs one WARNING ("ran 'payment' before 'shipping': the
site changed the order of the steps"), and so does each step that is not optional and never
showed by the time the last step ran. None of this is a heal, so pytest
--mendwright-strict does not fail a test for it.
"""

import fnmatch
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Callable

from .exceptions import AttentionRequired
from .flow import _is_id, _listed, journal_event, page_path
from .interruptions import Budget, nth_targets
from .journal import FLOW_STEP
from .selectors import XPATH, InvalidSelector, check_syntax, classify, normalize

# What an interruption handled while run_steps() waits is logged as handled before: no step
# shows yet, so there is no step to name. The angle brackets keep it from reading as a key.
WAITING_STEP = '<next step>'

# run_steps() runs at most this many times as many steps as were registered when it started
# (each step runs once; this stops a step function that keeps registering new ones).
MAX_RUNS_PER_STEP = 3

# How many matches of a landmark that matches several elements are looked at for one that
# is visible.
LANDMARK_MATCHES = 5


@dataclass(frozen=True)
class Step:
    """One step registered with heal.step(): its name, the function that does it, its
    landmarks (selectors, any visible match shows the step), its url pattern (normalized,
    see url_pattern) as the user wrote it in url_text, whether it ends the flow (last) and
    whether it may not show at all (optional)."""
    name: str
    fn: Callable
    when: tuple = ()
    url: str | None = None
    url_text: str | None = None
    last: bool = False
    optional: bool = False


# --- the arguments of heal.step() and run_steps() --------------------------------------------

def _flag(name, value, flag):
    if not isinstance(value, bool):
        raise TypeError('mendwright: heal.step(' + repr(name) + ', ...) takes ' + flag + '=True '
                        'or ' + flag + '=False, got ' + flag + '=' + repr(value) + '.')


def url_pattern(name, url):
    """The path pattern of url= for step ``name``, normalized as mendwright.flow.page_path()
    normalizes a page's path: lower case, without empty parts, with parts that look like
    record ids as '*'. '*' matches any characters, '/' included, and '?' one. A pattern
    that starts with '/' starts at the root of the path; one that starts with '*' is kept
    as written, so '*/payment' matches /payment and /checkout/payment. Raises TypeError for
    a non-string and ValueError for a pattern that is not a path (it must start with '/'
    or '*') or names the site root, which has no page."""
    if not isinstance(url, str):
        raise TypeError('mendwright: heal.step(' + repr(name) + ', url=...) takes the path of '
                        "the step's page as a string, such as '/checkout/payment', got "
                        + repr(url) + '.')
    text = url.strip()
    if not text.startswith(('/', '*')):
        raise ValueError('mendwright: heal.step(' + repr(name) + ', url=' + repr(url) + ') takes '
                         "the path of the step's page, starting with / or *, such as "
                         "'/checkout/payment' or '*/payment', not a whole address.")
    parts = ['*' if _is_id(part) else part for part in text.lower().split('/') if part]
    if not parts:
        raise ValueError('mendwright: heal.step(' + repr(name) + ', url=' + repr(url) + ') names '
                         'the site root, which has no page path to compare. Use when= for a '
                         'step on the site root.')
    return ('' if text.startswith('*') else '/') + '/'.join(parts)


def url_matches(pattern, url):
    """Whether the page at url is on the step's url pattern. Never for a page with no path
    (about:blank, a data: URL, the site root)."""
    path = page_path(url)
    return path is not None and fnmatch.fnmatchcase(path, pattern)


def _landmarks(name, when, check):
    """The landmark selectors of when=, checked and normalized. check, when given, is run on
    each one too (the Selenium healer rejects Playwright engine selectors there)."""
    if when is None:
        return ()
    items = list(when) if isinstance(when, (list, tuple)) else [when]
    if not items:
        raise ValueError('mendwright: heal.step(' + repr(name) + ', []) got an empty list. '
                         "Pass the selector of something only that step shows, for example "
                         "'#card-number'.")
    out = []
    for item in items:
        try:
            check_syntax(item)
            if check is not None:
                check(item)
        except InvalidSelector as exc:
            raise exc.for_key(name) from None
        out.append(normalize(item))
    return tuple(dict.fromkeys(out))


def step_conditions(name, when=None, *, url=None, last=False, optional=False, check=None):
    """Check heal.step()'s arguments other than the function, before anything touches the
    page: returns (landmarks, url pattern). Raises TypeError, ValueError or InvalidSelector,
    each naming the fix."""
    if not isinstance(name, str) or not name.strip():
        raise TypeError('mendwright: heal.step() needs a name, a non-empty string such as '
                        "'payment', got " + repr(name) + '.')
    _flag(name, last, 'last')
    _flag(name, optional, 'optional')
    if last and optional:
        raise ValueError('mendwright: heal.step(' + repr(name) + ', ..., last=True, '
                         'optional=True): the last step ends the flow, so it cannot be '
                         'optional. Drop optional=True.')
    landmarks = _landmarks(name, when, check)
    pattern = url_pattern(name, url) if url is not None else None
    if not landmarks and pattern is None:
        raise TypeError('mendwright: heal.step(' + repr(name) + ') needs when=, a selector of '
                        'something only that step shows (a heading, a field), or url=, the '
                        "path of its page: heal.step(" + repr(name) + ", '#card-number') or "
                        'heal.step(' + repr(name) + ", url='/checkout/payment').")
    return landmarks, pattern


def make_step(name, fn, landmarks, pattern, *, url=None, last=False, optional=False,
              awaits=False):
    """The Step for heal.step(name, ..., fn), from step_conditions()' result. awaits=False
    (the sync healer) rejects an async function, which it could not await."""
    if not callable(fn):
        raise TypeError('mendwright: heal.step(' + repr(name) + ', ...) takes the function that '
                        'does the step, got ' + repr(fn) + '. Use it as a decorator, '
                        '@heal.step(' + repr(name) + ", '#card-number'), or pass the function: "
                        'heal.step(' + repr(name) + ", '#card-number', " + 'fn).')
    if not awaits and (inspect.iscoroutinefunction(fn)
                       or inspect.iscoroutinefunction(type(fn).__call__)):
        raise TypeError('mendwright: heal.step(' + repr(name) + ', ...) got an async function, '
                        'which the healer of mendwright.open() cannot await. Make it a plain '
                        'function, or use mendwright.aio.open() with async Playwright.')
    return Step(name=name, fn=fn, when=landmarks, url=pattern,
                url_text=url.strip() if isinstance(url, str) else None, last=last,
                optional=optional)


def register(book, step):
    """Put step in book ({name: Step}, in the order registered). A name registered again is
    replaced and keeps its place. Raises ValueError for a second last step."""
    if step.last:
        for other in book.values():
            if other.last and other.name != step.name:
                raise ValueError('mendwright: heal.step(' + repr(step.name) + ', ..., last=True): '
                                 + repr(other.name) + ' is the last step already, and a flow '
                                 'has at most one. Steps stay on the healer for every later '
                                 'run_steps(), so for another flow, open a new healer. To '
                                 'change the last step of this flow, register '
                                 + repr(other.name) + ' again without last=True first, or '
                                 'drop last=True here.')
    book[step.name] = step


def add(core, name, when, fn, *, url=None, last=False, optional=False, awaits=False):
    """Healer.step() and AsyncHealer.step(): check the arguments now, and register the step
    in core._step_book (core is the sync Healer, the AsyncHealer's own core) once its
    function is known. Returns the function, or, with fn None, the decorator that takes
    it. On Selenium a Playwright engine selector in when= raises InvalidSelector here."""
    from .drivers.selenium import SeleniumDriver, selenium_locator
    check = selenium_locator if isinstance(core._driver, SeleniumDriver) else None
    landmarks, pattern = step_conditions(name, when, url=url, last=last, optional=optional,
                                         check=check)

    def decorate(function):
        register(core._step_book, make_step(name, function, landmarks, pattern, url=url,
                                            last=last, optional=optional, awaits=awaits))
        return function

    return decorate if fn is None else decorate(fn)


def run_args(book, timeout_ms, poll_ms, default_ms):
    """Check run_steps()'s arguments and the steps registered: returns (timeout_ms,
    poll_ms). Raises TypeError or ValueError before anything touches the page."""
    for label, value in (('timeout_ms', timeout_ms), ('poll_ms', poll_ms)):
        if value is None and label == 'timeout_ms':
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError('mendwright: run_steps() takes ' + label + '= as a number of '
                            'milliseconds, got ' + label + '=' + repr(value) + '.')
        if value < 0:
            raise ValueError('mendwright: run_steps() takes ' + label + '= of 0 or more, got '
                             + label + '=' + repr(value) + '.')
    if not book:
        raise ValueError('mendwright: run_steps() has no steps to run. Register them first '
                         "with heal.step(), for example @heal.step('payment', '#card-number').")
    steps = list(book.values())
    if not any(s.last for s in steps) and all(s.optional for s in steps):
        raise ValueError('mendwright: every step is optional and none has last=True, so '
                         'run_steps() would have nothing to wait for. Mark the step that ends '
                         'the flow with last=True.')
    return (default_ms if timeout_ms is None else timeout_ms), poll_ms


# --- looking at the page ---------------------------------------------------------------------

def _playwright(driver):
    from .drivers.playwright_async import AsyncPlaywrightDriver
    from .drivers.playwright_sync import PlaywrightSyncDriver
    return isinstance(driver, (PlaywrightSyncDriver, AsyncPlaywrightDriver))


def landmark_targets(driver, selector, count):
    """The selectors to ask is_visible() of for a landmark that matches count elements: the
    landmark itself for one match; for several, one per match of the first
    LANDMARK_MATCHES, as '(xpath)[n]' for an XPath and 'selector >> nth=n' on Playwright.
    A CSS selector that matches several on another driver is asked as it is, and Selenium
    answers for its first match."""
    if count == 1:
        return [selector]
    if count < 1:
        return []
    if classify(selector) == XPATH:
        return nth_targets(selector, count, LANDMARK_MATCHES)
    if _playwright(driver):
        from .drivers.playwright_sync import playwright_selector
        base = playwright_selector(selector)
        return [base + ' >> nth=' + str(i) for i in range(min(count, LANDMARK_MATCHES))]
    return [selector]


def shows(driver, step, url):
    """Whether step shows on the page now: its url pattern matches the page's path (when it
    has one), and one of its landmarks has a visible match (when it has any). Any error but
    InvalidSelector reads as not there; InvalidSelector is raised, naming the step."""
    if step.url is not None and not url_matches(step.url, url):
        return False
    if not step.when:
        return True
    for selector in step.when:
        try:
            count = driver.count(selector)
            for target in landmark_targets(driver, selector, count):
                if driver.is_visible(target):
                    return True
        except InvalidSelector as exc:
            raise exc.for_key(step.name) from None
        except Exception:
            continue
    return False


async def _awaited(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def ashows(driver, step, url):
    """shows(), with the driver's calls awaited (an async driver)."""
    if step.url is not None and not url_matches(step.url, url):
        return False
    if not step.when:
        return True
    for selector in step.when:
        try:
            count = await _awaited(driver.count(selector))
            for target in landmark_targets(driver, selector, count):
                if await _awaited(driver.is_visible(target)):
                    return True
        except InvalidSelector as exc:
            raise exc.for_key(step.name) from None
        except Exception:
            continue
    return False


# --- messages ------------------------------------------------------------------------------

def _seconds(seconds):
    return format(seconds, '.1f') + 's'


def _on(page):
    return page if page else 'this page'


def ran_message(name, page, seconds):
    """The INFO line for a step run_steps() ran."""
    return ('ran step ' + repr(name) + (' on ' + page if page else '') + ' ('
            + _seconds(seconds) + ')')


def order_message(earlier, name):
    """The WARNING for step name, which runs after the steps in earlier although it was
    registered before them."""
    return ('ran ' + _listed(repr(n) for n in earlier) + ' before ' + repr(name) + ': the site '
            'changed the order of the steps')


def dropped_message(name, last):
    """The WARNING for a step that is not optional and never showed before the last step
    ran."""
    return (repr(name) + ' never showed before the flow ended with ' + repr(last) + ': the site '
            'may have dropped it. If it did, delete its heal.step(). If it shows only '
            'sometimes, pass optional=True to heal.step(' + repr(name) + ', ...).')


def not_shown_message(name):
    """The INFO line for an optional step that never showed."""
    return 'step ' + repr(name) + ' did not show, and it is optional (optional=True)'


def _landmark_text(step):
    parts = list(step.when)
    if step.url_text:
        parts.append('url ' + step.url_text)
    return repr(step.name) + ' (' + ', '.join(parts) + ')'


def _ran_text(ran):
    return ('Steps that ran: ' + ', '.join(repr(n) for n in ran) + '.') if ran else (
        'No step ran yet.')


def again_message(name, page, timeout_ms, ran):
    """The AttentionRequired text for a step that ran and is on the page again, with no
    new step for the whole timeout."""
    return ('mendwright: ' + repr(name) + ' is on the page again after it ran, and no step '
            'that has not run showed on ' + _on(page) + ' within ' + format(timeout_ms, 'g')
            + ' ms: its submit probably failed, for example because the card was declined or '
            'the site rejected a field. mendwright runs each step once, so it stopped instead '
            'of running ' + repr(name) + ' again. Look at the page for the site\'s message. '
            + _ran_text(ran))


def changed_message(name, ran_on, page, timeout_ms, ran):
    """The AttentionRequired text for a step that ran on the page ran_on and shows on
    another page, with no new step for the whole timeout: the site probably added a step
    that shows part of it, such as its heading."""
    return ('mendwright: ' + repr(name) + ' shows on ' + page + ', and it ran on ' + ran_on
            + ', so the page changed after it ran, and no step that has not run showed within '
            + format(timeout_ms, 'g') + ' ms. This is probably a step the site added that '
            'shows part of ' + repr(name) + ', such as its heading. If it is, add it with '
            'heal.step(): @heal.step("<name>", "<selector of something only that step '
            'shows>"), or give ' + repr(name) + " a landmark only its own page shows. If its "
            "submit failed instead, look at the page for the site's message. mendwright runs "
            'each step once, so it stopped instead of running ' + repr(name) + ' again. '
            + _ran_text(ran))


def unknown_message(page, waiting, ran, timeout_ms, *, no_last=False):
    """The AttentionRequired text for a page where no step showed within the timeout.
    waiting are the steps run_steps() looked for, ran the names of those that ran.
    no_last: steps ran and none has last=True, so a step the site dropped is waited for."""
    return ('mendwright: no step showed on ' + _on(page) + ' within ' + format(timeout_ms, 'g')
            + ' ms. mendwright looked for ' + _listed(_landmark_text(s) for s in waiting) + '. '
            + _ran_text(ran) + ' If the site added a step here, add it with heal.step(): '
            '@heal.step("<name>", "<selector of something only that step shows>"). If a '
            "step's landmark changed, update its when=: landmarks are never healed."
            + (' No step has last=True, so run_steps() waits until every step that is not '
               'optional ran, and the site may have dropped one: mark the step that ends the '
               'flow with last=True, and the flow ends there, with a WARNING for a step that '
               'never showed.' if no_last else ''))


def plain_message(name, result):
    """The TypeError text for a plain function on the async healer that returned no
    awaitable."""
    return ('mendwright: the function of step ' + repr(name) + ' returned ' + repr(result)
            + ", not an awaitable. On mendwright.aio.open()'s healer, heal.page's calls are "
            'coroutines, and a plain function that makes them without await does nothing on '
            'the page: make it async def and await them, or return the awaitable, as in '
            "lambda page: page.click('#place').")


def cap_message(count, registered):
    """The AttentionRequired text when run_steps() reached its limit of step runs."""
    return ('mendwright: run_steps() ran ' + str(count) + ' steps, ' + str(MAX_RUNS_PER_STEP)
            + ' times the ' + str(registered) + ' registered when it started, and stopped '
            'instead of running more. A step function that registers new steps with '
            'heal.step() can keep a flow from ending: register every step before run_steps().')


def scope_message(key, step, page, last):
    """The INFO line for a key the page guard lets heal on a page it was never found on,
    because it was found during the step that runs now, and this is the page that step
    showed on."""
    return ('the page guard lets ' + repr(key) + ' heal on ' + page + ': it was found during '
            'step ' + repr(step) + ' before' + (' (last on ' + last + ')' if last else '')
            + ', and this is the page ' + repr(step) + ' showed on')


# --- one run of run_steps() ----------------------------------------------------------------

class Run:
    """What one run_steps() call knows and decides, shared by the sync and async loops:
    which steps ran, which to run next, when the flow is done, and what it reports. core
    is the sync Healer (the AsyncHealer's own core)."""

    def __init__(self, core, timeout_ms):
        self.core = core
        self.timeout_ms = timeout_ms
        self.ran = []          # names, in the order they ran
        self.ran_on = {}       # name -> the page path each step that ran showed on
        self.ended = None      # the last step, once it ran
        self.cap = MAX_RUNS_PER_STEP * len(core._step_book)
        self.registered = len(core._step_book)

    def steps(self):
        return list(self.core._step_book.values())

    def done(self):
        """True once the last step ran, or, with no last step, every step that is not
        optional ran."""
        if self.ended is not None:
            return True
        steps = self.steps()
        if any(s.last for s in steps):
            return False
        return all(s.name in self.ran for s in steps if not s.optional)

    def check_cap(self):
        __tracebackhide__ = True
        if len(self.ran) >= self.cap:
            raise AttentionRequired(cap_message(len(self.ran), self.registered))

    def waiting(self):
        """The steps that have not run, in the order registered."""
        return [s for s in self.steps() if s.name not in self.ran]

    def ran_steps(self):
        """The steps that ran and are still registered, most recent first."""
        book = self.core._step_book
        return [book[n] for n in reversed(self.ran) if n in book]

    @staticmethod
    def pick(shown):
        """The step to run of those that show and have not run: the first one registered,
        the last step only when no other shows."""
        for step in shown:
            if not step.last:
                return step
        return shown[0]

    def stuck(self, again, page):
        """The AttentionRequired for a wait that ran out. again are the steps that ran and
        show now, most recent first. One that shows on another page than the one it ran on
        is probably part of a step the site added, not a submit that failed."""
        if again:
            name = again[0].name
            ran_on = self.ran_on.get(name)
            if ran_on and page and ran_on != page:
                return AttentionRequired(changed_message(name, ran_on, page, self.timeout_ms,
                                                         self.ran))
            return AttentionRequired(again_message(name, page, self.timeout_ms, self.ran))
        no_last = bool(self.ran) and not any(s.last for s in self.steps())
        return AttentionRequired(unknown_message(page, self.waiting(), self.ran,
                                                 self.timeout_ms, no_last=no_last))

    def _emit(self, level, message):
        self.core.coordinator._emit(level, message)

    def _position(self, name):
        names = list(self.core._step_book)
        return names.index(name) + 1 if name in names else None

    def starting(self, step):
        """Before step runs: the WARNING when steps registered after it ran already. Not
        for the last step, which runs once no other step shows wherever it was
        registered."""
        names = list(self.core._step_book)
        if step.last or step.name not in names:
            return
        mine = names.index(step.name)
        earlier = [n for n in self.ran if n in names and names.index(n) > mine]
        if earlier:
            self._emit(logging.WARNING, order_message(earlier, step.name))

    def record(self, step, page, seconds, error=None):
        """The INFO line and the flow.step journal event for one step that ran, or failed
        with error (only its type is journaled: an error text can quote the page)."""
        failed = error is not None
        self.ran.append(step.name)
        self.ran_on[step.name] = page
        if step.last and not failed:
            self.ended = step.name
        journal_event(self.core.coordinator.journal, FLOW_STEP, step.name,
                      position=len(self.ran), registered=self._position(step.name), page=page,
                      seconds=round(seconds, 3), outcome='failed' if failed else 'ran',
                      error=type(error).__name__ if failed else None)
        if not failed:
            self._emit(logging.INFO, ran_message(step.name, page, seconds))

    def finish(self):
        """Once the flow is done: one WARNING for each step that is not optional and never
        showed (only a last step can end the flow before they ran), one INFO line for each
        optional step that did not show. Returns the names that ran, in order."""
        for step in self.waiting():
            if step.optional:
                self._emit(logging.INFO, not_shown_message(step.name))
            else:
                self._emit(logging.WARNING, dropped_message(step.name, self.ended))
                journal_event(self.core.coordinator.journal, FLOW_STEP, step.name,
                              registered=self._position(step.name), outcome='missing')
        return list(self.ran)


def _sleep_ms(poll_ms):
    return max(poll_ms, 20) / 1000.0


class _Wait:
    """One wait for the next step: its deadline, the interruption budget it shares (up to
    3 handled, as for one action), and the keys of the interruptions whose action failed in
    it, which are not tried again in this wait (see interrupt())."""

    def __init__(self, now, timeout_ms):
        self.deadline = now + timeout_ms / 1000.0
        self.budget = Budget()
        self.failed = set()


def interrupt(core, budget, timeout_ms, poll_ms, failed, driver=None):
    """Handle the registered interruptions the page shows while run_steps() waits, one key
    at a time (Healer._interrupt with only=), leaving out the keys in failed. A key whose
    action fails (Healer._run_interruption logged a WARNING and handled nothing, though the
    budget went down) is added to failed, so it is not tried again in this wait, and the
    others still are. True when one was handled. core is the sync Healer; driver is the
    async healer's SyncBridge, from a worker thread."""
    __tracebackhide__ = True
    handled = False
    for item in core._others(WAITING_STEP):
        if item.key in failed:
            continue
        used = budget.used
        if core._interrupt(WAITING_STEP, budget, timeout_ms, poll_ms, driver, only=item.key):
            handled = True
        elif budget.used > used:
            failed.add(item.key)
    return handled


def run(healer, timeout_ms, poll_ms):
    """Healer.run_steps(): see the module docs. healer is a sync Healer."""
    __tracebackhide__ = True
    timeout_ms, poll_ms = run_args(healer._step_book, timeout_ms, poll_ms, healer.timeout_ms)
    flow = Run(healer, timeout_ms)
    page = healer._step_page()
    while not flow.done():
        flow.check_cap()
        step, path = _next_step(healer, flow, timeout_ms, poll_ms)
        flow.starting(step)
        started = time.monotonic()
        with healer._in_step(step.name, path):
            try:
                step.fn(page)
            except BaseException as exc:
                flow.record(step, path, time.monotonic() - started, exc)
                raise
        flow.record(step, path, time.monotonic() - started)
    return flow.finish()


def _next_step(healer, flow, timeout_ms, poll_ms):
    """Wait, up to timeout_ms, for a step that has not run to show, handling the
    registered interruptions the page shows meanwhile. Returns (step, page path): the
    page's path is read again after its landmarks, and they are looked at again when it
    changed meanwhile, so the step is not given the page it came from."""
    __tracebackhide__ = True
    driver, wait = healer._driver, _Wait(time.monotonic(), timeout_ms)
    while True:
        url = healer._url()
        shown = [s for s in flow.waiting() if shows(driver, s, url)]
        if shown:
            if (page_path(healer._url()) == page_path(url)
                    or time.monotonic() >= wait.deadline):
                return flow.pick(shown), page_path(url)
            continue    # the page changed while its landmarks were looked at: look again
        if interrupt(healer, wait.budget, timeout_ms, poll_ms, wait.failed):
            continue
        if time.monotonic() >= wait.deadline:
            again = [s for s in flow.ran_steps() if shows(driver, s, url)]
            raise flow.stuck(again, page_path(url))
        time.sleep(_sleep_ms(poll_ms))


async def run_async(healer, timeout_ms, poll_ms):
    """AsyncHealer.run_steps(): the same loop, with the page work awaited. The step function
    may be async (it is awaited) or plain when it returns an awaitable; for one that returns
    anything else the step is recorded as failed and TypeError is raised (plain_message).
    The healer's lock is never held while it runs: its own actions take it."""
    __tracebackhide__ = True
    core = healer._core
    timeout_ms, poll_ms = run_args(core._step_book, timeout_ms, poll_ms, core.timeout_ms)
    flow = Run(core, timeout_ms)
    page = healer.page
    while not flow.done():
        flow.check_cap()
        step, path = await _anext_step(healer, flow, timeout_ms, poll_ms)
        flow.starting(step)
        started = healer._clock()
        with core._in_step(step.name, path):
            try:
                result = step.fn(page)
                if not inspect.isawaitable(result):
                    raise TypeError(plain_message(step.name, result))
                await result
            except BaseException as exc:
                flow.record(step, path, healer._clock() - started, exc)
                raise
        flow.record(step, path, healer._clock() - started)
    return flow.finish()


async def _url(driver):
    try:
        return await _awaited(driver.url()) or ''
    except Exception:
        return ''


async def _anext_step(healer, flow, timeout_ms, poll_ms):
    """_next_step(), awaited: the landmarks on the event loop, the interruptions in a
    worker thread under the healer's lock (AsyncHealer._interrupt_waiting), as before an
    action."""
    __tracebackhide__ = True
    driver, wait = healer._driver, _Wait(healer._clock(), timeout_ms)
    while True:
        url = await _url(driver)
        shown = [s for s in flow.waiting() if await ashows(driver, s, url)]
        if shown:
            if (page_path(await _url(driver)) == page_path(url)
                    or healer._clock() >= wait.deadline):
                return flow.pick(shown), page_path(url)
            continue    # the page changed while its landmarks were looked at: look again
        if await healer._interrupt_waiting(wait.budget, timeout_ms, poll_ms, wait.failed):
            continue
        if healer._clock() >= wait.deadline:
            again = [s for s in flow.ran_steps() if await ashows(driver, s, url)]
            raise flow.stuck(again, page_path(url))
        await healer._sleep(_sleep_ms(poll_ms))

