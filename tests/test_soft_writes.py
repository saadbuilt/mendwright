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



def _seed(tmp_path):
    """A store file whose key k has the fingerprint FP_OLD, and another key."""
    with open(_store(tmp_path), 'w', encoding='utf-8') as f:
        json.dump({'schema': 2, 'selectors': {
            'k': {'expectedRole': 'input', 'fingerprint': FP_OLD,
                  'candidates': [{'xpath': "//input[@id='x']", 'source': 'code',
                                  'status': 'active'}]},
            'other': {'expectedRole': 'input',
                      'candidates': [{'xpath': "//input[@id='y']", 'source': 'code',
                                      'status': 'active'}]}}}, f)


def _waiting(store):
    """store's fingerprint FP_NEW for k, which waits: k was written a moment ago."""
    SOFT.written(store._soft_path(), ['k'])
    store.set_meta('k', fingerprint=FP_NEW)


def _field(name):
    return {'v': 1, 'tag': 'input', 'id': name}


def _flushed_by_another_store(tmp_path):
    """A store whose FP_NEW waited, written since by another store's flush in this process."""
    _seed(tmp_path)
    store = load_store(None, _store(tmp_path))
    _waiting(store)
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_OLD
    assert load_store(None, _store(tmp_path)).flush()
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_NEW
    return store


@pytest.mark.parametrize('changes', [1, 2, 3])
def test_this_store_s_change_another_store_flushed_is_what_its_next_ones_replace(
        tmp_path, soft_clock, changes):
    # The store's next fingerprints wait too, one after another. The flush at exit writes the
    # last over the store's own one the file holds, and not only over the one the store read.
    store = _flushed_by_another_store(tmp_path)
    for n in range(changes):
        store.set_meta('k', fingerprint=_field('field-%d' % n))
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_NEW
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('field-%d' % (changes - 1))


def test_a_write_of_the_store_s_own_comes_after_what_a_flush_left_before_it(tmp_path,
                                                                           soft_clock):
    # The store then writes a newer fingerprint itself, with a change that can't wait (a new
    # role). Its next fingerprint replaces that one, not what the flush left.
    store = _flushed_by_another_store(tmp_path)
    store.set_meta('k', fingerprint=_field('third'), role='textbox')
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('third')
    store.set_meta('k', fingerprint=_field('fourth'))
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('fourth')


def test_a_flush_between_a_refresh_s_read_and_its_merge_still_counts(tmp_path, soft_clock,
                                                                    monkeypatch):
    # Another store's flush, on another thread, writes the store's waiting change right after
    # the store's refresh read the file, so the refresh takes in what the file held before.
    _seed(tmp_path)
    store = load_store(None, _store(tmp_path))
    _waiting(store)
    data = json.loads(open(_store(tmp_path), encoding='utf-8').read())
    data['selectors']['other']['expectedText'] = ['Search']    # another process's change
    with open(_store(tmp_path), 'w', encoding='utf-8') as f:
        json.dump(data, f)
    other, real = load_store(None, _store(tmp_path)), store_module.parsed_override

    def parsed(*args, **kwargs):
        out = real(*args, **kwargs)
        monkeypatch.setattr(store_module, 'parsed_override', real)
        assert other.flush()
        return out
    monkeypatch.setattr(store_module, 'parsed_override', parsed)
    store.refresh('k')
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_NEW
    store.set_meta('k', fingerprint=_field('third'))
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('third')


def test_a_fingerprint_set_back_to_the_one_read_takes_the_place_of_the_one_that_waits(
        tmp_path, soft_clock):
    # As an A/B test's two variants take turns: the store's FP_NEW waits, then the store sees
    # FP_OLD again. The flush at exit leaves FP_OLD, which the file and the store hold.
    _seed(tmp_path)
    store = load_store(None, _store(tmp_path))
    _waiting(store)
    store.set_meta('k', fingerprint=FP_OLD)
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_OLD


