"""Build the training, validation and test sets by running every scenario through
mendwright's real pipeline.

For each scenario (gen_scenarios.make):

1. mendwright.open() on a StandInPage showing v1 at its URL, and heal.locator(key,
   selectors) so the selectors match and mendwright learns the role, the page and
   whatever else it records for the key.
2. The page switches to v2 and heal.locator() runs again. The selectors miss, so the
   coordinator builds the capture bundle and asks the provider, a CaptureProvider that
   keeps the bundle and returns nothing. heal_across_pages=True so the page guard does not
   decline first; the bundle still names the real page and lastPage.
3. The truth marker is taken out of the bundle's HTML, mendwright.candidates.extract()
   builds the candidate list, format_task() the messages, and the label is the truth's
   number in the list (or null with the scenario's reason).

A positive whose truth is not in the list is dropped and counted as a recall miss. For
each positive the first XPath robust_xpaths() builds for the truth is also checked
against v2 with mendwright's gate (validation.validate), and the share that pass is
reported.

    python training/build_dataset.py --out C:/path/data --train 32000 --val 2000 --test 3000

Writes train.jsonl, val.jsonl, test.jsonl and stats.json.
"""

import argparse
import json
import multiprocessing as mp
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

for _name in list(os.environ):
    if _name.startswith(('ANTHROPIC_', 'OPENAI_', 'GEMINI_', 'MENDWRIGHT_', 'OLLAMA_')):
        del os.environ[_name]

import lxml.html  # noqa: E402

import mendwright  # noqa: E402
from mendwright.candidates import (answer_text, extract, format_task, robust_xpaths,  # noqa: E402
                                   step_words, tokens)
from mendwright.demo import StandInPage  # noqa: E402
from mendwright.exceptions import AttentionRequired  # noqa: E402
from mendwright.validation import validate  # noqa: E402

from gen_scenarios import TRUTH_ATTR, make  # noqa: E402
from lexicon import CONCEPTS  # noqa: E402
from pages import srng  # noqa: E402

# Share of examples shown without the saved fingerprint, as for a key healed before
# mendwright recorded one. Only steps whose key, selectors or text still say what the
# element is are dropped this way: without the fingerprint the others cannot be answered.
NO_FINGERPRINT = 0.2


def _concept_words(sc):
    c = CONCEPTS[sc['concept']]
    words = set()
    for slug in c.slugs:
        words.update(tokens(slug))
    for variants in c.words.values():
        for v in variants:
            words.update(tokens(v))
    words.update(tokens(sc.get('item') or ''))
    return words


def informative(sc, bundle):
    """True when the step without its fingerprint still names the element: its key,
    selectors or expected text share a word with the control's meaning or its item."""
    words = step_words(dict(bundle, fingerprint=None))
    return bool(words & _concept_words(sc))

_MARK = re.compile(r'\s' + TRUTH_ATTR + r'="1"')
SPLIT_BASE = {'train': 0, 'val': 50_000_000, 'test': 90_000_000}


class CaptureProvider:
    """Keeps the bundle of the first propose() call and proposes nothing."""

    model = None

    def __init__(self):
        self.bundle = None
        self.last_error = None

    def propose(self, bundle):
        if self.bundle is None:
            self.bundle = dict(bundle)
        self.last_error = 'captured for training.'
        return None

    def report_outcome(self, key, xpath, success):
        return None


