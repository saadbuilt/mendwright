"""The pytest plugin (mendwright/pytest_plugin.py), driven through pytester.

Each test writes a small project and runs pytest on it in the same process. The project's
conftest gives a 'page' fixture backed by the lxml FakePage from tests/conftest.py, and
replaces auto_provider with a stub that proposes a fixed xpath per key. No browser, no
network, no model.

The plugin is loaded by module name with its entry point blocked, and entry-point
autoloading is off for the inner runs, so the same code runs whether or not mendwright is
installed and whatever other pytest plugins this machine has.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

import mendwright

FAKES = Path(__file__).with_name('conftest.py')
# The plugin module is already imported here, so pytest warns that it cannot rewrite its
# asserts when -p loads it again. That warning is about this setup, not the plugin.
PLUGIN = ('-p', 'no:mendwright', '-p', 'mendwright.pytest_plugin', '-p', 'no:cacheprovider',
          '-W', 'ignore::pytest.PytestAssertRewriteWarning')

CONFTEST = '''
import importlib.util

import pytest

import mendwright
import mendwright.providers

_spec = importlib.util.spec_from_file_location('_mendwright_fakes', {fakes!r})
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)
FakePage = _fakes.FakePage

HTML = "<form><input type='checkbox' id='new'><button id='go2'>Go</button></form>"
PROPOSALS = {{'terms': "//input[@type='checkbox']", 'go': "//button[@id='go2']"}}


class MapProvider:
    """Proposes PROPOSALS[key], or //input[@name=key] for any other key."""

    def propose(self, bundle):
        key = bundle['key']
        xpath = PROPOSALS.get(key, "//input[@name='" + key + "']")
        return {{'xpath': xpath, 'fallbacks': [], 'reasoning': 'stub'}}

    def report_outcome(self, key, xpath, success):
        pass


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    monkeypatch.setattr(mendwright.providers, 'auto_provider', lambda *a, **k: MapProvider())
'''

PAGE_FIXTURE = '''

@pytest.fixture
def page():
    return FakePage(HTML)
'''

HEAL_TERMS = '''
def test_terms(heal):
    heal.locator('terms', "//input[@id='old']", role='checkbox').check()
