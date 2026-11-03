"""A remote store (mendwright.remote) that stops answering, or refuses, while workers use it: reads
keep the copy they have, changes wait in memory and are written once the store takes them, the
store is not asked again until its wait is over, and the journal says when an outage starts and
ends, once each. Retry loops are bounded. Offline: a MemoryBackend behind a wrapper that fails on
demand, conftest's lxml FakePage and fake providers."""

import json
import random
import time

import pytest

import mendwright
from mendwright import fleet, load_store
from mendwright.backends import BackendError, Conflict, Denied, MemoryBackend, Unavailable
from mendwright.fleet import FLIGHTS, records_for
from mendwright.journal import Journal
from mendwright.remote import _health
from test_herd import CODE, HEALED, KEY, URL, V1, V2, _instant
from test_remote_store import Faulty, _doc, _health_is_fresh, _rows  # noqa: F401  (autouse)

ROLE = 'checkbox'
DOWN = Unavailable('could not reach the store: the connection was refused')


def _events(path, name=None):
    rows = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])
    return [e for e in rows if name is None or e['event'] == name]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _clocked(store):
    """The outage clocks of store's place, frozen: a test moves them on."""
    clock = Clock()
    for kind in ('read', 'write'):
        _health(store.remote_place, kind).clock = clock
    return clock


@pytest.fixture
def journal(tmp_path):
    return tmp_path / 'journal.jsonl'


def _store(backend, journal):
    store = load_store(None, backend)
    store.journal = Journal(journal)
    return store


def test_changes_wait_in_memory_and_go_with_a_write_once_the_store_answers(journal):
    backend = Faulty()
    store = _store(backend, journal)
    store.set_code_candidates(KEY, [CODE])
    clock = _clocked(store)
    backend.fail['get'] = backend.fail['put'] = DOWN
    asked = len(backend.calls)
    store.add_candidate(KEY, HEALED)                # the store does not answer
    assert len(backend.calls) == asked + 1          # one get, then the wait
    assert HEALED in {c['xpath'] for c in store.candidates(KEY)}
    [down] = _events(journal, 'store.unavailable')
    assert down['op'] == 'read' and down['error'] == 'Unavailable'
    assert down['store'] == store.remote_place and 'connection was refused' in down['reason']
    assert 1 <= down['retry_in'] <= 2
    # While it waits, nothing is asked: no request, no second event.
    store.set_meta(KEY, role=ROLE)
    store.refresh(KEY)
    assert len(backend.calls) == asked + 1 and len(_events(journal, 'store.unavailable')) == 1
    # The store answers again: the next write takes everything that waited along.
    backend.fail.clear()
    clock.now += 61
    store.set_meta(KEY, text='I agree')
    assert set(_rows(backend)) == {CODE, HEALED}
    entry = _doc(backend)['selectors'][KEY]
    assert entry['expectedRole'] == ROLE and entry['expectedText'] == ['I agree']
    [up] = _events(journal, 'store.available')
    assert up['op'] == 'read' and up['after'] == 61


@pytest.mark.parametrize('jitter', [0.8, 1.0])
def test_the_wait_after_failures_doubles_from_1_second_to_at_most_60(monkeypatch, jitter):
    from mendwright import remote
    asked = []

    def uniform(low, high):
        asked.append((low, high))
        return jitter
    monkeypatch.setattr(remote.random, 'uniform', uniform)
    health = remote._Health('read')
    health.clock = Clock()
    waits = []
    for _ in range(10):
        health.failed(DOWN)
        waits.append(health.wait())
    assert set(asked) == {(0.8, 1.0)}                     # the jitter only shortens a wait
    assert waits[0] == pytest.approx(jitter) and waits[1] == pytest.approx(2 * jitter)
    assert waits[-1] == pytest.approx(60.0 * jitter)      # at the cap: 48 to 60 seconds


def test_workers_that_wait_the_most_do_not_ask_the_store_again_together():
    # With the jitter before the cap, every worker of a fleet waited exactly 60 seconds once
    # the doubling passed about 75, and all asked the store again at the same moment.
    from mendwright import remote
    state = random.getstate()
    random.seed(20261103)
    try:
        waits = []
        for _ in range(2):
            health = remote._Health('read')
            health.clock = Clock()
            for _ in range(10):
                health.failed(DOWN)
            waits.append(health.wait())
    finally:
        random.setstate(state)
    assert all(48.0 <= w <= 60.0 for w in waits) and waits[0] != waits[1]


@pytest.mark.parametrize('error', [
    DOWN, Denied('the credentials were refused'),
    BackendError('the server answered HTTP 400 to PUT'),
], ids=['unavailable', 'denied', 'any-backend-error'])
def test_a_heal_made_while_the_store_fails_is_used_and_saved_later(error, make_page,
                                                                  stub_provider, tmp_path):
    backend = Faulty()
    store = load_store(None, backend)
    journal = tmp_path / 'journal.jsonl'
    mendwright.open(make_page(V1, url=URL), store=store, provider=stub_provider(None),
                    journal=Journal(journal), timeout_ms=0, fingerprint=False).locator(
        KEY, CODE, role=ROLE).check()
    clock = _clocked(store)
    backend.fail['put'] = error                     # reads work, writes fail
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, store=store, provider=stub_provider(HEALED),
                           journal=Journal(journal), timeout_ms=0, fingerprint=False)
    heal.locator(KEY, CODE).check()                 # healed, used, kept in memory
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert HEALED not in _rows(backend)
    [down] = _events(journal, 'store.unavailable')
    assert down['op'] == 'write' and down['error'] == type(error).__name__
    assert len(_events(journal, 'heal.applied')) == 1
    backend.fail.clear()
    clock.now += 61
    assert store.flush() is True                    # or with the next write
    assert HEALED in _rows(backend)
    assert [e['op'] for e in _events(journal, 'store.available')] == ['write']


