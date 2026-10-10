import json
import tempfile
import unittest
from pathlib import Path

from mendwright import infer_role, migrate_schema1, merge, load_store
DESTRUCTIVE_KEYS = frozenset({'registration_submit_STOP', 'apply_membership', 'payment_pay', 'payment_method_creditcard', 'confirm_account'})


class MigrateTests(unittest.TestCase):
    def test_infer_role_from_xpath(self):
        self.assertEqual(infer_role("//main//label/input[@type='checkbox']"), 'checkbox')
        self.assertEqual(infer_role("//main//button[@type='submit']"), 'button')
        self.assertEqual(infer_role("//main//a[contains(@href,'/personal')]"), 'link')
        self.assertEqual(infer_role("//input[@formcontrolname='code']"), 'input')
        self.assertEqual(infer_role("//*[@role='combobox']"), 'combobox')

    def test_migrate_builds_candidates_and_metadata(self):
        data = {'selectors': [
            {'key': 'terms', 'xpath': "//label/input[@type='checkbox']", 'stage': 'registration_address', 'action': 'DO_NOT_ACCEPT'},
            {'key': 'payment_pay', 'xpath': "//button[@type='submit']"},
        ]}
        out = migrate_schema1(data, DESTRUCTIVE_KEYS)
        self.assertEqual(out['schema'], 2)
        terms = out['selectors']['terms']
        self.assertEqual(terms['expectedRole'], 'checkbox')
        self.assertFalse(terms['destructive'])
        self.assertEqual(terms['action'], 'DO_NOT_ACCEPT')
        self.assertEqual(len(terms['candidates']), 1)
        self.assertEqual(terms['candidates'][0]['source'], 'seed')
        self.assertEqual(terms['candidates'][0]['status'], 'active')
        self.assertTrue(out['selectors']['payment_pay']['destructive'])
        self.assertIn('payment_pay', DESTRUCTIVE_KEYS)

    def test_duplicate_key_becomes_two_candidates_missing_xpath_skipped(self):
        data = {'selectors': [
            {'key': 'k', 'xpath': "//a[1]"},
            {'key': 'k', 'xpath': "//a[2]"},
            {'key': 'k', 'xpath': "//a[1]"},   # exact dup -> ignored
            {'key': 'bad'},                     # no xpath -> skipped
            {'xpath': "//x"},                   # no key -> skipped
        ]}
        out = migrate_schema1(data, DESTRUCTIVE_KEYS)
        self.assertEqual([c['xpath'] for c in out['selectors']['k']['candidates']], ["//a[1]", "//a[2]"])
        self.assertNotIn('bad', out['selectors'])


class MergeTests(unittest.TestCase):
    def _schema2(self, key, cands):
        return {'schema': 2, 'selectors': {key: {
            'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False,
            'maxCandidates': 5, 'stage': None, 'action': None, 'candidates': cands}}}

    def test_override_overlays_seed_candidate_by_xpath(self):
        seed = self._schema2('terms', [{'xpath': '//a', 'source': 'seed', 'addedAt': None, 'lastSucceededAt': None, 'status': 'active'}])
        ovr = self._schema2('terms', [{'xpath': '//a', 'source': 'seed', 'lastSucceededAt': '2026-10-06T10:00:00', 'status': 'active'}])
        m = merge(seed, ovr)
        cands = m['selectors']['terms']['candidates']
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]['lastSucceededAt'], '2026-10-06T10:00:00')

    def test_key_only_in_override_survives(self):
        seed = {'schema': 2, 'selectors': {}}
        ovr = self._schema2('healed_key', [{'xpath': '//b', 'source': 'heal', 'lastSucceededAt': None, 'status': 'active'}])
        m = merge(seed, ovr)
        self.assertIn('healed_key', m['selectors'])

    def test_candidates_ordered_by_recency_active_only(self):
        with tempfile.TemporaryDirectory() as d:
            seed = Path(d) / 'seed.json'
            seed.write_text(json.dumps({'selectors': [{'key': 'k', 'xpath': '//old'}, {'key': 'k', 'xpath': '//new'}]}))
            ovr = Path(d) / 'override.json'
            ovr.write_text(json.dumps(self._schema2('k', [
                {'xpath': '//new', 'source': 'seed', 'lastSucceededAt': '2026-10-06T12:00:00', 'status': 'active'},
                {'xpath': '//dead', 'source': 'heal', 'lastSucceededAt': '2026-10-07T00:00:00', 'status': 'quarantined'},
            ])))
            store = load_store(seed, ovr)
            xpaths = [c['xpath'] for c in store.candidates('k')]
            self.assertEqual(xpaths, ['//new', '//old'])
            self.assertEqual(store.candidates('absent'), [])