'''

TERMS_LINE = "terms: //input[@id='old'] -> //input[@type='checkbox']"
GO_LINE = "go: //button[@id='go'] -> //button[@id='go2']"


def _conftest(page_fixture=True):
    return CONFTEST.format(fakes=str(FAKES)) + (PAGE_FIXTURE if page_fixture else '')


@pytest.fixture
def project(pytester, monkeypatch):
    """A pytester project with the fakes in its conftest, ready to run."""
    monkeypatch.setenv('MENDWRIGHT_TIMEOUT_MS', '0')    # a missing element fails at once
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    for name in list(os.environ):
        if name.startswith(('ANTHROPIC_', 'OPENAI_', 'GEMINI_')):
            monkeypatch.delenv(name)
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    pytester.makeconftest(_conftest())
    return pytester


def run(pytester, *args):
    return pytester.runpytest_inprocess(*PLUGIN, *args)


def events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def kinds(path, key):
    return [(e['event'], e.get('success')) for e in events(path) if e.get('key') == key]


def glob(text):
    """text as a literal inside an fnmatch_lines pattern ('[' opens a class there)."""
    return text.replace('[', '[[]')


def summary_lines(result):
    return [line for line in result.outlines if line.startswith('mendwright: ')
            and 'healed in' in line]


# --- confirmation --------------------------------------------------------------------

def test_a_passing_test_confirms_its_heal(project):
    project.makepyfile(HEAL_TERMS)
    result = run(project)
    result.assert_outcomes(passed=1)
    journal = project.path / '.mendwright' / 'journal.jsonl'
    assert kinds(journal, 'terms')[-2:] == [('heal.applied', None), ('heal.canary', True)]
    result.stdout.fnmatch_lines([
        'mendwright: 1 selector healed in 1 test (1 confirmed by a passing test)',
        '  test_a_passing_test_confirms_its_heal.py::test_terms  ' + TERMS_LINE,
    ])


def test_a_failing_test_leaves_its_heal_unconfirmed(project):
    project.makepyfile('''
        def test_terms(heal):
            heal.locator('terms', "//input[@id='old']", role='checkbox').check()
            assert 1 == 2, 'an unrelated assertion'
    ''')
    result = run(project)
    result.assert_outcomes(failed=1)
    journal = project.path / '.mendwright' / 'journal.jsonl'
    assert [k for k, _ in kinds(journal, 'terms')] == [
        'heal.requested', 'heal.proposed', 'heal.applied']    # no canary, no quarantine
    store = json.loads((project.path / '.mendwright' / 'overrides.json').read_text())
    healed = store['selectors']['terms']['candidates'][0]
    assert (healed['xpath'], healed['status']) == ("//input[@type='checkbox']", 'active')
    result.stdout.fnmatch_lines([
        '*- mendwright -*',
        '1 selector was healed during this test:',
        '  ' + TERMS_LINE + '  (not confirmed: the test did not pass)',
        'A heal from a failed test is kept but not confirmed.*',
    ])
    result.stdout.fnmatch_lines([
        'mendwright: 1 selector healed in 1 test (1 not confirmed)',
        '*::test_terms  ' + glob(TERMS_LINE) + '  (not confirmed: the test did not pass)',
    ])


def test_a_failed_action_still_quarantines_and_the_summary_says_so(project):
    project.makepyfile('''
        def test_go(heal, page):
            page.fail['click'] = RuntimeError('element is detached')
            heal.locator('go', "//button[@id='go']", role='button').click()
    ''')
    result = run(project)
    result.assert_outcomes(failed=1)
    journal = project.path / '.mendwright' / 'journal.jsonl'
    assert kinds(journal, 'go')[-2:] == [('heal.canary', False), ('heal.quarantine', None)]
    result.stdout.fnmatch_lines([
        'mendwright: 1 selector healed in 1 test (1 quarantined)',
        '*::test_go  ' + glob(GO_LINE) + '  (quarantined: an action on it failed)',
    ])


def test_a_healer_the_test_opened_itself_is_confirmed(project):
    project.makepyfile('''
        import gc

        import mendwright
        from mendwright.facade import live_healers

        def test_own(page):
            own = mendwright.open(page, store_path='own/overrides.json')
            own.locator('terms', "//input[@id='old']", role='checkbox').check()
            # Only a local variable holds it, and the plugin must not keep it alive.
            ident = id(own)
            del own
            gc.collect()
            assert ident not in [id(h) for h in live_healers()]
    ''')
    result = run(project)
    result.assert_outcomes(passed=1)
    assert kinds(project.path / 'own' / 'journal.jsonl', 'terms')[-1] == ('heal.canary', True)
    result.stdout.fnmatch_lines(['mendwright: 1 selector healed in 1 test (1 confirmed by a '
                                 'passing test)'])


def test_only_heals_made_during_a_passing_test_are_confirmed(project):
    project.makeconftest(_conftest() + '''

@pytest.fixture(scope='module')
def shared():
    return mendwright.open(FakePage(HTML), provider=MapProvider(),
                           store_path='shared/overrides.json')
''')
    project.makepyfile('''
        def test_a(shared):
            shared.locator('terms', "//input[@id='old']", role='checkbox').check()
            assert 1 == 2

        def test_b(shared):
            shared.locator('go', "//button[@id='go']", role='button').click()
    ''')
    result = run(project)
    result.assert_outcomes(passed=1, failed=1)
    journal = project.path / 'shared' / 'journal.jsonl'
    assert ('heal.canary', True) in kinds(journal, 'go')
    assert [k for k, _ in kinds(journal, 'terms')][-1] == 'heal.applied'
    result.stdout.fnmatch_lines([
        'mendwright: 2 selectors healed in 2 tests (1 confirmed by a passing test, '
        '1 not confirmed)',
        '*::test_a  ' + glob(TERMS_LINE) + '  (not confirmed: the test did not pass)',
        '*::test_b  ' + glob(GO_LINE),
    ])


GRID = '''

def _grid(cls):
    cards = ''.join("<div id='p" + str(i) + "' class='" + cls + "'><h3>" + name + "</h3>"
                    "<button>Add</button></div>"
                    for i, name in enumerate(('Blue mug', 'Red mug', 'Tea pot'), 1))
    return "<html><body><main id='shop'><div id='grid'>" + cards + "</div></main></body></html>"


