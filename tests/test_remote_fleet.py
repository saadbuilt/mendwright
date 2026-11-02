"""A fleet on one remote store: M processes (multiprocessing, spawn), the machines, of T threads
each, share a store served by the reference HTTP server on 127.0.0.1, each with a journal folder
of its own (MENDWRIGHT_JOURNAL_DIR), and hit one broken key. One model call heals it for the whole
fleet, through the lease in the key's record in the store; the others adopt its heal after the
live check, or reuse its decline. Deterministic: the provider blocks on a multiprocessing Event
until every process says its followers wait (its threads on its own flight, and its own leader
on the lease of another process, or in propose() itself); no sleeps for timing. No model is
called: providers here are fakes. Exact counts, from the journals of every machine."""

import json
import multiprocessing
import threading

from mendwright.backends import key_record_name
from test_backends_contract import TOKEN
from test_herd import CODE, KEY, URL, V2, _html
from test_herd_processes import HEALED, ProcProvider

M, T = 4, 3
GONE = _html("<p class='note'>You accept the terms on the next page.</p>")


def _machine(index, url, folder, html, mode, shared):
    """One machine: T Healers on the remote store look KEY up at once."""
    import itertools
    import os
    os.environ['MENDWRIGHT_JOURNAL_DIR'] = folder
    import mendwright
    import mendwright.fleet as fleet
    from conftest import FakePage
    mine = threading.Event()
    healers = []
    for _ in range(T):
        page = FakePage(html, url=URL)
        heal = mendwright.open(page, provider=ProcProvider(shared, mode, mine), store_path=url,
                               timeout_ms=60000, fingerprint=False)
        assert heal.store.remote_place == url and heal.store.unread is None
        counter = itertools.count(0, 1000)
        heal._clock = lambda counter=counter: float(next(counter))   # the poll gives up at once
        healers.append(heal)

    def run(heal):
        try:
            heal.locator(KEY, CODE).check()
        except Exception:       # noqa: BLE001 - outcomes are read from the journals
            pass
    threads = [threading.Thread(target=run, args=(h,)) for h in healers]
    for thread in threads:
        thread.start()
    # Parked: the other threads wait on this machine's flight, and its leader waits on the lease
    # of another machine, in the store, or is the one in propose().
    fleet.FLIGHTS.wait_until(lambda: fleet.FLIGHTS.waiting() == T - 1 and (
        fleet.FLIGHTS.lease_waiting() == 1 or mine.is_set()), 120)
    with shared['cond']:
        shared['ready'].value += 1
        shared['cond'].notify_all()
    for thread in threads:
        thread.join(180)


def _fleet(tmp_path, monkeypatch, make_page, stub_provider, html, mode):
    import mendwright
    from mendwright.backends.server import StoreServer
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    with StoreServer(tmp_path / 'served', token=TOKEN) as server:
        url = server.url
        warm = mendwright.open(make_page(_html("<label><input id='accept-terms' type='checkbox'>"
                                               "<span>I accept</span></label>"), url=URL),
                               store_path=url, provider=stub_provider(None), timeout_ms=0,
                               fingerprint=False)
        warm.locator(KEY, CODE, role='checkbox').check()
        ctx = multiprocessing.get_context('spawn')
        shared = {'calls': ctx.Value('i', 0), 'ready': ctx.Value('i', 0),
                  'cond': ctx.Condition(), 'release': ctx.Event()}
        folders = [str(tmp_path / ('machine%d' % i)) for i in range(M)]
        procs = [ctx.Process(target=_machine, args=(i, url, folders[i], html, mode, shared))
                 for i in range(M)]
        for proc in procs:
            proc.start()
        with shared['cond']:
            assert shared['cond'].wait_for(lambda: shared['ready'].value == M, 180)
        shared['release'].set()
        for proc in procs:
            proc.join(240)
        assert not any(p.is_alive() for p in procs)
        assert [p.exitcode for p in procs] == [0] * M
        from mendwright.backends.http import HttpBackend
        record = json.loads(HttpBackend(url).get(key_record_name(KEY)).body)
        store = json.loads(HttpBackend(url).get('overrides.json').body)
    events = []
    for folder in folders:
        path = tmp_path / folder / 'journal.jsonl'
        events += [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    return shared['calls'].value, events, record, store


def _count(events, name, **fields):
    return sum(1 for e in events if e['event'] == name
               and all(e.get(k) == v for k, v in fields.items()))


def test_a_fleet_on_one_remote_store_makes_one_model_call(tmp_path, monkeypatch, make_page,
                                                          stub_provider):
    calls, events, record, store = _fleet(tmp_path, monkeypatch, make_page, stub_provider, V2,
                                          'heal')
    assert calls == 1
    assert _count(events, 'heal.applied') == 1
    assert _count(events, 'heal.adopted') == M * T - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    # The lease and the budget were the store's: one attempt in the key's record, no lease left.
    assert len(record['attempts']) == 1 and 'lease' not in record
    rows = {c['xpath'] for c in store['selectors'][KEY]['candidates']}
    assert rows == {CODE, HEALED}


def test_a_fleet_reuses_one_decline_through_the_store(tmp_path, monkeypatch, make_page,
                                                      stub_provider):
    calls, events, record, _store = _fleet(tmp_path, monkeypatch, make_page, stub_provider, GONE,
                                           'decline')
    assert calls == 1
    assert _count(events, 'heal.declined', code='not_here') == M * T
    assert _count(events, 'heal.declined', code='not_here', cached=True) == M * T - 1
    assert [row['code'] for row in record['declines'].values()] == ['not_here']

