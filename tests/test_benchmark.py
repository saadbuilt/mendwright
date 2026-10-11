"""The accuracy benchmark in benchmarks/: its cases are well formed, and its harness grades
what it is given.

- Every case: the selector matches one element on v1 and none on v2; the truth matches one
  element on v2 (or is null); that element keeps the role mendwright learns on v1 and is
  not hidden; the pages stay small and load nothing from the network.
- The grader: the oracle (it proposes the truth) gets every positive case right and
  declines every negative one; the none baseline misses every positive case and declines
  every negative one; a proposer that picks another element is graded wrong.

Everything runs offline on StandInPage, with no model and no browser.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / 'benchmarks'

if not (BENCH / 'run.py').is_file():
    pytest.skip('benchmarks/ is not in this copy of the project (the sdist leaves it out)',
                allow_module_level=True)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load('mendwright_benchmark_run', BENCH / 'run.py')
report = _load('mendwright_benchmark_report', BENCH / 'report.py')

CASES = bench.load_cases()
NEGATIVE_CATEGORIES = ('removed', 'ambiguous', 'removed_near_danger')
POSITIVE = [c for c in CASES if c.positive]
NEGATIVE = [c for c in CASES if not c.positive]
BY_ID = {c.id: c for c in CASES}


# --- the corpus ------------------------------------------------------------------------------

def test_the_corpus_has_the_size_and_spread_the_docs_describe():
    assert 36 <= len(CASES) <= 44
    assert 6 <= len(NEGATIVE) <= 10
    categories = {c.category for c in CASES}
    assert categories == set(bench.CATEGORIES)
    for name in bench.CATEGORIES:
        assert sum(c.category == name for c in CASES) >= 2, name


@pytest.mark.parametrize('case', CASES, ids=lambda c: c.id)
def test_case_is_well_formed(case):
    folder = BENCH / 'cases' / case.id
    meta = json.loads((folder / 'case.json').read_text(encoding='utf-8'))
    assert meta['id'] == folder.name
    assert {'id', 'category', 'description', 'key', 'selector', 'truth'} <= set(meta)
    assert set(meta) <= {'id', 'category', 'description', 'key', 'selector', 'truth', 'role', 'destructive'}
    assert case.description.strip() and case.key.strip()
    assert case.category in bench.CATEGORIES
    assert case.positive == (case.category not in NEGATIVE_CATEGORIES)

    v1, v2 = bench.parse(case.v1), bench.parse(case.v2)
    before = bench.select(v1, case.selector)
    assert len(before) == 1, 'the selector should match one element on v1'
    assert bench.select(v2, case.selector) == [], 'the selector should match nothing on v2'
    if bench.role_of(before[0]) != 'checkbox':
        assert not bench.is_hidden(before[0])
    if case.truth is not None:
        right = bench.select(v2, case.truth)
        assert len(right) == 1, 'the truth should match one element on v2'
        # The gate only accepts an element of the role learned on v1, and a visible one.
        assert bench.role_of(right[0]) == bench.role_of(before[0])
        assert not bench.is_hidden(right[0])
        assert bench.select(v2, case.truth)[0] is right[0]


@pytest.mark.parametrize('case', CASES, ids=lambda c: c.id)
def test_case_pages_behave_the_same_in_a_browser_and_on_standinpage(case):
    for name, html in (('v1', case.v1), ('v2', case.v2)):
        assert len(html.encode('utf-8')) < 25_000, name + ' is over 25 KB'
        doc = bench.parse(html)
        # A browser adds <tbody> to a table that has none; lxml does not.
        assert doc.xpath('//table/tr') == [], name + ': write <tbody> in tables'
        # Elements whose visibility or content a browser treats differently from lxml.
        assert doc.xpath('//template | //details | //dialog | //noscript') == [], name
        # Nothing is fetched from the network when the page loads.
        for attr in doc.xpath('//script/@src | //img/@src | //link/@href | //iframe/@src'):
            assert not attr.startswith(('http:', 'https:', '//')), name + ': ' + attr
        # Hidden things are hidden inline, which StandInPage understands too.
        for style in doc.xpath('//style/text()'):
            assert 'display:none' not in style.replace(' ', ''), name


def test_negative_cases_cover_removal_ambiguity_and_a_dangerous_neighbour():
    assert {c.category for c in NEGATIVE} == set(NEGATIVE_CATEGORIES)


# --- the grader --------------------------------------------------------------------------------

def test_the_oracle_gets_every_positive_case_and_declines_every_negative():
    records = bench.run_cases(CASES, lambda case: bench.OracleProposer(case.truth))
    outcomes = {r['id']: r['outcome'] for r in records}
    assert all(outcomes[c.id] == 'correct' for c in POSITIVE), outcomes
    assert all(outcomes[c.id] == 'declined' for c in NEGATIVE), outcomes
    for r in records:
        assert r['note'] is None
        assert r['model_calls'] == 1
        assert r['prompt_chars'] > 1000 and r['est_tokens'] == round(r['prompt_chars_total'] / 4)


def test_the_none_baseline_misses_every_positive_case_and_declines_every_negative():
    records = bench.run_cases(CASES, bench.NoneProposer())
    for r in records:
        assert r['outcome'] == ('missed' if r['expected'] == 'heal' else 'declined'), r['id']
        assert r['healed_xpath'] is None
        assert 'never proposes' in r['decline_reason']
        assert r['provider_error'] is False
    summary = bench.summarize(records)
    assert (summary['correct'], summary['wrong'], summary['wrong_negative']) == (0, 0, 0)
    assert summary['missed'] == len(POSITIVE) and summary['declined'] == len(NEGATIVE)


class OtherElement:
    """Proposes a visible element of the right role that is not the truth: a wrong pick
    that mendwright's checks cannot tell from the right one."""

    def __init__(self, case):
        self.case = case
        self.last_error = None

    def propose(self, bundle):
        doc = bench.parse(bundle['html'])
        truth = bench.select(doc, self.case.truth)[0] if self.case.truth else None
        for el in doc.iter():
            if (isinstance(el.tag, str) and el is not truth and not bench.is_hidden(el)
                    and bench.role_of(el) == bundle['expectedRole']):
                return {'xpath': bench.robust_xpath(doc, el)}
        return None

    def report_outcome(self, key, xpath, success):
        pass


