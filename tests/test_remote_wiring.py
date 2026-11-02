"""mendwright.open() and mendwright.aio.open() on a store URL (mendwright.remote): where the journal
goes, which rate limit and single-flight a remote store shares, what the pytest plugin passes on
and shows, and the fleet's coordination through the key records in the store itself. The
machines here are stores of their own on one MemoryBackend, each with a journal of its own.
Offline: conftest's lxml FakePage and fake providers; no model is called."""

import asyncio
import json
import logging
import threading
import types

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, fleet, load_store
from mendwright.backends import OVERRIDES, MemoryBackend, Unavailable, key_record_name
from mendwright.facade import _LIMITERS
from mendwright.fleet import DECLINES, FLIGHTS, records_for, store_name
from mendwright.journal import Journal
from mendwright.providers._errors import NOT_HERE_HINT
from mendwright.remote import RemoteSelectorStore
from test_aio import AsyncFakePage
from test_herd import CODE, HEALED, KEY, URL, V1, V2, Blocking, _count, _instant
from test_remote_store import Faulty, _health_is_fresh  # noqa: F401  (an autouse fixture)

ROLE = 'checkbox'
GONE = V2.replace("<label><input type='checkbox' data-testid='tos-optin'><span>I agree</span>"
                  "</label>", "<p>The terms are on the next page.</p>")


def _events(path):
    return ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])


def _machine(backend, page, tmp_path, name, provider, **kwargs):
    """A Healer of its own machine: a store of its own on the shared backend, and a journal
    in a folder of its own."""
    kwargs.setdefault('timeout_ms', 0)
    kwargs.setdefault('fingerprint', False)
    return mendwright.open(page, store=load_store(None, backend), provider=provider,
                           journal=Journal(tmp_path / name / 'journal.jsonl'), **kwargs)


def _warm(backend, make_page, tmp_path, stub_provider):
    _machine(backend, make_page(V1, url=URL), tmp_path, 'warm', stub_provider(None)).locator(
        KEY, CODE, role=ROLE).check()


# --- open() -------------------------------------------------------------------------------------

