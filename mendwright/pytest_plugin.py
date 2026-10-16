"""pytest plugin for mendwright, loaded through the pytest11 entry point named 'mendwright'.

What it adds:

- A ``heal`` fixture: ``mendwright.open()`` over the test's page. The page comes from the
  fixture named by ``mendwright_page_fixture`` (default ``page``, from pytest-playwright).
  Selenium users set it to their driver fixture, for example ``driver``.
- A ``mendwright_provider`` fixture, the provider ``heal`` passes to ``mendwright.open()``.
  It returns None, so open() picks one from the environment; override it in conftest.py
  to pass your own (Bedrock, LiteLLM, Azure, a proxy).
- Confirmation: when a test passes, every heal made during it is confirmed. That covers
  the ``heal`` fixture and any Healer the test opened itself. A failing test leaves its
  heals unconfirmed (not quarantined), because the failure may have nothing to do with
  the healed element.
- ``--mendwright-strict``: a test that needed a heal fails, after the heal is saved, so CI
  catches selector drift.
- A summary of the heals at the end of the run, and a header line when ``heal`` is used.
  The summary also names the keys with a list heal that heal.locator_all(...,
  nonempty=True) found and saved for you to check (run mendwright lists). A passing test
  never accepts one.

Options: --mendwright-page-fixture, --mendwright-store, --mendwright-dry-run,
--mendwright-disable, --mendwright-strict, --mendwright-heal-get-by, and the ini keys
mendwright_page_fixture and mendwright_store. ``-p no:mendwright`` turns the plugin off.

pytest loads this module in every session wherever mendwright is installed. Importing it
has no side effects, and a run that uses none of it prints nothing extra and writes no
files.
"""

from __future__ import annotations

import logging
import os

import pytest

from .getby import is_key

logger = logging.getLogger('mendwright')

_SESSION_PLUGIN = 'mendwright-session'
# Heal records ride on test reports under this attribute, so pytest-xdist carries them
# from the workers to the controller with the report itself.
_REPORT_ATTR = 'mendwright_heals'
# The list heals found for heal.locator_all(..., nonempty=True) and saved for a person to
# check ride on the reports the same way.
_HELD_ATTR = 'mendwright_held'
_DEFAULT_PAGE_FIXTURE = 'page'
# Per-heal lines shown in the end-of-run summary without -v, and keys named in its line
# about list heals that wait for a person to check them.
_SUMMARY_LIMIT = 10


def pytest_addoption(parser):
    group = parser.getgroup('mendwright', 'mendwright (self-healing selectors)')
    group.addoption(
        '--mendwright-page-fixture', dest='mendwright_page_fixture', default=None,
        metavar='NAME',
        help="fixture that the 'heal' fixture passes to mendwright.open(). Default: page "
             '(pytest-playwright). For Selenium, use the name of your driver fixture.')
    group.addoption(
        '--mendwright-store', dest='mendwright_store', default=None, metavar='PATH',
        help="override store for the 'heal' fixture. Default: .mendwright/overrides.json "
             'in the rootdir.')
    group.addoption(
        '--mendwright-dry-run', dest='mendwright_dry_run', action='store_true', default=False,
        help='ask for heals and check them, but do not apply them. Sets MENDWRIGHT_DRY_RUN=1 '
             'for the run.')
    group.addoption(
        '--mendwright-disable', dest='mendwright_disable', action='store_true', default=False,
        help='turn healing off: selectors still resolve, but nothing is healed and no model '
             'is called. Sets MENDWRIGHT_DISABLE=1 for the run.')
    group.addoption(
        '--mendwright-strict', dest='mendwright_strict', action='store_true', default=False,
        help='fail every test that needed a heal (the heal is still saved), so CI catches '
             'selectors that have drifted.')
    group.addoption(
        '--mendwright-heal-get-by', dest='mendwright_heal_get_by', action='store_true',
        default=False,
        help='heal heal.page.get_by_*() calls too, from their fingerprints. Sets '
             'MENDWRIGHT_HEAL_GET_BY=1 for the run.')
    parser.addini(
        'mendwright_page_fixture', default='',
        help="fixture that the 'heal' fixture passes to mendwright.open() (default: page).")
    parser.addini(
        'mendwright_store', default='',
        help="override store for the 'heal' fixture, relative to this ini file "
             '(default: .mendwright/overrides.json in the rootdir).')


def pytest_configure(config):
    config.pluginmanager.register(_Session(config), _SESSION_PLUGIN)