def capture(sc):
    """Run the scenario through mendwright and return the captured bundle, or (None,
    reason)."""
    folder = tempfile.mkdtemp(prefix='mw-ds-')
    try:
        page = StandInPage(url=sc['v1_url'] or 'about:blank')
        page.set_content(sc['v1'])
        provider = CaptureProvider()
        heal = mendwright.open(page, provider=provider, store_path=os.path.join(folder, 'o.json'),
                               dry_run=False, timeout_ms=0, rate_per_key_per_hour=None,
                               rate_global_per_hour=None, heal_across_pages=True)
        heal.coordinator.log = lambda line: None
        kwargs = {'poll_ms': 0}
        if sc['pass_role']:
            kwargs['role'] = sc['role']
        if sc['text']:
            kwargs['text'] = sc['text']
        selector = sc['selectors'] if len(sc['selectors']) > 1 else sc['selectors'][0]
        try:
            heal.locator(sc['key'], selector, **kwargs)
        except AttentionRequired:
            return None, 'v1 miss'
        page.url = sc['v2_url'] or 'about:blank'
        page.set_content(sc['v2'])
        try:
            heal.locator(sc['key'], selector, **kwargs)
            return None, 'v2 still matches'
        except AttentionRequired:
            pass
        if provider.bundle is None:
            return None, 'no heal asked'
        return provider.bundle, None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def example(sc, bundle):
    """The training record for a scenario and its bundle, plus diagnostics."""
    html = bundle.get('html') or ''
    marked = lxml.html.document_fromstring(html)
    truths = marked.xpath('//*[@' + TRUTH_ATTR + ']')
    bundle = dict(bundle, html=_MARK.sub('', html))
    info = {'recall_miss': False, 'xpath_ok': None, 'gate_ok': None}
    if bundle.get('fingerprint') and srng(sc['id'], 'nofp').random() < NO_FINGERPRINT \
            and informative(sc, bundle):
        bundle['fingerprint'] = None
    path = None
    if sc['positive']:
        if len(truths) != 1:
            info['recall_miss'] = 'no truth in bundle'
            return None, info
        path = truths[0].getroottree().getpath(truths[0])
    cands = extract(bundle)
    pick = None
    if path is not None:
        hits = cands.doc.xpath(path)
        el = hits[0] if hits else None
        pick = cands.number_of(el) if el is not None else None
        if pick is None:
            info['recall_miss'] = 'truth not in candidates (' + str(cands.total) + ' of role)'
            return None, info
        if cands[pick - 1].features.get('hidden') == 'hidden' and bundle.get('expectedRole') != 'checkbox':
            # The live check refuses a hidden element, so this is not a healable positive:
            # a generator slip, not something to teach.
            info['invalid'] = 'truth hidden'
            return None, info
        xpaths = robust_xpaths(el, cands.doc, limit=4)
        info['xpath_ok'] = bool(xpaths)
        if xpaths:
            full = _MARK.sub('', sc['v2'])
            res = validate(xpaths[0], full, expected_role=bundle.get('expectedRole'))
            ok = res.ok
            if ok:
                full_doc = lxml.html.document_fromstring(sc['v2'])
                hit = full_doc.xpath(xpaths[0])
                ok = len(hit) == 1 and hit[0].get(TRUTH_ATTR) == '1'
            info['gate_ok'] = ok
            if not ok:
                info['gate_detail'] = xpaths[0] + ' -> ' + res.reason
    why = sc['why']
    if pick is not None and len(cands) > 1 and not bundle.get('fingerprint') \
            and not informative(sc, bundle):
        # Nothing in the step says which element it is (a key such as 'step3', a positional
        # or id-only selector, no saved fingerprint): the right answer is that several fit.
        pick, why = None, 'ambiguous'
        info['relabelled'] = True
    system, user = format_task(bundle, cands)
    answer = answer_text(pick, None if pick else why)
    rec = {
        'id': sc['id'], 'split': sc['split'], 'family': sc['family'], 'kind': sc['kind'],
        'mutations': sc['mutations'], 'url_mode': sc['url_mode'], 'lang1': sc['lang1'],
        'lang2': sc['lang2'], 'role': bundle.get('expectedRole'), 'n_cand': len(cands),
        'total': cands.total, 'label': pick, 'why': None if pick else why,
        'has_fp': bool(bundle.get('fingerprint')), 'twin_of': sc.get('twin_of'),
        'system': system, 'user': user, 'answer': answer,
    }
    return rec, info


