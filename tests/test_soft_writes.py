"""Fewer writes: a store writes what the file does not have yet at once (a heal, a quarantine,
a destructive flag, a first success, a key's first pages, address, steps and fingerprint),
and changes to what it has (the order of the last successes, a new page in the list, a new
fingerprint) at most every SOFT_EVERY seconds per key per process, the rest when the process
exits (see the comment above mendwright.store.SOFT_EVERY).

The steady-state cases are the fleet analysis's (Q4): per-test Healers on one store, with
dynamic text in an element, a key used on more pages than the store keeps, and a heal another
worker made. Offline: conftest's lxml FakePage."""

import gc
import json
import multiprocessing
import os

import pytest

import mendwright
import mendwright.store as store_module
from mendwright import load_store
from mendwright.store import SOFT, SOFT_EVERY, _apply_soft, flush_all

URL = 'https://shop.test/'


def _html(cart_count=0):
    return ("<html><body><header><a id='cart' href='/cart'>Cart (" + str(cart_count)
            + " items)</a></header><main><form><input id='q' name='q' type='search'>"
            "<button id='go' type='submit'>Search</button></form></main></body></html>")


@pytest.fixture
def writes(monkeypatch):
    count = []
    real = store_module._atomic_write

    def counting(path, text, sleep=None):
        count.append(json.loads(text))
        return real(path, text) if sleep is None else real(path, text, sleep)
    monkeypatch.setattr(store_module, '_atomic_write', counting)
    return count


def _store(tmp_path):
    return str(tmp_path / 'overrides.json')


def _lookup(make_page, tmp_path, html, path='/', key='cart', selector='#cart', role='link'):
    page = make_page(html, url=URL.rstrip('/') + path, fingerprints=True)
    heal = mendwright.open(page, provider=None, store_path=_store(tmp_path), timeout_ms=0)
    heal.locator(key, selector, role=role)
    return heal


def _saved(tmp_path, key='cart'):
    return json.loads(open(_store(tmp_path), encoding='utf-8').read())['selectors'][key]


def test_dynamic_text_rewrites_the_fingerprint_once_per_interval(tmp_path, make_page, writes,
                                                                  soft_clock):
    _lookup(make_page, tmp_path, _html(0))              # the key, its role, page, fingerprint
    first = _saved(tmp_path)['fingerprint']
    del writes[:]
    for count in range(1, 51):                          # 50 per-test Healers, a new count each
        _lookup(make_page, tmp_path, _html(count))
    assert writes == []                                 # the analysis measured 50 writes
    assert _saved(tmp_path)['fingerprint'] == first
    soft_clock.advance(SOFT_EVERY)
    _lookup(make_page, tmp_path, _html(51))             # due: one write, the newest fingerprint
    assert len(writes) == 1
    assert _saved(tmp_path)['fingerprint'] != first


def test_a_key_on_more_pages_than_the_store_keeps_waits_too(tmp_path, make_page, writes,
                                                            soft_clock):
    pages = ['/p%d' % i for i in range(7)]
    _lookup(make_page, tmp_path, _html(), pages[0])
    del writes[:]
    for i in range(50):
        _lookup(make_page, tmp_path, _html(), pages[i % 7])
    assert writes == []                                 # the analysis measured 50 writes
    flush_all()                                         # what waits, as at exit: one write
    assert len(writes) == 1
    assert len(_saved(tmp_path)['pages']) == 5


def test_a_first_success_another_worker_recorded_is_not_written_again(tmp_path, make_page,
                                                                      writes):
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('k', ['#a'])
    other = load_store(None, _store(tmp_path))          # read before the first success
    store.record_success('k', '#a')
    assert _saved(tmp_path, 'k')['candidates'][0]['lastSucceededAt']
    del writes[:]
    other.record_success('k', '#a')                     # it looks at the file first
    assert writes == []


def test_heals_quarantines_and_destructive_flags_are_written_at_once(tmp_path, make_page,
                                                                     writes):
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('k', ['#a'])
    store.record_success('k', '#a')
    store.set_last_page('k', '/one')
    store.set_last_page('k', '/two')                    # waits (a change of the page list)
    del writes[:]
    store.add_candidate('k', '//a[@id="b"]')            # a heal: written at once, and takes
    assert len(writes) == 1                             # the waiting page list with it
    assert set(_saved(tmp_path, 'k')['pages']) == {'/one', '/two'}
    store.quarantine('k', '//a[@id="b"]')
    store.set_meta('k', destructive=True)
    assert len(writes) == 3
    assert _saved(tmp_path, 'k')['destructive'] is True


def test_a_dropped_store_s_waiting_change_is_written_when_the_process_exits(tmp_path,
                                                                            make_page, writes):
    _lookup(make_page, tmp_path, _html(), '/a')
    heal = _lookup(make_page, tmp_path, _html(), '/b')  # a new page: it waits
    assert _saved(tmp_path)['pages'] == ['/a']
    del heal
    gc.collect()                                        # the store is gone; its change is not
    assert SOFT.waiting(os.path.abspath(_store(tmp_path))) == ['cart']
    flush_all()                                         # what atexit runs
    assert _saved(tmp_path)['pages'] == ['/b', '/a']
    assert SOFT.waiting() == {}


def _child(path, done):
    import mendwright.store as store
    first = store.load_store(None, path)
    first.set_last_page('k', '/one')                    # written: the first pages
    first.set_last_page('k', '/two')                    # waits
    done.set()
    # The child returns: multiprocessing exits it without atexit, and its exit hook flushes.