PROPOSALS['.card'] = "//div[@id='p1']"


@pytest.fixture
def grid():
    page = FakePage(_grid('card'), fingerprints=True, css=True)
    page.redesign = lambda: page.set_html(_grid('tile'))
    return page
'''


def test_a_heal_of_one_element_of_a_list_s_key_is_confirmed_and_the_list_reports_nothing(project):
    # heal.locator_all('.card', ...) and heal.page.inner_text('.card') share a key: the first
    # card heals through the model, the list is never healed, and its elements' confirm()
    # does not report the card's heal.
    pytest.importorskip('cssselect')     # the inner test's fake page matches '.card' with it
    project.makeconftest(_conftest() + GRID)
    project.makepyfile('''
        import pytest
        import mendwright

        def test_grid(grid):
            heal = mendwright.open(grid, store_path='grid/overrides.json', timeout_ms=0)
            assert len(heal.locator_all('.card', '.card')) == 3
            grid.redesign()
            assert heal.page.inner_text('.card').startswith('Blue mug')
            with pytest.raises(mendwright.AttentionRequired):
                heal.locator_all('.card', '.card')
            cards = heal.locator_all('.card', '.tile')
            cards[0].confirm(False)
            assert len(cards) == 3
    ''')
    result = run(project)
    result.assert_outcomes(passed=1)
    journal = project.path / 'grid' / 'journal.jsonl'
    canaries = sorted((e['xpath'], e['success']) for e in events(journal)
                      if e['event'] == 'heal.canary')
    assert canaries == [("//div[@id='p1']", True)]
    result.stdout.fnmatch_lines(['mendwright: 1 selector healed in 1 test (1 confirmed by a '
                                 'passing test)'])


# --- strict mode ---------------------------------------------------------------------

def test_strict_fails_a_test_that_needed_a_heal(project, monkeypatch):
    monkeypatch.setenv('COLUMNS', '200')    # so the short summary line is not cut
    project.makepyfile('''
        def test_terms(heal):
            heal.locator('terms', "//input[@id='old']", role='checkbox').check()

        def test_no_heal(heal):
            heal.locator('go', "//button[@id='go2']", role='button').click()
    ''')
    result = run(project, '--mendwright-strict')
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines([
        'mendwright --mendwright-strict: this test passed only because 1 selector was healed.*',
        '  ' + TERMS_LINE,
        'Update the selector in your code to the new one*',
    ])
    result.stdout.fnmatch_lines(["FAILED *::test_terms - mendwright: 'terms' needed a heal*"])
    result.stdout.fnmatch_lines([
        'mendwright: 1 selector healed in 1 test (1 confirmed)',
        '*::test_terms  ' + glob(TERMS_LINE),
        '--mendwright-strict failed 1 test because it needed a heal. Update those selectors '
        'in your code.',
    ])
    # The heal is saved (and confirmed, since the steps passed with it).
    store = json.loads((project.path / '.mendwright' / 'overrides.json').read_text())
    assert store['selectors']['terms']['candidates'][0]['xpath'] == "//input[@type='checkbox']"
    assert kinds(project.path / '.mendwright' / 'journal.jsonl', 'terms')[-1] == (
        'heal.canary', True)

    # --tb=line shows the one-line reason too (on a fresh store, so it heals again).
    project.path.joinpath('.mendwright').rename(project.path / 'first-run')
    line = run(project, '--mendwright-strict', '--tb=line')
    line.assert_outcomes(passed=1, failed=1)
    line.stdout.fnmatch_lines(["mendwright: 'terms' needed a heal (--mendwright-strict)"])


def test_without_strict_a_healing_test_passes(project):
    project.makepyfile(HEAL_TERMS)
    run(project).assert_outcomes(passed=1)


def test_strict_passes_a_flow_whose_steps_came_in_another_order(project):
    # heal.run_steps() on a checkout that now asks for the card first: no selector broke,
    # so strict mode has nothing to fail, as for a handled interruption.
    project.makepyfile('''
        PAGES = {
            'pay': "<h1>Payment</h1><input id='card'>",
            'ship': "<h1>Shipping</h1><input id='ship'>",
            'review': "<button id='place'>Place order</button>",
        }

        def test_checkout(heal, page):
            def go(name):
                page.url = 'https://shop.example/checkout/' + name
                page.set_html(PAGES[name])

            @heal.step('shipping', "//h1[.='Shipping']")
            def shipping(p):
                p.fill('#ship', 'Ada')
                go('review')

            @heal.step('payment', "//h1[.='Payment']")
            def payment(p):
                p.fill('#card', '4242')
                go('ship')

            @heal.step('review', "//button[.='Place order']", last=True)
            def review(p):
                p.click('#place')

            go('pay')
            assert heal.run_steps(poll_ms=5) == ['payment', 'shipping', 'review']
            assert heal.heals == []
    ''')
    result = run(project, '--mendwright-strict')
    result.assert_outcomes(passed=1)
    assert '--mendwright-strict failed' not in result.stdout.str()


def test_the_heal_fixture_takes_a_provider_from_mendwright_provider(project, monkeypatch):
    # No provider in the environment; conftest.py hands one to the 'heal' fixture.
    project.makeconftest(_conftest() + '''