def test_open_takes_a_store_url_and_keeps_the_journal_on_this_machine(make_page, stub_provider,
                                                                      tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    heal = mendwright.open(make_page(V1, url=URL), store_path='memory:',
                           provider=stub_provider(None), timeout_ms=0, fingerprint=False)
    assert isinstance(heal.store, RemoteSelectorStore)
    heal.locator(KEY, CODE, role=ROLE).check()
    # The default folder is ./.mendwright, as for a local store, but holds only the journal.
    assert heal.coordinator.journal._path == tmp_path / '.mendwright' / 'journal.jsonl'
    assert heal.store.journal is heal.coordinator.journal
    assert not (tmp_path / '.mendwright' / 'overrides.json').exists()
    elsewhere = tmp_path / 'journals'
    monkeypatch.setenv('MENDWRIGHT_JOURNAL_DIR', str(elsewhere))
    monkeypatch.setenv('MENDWRIGHT_STORE', 'memory:')
    heal = mendwright.open(make_page(V1, url=URL), provider=stub_provider(None), timeout_ms=0,
                           fingerprint=False)
    assert isinstance(heal.store, RemoteSelectorStore)
    heal.locator(KEY, CODE, role=ROLE).check()
    assert heal.coordinator.journal._path == elsewhere / 'journal.jsonl'


def test_a_plain_path_is_a_local_store_as_before(make_page, stub_provider, tmp_path):
    heal = mendwright.open(make_page(V1, url=URL), store_path=str(tmp_path / 'o.json'),
                           provider=stub_provider(None), timeout_ms=0, fingerprint=False)
    assert not isinstance(heal.store, RemoteSelectorStore)
    heal.locator(KEY, CODE, role=ROLE).check()
    assert (tmp_path / 'o.json').is_file()
    assert heal.coordinator.journal._path == tmp_path / 'journal.jsonl'


def test_a_url_with_a_credential_is_refused_without_quoting_it(make_page, stub_provider):
    for url in ('https://user:SECRETVALUE@store.example/x/',
                'https://store.example/x/?token=SECRETVALUE', 'gs://bucket/x/'):
        with pytest.raises(ValueError) as info:
            mendwright.open(make_page(V1, url=URL), store_path=url, provider=stub_provider(None))
        assert 'SECRETVALUE' not in str(info.value) and 'store URL' in str(info.value)


def test_the_rate_limit_and_single_flight_are_per_store(make_page, stub_provider, tmp_path):
    one, two = MemoryBackend(), MemoryBackend()
    a = _machine(one, make_page(V1, url=URL), tmp_path, 'a', stub_provider(None))
    b = _machine(one, make_page(V1, url=URL), tmp_path, 'b', stub_provider(None))
    c = _machine(two, make_page(V1, url=URL), tmp_path, 'c', stub_provider(None))
    assert store_name(a.store) == store_name(b.store) == a.store.remote_place
    assert store_name(c.store) != store_name(a.store)
    limiter = a.coordinator.guardrails.rate_limiter
    assert b.coordinator.guardrails.rate_limiter is limiter
    assert c.coordinator.guardrails.rate_limiter is not limiter
    assert any(key[0] == a.store.remote_place for key in _LIMITERS)
    # The key records are the store's own, under keys/ in its backend.
    records = records_for(a.store)
    assert records.backend is a.store.backend and records.prefix == 'keys/'
    records.take(KEY, 'sig', 'me', 100.0, 15)
    assert one.get(key_record_name(KEY)).body is not None
    assert two.get(key_record_name(KEY)).body is None


def test_a_store_that_cannot_be_read_says_so_once_when_opened(make_page, stub_provider,
                                                              tmp_path, caplog):
    backend = Faulty()
    backend.fail['get'] = Unavailable('could not reach the store: the connection was refused')
    journal = tmp_path / 'journal.jsonl'
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        heal = mendwright.open(make_page(V1, url=URL), store=load_store(None, backend),
                               provider=stub_provider(HEALED), journal=Journal(journal),
                               timeout_ms=0, fingerprint=False)
    [line] = [r.getMessage() for r in caplog.records]
    assert line.startswith('mendwright.open(): mendwright could not read the store ' + backend.name)
    assert 'heals nothing until it reads the store' in line
    [event] = [e for e in _events(journal) if e['event'] == 'store.unavailable']
    assert event['op'] == 'open' and event['error'] == 'Unavailable'
    assert event['store'] == heal.store.remote_place


def test_the_async_healer_reads_a_remote_store_off_its_event_loop(make_page, stub_provider,
                                                                  tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    threads = []
    real = mendwright.aio.load_store

    def loading(*args, **kwargs):
        threads.append(threading.current_thread())
        return real(*args, **kwargs)
    monkeypatch.setattr(mendwright.aio, 'load_store', loading)
    page = make_page(V1, url=URL)

    async def main():
        heal = await mendwright.aio.open(AsyncFakePage(page), store_path='memory:',
                                         provider=stub_provider(None), timeout_ms=0,
                                         fingerprint=False)
        await heal.locator(KEY, CODE, role=ROLE).check()
        return heal
    heal = asyncio.run(main())
    assert isinstance(heal.store, RemoteSelectorStore)
    assert threads and threads[0] is not threading.main_thread()


# --- the fleet: several machines on one store --------------------------------------------------

def test_a_herd_of_machines_makes_one_model_call(make_page, stub_provider, tmp_path):
    backend = MemoryBackend()
    _warm(backend, make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for i in range(8):
        page = make_page(V2, url=URL)
        heal = _machine(backend, page, tmp_path, 'm%d' % i, Blocking(calls, release, changed),
                        timeout_ms=60000)
        heal._clock = _instant()
        workers.append((page, heal))
    errors = []

    def run(heal):
        try:
            heal.locator(KEY, CODE).check()
        except Exception as exc:        # noqa: BLE001 - reported below
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(heal,)) for _page, heal in workers]
    for thread in threads:
        thread.start()
    with changed:
        assert changed.wait_for(lambda: len(calls) == 1, 60)
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == 7, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    events = [e for i in range(8) for e in _events(tmp_path / ('m%d' % i) / 'journal.jsonl')]
    assert len(calls) == 1
    assert _count(events, 'heal.applied') == 1 and _count(events, 'heal.adopted') == 7
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page, _h in workers)
    # The leader counted its attempt in the key's record, in the store.
    record = json.loads(backend.get(key_record_name(KEY)).body)
    assert len(record['attempts']) == 1 and 'lease' not in record