def test_a_remote_store_s_change_another_store_flushed_is_what_its_next_one_replaces(
        soft_clock):
    # As on a file, and the store's next lookup refreshes the key from the process's read of
    # overrides.json, which takes what the other store's flush wrote.
    from mendwright.backends import OVERRIDES, MemoryBackend
    backend = MemoryBackend()

    def saved():
        return json.loads(backend.get(OVERRIDES).body.decode('utf-8'))['selectors']['k']

    seed = {'schema': 2, 'selectors': {
        'k': {'expectedRole': 'input', 'fingerprint': FP_OLD,
              'candidates': [{'xpath': "//input[@id='x']", 'source': 'code',
                              'status': 'active'}]},
        'other': {'expectedRole': 'input',
                  'candidates': [{'xpath': "//input[@id='y']", 'source': 'code',
                                  'status': 'active'}]}}}
    backend.put(OVERRIDES, json.dumps(seed).encode('utf-8'))
    store = load_store(None, backend)
    _waiting(store)
    seed['selectors']['other']['expectedText'] = ['Search']    # another machine's change
    backend.put(OVERRIDES, json.dumps(seed).encode('utf-8'))
    other = load_store(None, backend)
    other.refresh('k')
    assert other.flush() and saved()['fingerprint'] == FP_NEW
    store.refresh('k', max_age=1.0)
    store.set_meta('k', fingerprint=_field('third'))
    flush_all()
    assert saved()['fingerprint'] == _field('third')


def _rewrite(tmp_path, change):
    """Another process's write of the store file: change(data) edits its parse."""
    data = json.loads(open(_store(tmp_path), encoding='utf-8').read())
    change(data)
    with open(_store(tmp_path), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def test_a_fingerprint_the_store_only_read_never_takes_another_store_s_place(tmp_path,
                                                                            soft_clock):
    # The store's FP_NEW went out with a write of the store; then the store took in another
    # process's fingerprint. Another store of this process has a newer one waiting when the
    # store records a new page of the key: the flush at exit writes the other store's.
    _seed(tmp_path)
    store = load_store(None, _store(tmp_path))
    _waiting(store)
    store.set_meta('other', role='textbox')
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_NEW
    _rewrite(tmp_path, lambda d: d['selectors']['k'].update(fingerprint=_field('theirs')))
    store.refresh('k')
    load_store(None, _store(tmp_path)).set_meta('k', fingerprint=_field('newer'))
    store.set_last_page('k', '/b')
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('newer')


def test_a_fingerprint_another_process_put_back_is_what_the_next_one_replaces(tmp_path,
                                                                             soft_clock):
    # After a flush wrote the store's FP_NEW, another process put FP_OLD back, as the other
    # variant of an A/B test, and the store read it. Its next fingerprint replaces FP_OLD.
    store = _flushed_by_another_store(tmp_path)

    def back(data):
        data['selectors']['k']['fingerprint'] = FP_OLD
        data['selectors']['other']['expectedText'] = ['Search']
    _rewrite(tmp_path, back)
    store.refresh('k')
    store.set_meta('k', fingerprint=_field('third'))
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('third')


def test_a_change_made_while_another_store_s_flush_writes_goes_out_at_exit(tmp_path,
                                                                          soft_clock,
                                                                          monkeypatch):
    # As another thread would: the store sets a newer fingerprint while another store's flush
    # writes its FP_NEW, and one more after.
    _seed(tmp_path)
    store = load_store(None, _store(tmp_path))
    _waiting(store)
    real = store_module._atomic_write

    def atomic_write(*args, **kwargs):
        out = real(*args, **kwargs)
        monkeypatch.setattr(store_module, '_atomic_write', real)
        store.set_meta('k', fingerprint=_field('third'))
        return out
    monkeypatch.setattr(store_module, '_atomic_write', atomic_write)
    assert load_store(None, _store(tmp_path)).flush()
    assert _saved(tmp_path, 'k')['fingerprint'] == FP_NEW
    store.set_meta('k', fingerprint=_field('fourth'))
    flush_all()
    assert _saved(tmp_path, 'k')['fingerprint'] == _field('fourth')