def work(args):
    split, seed = args
    out = []
    try:
        scenarios = make(seed, split)
    except Exception as exc:   # a generator bug: count it, keep going
        return [('gen_error', split, type(exc).__name__ + ': ' + str(exc)[:200], None)]
    for sc in scenarios:
        try:
            bundle, why = capture(sc)
        except Exception as exc:
            out.append(('capture_error', split, type(exc).__name__ + ': ' + str(exc)[:200], sc['id']))
            continue
        if bundle is None:
            out.append(('skip', split, why, sc['id']))
            continue
        try:
            rec, info = example(sc, bundle)
        except Exception as exc:
            out.append(('example_error', split, type(exc).__name__ + ': ' + str(exc)[:200], sc['id']))
            continue
        out.append(('ok', split, rec, info))
    return out


def build(out_dir, sizes, workers, start=0):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    for split, want in sizes.items():
        if not want:
            continue
        started = time.time()
        counts = Counter()
        details = defaultdict(Counter)
        recs = []
        seed = SPLIT_BASE[split] + start
        with mp.Pool(workers) as pool:
            while len(recs) < want:
                need = want - len(recs)
                batch = [(split, s) for s in range(seed, seed + max(64, int(need * 1.4)))]
                seed += len(batch)
                for results in pool.imap_unordered(work, batch, chunksize=8):
                    for status, _split, payload, info in results:
                        counts[status] += 1
                        if status == 'ok':
                            rec = payload
                            if info.get('recall_miss'):
                                counts['recall_miss'] += 1
                                details['recall_miss'][info['recall_miss'][:40]] += 1
                                continue
                            if info.get('invalid'):
                                counts['invalid'] += 1
                                details['invalid'][info['invalid']] += 1
                                continue
                            if info.get('xpath_ok') is False:
                                counts['xpath_none'] += 1
                            if info.get('gate_ok') is False:
                                counts['gate_fail'] += 1
                                details['gate_fail'][info.get('gate_detail', '')[-60:]] += 1
                            if info.get('gate_ok'):
                                counts['gate_ok'] += 1
                            recs.append(rec)
                        elif status != 'skip':
                            details[status][str(payload)[:120]] += 1
                        else:
                            details['skip'][payload] += 1
        recs.sort(key=lambda r: r['id'])
        recs = recs[:want]
        with open(out_dir / (split + '.jsonl'), 'w', encoding='utf-8') as fh:
            for rec in recs:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        pos = [r for r in recs if r['label'] is not None]
        positives_seen = len(pos) + counts['recall_miss']
        stats[split] = {
            'examples': len(recs), 'positives': len(pos), 'negatives': len(recs) - len(pos),
            'recall': round(1 - counts['recall_miss'] / positives_seen, 5) if positives_seen else None,
            'recall_misses': counts['recall_miss'],
            'xpath_none': counts['xpath_none'], 'gate_ok': counts['gate_ok'], 'gate_fail': counts['gate_fail'],
            'counts': dict(counts), 'details': {k: dict(v.most_common(15)) for k, v in details.items()},
            'kinds': dict(Counter(r['kind'] for r in recs)),
            'families': dict(Counter(r['family'] for r in recs)),
            'roles': dict(Counter(r['role'] for r in recs)),
            'n_cand_max': max((r['n_cand'] for r in recs), default=0),
            'with_fingerprint': sum(1 for r in recs if r['has_fp']),
            'seconds': round(time.time() - started, 1),
        }
        print(split, json.dumps({k: v for k, v in stats[split].items() if k not in ('details',)}), flush=True)
    with open(out_dir / 'stats.json', 'w', encoding='utf-8') as fh:
        json.dump(stats, fh, indent=2, ensure_ascii=False)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build the local model dataset through mendwright.')
    parser.add_argument('--out', required=True)
    parser.add_argument('--train', type=int, default=32000)
    parser.add_argument('--val', type=int, default=2000)
    parser.add_argument('--test', type=int, default=3000)
    parser.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument('--start', type=int, default=0, help='first seed offset (for a fresh sample)')
    args = parser.parse_args(argv)
    build(args.out, {'val': args.val, 'test': args.test, 'train': args.train}, args.workers, args.start)
    return 0


if __name__ == '__main__':
    sys.exit(main())
