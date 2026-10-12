"""Combine benchmark results files into benchmarks/RESULTS.md.

    python benchmarks/report.py                      # every file in benchmarks/results/
    python benchmarks/report.py a.json b.json        # just these
    python benchmarks/report.py --out somewhere.md

Each results file is one run of benchmarks/run.py. The page lists every run in one table,
then each run on its own: the counts per category and every wrong heal with its case id.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / 'results'
DEFAULT_OUT = HERE / 'RESULTS.md'

BASELINES = ('similarity', 'none')

CATEGORY_NAMES = {
    'id_renamed': 'id renamed or removed',
    'hashed_class': 'hashed class churn',
    'wrapped': 'wrapped in a new element',
    'label_reworded': 'label text reworded',
    'moved': 'moved to another region',
    'attribute_churn': 'attribute churn',
    'tag_change': 'tag change, same role',
    'near_duplicate': 'near-duplicates',
    'hidden_duplicate': 'hidden duplicate',
    'translated': 'translated text',
    'field_order': 'form fields reordered',
    'removed': 'removed (negative)',
    'ambiguous': 'ambiguous duplicates (negative)',
    'removed_near_danger': 'removed, dangerous control nearby (negative)',
    'flow': 'step moved to another page (negative)',
}

COMMANDS = """```bash
python benchmarks/run.py --provider anthropic --model <model id>
python benchmarks/run.py --provider openai --model <model id>
python benchmarks/run.py --provider gemini --model <model id>
python benchmarks/run.py --provider ollama --model <model name>
python benchmarks/report.py
```"""


def load(paths):
    runs = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('format') != 1 or 'summary' not in data:
            raise SystemExit('report: ' + str(path) + ' is not a results file from benchmarks/run.py')
        data['_file'] = Path(path).name
        runs.append(data)
    # Model runs first, newest first. Then the baselines in a fixed order: similarity before
    # none, StandInPage before a browser.
    def baseline_order(run):
        return BASELINES.index(run['provider']), run['page'] != 'standin', run['page'], run['date']

    models = sorted((r for r in runs if r['provider'] not in BASELINES), key=lambda r: r['date'], reverse=True)
    baselines = sorted((r for r in runs if r['provider'] in BASELINES), key=baseline_order)
    return models + baselines


def who(run):
    if run['provider'] in BASELINES:
        return run['provider'] + ' baseline'
    return run['provider'] + (' (' + run['model'] + ')' if run.get('model') else '')


def page(run):
    return 'StandInPage' if run['page'] == 'standin' else 'headless ' + run['page']


def pct(part, whole):
    return '-' if not whole else format(100.0 * part / whole, '.1f') + '%'


def latency(ms):
    if ms is None:
        return '-'
    if ms < 1:
        return 'under 1 ms'
    if ms < 1000:
        return format(ms, '.0f') + ' ms'
    return format(ms / 1000.0, '.1f') + ' s'


def prompt(s):
    if not s.get('avg_prompt_chars'):
        return '-'
    return format(s['avg_prompt_chars'], ',') + ' (~' + format(s['avg_prompt_tokens_est'], ',') + ')'


def summary_table(runs):
    rows = ['| Provider | Page | Date | Correct | Wrong | Missed | Negatives declined | Wrong on negatives '
            '| Avg model calls | Avg latency | Avg prompt chars (est. tokens) |',
            '|---|---|---|---|---|---|---|---|---|---|---|']
    for run in runs:
        s = run['summary']
        rows.append('| ' + ' | '.join([
            who(run), page(run), run['date'][:10],
            str(s['correct']) + '/' + str(s['positives']) + ' (' + pct(s['correct'], s['positives']) + ')',
            str(s['wrong']) + ' (' + pct(s['wrong'], s['positives']) + ')',
            str(s['missed']) + ' (' + pct(s['missed'], s['positives']) + ')',
            str(s['declined']) + '/' + str(s['negatives']),
            str(s['wrong_negative']) + '/' + str(s['negatives']),
            format(s['avg_model_calls'], '.2f') if s['avg_model_calls'] is not None else '-',
            latency(s['avg_model_ms']),
            prompt(s),
        ]) + ' |')
    return '\n'.join(rows)


def category_table(run):
    rows = ['| Category | Cases | Correct | Wrong | Missed | Declined | Wrong on negatives |',
            '|---|---|---|---|---|---|---|']
    for name, counts in run['by_category'].items():
        rows.append('| ' + ' | '.join([
            CATEGORY_NAMES.get(name, name), str(counts['cases']), str(counts['correct']),
            str(counts['wrong']), str(counts['missed']), str(counts['declined']),
            str(counts['wrong_negative'])]) + ' |')
    return '\n'.join(rows)


def wrong_heals(run):
    lines = []
    for r in run['records']:
        if r['outcome'] not in ('wrong', 'wrong_negative'):
            continue
        if r['outcome'] == 'wrong_negative':
            kind = 'negative case, should not heal'
        else:
            kind = 'reached another element'
        run_note = (' (run ' + str(r['run']) + ')') if run.get('repeat', 1) > 1 else ''
        lines.append('- `' + r['id'] + '`' + run_note + ': healed to `' + str(r['healed_xpath'])
                     + '` (' + kind + ').')
    return lines


def run_section(run):
    s = run['summary']
    out = ['### ' + who(run) + ', ' + page(run) + ', ' + run['date'][:10], '']
    out.append('mendwright ' + run['mendwright'] + ', Python ' + run['python'] + ', ' + run['platform']
               + '. Provider class ' + run['provider_class']
               + ((', model ' + run['model']) if run.get('model') else '')
               + ((', endpoint ' + run['endpoint']) if run.get('endpoint') else '') + '.')
    out.append(str(run['case_count']) + ' cases' + ((', each run ' + str(run['repeat']) + ' times')
                                                     if run.get('repeat', 1) > 1 else '')
               + ((' (corpus ' + run['corpus'] + ')') if run.get('corpus') else '')
               + '. Results file: `benchmarks/results/' + run['_file'] + '`.')
    out.append('')
    out.append('Wrong heals: ' + str(s['wrong_heals']) + ' of ' + str(s['cases']) + ' cases. '
               'Wrong proposals the gate stopped: ' + str(s['gate_caught_wrong']) + '. '
               'Prompt text for one pass over the cases: about ' + format(s['run_tokens_est'], ',')
               + ' input tokens (characters / ' + str(run.get('chars_per_token', 4)) + ', an estimate).')
    if s.get('provider_errors'):
        out.append('')
        out.append(str(s['provider_errors']) + ' case(s) were declined because a provider call failed. '
                   'They count as missed or declined above.')
    if s.get('errors'):
        out.append('')
        out.append(str(s['errors']) + ' case(s) could not run (outcome "error" in the results file).')
    out += ['', category_table(run), '']
    heals = wrong_heals(run)
    if heals:
        out += ['Wrong heals:', ''] + heals + ['']
    else:
        out += ['No wrong heals.', '']
    return out


def render(runs):
    models = [r for r in runs if r['provider'] not in BASELINES]
    cases = max((r['case_count'] for r in runs), default=0)
    first = runs[0]['summary'] if runs else {'positives': 0, 'negatives': 0}
    out = [
        '# Benchmark results',
        '',
        'How often does a heal land on the right element, and how often does mendwright accept a '
        'wrong one? [docs/benchmark.md](../docs/benchmark.md) explains the cases, the grading and '
        'the caveats. This page is generated by `python benchmarks/report.py` from the files in '
        '`benchmarks/results/`.',
        '',
        'The corpus has ' + str(cases) + ' cases: ' + str(first['positives']) + ' where a heal is the '
        'right answer and ' + str(first['negatives']) + ' where the right answer is to not heal.',
        '',
        '## Model runs',
        '',
    ]
    if models:
        out.append('Each model row is one run on one date. Results vary between runs and model '
                   'versions, so compare rows from the same date where you can.')
    else:
        out.append('None yet. The rows below are the two model-free baselines, which show what the '
                   'harness and the gate do without a model. Model numbers will be added when the '
                   'runs below have been made.')
    out += ['', 'To add a model run (the key and endpoint come from your environment, as in '
            '`mendwright doctor`):', '', COMMANDS, '', '## All runs', '', summary_table(runs), '',
            '"Wrong" counts heals that landed on another element in a case where a heal was right. '
            '"Wrong on negatives" counts heals in cases where the right answer was to not heal. '
            'Both are heals mendwright accepted, after its checks, onto an element the step did '
            'not mean.', '',
            'The similarity baseline records the element while the old selector still matches, '
            'and later picks the most similar element on the new page. A model gets less: the '
            'key, the old selector and the new page, never the old page.', '',
            '## Each run', '']
    for run in runs:
        out += run_section(run)
    return '\n'.join(out).rstrip() + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python benchmarks/report.py',
                                     description='Write benchmarks/RESULTS.md from results files.')
    parser.add_argument('files', nargs='*', help='results files (default: benchmarks/results/*.json)')
    parser.add_argument('--out', default=str(DEFAULT_OUT),
                        help='where to write (default: benchmarks/RESULTS.md)')
    args = parser.parse_args(argv)
    paths = args.files or sorted(str(p) for p in RESULTS_DIR.glob('*.json'))
    if not paths:
        print('report: no results files. Run benchmarks/run.py first.', file=sys.stderr)
        return 1
    text = render(load(paths))
    Path(args.out).write_text(text, encoding='utf-8', newline='\n')
    print('Wrote ' + args.out + ' from ' + str(len(paths)) + ' results file(s).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