def test_a_store_that_refuses_writes_is_still_read(journal):
    # A role that may read the store and not write it: the wait after a refused write does not
    # stop the reads, so another machine's heal still arrives.
    backend = Faulty()
    store = _store(backend, journal)
    store.set_code_candidates(KEY, [CODE])
    backend.fail['put'] = Denied('S3 refused PutObject (AccessDenied, HTTP 403)')
    store.set_meta(KEY, role=ROLE)                  # refused: waits in memory
    load_store(None, backend.inner).add_candidate(KEY, HEALED)   # another machine's heal
    assert store.refresh(KEY) is True and store.arrived(KEY, HEALED)
    [down] = _events(journal, 'store.unavailable')
    assert down['op'] == 'write' and down['error'] == 'Denied'


def test_an_outage_a_key_record_met_first_is_said_by_the_next_store_call(journal):
    backend = Faulty()
    store = _store(backend, journal)
    store.set_code_candidates(KEY, [CODE])
    backend.fail['get'] = DOWN
    with pytest.raises(Unavailable):
        records_for(store).look(KEY, time.time())       # the lease's look meets it
    assert _events(journal, 'store.unavailable') == []
    store.refresh(KEY)                                  # the next look of the store says it
    [down] = _events(journal, 'store.unavailable')
    assert down['op'] == 'read' and 'connection was refused' in down['reason']


def test_a_body_that_stops_being_a_selector_file_keeps_the_copy_read_and_is_never_replaced(
        journal, caplog):
    backend = Faulty()
    store = _store(backend, journal)
    store.set_code_candidates(KEY, [CODE])
    backend.inner.put('overrides.json', b'<<<<<<< HEAD')        # a bad hand merge
    assert store.refresh(KEY) is False and [c['xpath'] for c in store.candidates(KEY)] == [CODE]
    store.add_candidate(KEY, HEALED)
    assert backend.inner.get('overrides.json').body == b'<<<<<<< HEAD'
    assert 'cannot be read now' in caplog.text
    [event] = _events(journal, 'store.unavailable')
    assert event['error'] == 'StoreReadError' and event['op'] == 'write'


# --- bounded waits ------------------------------------------------------------------------------

def test_a_write_tries_at_most_five_times_with_short_pauses(journal):
    backend = Faulty()
    store = _store(backend, journal)
    store.set_code_candidates(KEY, [CODE])
    pauses = []
    store._sleep = pauses.append
    backend.fail['put'] = Conflict('another writer won')
    store.add_candidate(KEY, HEALED)
    assert len([c for c in backend.calls if c[0] == 'put']) == 2 + 4    # 1 + the 5 tries
    assert len(pauses) == 4 and all(0 <= p <= store.CAS_PAUSE for p in pauses)
    assert store._unsaved


def test_a_key_record_is_tried_eight_times_with_pauses_of_at_most_50_ms(monkeypatch):
    backend = Faulty()
    store = load_store(None, backend)
    records = records_for(store)
    pauses = []
    monkeypatch.setattr(fleet.time, 'sleep', pauses.append)
    backend.fail['put'] = Conflict('another writer won')
    with pytest.raises(Conflict, match='under 8 tries'):
        records.take(KEY, 'sig', 'me', 100.0, 15)
    assert len(pauses) == 7 and all(0 <= p <= 0.05 for p in pauses)


def test_a_lease_wait_ends_at_the_cap_whatever_the_lease_says(make_page, stub_provider,
                                                              tmp_path, monkeypatch):
    # A machine holds the key's lease and never gives it back: the leader here stops waiting
    # at FOLLOW_CAP (0.5 s in this test), then heals on its own.
    monkeypatch.setattr(fleet, 'FOLLOW_CAP', 0.5)
    backend = MemoryBackend()
    store = load_store(None, backend)
    mendwright.open(make_page(V1, url=URL), store=store, provider=stub_provider(None),
                    journal=Journal(tmp_path / 'j.jsonl'), timeout_ms=0,
                    fingerprint=False).locator(KEY, CODE, role=ROLE).check()
    records = records_for(store)
    records.take(KEY, fleet.page_signature('/checkout', [CODE]), 'stuck/1/x', time.time(), 3600)
    waited = []
    monkeypatch.setattr(FLIGHTS, '_pause', lambda seconds: (waited.append(seconds),
                                                            time.sleep(seconds)))
    provider = stub_provider(HEALED)
    heal = mendwright.open(make_page(V2, url=URL), store=store, provider=provider,
                           journal=Journal(tmp_path / 'j.jsonl'), timeout_ms=60000,
                           fingerprint=False)
    heal._clock = _instant()
    started = time.monotonic()
    heal.locator(KEY, CODE).check()
    assert time.monotonic() - started < 0.5 + 2 * fleet.REMOTE_LEASE_POLL + 1.0
    assert 1 <= len(waited) <= 4 and len(provider.bundles) == 1