@pytest.fixture
def mendwright_provider():
    """The model provider the ``heal`` fixture passes to mendwright.open().

    None (this default) lets open() pick one from the environment with auto_provider().
    Override it in conftest.py to use a provider of your own; the store settings of the
    ``heal`` fixture still apply::

        @pytest.fixture(scope='session')
        def mendwright_provider():
            from mendwright import OpenAICompatibleProvider
            return OpenAICompatibleProvider(base_url='https://api.groq.com/openai/v1',
                                            model='llama-3.3-70b-versatile')
    """
    return None


@pytest.fixture
def heal(request, mendwright_provider):
    """A mendwright Healer for this test's page: ``heal.locator(key, selector, role=...)``.

    The page comes from the fixture named by mendwright_page_fixture (default ``page``),
    and the provider from the ``mendwright_provider`` fixture (default: chosen from the
    environment). Heals go to the store from --mendwright-store, the mendwright_store ini
    key or MENDWRIGHT_STORE, else .mendwright/overrides.json in the rootdir. They are
    confirmed when the test passes.
    """
    config = request.config
    name = _page_fixture(config)
    if name == 'heal':
        pytest.fail(
            "mendwright: mendwright_page_fixture is set to 'heal', which is this fixture. "
            'Set it to the fixture that returns your page or driver, for example page or '
            'driver.', pytrace=False)
    missing = False
    try:
        page = request.getfixturevalue(name)
    except pytest.FixtureLookupError as exc:
        if getattr(exc, 'argname', None) != name:
            raise   # the page fixture exists; something it needs does not
        missing = True
    if missing:
        pytest.fail(_missing_fixture_message(name), pytrace=False)
    import mendwright
    return mendwright.open(page, provider=mendwright_provider, store_path=_store_path(config))


def _missing_fixture_message(name):
    if name == _DEFAULT_PAGE_FIXTURE:
        where = ("The 'heal' fixture wraps the fixture named 'page', and this test has none. "
                 "Install pytest-playwright, which provides 'page', or point mendwright at "
                 'your own fixture')
    else:
        where = ("The 'heal' fixture wraps the fixture named " + repr(name) + ' (from '
                 'mendwright_page_fixture), and this test has none. Define that fixture, or '
                 'point mendwright at the fixture you have')
    return ('mendwright: ' + where + ': add mendwright_page_fixture = driver to the [pytest] '
            'section of your pytest.ini (or [tool.pytest.ini_options] in pyproject.toml), '
            'or pass --mendwright-page-fixture=driver, using the name of the fixture that '
            "returns your page or WebDriver. You can also skip 'heal' and call "
            'mendwright.open(page) in the test yourself.')


def _page_fixture(config):
    return (config.getoption('mendwright_page_fixture')
            or (config.getini('mendwright_page_fixture') or '').strip()
            or _DEFAULT_PAGE_FIXTURE)


def _absolute(path, base):
    path = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(path):
        path = os.path.join(os.fspath(base), path)
    return os.path.normpath(path)


def _store_path(config):
    """Where the 'heal' fixture keeps heals. The first one set wins: --mendwright-store
    (relative to where pytest was started), the mendwright_store ini key (relative to the
    ini file), MENDWRIGHT_STORE (relative to where pytest was started), then
    .mendwright/overrides.json in the rootdir."""
    start = config.invocation_params.dir
    option = config.getoption('mendwright_store')
    if option:
        return _absolute(option, start)
    ini = (config.getini('mendwright_store') or '').strip()
    if ini:
        base = config.inipath.parent if config.inipath else config.rootpath
        return _absolute(ini, base)
    env = os.environ.get('MENDWRIGHT_STORE', '').strip()
    if env:
        return _absolute(env, start)
    return os.path.join(os.fspath(config.rootpath), '.mendwright', 'overrides.json')