class PersistTests(unittest.TestCase):
    def _store(self, d):
        seed = Path(d) / 'seed.json'
        seed.write_text(json.dumps({'selectors': [{'key': 'k', 'xpath': '//old'}, {'key': 'k', 'xpath': '//new'}]}))
        return load_store(seed, Path(d) / 'override.json'), Path(d) / 'override.json'

    def test_record_success_promotes_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            store, ovr = self._store(d)
            self.assertTrue(store.record_success('k', '//new', now='2026-10-06T12:00:00'))
            self.assertEqual([c['xpath'] for c in store.candidates('k')], ['//new', '//old'])
            saved = json.loads(ovr.read_text(encoding='utf-8-sig'))
            self.assertEqual(saved['schema'], 2)
            self.assertTrue(any(c['xpath'] == '//new' and c['lastSucceededAt'] == '2026-10-06T12:00:00'
                                for c in saved['selectors']['k']['candidates']))

    def test_record_success_on_top_candidate_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            store, ovr = self._store(d)
            # //old is top initially (insertion order, both None) -> recording it changes nothing to persist
            self.assertFalse(store.record_success('k', '//old', now='2026-10-06T09:00:00'))
            self.assertFalse(ovr.exists())

    def test_the_first_success_of_a_code_candidate_is_written_once(self):
        with tempfile.TemporaryDirectory() as d:
            store, ovr = load_store(None, Path(d) / 'override.json'), Path(d) / 'override.json'
            store._entry('k')['candidates'].append(
                {'xpath': '//c', 'source': 'code', 'addedAt': None, 'lastSucceededAt': None,
                 'status': 'active'})
            self.assertFalse(store.record_success('k', '//c', now='2026-10-06T09:00:00'))
            saved = json.loads(ovr.read_text(encoding='utf-8-sig'))
            self.assertEqual(saved['selectors']['k']['candidates'][0]['lastSucceededAt'],
                             '2026-10-06T09:00:00')
            ovr.unlink()
            # A later success of the same top candidate is not worth a write.
            self.assertFalse(store.record_success('k', '//c', now='2026-10-06T10:00:00'))
            self.assertFalse(ovr.exists())

    def test_add_candidate_then_quarantine(self):
        with tempfile.TemporaryDirectory() as d:
            store, ovr = self._store(d)
            store.add_candidate('k', '//healed', source='heal', now='2026-10-06T13:00:00')
            self.assertIn('//healed', [c['xpath'] for c in store.candidates('k')])
            store.quarantine('k', '//healed')
            self.assertNotIn('//healed', [c['xpath'] for c in store.candidates('k')])

    def test_corrupt_override_raises_and_names_the_file(self):
        # Reading it as empty would drop its destructive flags and quarantines, and the
        # next write would replace it.
        with tempfile.TemporaryDirectory() as d:
            seed = Path(d) / 'seed.json'
            seed.write_text(json.dumps({'selectors': [{'key': 'k', 'xpath': '//x'}]}))
            ovr = Path(d) / 'override.json'
            ovr.write_text('{ this is not json')
            with self.assertRaises(ValueError) as caught:
                load_store(seed, ovr)
            self.assertIn(str(ovr), str(caught.exception))
            self.assertIn('is not valid JSON', str(caught.exception))
            self.assertEqual(ovr.read_text(), '{ this is not json')


if __name__ == '__main__':
    unittest.main()