def _nothing(*a, **k):
    raise ValueError('no provider configured')


@pytest.fixture(autouse=True)
def _no_auto_provider(monkeypatch):
    monkeypatch.setattr(mendwright.providers, 'auto_provider', _nothing)


@pytest.fixture(scope='session')
def mendwright_provider():
    return MapProvider()
''')
    project.makepyfile(HEAL_TERMS)
    monkeypatch.chdir(project.path / '..')
    result = run(project, str(project.path), '--rootdir', str(project.path))
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(['mendwright: store .mendwright?overrides.json, mode heal'])
    saved = json.loads((project.path / '.mendwright' / 'overrides.json').read_text())
    healed = [c['xpath'] for c in saved['selectors']['terms']['candidates']
              if c['source'] == 'heal']
    assert healed == ["//input[@type='checkbox']"]


def test_a_failed_heal_points_at_the_test_line_not_mendwright_internals(project):
    project.makeconftest(_conftest() + '''

@pytest.fixture(scope='session')
def mendwright_provider():
    class Empty:
        def propose(self, bundle):
            return None

        def report_outcome(self, *a):
            pass
    return Empty()
''')
    project.makepyfile(HEAL_TERMS)
    result = run(project)
    result.assert_outcomes(failed=1)
    text = result.stdout.str()
    assert "heal.locator('terms'" in text
    assert 'def resolve_with_heal' not in text and 'def locator' not in text
    result.stdout.fnmatch_lines(["E * the model returned no usable selector for 'terms'*"])


def test_mendwright_provider_defaults_to_none(project):
    project.makepyfile('''
        def test_default(mendwright_provider):
            assert mendwright_provider is None
    ''')
    run(project).assert_outcomes(passed=1)


FLAKY_FIRST_ATTEMPT = '''
import os

def test_terms(heal):
    heal.locator('terms', "//input[@id='old']", role='checkbox').check()
    if not os.path.exists('attempted'):
        open('attempted', 'w').close()
        raise AssertionError('a flaky failure unrelated to the heal')
'''


@pytest.mark.parametrize('code', [HEAL_TERMS, FLAKY_FIRST_ATTEMPT], ids=['passes', 'flaky'])
def test_strict_still_fails_when_pytest_rerunfailures_reruns_the_test(code, project):
    # The rerun resolves the heal the first attempt saved, so it heals nothing itself, but
    # it still passes only because of that heal.
    pytest.importorskip('pytest_rerunfailures')
    project.makepyfile(code)
    result = run(project, '-p', 'pytest_rerunfailures', '--mendwright-strict', '--reruns', '1')
    outcomes = result.parseoutcomes()
    assert (outcomes.get('failed'), outcomes.get('rerun'), outcomes.get('passed')) == (1, 1, None)
    assert result.ret == 1
    result.stdout.fnmatch_lines([
        '--mendwright-strict failed 1 test because it needed a heal. Update those selectors '
        'in your code.'])

    plain = run(project, '-p', 'pytest_rerunfailures', '--reruns', '1')   # healed already
    plain.assert_outcomes(passed=1)


# --- summary -------------------------------------------------------------------------

def test_summary_counts_heals_and_tests_and_lists_each_one(project):
    project.makepyfile('''
        def test_terms(heal):
            heal.locator('terms', "//input[@id='old']", role='checkbox').check()

        def test_go(heal):
            heal.locator('go', "//button[@id='go']", role='button').click()
    ''')
    result = run(project)
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines([
        '*= mendwright =*',
        'mendwright: 2 selectors healed in 2 tests (2 confirmed by passing tests)',
        '  test_summary_counts_heals_and_tests_and_lists_each_one.py::test_terms  '
        + TERMS_LINE,
        '  test_summary_counts_heals_and_tests_and_lists_each_one.py::test_go  ' + GO_LINE,
    ])
    assert len(summary_lines(result)) == 1

    # With -q, only the count line.
    project.path.joinpath('.mendwright').rename(project.path / 'first-run')
    quiet = run(project, '-q')
    assert summary_lines(quiet) == [
        'mendwright: 2 selectors healed in 2 tests (2 confirmed by passing tests)']
    assert '::test_terms  ' not in quiet.stdout.str()
    assert '= mendwright =' not in quiet.stdout.str()


def test_summary_lists_ten_heals_unless_verbose(project):
    project.makeconftest(_conftest() + '''