def _env_on(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _provider_overridden(items):
    """True when a test that uses 'heal' gets mendwright_provider from a fixture other than
    this plugin's default (an override in conftest.py)."""
    for item in items:
        defs = getattr(getattr(item, '_fixtureinfo', None), 'name2fixturedefs', {}) or {}
        chosen = (defs.get('mendwright_provider') or (None,))[-1]
        module = getattr(getattr(chosen, 'func', None), '__module__', __name__)
        if module != __name__:
            return True
    return False


def _no_provider(items):
    """True when the 'heal' fixture would have no model provider: mendwright_provider is
    not overridden and auto_provider() finds none. Builds a provider object only; nothing
    is sent."""
    try:
        if _provider_overridden(items):
            return False
        from .providers import auto_provider
        auto_provider()
    except Exception:
        return True
    return False


def _fingerprint_on():
    """Whether mendwright.open() would run the fingerprint tier (MENDWRIGHT_FINGERPRINT)."""
    try:
        from .facade import fingerprint_setting
        return fingerprint_setting()
    except ValueError:
        return True


def _shown_path(path, root):
    try:
        rel = os.path.relpath(path, os.fspath(root))
    except ValueError:   # another drive on Windows
        return path
    return path if rel.startswith('..') else rel


def _old_text(old):
    """The selectors that stopped matching, the way the heal log line shows them."""
    if not old:
        return '(no stored selector)'
    if len(old) == 1:
        return old[0]
    return old[0] + ' (+' + str(len(old) - 1) + ' more)'


def _heal_line(record):
    return record['key'] + ': ' + _old_text(record['old']) + ' -> ' + record['new']


def _state(record):
    """'' for a confirmed heal, else a short note on why it is not."""
    if record['confirmed'] is False:
        return '  (quarantined: an action on it failed)'
    if record['confirmed'] is None:
        return '  (not confirmed)' if record.get('passed') else \
            '  (not confirmed: the test did not pass)'
    return ''


def _plural(count, word):
    return str(count) + ' ' + word + ('' if count == 1 else 's')


class _Session:
    """Per-config state, registered as a plugin in pytest_configure. Kept off the module
    because an in-process pytester run registers this same module in a second config."""

    def __init__(self, config):
        self.config = config
        self.strict = bool(config.getoption('mendwright_strict'))
        self.records = []     # heal records from test reports (from xdist workers too)
        self.held = []        # records of list heals saved for a person to check
        self.strict_failed = set()   # nodeids whose final report strict mode failed
        self._tracker = None
        self._env = None
        # The heal fixture passes none of these to open(), and Healers a test opens itself
        # should follow them too, so they go through the documented environment variables.
        dry_run = config.getoption('mendwright_dry_run')
        disable = config.getoption('mendwright_disable')
        get_by = config.getoption('mendwright_heal_get_by')
        if dry_run or disable or get_by:
            self._env = pytest.MonkeyPatch()
            if dry_run:
                self._env.setenv('MENDWRIGHT_DRY_RUN', '1')
            if disable:
                self._env.setenv('MENDWRIGHT_DISABLE', '1')
            if get_by:
                self._env.setenv('MENDWRIGHT_HEAL_GET_BY', '1')

    def pytest_unconfigure(self, config):
        if self._env is not None:
            self._env.undo()
            self._env = None

    # --- header -----------------------------------------------------------------------

    def pytest_report_collectionfinish(self, config, items):
        if config.getoption('verbose') < 0:
            return []
        healing = [item for item in items if 'heal' in getattr(item, 'fixturenames', ())]
        if not healing:
            return []
        if _env_on('MENDWRIGHT_DISABLE'):
            mode = 'disabled'
        elif _env_on('MENDWRIGHT_DRY_RUN'):
            mode = 'dry run'
        else:
            mode = 'heal'
        if self.strict:
            mode += ', strict'
        if mode != 'disabled' and _env_on('MENDWRIGHT_HEAL_GET_BY'):
            mode += ', get_by_* healed'
        if mode != 'disabled' and _no_provider(healing):
            if _fingerprint_on():
                mode += ' (no provider: only fingerprint heals, no model; see mendwright doctor)'
            else:
                mode += ' (no provider: broken selectors will fail; see mendwright doctor)'
        return ['mendwright: store ' + _shown_path(_store_path(config), config.rootpath)
                + ', mode ' + mode]

    # --- per test ---------------------------------------------------------------------

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        try:
            self._tracker = _Tracker()
        except Exception:   # never let bookkeeping break a test run
            logger.warning('mendwright: could not watch heals for %s', item.nodeid,
                           exc_info=True)
            self._tracker = None
        try:
            yield
        finally:
            tracker, self._tracker = self._tracker, None
            if tracker is not None:
                tracker.close()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        tracker = self._tracker
        if tracker is None:
            return
        report = outcome.get_result()
        tracker.collect()
        if report.when == 'setup' and report.passed:
            return   # heals made in setup are reported with the call phase
        if report.when == 'call' and report.passed:
            tracker.confirm()   # the test passed with the healed selectors
        heals = tracker.take()
        records = [{'nodeid': report.nodeid, 'key': str(h.get('key')),
                    'old': [str(x) for x in h.get('old') or ()], 'new': str(h.get('new')),
                    'confirmed': h.get('confirmed'), 'passed': bool(report.passed)}
                   for h in heals]
        if records:
            setattr(report, _REPORT_ATTR, records)
            tracker.healed.extend(records)
        held = [{'nodeid': report.nodeid, 'key': str(r.get('key')), 'xpath': str(r.get('xpath')),
                 'id': r.get('id') if isinstance(r.get('id'), str) else None}
                for r in tracker.take_held()]
        if held:
            setattr(report, _HELD_ATTR, held)
        if report.when != 'call':
            return
        # tracker.healed spans every attempt of this item: the protocol hookwrapper wraps
        # pytest-rerunfailures' loop. A rerun resolves the heal an earlier attempt saved,
        # so it heals nothing itself, but it still passes only because of that heal.
        if self.strict and report.passed and tracker.healed:
            shown = records or tracker.healed
            report.outcome = 'failed'
            keys = ', '.join(repr(k) for k in dict.fromkeys(r['key'] for r in shown))
            report.longrepr = _StrictFailure(
                self._strict_message(shown),
                'mendwright: ' + keys + ' needed a heal (--mendwright-strict)')
            report.mendwright_strict = True
            for record in records:
                record['strict'] = True
        elif report.failed and records:
            report.sections.append(('mendwright', self._failed_note(records)))

    def _strict_message(self, records):
        if len(records) == 1:
            head = ('1 selector was healed. The heal is saved, but the selector in your code '
                    'no longer matches the page:')
            fix = 'Update the selector in your code to the new one'
        else:
            head = (str(len(records)) + ' selectors were healed. The heals are saved, but the '
                    'selectors in your code no longer match the page:')
            fix = 'Update the selectors in your code to the new ones'
        lines = ['mendwright --mendwright-strict: this test passed only because ' + head]
        lines += ['  ' + _heal_line(r) for r in records]
        lines.append(fix + ', or run without --mendwright-strict to let heals pass.')
        if any(is_key(r['key']) for r in records):
            lines.append('For a get_by_* call, run mendwright fix to see the healed selector and '
                         'where the call is; it does not change the call itself.')
        return '\n'.join(lines)

    def _failed_note(self, records):
        lines = [_plural(len(records), 'selector') + (' was' if len(records) == 1 else ' were')
                 + ' healed during this test:']
        lines += ['  ' + _heal_line(r) + _state(r) for r in records]
        if any(r['confirmed'] is None for r in records):
            lines.append('A heal from a failed test is kept but not confirmed. If the failure '
                         'is about that element, check the new selector before the next run.')
        return '\n'.join(lines)

    # --- end of run -------------------------------------------------------------------

    def pytest_runtest_logreport(self, report):
        records = getattr(report, _REPORT_ATTR, None)
        if records:
            self.records.extend(records)
        held = getattr(report, _HELD_ATTR, None)
        if held:
            self.held.extend(held)
        if getattr(report, 'mendwright_strict', False) and report.outcome == 'failed':
            self.strict_failed.add(report.nodeid)

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, 'workerinput') or not (self.records or self.held):
            return   # an xdist worker: the controller prints the summary once
        tr = terminalreporter
        verbose = config.getoption('verbose')
        # -q: the count lines only. Default: the first heals. -v: every heal.
        if verbose < 0:
            tr.ensure_newline()
        else:
            tr.write_sep('=', 'mendwright')
        if self.records:
            self._heals_summary(tr, verbose)
        if self.held:
            held = list(dict.fromkeys((r['key'], r['xpath'], r['id']) for r in self.held))
            keys = list(dict.fromkeys(key for key, _, _ in held))
            named = ', '.join(keys[:_SUMMARY_LIMIT]) + (
                ', and ' + str(len(keys) - _SUMMARY_LIMIT) + ' more'
                if len(keys) > _SUMMARY_LIMIT else '')
            tr.write_line('mendwright: ' + str(len(held))
                          + (' list heal waits for you to check it' if len(held) == 1
                             else ' list heals wait for you to check them')
                          + ' (run mendwright lists): ' + named)
        failed = len(self.strict_failed)
        if failed:
            tr.write_line('--mendwright-strict failed ' + _plural(failed, 'test') + ' because '
                          + ('it' if failed == 1 else 'they') + ' needed a heal. Update those '
                          'selectors in your code.')

    def _heals_summary(self, tr, verbose):
        records = self.records
        confirmed = sum(1 for r in records if r['confirmed'] is True)
        quarantined = sum(1 for r in records if r['confirmed'] is False)
        unconfirmed = len(records) - confirmed - quarantined
        tests = len({r['nodeid'] for r in records})
        parts = []
        if confirmed and self.strict:
            # The steps passed with the heal, but strict mode failed those tests.
            parts.append(str(confirmed) + ' confirmed')
        elif confirmed:
            parts.append(str(confirmed) + (' confirmed by a passing test' if confirmed == 1
                                           else ' confirmed by passing tests'))
        if unconfirmed:
            parts.append(str(unconfirmed) + ' not confirmed')
        if quarantined:
            parts.append(str(quarantined) + ' quarantined')
        if verbose < 0:
            shown = []
        else:
            shown = records if verbose > 0 else records[:_SUMMARY_LIMIT]
        tr.write_line('mendwright: ' + _plural(len(records), 'selector') + ' healed in '
                      + _plural(tests, 'test') + ' (' + ', '.join(parts) + ')')
        for r in shown:
            tr.write_line('  ' + r['nodeid'] + '  ' + _heal_line(r) + _state(r))
        if shown and len(shown) < len(records):
            tr.write_line('  ... and ' + str(len(records) - len(shown)) + ' more. Run with '
                          '-v to list them all.')


