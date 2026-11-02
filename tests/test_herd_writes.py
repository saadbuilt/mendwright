"""Workers that only take in what another worker saved write nothing: the fleet analysis's herds on
a store that has never seen the key (a first run, a new key in the code), where every worker used
to save the same code selector and role, and soft changes that came due in many stores of one
process at once. Offline: conftest's lxml FakePage and fake providers, no model is called."""

import json
import threading

import pytest

import mendwright
import mendwright.store as store_module
from mendwright import load_store
from mendwright.fleet import FLIGHTS
from mendwright.store import SOFT, SelectorStore
from test_herd import CODE, HEALED, KEY, URL, V2, Blocking, _count, _instant, _journal, _store

N = 12


@pytest.fixture
def writes(monkeypatch):
    """The paths of the store files written (store._atomic_write), in order."""
    count = []
    real = store_module._atomic_write

    def counting(path, text, sleep=None):
        count.append(str(path))
        return real(path, text) if sleep is None else real(path, text, sleep)
    monkeypatch.setattr(store_module, '_atomic_write', counting)
    return count


def _saved(tmp_path):
    return json.loads((tmp_path / 'overrides.json').read_text(encoding='utf-8'))


def test_a_herd_on_a_key_the_store_never_saw_saves_it_once(tmp_path, make_page, writes):
    # The store file does not exist yet: every worker registers the same code selector and
    # role, one heals, the others adopt its heal. Every worker used to write the selector and
    # the role itself, 2 writes each, under one lock.
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for _ in range(N):
        page = make_page(V2, url=URL)
        heal = mendwright.open(page, provider=Blocking(calls, release, changed),
                               store_path=_store(tmp_path), timeout_ms=60000, fingerprint=False)
        heal._clock = _instant()
        workers.append((page, heal))
    start = threading.Barrier(N)
    errors = []

    def run(heal):
        start.wait()
        try:
            heal.locator(KEY, CODE, role='checkbox').check()
        except Exception as exc:        # noqa: BLE001 - reported below
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(heal,)) for _page, heal in workers]
    for thread in threads:
        thread.start()
    with changed:
        assert changed.wait_for(lambda: len(calls) == 1, 60)
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    events = _journal(tmp_path)
    assert len(calls) == 1 and _count(events, 'heal.adopted') == N - 1
    # The code's selector, its role, the heal, its first success, and the page and address it
    # was found on, each written once: by whoever saved it first.
    assert len(writes) == 5, writes
    entry = _saved(tmp_path)['selectors'][KEY]
    assert entry['expectedRole'] == 'checkbox'
    rows = {c['xpath']: c for c in entry['candidates']}
    assert rows[CODE]['source'] == 'code' and rows[HEALED]['source'] == 'heal'
    assert rows[HEALED]['lastSucceededAt']


def test_a_store_does_not_write_what_another_worker_saved(tmp_path, writes):
    path = _store(tmp_path)
    first, second = load_store(None, path), load_store(None, path)
    first.set_code_candidates(KEY, [CODE])
    first.set_meta(KEY, role='checkbox', destructive=False)
    assert len(writes) == 2
    # The second store loaded before those writes: the file holds all it would save.
    second.set_code_candidates(KEY, [CODE])
    second.set_meta(KEY, role='checkbox', destructive=False)
    assert len(writes) == 2
    assert second.meta(KEY)['expectedRole'] == 'checkbox'
    # A change of its own is written, with what it took in.
    second.set_meta(KEY, text='I agree')
    assert len(writes) == 3
    entry = _saved(tmp_path)['selectors'][KEY]
    assert entry['expectedText'] == ['I agree'] and entry['expectedRole'] == 'checkbox'


def test_a_write_under_the_lock_that_would_change_nothing_is_skipped(tmp_path, writes):
    path = _store(tmp_path)
    first, second = load_store(None, path), load_store(None, path)
    first.set_code_candidates(KEY, [CODE])
    second._entry(KEY)['candidates'].append({'xpath': CODE, 'source': 'code',
                                             'addedAt': '2026-01-01T00:00:00+00:00',
                                             'lastSucceededAt': None, 'status': 'active'})
    del writes[:]
    second._persist_override()          # read and merged under the lock: the same as the file
    assert writes == []
    assert second.refresh(KEY) is False  # it knows the file's version now


def test_a_write_that_failed_is_never_skipped(tmp_path, writes, monkeypatch):
    path = _store(tmp_path)
    first, second = load_store(None, path), load_store(None, path)
    real = store_module._atomic_write
    failing = [True]

    def flaky(path, text, sleep=None):
        if failing:
            failing.pop()
            raise PermissionError('the disk said no')
        return real(path, text) if sleep is None else real(path, text, sleep)
    monkeypatch.setattr(store_module, '_atomic_write', flaky)
    second.add_candidate(KEY, HEALED)                 # the write fails: kept in memory
    assert not (tmp_path / 'overrides.json').exists()
    first.set_code_candidates(KEY, [CODE])            # another worker saves the selector
    second.set_code_candidates(KEY, [CODE])           # the file holds that, not the heal
    rows = {c['xpath'] for c in _saved(tmp_path)['selectors'][KEY]['candidates']}
    assert rows == {CODE, HEALED}


def test_a_soft_change_that_comes_due_is_claimed_by_one_store_of_the_process(tmp_path):
    path = str(tmp_path / 'overrides.json')
    won, start = [], threading.Barrier(16)

    def claim(i):
        start.wait()
        if SOFT.claim_due(path, [KEY]):
            won.append(i)
    threads = [threading.Thread(target=claim, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(won) == 1
    assert SOFT.claim_due(path, [KEY]) is False         # within SOFT_EVERY of the claim
    assert SOFT.claim_due(path, ['another key']) is True


def test_a_new_order_of_successes_in_many_stores_is_written_once(tmp_path, monkeypatch, soft_clock):
    path = _store(tmp_path)
    seed = load_store(None, path)
    seed.set_code_candidates(KEY, [CODE])
    seed.add_candidate(KEY, HEALED)
    seed.record_success(KEY, CODE, now='2026-10-01T00:00:00+00:00')
    seed.record_success(KEY, HEALED, now='2026-10-01T00:00:01+00:00')
    stores = [load_store(None, path) for _ in range(8)]
    SOFT.clear()
    persists = []
    real = SelectorStore._persist_override

    def counting(self):
        persists.append(self)
        return real(self)
    monkeypatch.setattr(SelectorStore, '_persist_override', counting)
    for store in stores:
        store.record_success(KEY, CODE, now='2026-10-02T00:00:00+00:00')   # CODE moves up
    assert len(persists) == 1
    rows = {c['xpath']: c for c in _saved(tmp_path)['selectors'][KEY]['candidates']}
    assert rows[CODE]['lastSucceededAt'] == '2026-10-02T00:00:00+00:00'