@pytest.fixture
def wide_page():
    return FakePage('<form>' + ''.join(
        "<input type='checkbox' name='k%d'>" % i for i in range(12)) + '</form>')
''')
    project.makepyfile('''
        import pytest

        @pytest.mark.parametrize('n', range(12))
        def test_box(heal, n):
            heal.locator('k%d' % n, "//input[@id='gone']", role='checkbox').check()
    ''')
    result = run(project, '--mendwright-page-fixture', 'wide_page')
    result.assert_outcomes(passed=12)
    result.stdout.fnmatch_lines([
        'mendwright: 12 selectors healed in 12 tests (12 confirmed by passing tests)',
        '*::test_box?0?  k0: *',
        '  ... and 2 more. Run with -v to list them all.',
    ])
    assert len([line for line in result.outlines if '::test_box[' in line]) == 10

    project.path.joinpath('.mendwright').rename(project.path / 'first-run')
    verbose = run(project, '--mendwright-page-fixture', 'wide_page', '-v')
    lines = [line for line in verbose.outlines if line.startswith('  ') and '::test_box[' in line]
    assert len(lines) == 12
    assert '... and' not in verbose.stdout.str()


# --- the page fixture ----------------------------------------------------------------

@pytest.mark.parametrize('how', ['ini', 'option', 'option beats ini'])
def test_the_page_fixture_can_be_renamed(project, how):
    project.makeconftest(_conftest(page_fixture=False) + '''

@pytest.fixture
def my_driver():
    return FakePage(HTML)

@pytest.fixture
def other_driver():
    return FakePage(HTML)
''')
    project.makepyfile('''
        def test_uses_it(heal, my_driver):
            assert heal.page.native is my_driver
    ''')
    args = []
    if how == 'ini':
        project.makeini('[pytest]\nmendwright_page_fixture = my_driver\n')
    elif how == 'option':
        args = ['--mendwright-page-fixture=my_driver']
    else:
        project.makeini('[pytest]\nmendwright_page_fixture = other_driver\n')
        args = ['--mendwright-page-fixture', 'my_driver']
    run(project, *args).assert_outcomes(passed=1)


def test_a_missing_page_fixture_says_how_to_fix_it(project):
    project.makeconftest(_conftest(page_fixture=False))
    project.makepyfile('''
        def test_x(heal):
            pass
    ''')
    result = run(project)
    result.assert_outcomes(errors=1)
    text = result.stdout.str()
    assert "the fixture named 'page', and this test has none" in text
    assert 'Install pytest-playwright' in text
    assert 'mendwright_page_fixture = driver' in text
    assert '--mendwright-page-fixture=driver' in text
    assert 'mendwright.open(page)' in text
    assert 'Traceback' not in text and 'FixtureLookupError' not in text

    custom = run(project, '--mendwright-page-fixture=selenium_session')
    custom.assert_outcomes(errors=1)
    assert "the fixture named 'selenium_session' (from mendwright_page_fixture)" in (
        custom.stdout.str())


def test_an_error_inside_the_page_fixture_is_not_hidden(project):
    project.makeconftest(_conftest(page_fixture=False) + '''

