"""Evaluate the local selector model on val.jsonl or test.jsonl with greedy decoding.

Two backends:

    python training/evaluate.py --data DATA --split test --adapter RUN/adapter      # base + LoRA (4-bit)
    python training/evaluate.py --data DATA --split test --ollama mendwright-selector

The Ollama run measures the model mendwright actually ships, through the same /api/chat
request LocalModelProvider sends (temperature 0, JSON output).

Metrics, from mendwright.candidates.parse_answer() on each reply:
- positives: correct pick, wrong pick (another candidate), missed (null)
- negatives: correct null, wrong pick
- valid JSON: the share of replies parse_answer() accepts
and the same split by kind, family, role and mutation. Writes <out>.json with the numbers
and <out>.errors.jsonl with every wrong answer.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mendwright.candidates import answer_confidence, parse_answer  # noqa: E402

THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)


def load(path, limit=None):
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    if limit:
        rows = rows[:limit]
    return rows


def hf_predict(rows, base, adapter, batch=8, max_new=16):
    import torch
    from transformers import AutoTokenizer

    from train import load_model
    tok = AutoTokenizer.from_pretrained(base)
    tok.padding_side = 'left'
    model = load_model(base, adapter=adapter, train=False)
    outs = [None] * len(rows)
    times = [0.0] * len(rows)
    order = sorted(range(len(rows)), key=lambda i: len(rows[i]['user']))
    with torch.no_grad():
        for start in range(0, len(order), batch):
            idx = order[start:start + batch]
            texts = [tok.apply_chat_template([{'role': 'system', 'content': rows[i]['system']},
                                              {'role': 'user', 'content': rows[i]['user']}],
                                             tokenize=False, add_generation_prompt=True) for i in idx]
            enc = tok(texts, return_tensors='pt', padding=True, add_special_tokens=False).to('cuda')
            t0 = time.perf_counter()
            gen = model.generate(**enc, max_new_tokens=max_new, do_sample=False, temperature=None,
                                 top_p=None, top_k=None, pad_token_id=tok.pad_token_id)
            dt = (time.perf_counter() - t0) / len(idx)
            for j, i in enumerate(idx):
                outs[i] = tok.decode(gen[j][enc['input_ids'].shape[1]:], skip_special_tokens=True)
                times[i] = dt
            if start % (batch * 25) == 0:
                print('  %d/%d' % (start + len(idx), len(rows)), flush=True)
    return outs, times


def ollama_predict(rows, model, host='http://127.0.0.1:11434', options=None, confs=None):
    outs, times = [], []
    for n, row in enumerate(rows):
        body = {'model': model, 'stream': False, 'format': 'json', 'logprobs': True,
                'messages': [{'role': 'system', 'content': row['system']},
                             {'role': 'user', 'content': row['user']}],
                'options': dict({'temperature': 0, 'num_ctx': 4096, 'num_predict': 24}, **(options or {}))}
        req = urllib.request.Request(host.rstrip('/') + '/api/chat', data=json.dumps(body).encode('utf-8'),
                                     headers={'content-type': 'application/json'})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        times.append(time.perf_counter() - t0)
        outs.append(((data.get('message') or {}).get('content')) or '')
        if confs is not None:
            confs.append(answer_confidence(data.get('logprobs')))
        if n % 50 == 0:
            print('  %d/%d' % (n, len(rows)), flush=True)
    return outs, times


def thresholds(rows, outs, confs):
    """Outcomes when a pick below each confidence threshold counts as no pick, as
    LocalModelProvider(min_confidence=...) does."""
    table = {}
    for t in THRESHOLDS:
        c = defaultdict(int)
        for row, text, conf in zip(rows, outs, confs, strict=True):
            ans = parse_answer(text)
            pick = ans['pick'] if ans else 'invalid'
            if isinstance(pick, int) and conf is not None and conf < t:
                pick = None
            if row['label'] is not None:
                c['pos'] += 1
                c['pos_correct'] += pick == row['label']
                c['pos_wrong'] += isinstance(pick, int) and pick != row['label']
            else:
                c['neg'] += 1
                c['neg_correct'] += pick is None
        table[str(t)] = {'pos_correct': round(c['pos_correct'] / max(c['pos'], 1), 4),
                         'pos_wrong': round(c['pos_wrong'] / max(c['pos'], 1), 4),
                         'neg_correct': round(c['neg_correct'] / max(c['neg'], 1), 4)}
    return table


def score(rows, outs, times):
    total = defaultdict(lambda: defaultdict(int))
    errors = []

    def bump(group, name, field):
        total[group + ':' + str(name)][field] += 1

    for row, text in zip(rows, outs, strict=True):
        ans = parse_answer(text)
        label = row['label']
        groups = [('all', 'all'), ('kind', row['kind']), ('family', row['family']), ('role', row['role']),
                  ('url', row['url_mode'])]
        groups += [('mut', m) for m in row.get('mutations') or ()]
        if row.get('has_fp') is not None:
            groups.append(('fp', row['has_fp']))
        if row.get('fp_tier'):
            # 'unsure' and 'none' are what reaches the model at run time: the fingerprint
            # tier heals the 'right' ones (and the 'wrong' ones, through the gate) first.
            groups.append(('tier', row['fp_tier']))
            groups.append(('reaches_model', row['fp_tier'] in ('unsure', 'none')))
        for flag in ('intent', 'first_use'):
            if row.get(flag):
                groups.append((flag, True))
        if label is not None:
            if ans is None:
                field = 'pos_invalid'
            elif ans['pick'] == label:
                field = 'pos_correct'
            elif ans['pick'] is None:
                field = 'pos_missed'
            else:
                field = 'pos_wrong'
        else:
            if ans is None:
                field = 'neg_invalid'
            elif ans['pick'] is None:
                field = 'neg_correct'
            else:
                field = 'neg_wrong'
        for g, name in groups:
            bump(g, name, field)
            bump(g, name, 'n')
        if field not in ('pos_correct', 'neg_correct'):
            errors.append({'id': row['id'], 'field': field, 'label': label, 'why': row.get('why'),
                           'reply': text, 'kind': row['kind'], 'mutations': row.get('mutations'),
                           'user': row['user']})
    report = {}
    for key, c in sorted(total.items()):
        pos = c['pos_correct'] + c['pos_wrong'] + c['pos_missed'] + c['pos_invalid']
        neg = c['neg_correct'] + c['neg_wrong'] + c['neg_invalid']
        report[key] = {
            'n': c['n'], 'positives': pos, 'negatives': neg,
            'pos_correct': round(c['pos_correct'] / pos, 4) if pos else None,
            'pos_wrong': round(c['pos_wrong'] / pos, 4) if pos else None,
            'pos_missed': round(c['pos_missed'] / pos, 4) if pos else None,
            'neg_correct': round(c['neg_correct'] / neg, 4) if neg else None,
            'neg_wrong': round(c['neg_wrong'] / neg, 4) if neg else None,
            'json_valid': round(1 - (c['pos_invalid'] + c['neg_invalid']) / c['n'], 4) if c['n'] else None,
        }
    times_sorted = sorted(times)
    report['latency'] = {'mean_s': round(sum(times) / len(times), 4),
                         'p50_s': round(times_sorted[len(times) // 2], 4),
                         'p95_s': round(times_sorted[int(len(times) * 0.95)], 4)}
    return report, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description='Evaluate the mendwright local selector model.')
    parser.add_argument('--data', required=True)
    parser.add_argument('--split', default='val')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--base', default=os.environ.get('MW_BASE', r'C:\Users\Omen\mendwright-train\base\Qwen2.5-1.5B-Instruct'))
    parser.add_argument('--adapter')
    parser.add_argument('--ollama', help='Ollama model name')
    parser.add_argument('--host', default='http://127.0.0.1:11434')
    parser.add_argument('--num-gpu', type=int, default=None, help='Ollama num_gpu option (0 = CPU only)')
    parser.add_argument('--out', help='output prefix (default: next to the data)')
    args = parser.parse_args(argv)
    rows = load(Path(args.data) / (args.split + '.jsonl'), args.limit or None)
    started = time.time()
    confs = []
    if args.ollama:
        options = {'num_gpu': args.num_gpu} if args.num_gpu is not None else None
        outs, times = ollama_predict(rows, args.ollama, args.host, options, confs)
        who = 'ollama-' + args.ollama.replace(':', '_').replace('/', '_')
    else:
        outs, times = hf_predict(rows, args.base, args.adapter)
        who = 'hf-' + Path(args.adapter).parent.name if args.adapter else 'hf-base'
    report, errors = score(rows, outs, times)
    report['seconds'] = round(time.time() - started, 1)
    if confs and any(c is not None for c in confs):
        report['thresholds'] = thresholds(rows, outs, confs)
        by_id = {row['id']: conf for row, conf in zip(rows, confs, strict=True)}
        for e in errors:
            e['confidence'] = by_id.get(e['id'])
    out = Path(args.out) if args.out else Path(args.data) / ('eval-' + who + '-' + args.split)
    out.parent.mkdir(parents=True, exist_ok=True)
    Path(str(out) + '.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with open(str(out) + '.errors.jsonl', 'w', encoding='utf-8') as fh:
        for e in errors:
            fh.write(json.dumps(e, ensure_ascii=False) + '\n')
    a = report['all:all']
    print(json.dumps({'split': args.split, 'n': a['n'], 'pos_correct': a['pos_correct'], 'pos_wrong': a['pos_wrong'],
                      'pos_missed': a['pos_missed'], 'neg_correct': a['neg_correct'], 'neg_wrong': a['neg_wrong'],
                      'json_valid': a['json_valid'], 'latency': report['latency']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