@pytest.mark.parametrize('case_id', ['near-dup-product-add-to-cart', 'near-dup-team-row-edit',
                                     'attr-churn-coupon-apply'])
def test_a_heal_onto_another_element_is_graded_wrong(case_id):
    case = BY_ID[case_id]
    record = bench.run_case(case, OtherElement(case), bench.StandInPage())
    assert record['outcome'] == 'wrong'
    assert record['healed_target'] == 'other'
    assert record['proposals'][0]['result'] == 'applied'


@pytest.mark.parametrize('case_id', ['neg-danger-save-profile', 'neg-ambiguous-invite-accept'])
def test_a_heal_on_a_negative_case_is_graded_wrong_negative(case_id):
    case = BY_ID[case_id]
    record = bench.run_case(case, OtherElement(case), bench.StandInPage())
    assert record['outcome'] == 'wrong_negative'
    assert record['healed_xpath']


class HiddenFirst:
    """Proposes the hidden copy first, then the truth: the live check should refuse the
    first, and the re-prompt should land on the second."""

    def __init__(self, hidden, truth):
        self.answers = [hidden, truth]
        self.last_error = None

    def propose(self, bundle):
        return {'xpath': self.answers.pop(0)}

    def report_outcome(self, key, xpath, success):
        pass


def test_a_wrong_pick_the_gate_refuses_is_counted_and_the_retry_is_graded():
    case = BY_ID['hidden-dup-sticky-add-to-cart']
    hidden = "//div[@data-testid='sticky-buy-bar']//button"
    record = bench.run_case(case, HiddenFirst(hidden, case.truth), bench.StandInPage())
    assert record['outcome'] == 'correct'
    assert record['model_calls'] == 2
    assert record['gate_caught_wrong'] == 1
    first, second = record['proposals']
    assert first['target'] == 'other' and 'live page' in first['result']
    assert second['target'] == 'truth' and second['result'] == 'applied'
    assert record['prompt_chars_total'] > record['prompt_chars']


