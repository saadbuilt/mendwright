"""Read-only workers (MENDWRIGHT_STORE_READ_ONLY=1, load_store(writes=False)): a production worker
that takes in the heals and quarantines other workers save, and never writes the store or its key
records. Its own heals stay in memory, for its process, and the journal says so (saved: false).
Offline: a MemoryBackend behind a counting wrapper, conftest's lxml FakePage and fake providers."""

import json
import time

import pytest

import mendwright
from mendwright import AttentionRequired, load_store
from mendwright.backends import Denied
from mendwright.fleet import KeyRecords, ReadOnlyRecords, page_signature, records_for
from mendwright.journal import Journal
from mendwright.providers._errors import NOT_HERE_HINT
from test_herd import CODE, HEALED, KEY, URL, V1, V2, _instant
from test_remote_store import Faulty, _health_is_fresh  # noqa: F401  (an autouse fixture)
from test_remote_wiring import GONE

ROLE = 'checkbox'


def _events(path, name):
    rows = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])
    return [e for e in rows if e['event'] == name]


def _warm(backend, make_page, stub_provider, tmp_path):
    mendwright.open(make_page(V1, url=URL), store=load_store(None, backend),
                    provider=stub_provider(None), journal=Journal(tmp_path / 'warm.jsonl'),
                    timeout_ms=0, fingerprint=False).locator(KEY, CODE, role=ROLE).check()


def test_a_read_only_worker_writes_nothing_through_heals_adoptions_declines_and_quarantines(
        make_page, stub_provider, tmp_path, monkeypatch):
    backend = Faulty()
    _warm(backend.inner, make_page, stub_provider, tmp_path)
    monkeypatch.setenv('MENDWRIGHT_STORE_READ_ONLY', '1')
    journal = tmp_path / 'journal.jsonl'

    def worker(html, provider, **kwargs):
        page = make_page(html, url=URL)
        heal = mendwright.open(page, store=load_store(None, backend), provider=provider,
                               journal=Journal(journal), timeout_ms=0, fingerprint=False,
                               **kwargs)
        assert heal.store.writes is False
        return page, heal
    # Its own heal: used, and kept in memory.
    page, heal = worker(V2, stub_provider(HEALED))
    heal.locator(KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    [applied] = _events(journal, 'heal.applied')
    assert applied['saved'] is False
    # The next action fails: quarantined, in memory.
    heal.coordinator.confirm(KEY, HEALED, False, by='action')
    [quarantined] = _events(journal, 'heal.quarantine')
    assert quarantined['saved'] is False
    # A heal another worker saves after this worker opened is adopted.
    page, heal = worker(V2, stub_provider(None))
    load_store(None, backend.inner).add_candidate(KEY, HEALED)
    heal.locator(KEY, CODE).check()
    assert len(_events(journal, 'heal.adopted')) == 1
    # A decline: the model's answer is not kept in the store's record.
    declining = stub_provider(None, last_error='the model says no element on this page fits. '
                                               + NOT_HERE_HINT)
    page, heal = worker(GONE, declining, rate_per_key_per_hour=None)
    with pytest.raises(AttentionRequired):
        heal.locator('gone_key', '#gone', role=ROLE).check()
    assert backend.writes() == []
    assert not [c for c in backend.calls if c[1].startswith('keys/') and c[0] != 'get']


def test_a_read_only_worker_takes_in_what_other_workers_save(make_page, stub_provider,
                                                            tmp_path, monkeypatch):
    backend = Faulty()
    _warm(backend.inner, make_page, stub_provider, tmp_path)
    monkeypatch.setenv('MENDWRIGHT_STORE_READ_ONLY', '1')
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, store=load_store(None, backend), provider=stub_provider(None),
                           journal=Journal(tmp_path / 'j.jsonl'), timeout_ms=0,
                           fingerprint=False)
    writer = load_store(None, backend.inner)
    writer.add_candidate(KEY, HEALED)
    heal.locator(KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    writer.quarantine(KEY, HEALED)
    heal.store._looked.clear()
    assert heal.store.refresh(KEY) is True
    assert heal.store.status(KEY, HEALED) == 'quarantined'
    assert backend.writes() == []


def test_a_read_only_worker_waits_for_a_writers_lease_and_takes_none(make_page, stub_provider,
                                                                     tmp_path, monkeypatch):
    backend = Faulty()
    _warm(backend.inner, make_page, stub_provider, tmp_path)
    writer_records = KeyRecords(backend.inner)          # another machine's, not counted
    owner = 'a-writer/1/abc'
    writer_records.take(KEY, page_signature('/checkout', [CODE]), owner, time.time(), 15)
    store = load_store(None, backend, writes=False)
    records = records_for(store)
    assert isinstance(records, ReadOnlyRecords)
    with pytest.raises(Denied):
        records.take(KEY, 'sig', 'me', time.time(), 15)
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            load_store(None, backend.inner).add_candidate(KEY, HEALED)
            writer_records.release(KEY, owner)
    from mendwright.fleet import FLIGHTS
    monkeypatch.setattr(FLIGHTS, '_pause', pause)
    del backend.calls[:]
    provider = stub_provider(None)
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, store=store, provider=provider, timeout_ms=100,
                           fingerprint=False, journal=Journal(tmp_path / 'j.jsonl'))
    heal._clock = _instant()
    heal.locator(KEY, CODE).check()
    assert len(pauses) == 2 and provider.bundles == []
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert backend.writes() == []


def test_read_only_holds_for_a_local_store_too(make_page, stub_provider, tmp_path, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_STORE_READ_ONLY', '1')
    path = tmp_path / 'overrides.json'
    mendwright.open(make_page(V1, url=URL), store_path=str(path), provider=stub_provider(None),
                    timeout_ms=0, fingerprint=False)       # nothing written yet: read-only
    assert not path.exists()
    path.write_text(json.dumps({'schema': 2, 'selectors': {KEY: {
        'expectedRole': ROLE, 'pages': ['/checkout'], 'lastPage': '/checkout',
        'candidates': [{'xpath': CODE, 'source': 'code', 'status': 'active',
                        'addedAt': '2026-10-01T00:00:00+00:00',
                        'lastSucceededAt': '2026-10-01T00:00:00+00:00'}]}}}), encoding='utf-8')
    before = path.read_bytes()
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, store_path=str(path), provider=stub_provider(HEALED),
                           timeout_ms=0, fingerprint=False)
    heal.locator(KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert heal.store.flush() is True
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['journal.jsonl', 'overrides.json']
    [applied] = _events(tmp_path / 'journal.jsonl', 'heal.applied')
    assert applied['saved'] is False


def test_a_value_read_only_does_not_take_stops_open_and_doctor_lists_it(make_page,
                                                                       stub_provider,
                                                                       monkeypatch):
    from mendwright.cli import _settings_problems
    monkeypatch.setenv('MENDWRIGHT_STORE_READ_ONLY', 'maybe')
    with pytest.raises(ValueError, match='MENDWRIGHT_STORE_READ_ONLY must be 1 or 0'):
        mendwright.open(make_page(V1, url=URL), store_path='memory:',
                        provider=stub_provider(None))
    assert any('MENDWRIGHT_STORE_READ_ONLY' in p for p in _settings_problems())
