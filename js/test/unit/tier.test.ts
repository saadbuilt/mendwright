// The fingerprint tier against the Python package's, on the 47 cases of the accuracy
// benchmark (benchmarks/cases): for each case, the fingerprint the Python package records
// on the page before the redesign, and fingerprint.find() on the page after it. The port
// must give the same answer, the same scores and the same reason for every case.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { describe, test } from 'node:test';

import { PlaywrightDriver } from '../../src/driver';
import type { Fingerprint } from '../../src/fingerprint';
import { findMatch } from '../../src/tier';
import { FakePage } from '../support/fakepage';
import { REPO, needsPython, runPython } from '../support/python';

const CASES = path.join(REPO, 'benchmarks', 'cases');
const skip = needsPython() || (fs.existsSync(CASES) ? false : 'benchmarks/cases is not in this copy');

interface PyCase {
  id: string;
  v1: string;
  v2: string;
  fp: Fingerprint;
  role: string;
  recorded: Fingerprint;
  match: { xpath: string | null; score: number; runner_up: number; reason: string };
}

const SCRIPT = `
import json, sys, glob, os
import lxml.html
from mendwright.fingerprint import of_element, find, valid
from mendwright.validation import _role_of
out = []
for case_dir in sorted(glob.glob(os.path.join(json.load(sys.stdin), '*'))):
    case = json.load(open(os.path.join(case_dir, 'case.json'), encoding='utf-8'))
    v1 = open(os.path.join(case_dir, 'v1.html'), encoding='utf-8').read()
    v2 = open(os.path.join(case_dir, 'v2.html'), encoding='utf-8').read()
    sel = case['selector']
    xp = "//*[@id='" + sel[1:] + "']" if sel.startswith('#') else sel
    root = lxml.html.fromstring(v1)
    el = root.xpath(xp)[0]
    fp = of_element(el)
    role = _role_of(el)
    m = find(fp, v2, role)
    out.append({'id': case['id'], 'v1': v1, 'v2': v2, 'fp': fp, 'role': role, 'selector': xp,
                'match': {'xpath': m.xpath, 'score': m.score, 'runner_up': m.runner_up, 'reason': m.reason}})
print(json.dumps(out))
`;

describe('the fingerprint tier on the benchmark cases', { skip }, () => {
  const cases = skip ? [] : runPython<(PyCase & { selector: string })[]>(SCRIPT, CASES);

  test('every case was read', () => {
    assert.equal(cases.length, 47);
  });

  test('the same match, scores and reason as the Python package', async () => {
    const differ: string[] = [];
    let healed = 0;
    for (const c of cases) {
      const page = new FakePage(c.v2);
      const driver = new PlaywrightDriver(page as never);
      const ours = await findMatch(c.fp, c.role, driver);
      const want = { xpath: c.match.xpath, score: c.match.score, runnerUp: c.match.runner_up, reason: c.match.reason };
      try {
        assert.deepEqual(ours, want);
      } catch {
        differ.push(c.id + ': ' + JSON.stringify(ours) + ' != ' + JSON.stringify(want));
      }
      if (ours.xpath) healed += 1;
    }
    assert.deepEqual(differ, []);
    assert.ok(healed >= 15, 'the tier healed only ' + healed + ' cases');
  });

  test('the fingerprint read in the page is the one lxml reads', async () => {
    for (const c of cases) {
      const page = new FakePage(c.v1);
      const driver = new PlaywrightDriver(page as never);
      const recorded = await driver.fingerprint(c.selector);
      assert.deepEqual(JSON.parse(JSON.stringify(recorded)), c.fp, c.id);
    }
  });
});