def test_a_multiprocessing_child_flushes_what_waits_when_it_exits(tmp_path):
    path = _store(tmp_path)
    seed = load_store(None, path)
    seed.set_code_candidates('k', ['#a'])
    ctx = multiprocessing.get_context('spawn')
    done = ctx.Event()
    child = ctx.Process(target=_child, args=(path, done))
    child.start()
    child.join(60)
    assert child.exitcode == 0 and done.is_set()
    assert set(_saved(tmp_path, 'k')['pages']) == {'/one', '/two'}


def test_a_flush_never_makes_a_store_or_replaces_one_it_cannot_read(tmp_path, caplog):
    path = os.path.abspath(_store(tmp_path))
    SOFT.record(path, 'k', meta={'pages': ['/x']})
    flush_all()
    assert not os.path.exists(path)                     # soft changes alone make no store
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('{"schema": 2, "selectors": {')       # a merge conflict, say
    SOFT.record(path, 'k', meta={'pages': ['/x']})
    assert SOFT.flush(path, timeout=1) is False
    assert open(path, encoding='utf-8').read() == '{"schema": 2, "selectors": {'
    assert SOFT.waiting(path) == ['k']                  # it waits, and is said once
    assert any('could not save the pages' in r.getMessage() for r in caplog.records)


def test_the_flush_keeps_the_merge_rules_of_a_write():
    data = {'schema': 2, 'selectors': {'k': {'pages': ['/a'], 'candidates': [
        {'xpath': '#code', 'source': 'code', 'lastSucceededAt': '2026-10-03T00:00:00+00:00',
         'status': 'active'},
        {'xpath': '#heal', 'source': 'heal', 'lastSucceededAt': None, 'status': 'quarantined'},
        {'xpath': '#old', 'source': 'heal', 'lastSucceededAt': '2026-10-01T00:00:00+00:00',
         'status': 'active'}]}}}
    # An older success, and the success of a quarantined row, change nothing; the order
    # does not change for #code, already on top.
    assert _apply_soft(data, {'k': {'rows': {
        '#code': '2026-10-05T00:00:00+00:00', '#old': '2026-09-01T00:00:00+00:00'},
        'meta': {}}}) is False
    rows = {c['xpath']: c for c in data['selectors']['k']['candidates']}
    assert rows['#heal']['status'] == 'quarantined'
    # A newer success that moves a row up is written, and a first success too.
    assert _apply_soft(data, {'k': {'rows': {'#old': '2026-10-04T00:00:00+00:00'},
                                    'meta': {'pages': ['/b']}}}) is True
    assert rows['#old']['lastSucceededAt'] == '2026-10-04T00:00:00+00:00'
    assert data['selectors']['k']['pages'] == ['/b', '/a']
    assert _apply_soft(data, {'k': {'rows': {'#heal': '2026-10-06T00:00:00+00:00'},
                                    'meta': {}}}) is True
    assert rows['#heal']['status'] == 'quarantined'     # a soft change never reactivates


FP_OLD = {'v': 1, 'tag': 'input', 'id': 'old-field'}
FP_NEW = {'v': 1, 'tag': 'input', 'id': 'new-field'}


def test_a_flush_never_writes_back_a_fingerprint_this_process_only_read(tmp_path, soft_clock):
    # This process finds the key on another page, a soft change that waits. Another process
    # (an xdist worker) then heals the key and saves a new fingerprint. This process's flush at
    # exit keeps it: it never changed the fingerprint.
    import subprocess
    import sys
    path = tmp_path / 'overrides.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'input', 'fingerprint': FP_OLD, 'pages': ['/a'], 'lastPage': '/a',
        'candidates': [{'xpath': "//input[@id='x']", 'source': 'code',
                        'lastSucceededAt': '2026-01-01T00:00:00+00:00', 'status': 'active'}]}}}),
        encoding='utf-8')
    load_store(None, str(path)).set_last_page('k', '/b')
    root = os.path.dirname(os.path.dirname(os.path.abspath(mendwright.__file__)))
    code = ('from mendwright import load_store\n'
            's = load_store(None, %r)\n'
            's.set_meta("k", fingerprint=%r)\n'
            'assert s.flush()\n' % (str(path), FP_NEW))
    subprocess.run([sys.executable, '-c', code], check=True,
                   env=dict(os.environ, PYTHONPATH=root))
    flush_all()
    entry = json.loads(path.read_text(encoding='utf-8'))['selectors']['k']
    assert entry['fingerprint'] == FP_NEW and set(entry['pages']) == {'/a', '/b'}


def test_a_waiting_fingerprint_is_written_only_over_the_one_it_replaced():
    def file(fp):
        return {'schema': 2, 'selectors': {'k': {'fingerprint': fp, 'candidates': []}}}
    mine = {'k': {'rows': {}, 'meta': {'fingerprint': FP_NEW}, 'replaced': {'fingerprint': FP_OLD}}}
    data = file(FP_OLD)
    assert _apply_soft(data, mine) is True and data['selectors']['k']['fingerprint'] == FP_NEW
    other = {'v': 1, 'tag': 'input', 'id': 'other-field'}     # another process's, newer
    data = file(other)
    assert _apply_soft(data, mine) is False and data['selectors']['k']['fingerprint'] == other