@pytest.fixture
def page(browser_that_does_not_exist):
    return None
''')
    project.makepyfile('''
        def test_x(heal):
            pass
    ''')
    result = run(project)
    result.assert_outcomes(errors=1)
    assert "fixture 'browser_that_does_not_exist' not found" in result.stdout.str()
    assert 'mendwright_page_fixture' not in result.stdout.str()


# --- the store -----------------------------------------------------------------------

def test_the_store_is_in_the_rootdir_not_the_cwd(project, monkeypatch):
    project.makeini('[pytest]\n')
    sub = project.path / 'sub'
    sub.mkdir()
    (sub / 'test_sub.py').write_text(HEAL_TERMS)
    monkeypatch.chdir(sub)
    run(project).assert_outcomes(passed=1)
    assert (project.path / '.mendwright' / 'overrides.json').exists()
    assert not (sub / '.mendwright').exists()


@pytest.mark.parametrize('settings, expected', [
    ({'env': 'env/o.json'}, 'env/o.json'),
    ({'env': 'env/o.json', 'ini': 'ini/o.json'}, 'ini/o.json'),
    ({'env': 'env/o.json', 'ini': 'ini/o.json', 'option': 'cli/o.json'}, 'cli/o.json'),
])
def test_the_store_can_be_moved_and_the_explicit_setting_wins(project, monkeypatch,
                                                              settings, expected):
    project.makepyfile(HEAL_TERMS)
    if 'env' in settings:
        monkeypatch.setenv('MENDWRIGHT_STORE', settings['env'])
    if 'ini' in settings:
        project.makeini('[pytest]\nmendwright_store = ' + settings['ini'] + '\n')
    args = ['--mendwright-store', settings['option']] if 'option' in settings else []
    run(project, *args).assert_outcomes(passed=1)
    made = sorted(p.relative_to(project.path).as_posix()
                  for p in project.path.rglob('o.json'))
    assert made == [expected]
    assert not (project.path / '.mendwright').exists()


# --- dry run and disable -------------------------------------------------------------

@pytest.mark.parametrize('flag, variable, message, mode', [
    ('--mendwright-dry-run', 'MENDWRIGHT_DRY_RUN', 'dry-run mode is on',
     'dry run (no provider: only fingerprint heals, no model; see mendwright doctor)'),
    ('--mendwright-disable', 'MENDWRIGHT_DISABLE', 'healing is turned off', 'disabled'),
])
def test_dry_run_and_disable_reach_open(project, flag, variable, message, mode):
    project.makepyfile(HEAL_TERMS + '''

import mendwright

def test_own(page):
    mendwright.open(page, store_path='own/o.json').locator(
        'go', "//button[@id='go']", role='button')