def test_the_similarity_baseline_does_not_depend_on_the_truth():
    for case_id in ('near-dup-cart-remove', 'id-renamed-login-email', 'neg-removed-quarterly-report'):
        case = BY_ID[case_id]
        first = bench.run_case(case, bench.SimilarityProposer(), bench.StandInPage())
        decoy = bench.Case(**{**case.__dict__, 'truth': None})
        second = bench.run_case(decoy, bench.SimilarityProposer(), bench.StandInPage())
        assert first['healed_xpath'] == second['healed_xpath']
        assert [p['xpath'] for p in first['proposals']] == [p['xpath'] for p in second['proposals']]


def test_a_provider_error_is_flagged_and_secrets_are_scrubbed(monkeypatch):
    monkeypatch.setenv('BENCH_TEST_API_KEY', 'sk-bench-0123456789abcdef')

    class Failing:
        last_error = None

        def propose(self, bundle):
            self.last_error = 'HTTP 401 for key sk-bench-0123456789abcdef'
            return None

        def report_outcome(self, key, xpath, success):
            pass

    record = bench.run_case(BY_ID['id-renamed-login-email'], Failing(), bench.StandInPage())
    assert record['outcome'] == 'missed'
    assert record['provider_error'] is True
    assert 'sk-bench-0123456789abcdef' not in json.dumps(record)
    assert '***' in record['decline_reason']


def test_a_run_whose_first_call_fails_stops_and_writes_nothing(tmp_path, monkeypatch, capsys):
    class Unreachable:
        model = 'test-model'
        base_url = 'http://127.0.0.1:9/v1'
        api_key = 'sk-bench-0123456789abcdef'
        last_error = None

        def propose(self, bundle):
            self.last_error = 'could not connect to http://127.0.0.1:9/v1 (connection refused).'
            return None

        def report_outcome(self, key, xpath, success):
            pass

    monkeypatch.setattr(bench, 'build_provider', lambda name, model=None: Unreachable())
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://127.0.0.1:9/v1')
    out = tmp_path / 'run.json'
    assert bench.main(['--provider', 'openai', '--out', str(out)]) == 2
    captured = capsys.readouterr()
    assert 'endpoint: http://127.0.0.1:9/v1' in captured.out
    assert 'note: OPENAI_BASE_URL is set' in captured.out
    assert 'sk-bench' not in captured.out + captured.err
    assert 'the first provider call failed' in captured.err
    assert captured.err.count('] ') == 1   # one case ran, then it stopped
    assert not out.exists()


def test_the_command_line_writes_results_and_the_report_reads_them(tmp_path, capsys):
    out = tmp_path / 'similarity.json'
    assert bench.main(['--provider', 'similarity', '--cases', 'near-dup-*', '--out', str(out)]) == 0
    printed = capsys.readouterr().out
    assert '| similarity | standin |' in printed
    assert 'endpoint' not in printed   # a baseline makes no network call
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['format'] == 1 and data['provider_class'] == 'SimilarityProposer'
    assert data['case_count'] == 4 and len(data['records']) == 4
    assert data['corpus'] == bench.corpus_hash([c for c in CASES if c.id.startswith('near-dup-')])
    assert data['summary']['positives'] == 4

    md = tmp_path / 'RESULTS.md'
    assert report.main([str(out), '--out', str(md)]) == 0
    text = md.read_text(encoding='utf-8')
    assert '| similarity baseline | StandInPage |' in text
    assert 'python benchmarks/run.py --provider anthropic --model <model id>' in text
    assert '—' not in text


def test_the_published_baseline_runs_are_for_the_current_cases():
    files = sorted((BENCH / 'results').glob('*.json'))
    baselines = [json.loads(f.read_text(encoding='utf-8')) for f in files]
    baselines = [r for r in baselines if r['provider'] in bench.BASELINES]
    assert {r['provider'] for r in baselines} == set(bench.BASELINES)
    for run in baselines:
        assert run['case_count'] == len(CASES)
        assert run['corpus'] == bench.corpus_hash(CASES), \
            'the cases changed since this baseline ran: rerun it, then python benchmarks/report.py'


def test_the_published_results_page_matches_the_results_files():
    files = sorted((BENCH / 'results').glob('*.json'))
    assert files, 'benchmarks/results/ should hold the baseline runs'
    expected = report.render(report.load(files))
    assert (BENCH / 'RESULTS.md').read_text(encoding='utf-8') == expected, \
        'benchmarks/RESULTS.md is stale: run python benchmarks/report.py'