def test_a_decline_reaches_the_other_machines_through_the_store(make_page, stub_provider,
                                                                tmp_path):
    backend = MemoryBackend()
    _warm(backend, make_page, tmp_path, stub_provider)
    first = stub_provider(None, last_error='the model says no element on this page fits: '
                                           'the consent checkbox is not on this page. '
                                           + NOT_HERE_HINT)
    with pytest.raises(AttentionRequired):
        _machine(backend, make_page(GONE, url=URL), tmp_path, 'a', first).locator(KEY, CODE).check()
    assert len(first.bundles) == 1
    record = json.loads(backend.get(key_record_name(KEY)).body)
    assert [row['code'] for row in record['declines'].values()] == ['not_here']
    DECLINES.clear()                    # another process: nothing in its memory
    second = stub_provider(HEALED)
    with pytest.raises(AttentionRequired) as info:
        _machine(backend, make_page(GONE, url=URL), tmp_path, 'b', second).locator(
            KEY, CODE).check()
    assert second.bundles == []
    assert 'reuses its answer for 600 seconds' in str(info.value)
    [declined] = [e for e in _events(tmp_path / 'b' / 'journal.jsonl')
                  if e['event'] == 'heal.declined']
    assert declined['code'] == 'not_here' and declined['cached'] is True


def test_a_quarantine_reaches_the_other_machines(make_page, stub_provider, tmp_path):
    backend = MemoryBackend()
    _warm(backend, make_page, tmp_path, stub_provider)
    load_store(None, backend).add_candidate(KEY, HEALED)
    page_b = make_page(V2, url=URL)
    b = _machine(backend, page_b, tmp_path, 'b', stub_provider(None))      # open before
    page_a = make_page(V2, url=URL)
    a = _machine(backend, page_a, tmp_path, 'a', stub_provider(None))
    a.locator(KEY, CODE).check()
    a.coordinator.confirm(KEY, HEALED, False, by='action')     # its action failed
    rows = {c['xpath']: c for c in json.loads(backend.get(OVERRIDES).body)['selectors'][KEY][
        'candidates']}
    assert rows[HEALED]['status'] == 'quarantined'
    b.store._looked.clear()                 # its next lookup looks at the store again
    with pytest.raises(AttentionRequired):
        b.locator(KEY, CODE).check()
    assert not any(a[:2] == ('xpath=' + HEALED, 'check') for a in page_b.actions)
    assert b.store.status(KEY, HEALED) == 'quarantined'


def test_a_leader_waits_for_another_machines_lease_in_the_store(make_page, stub_provider,
                                                                tmp_path, monkeypatch):
    # Another machine holds the key's lease for this page in the store's record and heals it:
    # this machine's leader waits, then uses that heal. The record is the store's: the
    # wait looks at it four times a second.
    backend = MemoryBackend()
    _warm(backend, make_page, tmp_path, stub_provider)
    store = load_store(None, backend)
    records = records_for(store)
    assert records.poll == fleet.REMOTE_LEASE_POLL
    owner = 'another-machine/1/abc'
    records.take(KEY, fleet.page_signature('/checkout', [CODE]), owner, __import__('time').time(),
                 15)
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            load_store(None, backend).add_candidate(KEY, HEALED)
            records.release(KEY, owner)
    monkeypatch.setattr(FLIGHTS, '_pause', pause)
    provider = stub_provider(None)
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, store=store, provider=provider, timeout_ms=100,
                           fingerprint=False, journal=Journal(tmp_path / 'journal.jsonl'))
    heal._clock = _instant()
    heal.locator(KEY, CODE).check()
    assert pauses == [fleet.REMOTE_LEASE_POLL] * 3 and provider.bundles == []
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')


# --- the pytest plugin ---------------------------------------------------------------------------

def _config(tmp_path, option=None, ini=''):
    return types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(dir=tmp_path), inipath=None, rootpath=tmp_path,
        getoption=lambda name: option, getini=lambda name: ini)


def test_the_plugin_passes_a_store_url_on_and_shows_it_without_a_credential(tmp_path,
                                                                           monkeypatch):
    from mendwright.pytest_plugin import _shown_path, _store_path
    assert _store_path(_config(tmp_path, option='s3://bucket/team/')) == 's3://bucket/team/'
    assert _store_path(_config(tmp_path, ini='https://store.example/x/')) == \
        'https://store.example/x/'
    monkeypatch.setenv('MENDWRIGHT_STORE', 'memory:')
    assert _store_path(_config(tmp_path)) == 'memory:'
    assert _shown_path('s3://bucket/team/overrides.json', tmp_path) == 's3://bucket/team/'
    shown = _shown_path('https://user:SECRETVALUE@store.example/x/', tmp_path)
    assert 'SECRETVALUE' not in shown and shown.startswith('a store URL mendwright refuses')
    # A local path is shown as before.
    assert _store_path(_config(tmp_path, option='o.json')) == str(tmp_path / 'o.json')