''')
    result = run(project, flag)
    result.assert_outcomes(failed=2)    # nothing was healed, so neither element was found
    assert result.stdout.str().count(message) >= 2
    result.stdout.fnmatch_lines(['mendwright: store .mendwright?overrides.json, mode ' + mode])
    assert not summary_lines(result)
    for journal in (project.path / '.mendwright' / 'journal.jsonl',
                    project.path / 'own' / 'journal.jsonl'):
        assert not journal.exists() or 'heal.applied' not in journal.read_text()
    assert variable not in os.environ    # put back after the run


# --- quiet when unused ---------------------------------------------------------------

def test_the_header_line_shows_only_when_heal_is_used(project, monkeypatch):
    project.makepyfile(HEAL_TERMS)
    used = run(project, '--mendwright-strict')
    used.stdout.fnmatch_lines([
        'collected 1 item',
        'mendwright: store .mendwright?overrides.json, mode heal, strict (no provider: only '
        'fingerprint heals, no model; see mendwright doctor)',
    ])
    monkeypatch.setenv('MENDWRIGHT_FINGERPRINT', '0')
    off = run(project, '--mendwright-strict')
    off.stdout.fnmatch_lines([
        'collected 1 item',
        'mendwright: store .mendwright?overrides.json, mode heal, strict (no provider: broken '
        'selectors will fail; see mendwright doctor)',
    ])
    monkeypatch.delenv('MENDWRIGHT_FINGERPRINT')
    # With a provider in the environment (built only, never called) the note goes away.
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'not-a-real-key')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'some-model')
    configured = run(project)
    configured.stdout.fnmatch_lines([
        'collected 1 item', 'mendwright: store .mendwright?overrides.json, mode heal'])
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    quiet = run(project, '-q')
    assert not [line for line in quiet.outlines if line.startswith('mendwright: store')]

    project.makepyfile(test_a_passing_test='def test_plain():\n    pass\n')
    (project.path / 'test_the_header_line_shows_only_when_heal_is_used.py').unlink()
    unused = run(project)
    assert not [line for line in unused.outlines if line.startswith('mendwright:')]


def test_the_plugin_is_inert_when_unused(pytester, monkeypatch):
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    pytester.makepyfile('''
        def test_plain():
            assert True

        def test_fails():
            assert 1 == 2
    ''')
    before = sorted(p.name for p in pytester.path.iterdir())
    with_plugin = run(pytester)
    without = pytester.runpytest_inprocess('-p', 'no:mendwright', '-p', 'no:cacheprovider',
                                           '-W', 'ignore::pytest.PytestAssertRewriteWarning')
    with_plugin.assert_outcomes(passed=1, failed=1)

    def normal(result):
        return [re.sub(r' in [0-9.]+s', '', line) for line in result.outlines]

    assert normal(with_plugin) == normal(without)
    assert sorted(p.name for p in pytester.path.iterdir()) == before


def test_the_options_are_listed_in_help(project):
    result = run(project, '--help')
    text = result.stdout.str()
    for name in ('--mendwright-page-fixture', '--mendwright-store', '--mendwright-dry-run',
                 '--mendwright-disable', '--mendwright-strict', '--mendwright-heal-get-by',
                 'mendwright_page_fixture', 'mendwright_store'):
        assert name in text


# --- pytest-xdist --------------------------------------------------------------------

def test_under_xdist_the_summary_is_printed_once(project, monkeypatch):
    pytest.importorskip('xdist')
    # Workers are new processes: make them import this mendwright, not an installed one.
    root = str(Path(mendwright.__file__).resolve().parents[1])
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(
        p for p in (root, os.environ.get('PYTHONPATH')) if p))
    project.makepyfile('''
        def test_terms(heal):
            heal.locator('terms', "//input[@id='old']", role='checkbox').check()

        def test_go(heal):
            heal.locator('go', "//button[@id='go']", role='button').click()
    ''')
    result = project.runpytest_subprocess(*PLUGIN, '-p', 'xdist.plugin', '-n', '2',
                                          timeout=120)
    result.assert_outcomes(passed=2)
    assert summary_lines(result) == [
        'mendwright: 2 selectors healed in 2 tests (2 confirmed by passing tests)']
    text = result.stdout.str()
    assert '::test_terms  ' + TERMS_LINE in text
    assert '::test_go  ' + GO_LINE in text



# --- --mendwright-heal-get-by -----------------------------------------------------------------

def test_heal_get_by_option_sets_the_variable_for_the_run_and_the_header_says_so(project):
    project.makepyfile("""
        import os

        def test_it(heal):
            assert os.environ.get('MENDWRIGHT_HEAL_GET_BY') == '1'
            assert heal._heal_get_by is True
    """)
    result = run(project, '--mendwright-heal-get-by')
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(['mendwright: store .mendwright?overrides.json, mode heal, '
                                 'get_by_* healed (no provider*'])
    assert 'MENDWRIGHT_HEAL_GET_BY' not in os.environ    # put back after the run
    result = run(project)
    result.assert_outcomes(failed=1)
    assert 'get_by_* healed' not in result.stdout.str()


def test_strict_mode_names_fix_for_a_call_heal():
    from mendwright.pytest_plugin import _Session

    class Config:
        def getoption(self, name):
            return name == 'mendwright_strict'

    session = _Session(Config())
    record = {'key': 'get_by_role("button", name="Sign in")', 'old': ['get_by_role("button", '
              'name="Sign in")'], 'new': "//button[@id='login']"}
    message = session._strict_message([record])
    assert message.splitlines()[-1] == (
        'For a get_by_* call, run mendwright fix to see the healed selector and where the call '
        'is; it does not change the call itself.')
    assert 'For a get_by_* call' not in session._strict_message([dict(record, key='login')])
