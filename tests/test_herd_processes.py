"""Herds across processes: M processes (multiprocessing, spawn) of T threads each, on one store
file, hit the same broken key, and mendwright heals it once (the key's lease, see
mendwright.fleet.KeyRecords). Deterministic: the provider blocks on a multiprocessing Event
until every process says its followers wait (its threads on its in-process flight, and its own
leader on the lease of another process, or in propose() itself); no sleeps for timing. No
model is called: providers here are fakes. Exact counts, from the journal they all share."""

import json
import multiprocessing
import threading

import pytest

from test_herd import CODE, KEY, URL, V2, _html, _warm

M, T = 4, 3
GONE = _html("<p class='note'>You accept the terms on the next page.</p>")
HEALED = "//input[@data-testid='tos-optin']"
NOT_HERE_REASON = ('the model says no element on this page fits: the consent checkbox is not '
                   'on this page. ')


class ProcProvider:
    """A provider for the processes: counts calls in a shared Value, then blocks until the
    test releases it ('block'), or dies with the process ('die', only the first call of all),
    and heals or declines."""

    def __init__(self, shared, mode, mine):
        self.shared, self.mode, self.mine = shared, mode, mine
        self.last_error = None

    def propose(self, bundle):
        from mendwright.fleet import FLIGHTS
        from mendwright.providers._errors import NOT_HERE_HINT
        calls, cond, release = self.shared['calls'], self.shared['cond'], self.shared['release']
        with cond:
            calls.value += 1
            first = calls.value == 1
            cond.notify_all()
        if self.mode.endswith('die') and first:
            import os
            os._exit(3)                         # the leader dies, holding the lease
        self.mine.set()
        with FLIGHTS._changed:
            FLIGHTS._changed.notify_all()       # this process's wait for readiness looks again
        if not self.mode.endswith('die'):
            assert release.wait(120), 'the test never released the provider'
        if self.mode.startswith('decline'):
            self.last_error = NOT_HERE_REASON + NOT_HERE_HINT
            return None
        return {'xpath': HEALED, 'fallbacks': [], 'reasoning': 'fake'}

    def report_outcome(self, key, xpath, success):
        pass


def _fake_page_class():
    """FakePage from this folder's conftest.py, for a spawned process. It can't ask pytest for
    it, and the name conftest can be another folder's: a process copies the run's sys.path, and
    a whole run under pytest 7 puts tests/browser, with a conftest.py of its own, first."""
    import importlib.util
    import pathlib
    import sys
    path = pathlib.Path(__file__).resolve().with_name('conftest.py')
    spec = importlib.util.spec_from_file_location('mendwright_tests_conftest', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FakePage


def _worker(index, path, html, mode, shared, lease_ttl):
    """One process: T Healers on the shared store file look KEY up at once."""
    import itertools
    import mendwright
    import mendwright.fleet as fleet
    FakePage = _fake_page_class()
    if lease_ttl:
        fleet.LEASE_TTL = lease_ttl
    mine = threading.Event()
    healers = []
    for _ in range(T):
        page = FakePage(html, url=URL)
        heal = mendwright.open(page, provider=ProcProvider(shared, mode, mine), store_path=path,
                               timeout_ms=60000, fingerprint=False)
        counter = itertools.count(0, 1000)
        heal._clock = lambda counter=counter: float(next(counter))   # the poll gives up at once
        healers.append(heal)

    def run(heal):
        try:
            heal.locator(KEY, CODE).check()
        except Exception:       # noqa: BLE001 - outcomes are read from the journal
            pass
    threads = [threading.Thread(target=run, args=(h,)) for h in healers]
    for thread in threads:
        thread.start()
    if not mode.endswith('die'):
        # Parked: the other threads wait on this process's flight, and its leader waits on the
        # lease of another process, or is the one in propose().
        fleet.FLIGHTS.wait_until(lambda: fleet.FLIGHTS.waiting() == T - 1 and (
            fleet.FLIGHTS.lease_waiting() == 1 or mine.is_set()), 120)
        with shared['cond']:
            shared['ready'].value += 1
            shared['cond'].notify_all()
    for thread in threads:
        thread.join(180)


def _run(tmp_path, html, mode, lease_ttl=None, expired_lease=False):
    path = str(tmp_path / 'overrides.json')
    ctx = multiprocessing.get_context('spawn')
    shared = {'calls': ctx.Value('i', 0), 'ready': ctx.Value('i', 0), 'cond': ctx.Condition(),
              'release': ctx.Event()}
    if expired_lease:
        from mendwright import load_store
        from mendwright.fleet import records_for
        records_for(load_store(None, path)).take(KEY, 'any', 'dead-host/1/gone', 0.0, 15)
    procs = [ctx.Process(target=_worker, args=(i, path, html, mode, shared, lease_ttl))
             for i in range(M)]
    for proc in procs:
        proc.start()
    if not mode.endswith('die'):
        with shared['cond']:
            assert shared['cond'].wait_for(lambda: shared['ready'].value == M, 180)
        shared['release'].set()
    for proc in procs:
        proc.join(240)
    assert not any(p.is_alive() for p in procs)
    events = [json.loads(line) for line in
              (tmp_path / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
    return shared['calls'].value, events, [p.exitcode for p in procs]


def _count(events, name, **fields):
    return sum(1 for e in events if e['event'] == name
               and all(e.get(k) == v for k, v in fields.items()))


@pytest.fixture
def warm(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)


def test_a_herd_of_processes_makes_one_model_call(tmp_path, warm):
    calls, events, codes = _run(tmp_path, V2, 'heal')
    assert codes == [0] * M
    assert calls == 1
    assert _count(events, 'heal.applied') == 1
    assert _count(events, 'heal.adopted') == M * T - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0


def test_a_decline_is_shared_by_every_process(tmp_path, warm):
    calls, events, codes = _run(tmp_path, GONE, 'decline')
    assert calls == 1
    assert _count(events, 'heal.declined', code='not_here') == M * T
    assert _count(events, 'heal.declined', code='not_here', cached=True) == M * T - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0


def test_an_expired_lease_is_taken_over_by_exactly_one_process(tmp_path, warm):
    calls, events, codes = _run(tmp_path, V2, 'heal', expired_lease=True)
    assert calls == 1 and _count(events, 'heal.adopted') == M * T - 1


def test_when_the_leader_dies_one_waiter_takes_over(tmp_path, warm):
    # The first process to ask the model dies holding the lease; it is not renewed, expires
    # after 2 s, and exactly one of the others takes it over and heals.
    calls, events, codes = _run(tmp_path, V2, 'heal-die', lease_ttl=2.0)
    assert sorted(codes) == [0] * (M - 1) + [3]
    assert calls == 2
    assert _count(events, 'heal.applied') == 1
    assert _count(events, 'heal.adopted') == (M - 1) * T - 1
