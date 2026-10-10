import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mendwright import publish


def write(tmp, data):
    p = Path(tmp) / 'app-selectors.json'
    p.write_text(json.dumps(data), encoding='utf-8')
    return p


def base(max_candidates=5, candidates=None):
    return {'schema': 2, 'selectors': {'terms': {
        'expectedRole': 'checkbox', 'maxCandidates': max_candidates,
        'candidates': candidates or [{'xpath': '//old', 'source': 'seed', 'status': 'active',
                                      'lastSucceededAt': None}]}}}


class PublishTests(unittest.TestCase):
    def test_appends_confirmed_candidate(self):
        with TemporaryDirectory() as tmp:
            p = write(tmp, base())
            publish(str(p), 'terms', '//new', now='2026-10-09T00:00:00Z')
            cands = json.loads(p.read_text())['selectors']['terms']['candidates']
            xs = [c['xpath'] for c in cands]
            self.assertIn('//new', xs)
            new = next(c for c in cands if c['xpath'] == '//new')
            self.assertEqual(new['status'], 'active')
            self.assertEqual(new['lastSucceededAt'], '2026-10-09T00:00:00Z')  # confirmed => stamped

    def test_dedupes_existing_xpath(self):
        with TemporaryDirectory() as tmp:
            p = write(tmp, base(candidates=[{'xpath': '//dup', 'source': 'seed', 'status': 'active',
                                             'lastSucceededAt': None}]))
            publish(str(p), 'terms', '//dup', now='2026-10-09T00:00:00Z')
            cands = json.loads(p.read_text())['selectors']['terms']['candidates']
            self.assertEqual([c['xpath'] for c in cands].count('//dup'), 1)
            self.assertEqual(cands[0]['lastSucceededAt'], '2026-10-09T00:00:00Z')  # refreshed

    def test_caps_active_candidates(self):
        with TemporaryDirectory() as tmp:
            p = write(tmp, base(max_candidates=2, candidates=[
                {'xpath': '//a', 'source': 'seed', 'status': 'active', 'lastSucceededAt': '2026-01-01T00:00:00Z'},
                {'xpath': '//b', 'source': 'seed', 'status': 'active', 'lastSucceededAt': '2026-02-01T00:00:00Z'}]))
            publish(str(p), 'terms', '//c', now='2026-10-09T00:00:00Z')
            actives = [c['xpath'] for c in json.loads(p.read_text())['selectors']['terms']['candidates']
                       if c['status'] == 'active']
            self.assertEqual(len(actives), 2)
            self.assertIn('//c', actives)      # newest kept
            self.assertNotIn('//a', actives)   # oldest dropped

    def test_unknown_key_raises(self):
        with TemporaryDirectory() as tmp:
            p = write(tmp, base())
            with self.assertRaises(ValueError):
                publish(str(p), 'nope', '//x')


if __name__ == '__main__':
    unittest.main()
