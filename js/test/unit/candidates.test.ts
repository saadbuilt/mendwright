// The local selector model's task against the Python package's: for each page after a
// redesign in the benchmark (benchmarks/cases), the same candidate lines, the same task text
// and the same XPaths for each candidate, so the model sees what it was trained on. Also the
// page cleaning: the Python package reads the page this package cleaned into the same task.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { redact } from '../../src/capture';
import type { Bundle } from '../../src/prompt';
import { answerConfidence, chooseCandidates, formatTask, parseAnswer } from '../../src/providers/candidates';
import { REPO, needsPython, runPython } from '../support/python';

const CASES = path.join(REPO, 'benchmarks', 'cases');
const skip = needsPython() || (fs.existsSync(CASES) ? false : 'benchmarks/cases is not in this copy');

const SCRIPT = `
import json, sys, glob, os
import lxml.html
from mendwright.capture import build_bundle
from mendwright.candidates import extract, format_task, robust_xpaths
from mendwright.fingerprint import of_element
from mendwright.validation import _role_of
out = []
for case_dir in sorted(glob.glob(os.path.join(json.load(sys.stdin), '*'))):
    case = json.load(open(os.path.join(case_dir, 'case.json'), encoding='utf-8'))
    v1 = open(os.path.join(case_dir, 'v1.html'), encoding='utf-8').read()
    v2 = open(os.path.join(case_dir, 'v2.html'), encoding='utf-8').read()
    sel = case['selector']
    xp = "//*[@id='" + sel[1:] + "']" if sel.startswith('#') else sel
    el = lxml.html.fromstring(v1).xpath(xp)[0]
    meta = {'expectedRole': _role_of(el), 'expectedText': [], 'fingerprint': of_element(el), 'intent': None}
    bundle = build_bundle(case['key'], meta, [{'xpath': sel}], [], v2,
                          {'url': None, 'page': '/now', 'lastPage': '/before'}, with_fingerprint=True)
    cands = extract(bundle)
    system, user = format_task(bundle, cands)
    out.append({'id': case['id'], 'v2': v2, 'bundle': bundle, 'user': user,
                'xpaths': [robust_xpaths(c.element, cands.doc, limit=4) for c in cands]})
print(json.dumps(out))
`;

function lib(html = '<html><body></body></html>') {
  const dom = new JSDOM(html, { runScripts: 'outside-only' });
  return dom.window.eval('(' + mendwrightLib.toString() + ')()') as ReturnType<typeof mendwrightLib>;
}

describe('the local model task on the benchmark pages', { skip }, () => {
  const cases = skip ? [] : runPython<{ id: string; v2: string; bundle: Bundle; user: string; xpaths: string[][] }[]>(SCRIPT, CASES);
  const page = lib();

  test('the same candidates, task text and XPaths as the Python package', () => {
    assert.equal(cases.length, 47);
    const differ: string[] = [];
    for (const c of cases) {
      const found = JSON.parse(JSON.stringify(page.candidates({ html: c.bundle.html, role: c.bundle.expectedRole as string,
                                                                fingerprint: c.bundle.fingerprint })));
      const cands = chooseCandidates(c.bundle, found.items, found.fingerprintLine, found.fingerprintRegion);
      const [, user] = formatTask(c.bundle, cands);
      if (user !== c.user) differ.push(c.id + ':\n' + user + '\n--- python ---\n' + c.user);
      const xpaths = cands.items.map((item) => JSON.parse(JSON.stringify(page.robustXpaths({ html: c.bundle.html, index: item.index, limit: 4 }))));
      if (JSON.stringify(xpaths) !== JSON.stringify(c.xpaths)) differ.push(c.id + ' xpaths: ' + JSON.stringify(xpaths) + ' != ' + JSON.stringify(c.xpaths));
    }
    assert.deepEqual(differ, []);
  });

  test('the page this package cleans reads as the same task in the Python package', () => {
    const cleaned = cases.map((c) => redact(page.cleanHtml(redact(c.v2)) as string));
    const users = runPython<string[]>(`
import json, sys
from mendwright.candidates import extract, format_task
data = json.load(sys.stdin)
print(json.dumps([format_task(dict(b, html=h), extract(dict(b, html=h)))[1] for b, h in zip(data['bundles'], data['html'])]))`,
    { bundles: cases.map((c) => c.bundle), html: cleaned });
    const differ = cases.filter((c, i) => users[i] !== c.user).map((c) => c.id);
    assert.deepEqual(differ, []);
  });
});

describe('the local model reply', () => {
  test('a pick, a null pick with its reason, and what is not one', () => {
    assert.deepEqual(parseAnswer('{"pick": 3}'), { pick: 3, why: null });
    assert.deepEqual(parseAnswer('sure {"pick": null, "why": "moved"}'), { pick: null, why: 'moved' });
    assert.deepEqual(parseAnswer('{"pick": null}'), { pick: null, why: 'removed' });
    assert.deepEqual(parseAnswer('{"pick": "2"}'), { pick: 2, why: null });
    for (const reply of ['{"pick": 0}', '{"pick": true}', '{"pick": 2.5}', '{"pick": 2.0}', '{"choice": 1}', 'no']) {
      assert.equal(parseAnswer(reply), null, reply);
    }
  });

  test('the confidence of the pick from the token probabilities', () => {
    const tokens = [['{"', -0.1], ['pick', -0.1], ['":', -0.2], [' ', -0.05], ['3', Math.log(0.9)], ['}', -0.01]];
    const logprobs = tokens.map(([token, logprob]) => ({ token, logprob }));
    assert.ok(Math.abs((answerConfidence(logprobs) as number) - Math.exp(-0.2 - 0.05 + Math.log(0.9))) < 1e-12);
    assert.equal(answerConfidence([]), null);
    assert.equal(answerConfidence(null), null);
  });
});
