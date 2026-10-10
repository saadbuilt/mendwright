"""Regression tests for the v2 core-correctness fixes:
  1. the override store persists ONLY override-origin / locally-mutated candidates,
     never untouched seed rows (so deleted/quarantined-in-seed can't resurrect);
  2. coordinator.pending is a per-key dict (multi-field pages don't drop canaries);
  3. apply() surfaces the real validation reason for the re-prompt.
"""

import json

from mendwright import HealCoordinator, SelectorStore, apply, load_store


def _seed_store(tmp_path, candidates):
    data = {'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'expectedText': [], 'destructive': False,
        'maxCandidates': 5, 'candidates': candidates}}}
    return SelectorStore(data, str(tmp_path / 'override.json'))


# --- 1. override persistence -------------------------------------------------
def test_untouched_seed_candidate_is_not_persisted(tmp_path):
    store = _seed_store(tmp_path, [{'xpath': '//seed', 'source': 'seed', 'status': 'active'}])
    store.add_candidate('k', '//healed', source='heal')
    saved = json.loads((tmp_path / 'override.json').read_text())
    xps = [c['xpath'] for c in saved['selectors']['k']['candidates']]
    assert '//healed' in xps            # override-origin candidate persists
    assert '//seed' not in xps          # untouched seed row must NOT leak into the override


def test_local_quarantine_of_seed_row_persists(tmp_path):
    store = _seed_store(tmp_path, [{'xpath': '//seed', 'source': 'seed', 'status': 'active'}])
    store.quarantine('k', '//seed')     # a local mutation of a seed row
    saved = json.loads((tmp_path / 'override.json').read_text())
    assert any(c['xpath'] == '//seed' and c['status'] == 'quarantined'
               for c in saved['selectors']['k']['candidates'])


def test_prior_session_quarantine_survives_next_persist(tmp_path):
    # Fail-open regression guard: a seed row quarantined in session 1 must not revert to
    # active when a later session (with an empty dirty-set) persists for an unrelated reason.
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'candidates': [
            {'xpath': '//a', 'source': 'seed', 'status': 'active'},
            {'xpath': '//b', 'source': 'seed', 'status': 'active'}]}}}))
    override = tmp_path / 'o.json'

    load_store(str(seed), str(override)).quarantine('k', '//a')          # session 1

    s2 = load_store(str(seed), str(override))                            # session 2 (dirty empty)
    assert '//a' not in [c['xpath'] for c in s2.candidates('k')]         # quarantined -> not active
    s2.add_candidate('k', '//healed', source='heal')                    # unrelated persist

    cands = {c['xpath']: c for c in json.loads(override.read_text())['selectors']['k']['candidates']}
    assert cands.get('//a', {}).get('status') == 'quarantined'  # NOT dropped (fail-open fixed)
    assert '//healed' in cands
    assert '//b' not in cands                                   # untouched seed still not persisted


# --- 2. per-key pending ------------------------------------------------------
class _StubHttp:
    def post(self, url, *, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200
            def json(self_):  # noqa: N805
                return {'status': 'ok'}
        return _R()


def _coord(store):
    return HealCoordinator(store, 'https://gw', 'k', http=_StubHttp(),
                           html_provider=lambda: '<html></html>', inventory_provider=lambda: [],
                           guard=lambda: True)


def test_pending_is_per_key_and_confirm_one_keeps_the_other(tmp_path):
    coord = _coord(_seed_store(tmp_path, []))
    coord.pending['a'] = '//xa'
    coord.pending['b'] = '//xb'
    coord.confirm_pending('a', True)          # confirm a's canary
    assert 'a' not in coord.pending
    assert coord.pending.get('b') == '//xb'   # b's canary is NOT dropped


# --- 3. real reject reason ---------------------------------------------------
class _FakeStore:
    def add_candidate(self, *a, **k):
        pass


def test_apply_returns_real_reason_not_generic_string():
    applied, reason = apply(_FakeStore(), 'k', {'xpath': '//button'}, '<button>x</button>',
                            {'expectedRole': 'checkbox'})
    assert applied is None
    assert 'role mismatch' in reason          # the actual failure, usable in a re-prompt
    assert 'submitted snapshot' not in reason  # not the old hardcoded placeholder
