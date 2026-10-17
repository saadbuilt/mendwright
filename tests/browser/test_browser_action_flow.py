"""From a redesign to the change the GitHub Action would push, on real browsers: Playwright
Chromium and Selenium Chrome (see conftest.py).

A checkout page is redesigned, which breaks three selectors: the fingerprint finds the
checkbox (accept_terms), a stub provider heals the search box (search), and the pay button
is marked destructive (pay). A cookie banner covers the page and cookies='reject' handles
it, and a heal.interrupt() is registered with the checkbox's old selector. The user's test
file runs through pytester with the pytest plugin, which confirms the heals of the passing
test. Then mendwright fix --format json says what the action would write back, and
--apply writes it: the next run heals nothing and passes, and the banner is still handled.
"""

import json
import sys
import types

import pytest

import mendwright
from mendwright import cli
from mw_engines import CHECKOUT_V1 as V1, CHECKOUT_V2 as V2, FAST_MS, journal_events

pytestmark = [pytest.mark.browser, pytest.mark.engines('pw-chromium', 'sel-chrome')]

PLUGIN = ('-p', 'no:mendwright', '-p', 'mendwright.pytest_plugin', '-p', 'no:cacheprovider',
          '-W', 'ignore::pytest.PytestAssertRewriteWarning')

SEARCH = "//input[@name='q']"

CONFTEST = '''
import sys

import pytest

import mendwright

OUTER = sys.modules['_mendwright_action_flow']


@pytest.fixture
def open_heal():
    def make():
        return mendwright.open(OUTER.target, provider=OUTER.provider, store_path=OUTER.store,
                               cookies='reject', timeout_ms=OUTER.fast)
    return make
'''

TEST_FILE = '''import pytest

import mendwright


def test_checkout(open_heal):
    heal = open_heal()
    heal.interrupt("terms_popup", "#accept-terms")
    heal.locator("accept_terms", "#accept-terms", role="checkbox").check()
    heal.locator("search", "#search-box", role="textbox").fill("shoes")
    with pytest.raises(mendwright.AttentionRequired, match="destructive"):
        heal.locator("pay", "#pay", role="button", destructive=True).click()
'''


class MapProvider:
    """Proposes a selector for search only, and records what it is asked."""

    def __init__(self):
        self.keys = []

    def propose(self, bundle):
        self.keys.append(bundle['key'])
        if bundle['key'] != 'search':
            return None
        return {'xpath': SEARCH, 'fallbacks': [], 'reasoning': 'stub'}

    def report_outcome(self, key, xpath, success):
        pass


@pytest.fixture
def project(pytester, monkeypatch, tab):
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    store = pytester.path / '.mendwright' / 'overrides.json'
    outer = types.ModuleType('_mendwright_action_flow')
    outer.target, outer.store = tab.target, str(store)
    outer.provider, outer.fast = MapProvider(), FAST_MS
    monkeypatch.setitem(sys.modules, '_mendwright_action_flow', outer)
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_checkout_flow=TEST_FILE)
    return pytester, outer


def fix(capsys, store, *args):
    capsys.readouterr()                  # what the inner pytest run printed
    code = cli.main(['fix', '--store', store, *args])
    out = capsys.readouterr().out
    assert code == 0, out
    return out


def test_a_redesign_becomes_the_change_the_action_pushes(project, tab, capsys):
    pytester, outer = project
    tab.load(V1)                    # an earlier run, while the selectors worked
    before = mendwright.open(tab.target, provider=outer.provider, store_path=outer.store)
    before.locator('accept_terms', '#accept-terms', role='checkbox').check()

    tab.load(V2)
    result = pytester.runpytest_inprocess(*PLUGIN)
    result.assert_outcomes(passed=1)
    assert tab.text('#consent') == 'rejected'
    assert outer.provider.keys == ['search']          # the fingerprint found accept_terms

    data = json.loads(fix(capsys, outer.store, '--format', 'json'))
    written = {w['key']: w for w in data['written_back']}
    assert sorted(written) == ['accept_terms', 'search']
    terms = written['accept_terms']
    assert terms['found_by']['tier'] == 'fingerprint'
    assert terms['confirmed_by']['by'] == 'test'
    assert terms['confirmed_by']['test'] == 'test_checkout_flow.py::test_checkout'
    assert written['search']['found_by']['tier'] == 'model'
    assert written['search']['found_by']['provider'] == 'MapProvider'
    assert written['search']['confirmed_by']['by'] == 'test'
    assert 'pay' not in json.dumps(data['written_back'])
    assert not [i for i in data['left_alone'] if i.get('key') == 'pay']
    flow = [i for i in data['left_alone'] if i['code'] == 'flow_selector']
    assert [(i['path'], i['line']) for i in flow] == [('test_checkout_flow.py', 8)]

    fix(capsys, outer.store, '--apply')
    source = (pytester.path / 'test_checkout_flow.py').read_text(encoding='utf-8')
    assert 'heal.interrupt("terms_popup", "#accept-terms")' in source
    assert '"#accept-terms", role="checkbox"' not in source and '"#search-box"' not in source
    # Both old selectors are CSS, so each gets its heal's CSS form when it has one.
    for key in ('accept_terms', 'search'):
        assert written[key]['now'] == [written[key]['css'] or written[key]['xpath']]
        assert written[key]['now'][0] in source, key

    heals = len([e for e in journal_events(outer.store) if e['event'] == 'heal.applied'])
    tab.load(V2)
    result = pytester.runpytest_inprocess(*PLUGIN)
    result.assert_outcomes(passed=1)
    assert len([e for e in journal_events(outer.store) if e['event'] == 'heal.applied']) == heals
    assert tab.text('#consent') == 'rejected'
    assert tab.checked("[data-testid='legal-consent']") is True
    assert fix(capsys, outer.store).splitlines()[0] == 'mendwright fix: nothing to write back.'