class _Crash:
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class _StrictFailure:
    """The failure text of a test failed by --mendwright-strict. reprcrash gives pytest's
    short summary and --tb=line a one-line reason, as an exception's failure would."""

    def __init__(self, text, message):
        self.text = text
        self.reprcrash = _Crash(message)

    def toterminal(self, tw):
        for line in self.text.splitlines():
            tw.line(line)

    def __str__(self):
        return self.text


class _Tracker:
    """The heals applied while one test runs (setup, call and teardown).

    It registers a healer listener (mendwright.facade.add_healer_listener), so it sees
    every Healer that is alive or opened during the test. It keeps each Healer's coordinator, not the
    Healer: a Healer held in a local variable is freed when the test function returns,
    as before, but its coordinator stays reachable until the call phase is reported, so
    its heals can still be confirmed."""

    def __init__(self):
        from . import facade
        self._facade = facade
        # [coordinator, number of its heals already collected, and of its held list heals]
        self._watched = []
        self._ids = set()
        self._found = []      # (coordinator, heal dict) not yet attached to a report
        self._held = []       # the coordinators' held list heals not yet attached to a report
        self.healed = []      # records of every attempt of this item (reruns too)
        for healer in facade.live_healers():
            self._watch(healer)
        facade.add_healer_listener(self._watch)

    def _watch(self, healer):
        coordinator = getattr(healer, 'coordinator', None)
        heals = getattr(coordinator, 'heals', None)
        if not isinstance(heals, list) or id(coordinator) in self._ids:
            return
        self._ids.add(id(coordinator))
        held = getattr(coordinator, 'held', None)
        self._watched.append([coordinator, len(heals),
                              len(held) if isinstance(held, list) else 0])

    def collect(self):
        """Pick up the heals applied, and the list heals held, since the last call."""
        for entry in self._watched:
            coordinator, seen, seen_held = entry
            heals = coordinator.heals
            for heal in heals[seen:]:
                if isinstance(heal, dict):
                    self._found.append((coordinator, heal))
            entry[1] = len(heals)
            held = getattr(coordinator, 'held', None)
            if isinstance(held, list):
                self._held.extend(r for r in held[seen_held:] if isinstance(r, dict))
                entry[2] = len(held)

    def take_held(self):
        held, self._held = self._held, []
        return held

    def confirm(self):
        """Confirm the heals of this test that are still waiting for an outcome."""
        for coordinator, heal in self._found:
            key = heal.get('key')
            pending = getattr(coordinator, 'pending', None)
            if heal.get('confirmed') is not None or not isinstance(pending, dict):
                continue
            if pending.get(key) != heal.get('new'):
                continue   # already reported, or replaced by a later heal of the same key
            try:
                coordinator.confirm_pending(key, True)
            except Exception:
                logger.warning('mendwright: could not confirm the heal for %r', key,
                               exc_info=True)

    def take(self):
        heals = [heal for _, heal in self._found]
        self._found = []
        return heals

    def close(self):
        self._facade.remove_healer_listener(self._watch)
        self._watched = []
        self._ids = set()
        self._found = []
        self._held = []
