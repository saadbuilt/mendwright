"""Herds: many workers of one process hit the same broken key at once, and mendwright heals it
once (mendwright.fleet). Deterministic: the fake provider blocks on a threading.Event until the
test has seen every follower waiting (FLIGHTS.wait_until, no sleeps for timing), and the
lookups' poll clock jumps, so the poll gives up at once and only the follower's wait (the
step's timeout) runs on the real clock. No model is called: providers here are fakes.

The counts are exact: a heal gives 1 propose(), N-1 adoptions and 0 rate declines. The decline
herd and the herd of processes are in test_herd_declines.py and test_herd_processes.py."""

import asyncio
import concurrent.futures
import itertools
import json
import threading
import time

import pytest

import mendwright
import mendwright.aio
import mendwright.fleet as fleet
import mendwright.store as store_module
from mendwright import AttentionRequired, load_store
from mendwright.fleet import FLIGHTS, page_signature, records_for
from test_aio import AsyncFakePage

URL = 'https://shop.test/checkout'
KEY = 'accept_terms'
CODE = '#accept-terms'
HEALED = "//input[@data-testid='tos-optin']"
N = 12


def _html(body):
    return ("<html><body><main><h1>Checkout</h1><form id='checkout'>"
            "<label for='email'>Email</label><input id='email' name='email' type='email'>"
            + body + "<button id='pay' type='submit'>Pay now</button></form></main></body></html>")


V1 = _html("<label><input id='accept-terms' type='checkbox'><span>I accept</span></label>")
V2 = _html("<label><input type='checkbox' data-testid='tos-optin'><span>I agree</span></label>")


class Blocking:
    """A provider that counts its calls in calls (shared by every worker) and blocks each
    one until release is set, then proposes HEALED."""

    def __init__(self, calls, release, changed):
        self.calls, self.release, self.changed = calls, release, changed

    def propose(self, bundle):
        with self.changed:
            self.calls.append(bundle.get('key'))
            self.changed.notify_all()
        assert self.release.wait(60), 'the test never released the provider'
        return {'xpath': HEALED, 'fallbacks': [], 'reasoning': 'fake'}

    def report_outcome(self, key, xpath, success):
        pass


def _instant():
    """A poll clock that jumps 1000 s each time it is read: the poll gives up at once."""
    counter = itertools.count(0, 1000)
    return lambda: float(next(counter))


def _store(tmp_path):
    return str(tmp_path / 'overrides.json')


def _warm(make_page, tmp_path, stub_provider):
    """The store after a run on V1, a day before: the code selector, its role and page."""
    page = make_page(V1, url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0, fingerprint=False)
    heal.locator(KEY, CODE, role='checkbox').check()
    path = tmp_path / 'overrides.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    for row in data['selectors'][KEY]['candidates']:
        row['lastSucceededAt'] = row['addedAt'] = '2026-10-01T00:00:00+00:00'
    path.write_text(json.dumps(data), encoding='utf-8')


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _count(events, name, **fields):
    return sum(1 for e in events if e['event'] == name
               and all(e.get(k) == v for k, v in fields.items()))


@pytest.fixture
def writes(monkeypatch):
    count = []
    real = store_module._atomic_write

    def counting(path, text, sleep=None):
        count.append(path)
        return real(path, text) if sleep is None else real(path, text, sleep)
    monkeypatch.setattr(store_module, '_atomic_write', counting)
    return count


def test_a_herd_of_threads_makes_one_model_call(tmp_path, make_page, stub_provider, writes):
    _warm(make_page, tmp_path, stub_provider)
    del writes[:]
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for _ in range(N):
        page = make_page(V2, url=URL)
        heal = mendwright.open(page, provider=Blocking(calls, release, changed),
                               store_path=_store(tmp_path), timeout_ms=60000, fingerprint=False)
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
    # The leader is in propose(), and everyone else waits for it (both only grow until then).
    with changed:
        assert changed.wait_for(lambda: len(calls) == 1, 60)
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    events = _journal(tmp_path)
    assert len(calls) == 1
    assert _count(events, 'heal.applied') == 1
    assert _count(events, 'heal.adopted') == N - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page, _h in workers)
    # The heal and its first success: nobody else writes the store.
    assert len(writes) == 2


def test_healers_that_share_one_store_make_one_model_call(tmp_path, make_page, stub_provider):
    # open(store=shared): the leader's heal is in the shared store's memory when a follower
    # wakes, among the store's selectors, and never arrives from the file. A follower adopts
    # it as a heal added since its own selectors missed, instead of asking its model.
    _warm(make_page, tmp_path, stub_provider)
    shared = load_store(None, _store(tmp_path))
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for _ in range(N):
        page = make_page(V2, url=URL)
        heal = mendwright.open(page, provider=Blocking(calls, release, changed), store=shared,
                               timeout_ms=60000, fingerprint=False)
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
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    events = _journal(tmp_path)
    assert len(calls) == 1 and _count(events, 'heal.adopted') == N - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page, _h in workers)
    assert sum(len(h.pending()) for _page, h in workers) == N     # each one's canary decides


def test_async_healers_that_share_one_store_make_one_model_call(tmp_path, make_page,
                                                                 stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    shared = load_store(None, _store(tmp_path))
    calls, release, changed = [], threading.Event(), threading.Condition()
    pages = [make_page(V2, url=URL) for _ in range(N)]

    async def main():
        healers = []
        for page in pages:
            heal = await mendwright.aio.open(AsyncFakePage(page), store=shared,
                                             provider=Blocking(calls, release, changed),
                                             timeout_ms=60000, fingerprint=False)
            heal._clock = _instant()
            healers.append(heal)
        tasks = [asyncio.ensure_future(h.locator(KEY, CODE).check()) for h in healers]

        def ready():
            with changed:
                changed.wait_for(lambda: len(calls) == 1, 60)
            return FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
        waited = await asyncio.to_thread(ready)
        release.set()
        await asyncio.gather(*tasks)
        return waited
    assert asyncio.run(main())
    assert len(calls) == 1 and _count(_journal(tmp_path), 'heal.adopted') == N - 1
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page in pages)


def test_the_async_healer_follows_on_its_event_loop(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    pages = [make_page(V2, url=URL) for _ in range(N)]

    async def main():
        # Two worker threads in all: the leader's heal takes one, this test's wait the other.
        # A follower that held a thread while it waits would leave no room for either.
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=2))
        healers = []
        for page in pages:
            heal = await mendwright.aio.open(AsyncFakePage(page),
                                             provider=Blocking(calls, release, changed),
                                             store_path=_store(tmp_path), timeout_ms=60000,
                                             fingerprint=False)
            heal._clock = _instant()
            healers.append(heal)
        tasks = [asyncio.ensure_future(h.locator(KEY, CODE).check()) for h in healers]

        def ready():
            with changed:
                changed.wait_for(lambda: len(calls) == 1, 60)
            return FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
        waited = await asyncio.to_thread(ready)
        release.set()
        await asyncio.gather(*tasks)
        return waited
    assert asyncio.run(main())
    events = _journal(tmp_path)
    assert len(calls) == 1
    assert _count(events, 'heal.adopted') == N - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page in pages)


def test_a_follower_waits_for_the_leader_past_its_step_timeout(tmp_path, make_page,
                                                               stub_provider, monkeypatch):
    # A step timeout of 200 ms and a model that answers later: the followers still wait for
    # the leader (at most FOLLOW_CAP), so there is one model call, not one per worker.
    _warm(make_page, tmp_path, stub_provider)
    waits = []
    real_wait = FLIGHTS.wait

    def wait(flight, timeout):
        waits.append(timeout)
        return real_wait(flight, timeout)
    monkeypatch.setattr(FLIGHTS, 'wait', wait)
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for _ in range(N):
        page = make_page(V2, url=URL)
        heal = mendwright.open(page, provider=Blocking(calls, release, changed),
                               store_path=_store(tmp_path), timeout_ms=200, fingerprint=False)
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
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    assert waits == [fleet.FOLLOW_CAP] * (N - 1) and fleet.FOLLOW_CAP == 60.0
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    events = _journal(tmp_path)
    assert len(calls) == 1
    assert _count(events, 'heal.adopted') == N - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    assert all(page.actions[-1][:2] == ('xpath=' + HEALED, 'check') for page, _h in workers)


def test_a_follower_stops_waiting_at_the_cap_then_tries_itself(tmp_path, make_page,
                                                                stub_provider, monkeypatch):
    monkeypatch.setattr(fleet, 'FOLLOW_CAP', 0.2)
    _warm(make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    workers = []
    for _ in range(4):
        page = make_page(V2, url=URL)
        heal = mendwright.open(page, provider=Blocking(calls, release, changed),
                               store_path=_store(tmp_path), timeout_ms=60000, fingerprint=False,
                               rate_per_key_per_hour=3)
        heal._clock = _instant()
        workers.append(heal)
    outcomes = []

    def run(heal):
        try:
            heal.locator(KEY, CODE).check()
            outcome = 'ok'
        except AttentionRequired as exc:
            outcome = str(exc)
        with changed:
            outcomes.append(outcome)
            changed.notify_all()
    threads = [threading.Thread(target=run, args=(heal,)) for heal in workers]
    for thread in threads:
        thread.start()
    # The leader blocks. Each follower stops waiting at the cap (200 ms here) and tries itself,
    # under the normal caps: two more attempts fit the per-key cap of 3, the last is refused.
    with changed:
        assert changed.wait_for(lambda: len(calls) == 3 and len(outcomes) == 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    assert len(calls) == 3
    [refused] = [o for o in outcomes if o != 'ok']
    assert "mendwright did not ask the model to repair 'accept_terms'" in refused
    # This process's cap, or the key record's, which counts the workers of every process:
    # whichever the last two followers, racing, reach first.
    assert ' made 3 heal attempts for it in the last hour, and rate_per_key_per_hour is 3' in refused
    # The rate was passed to open(), where the variable changes nothing.
    assert 'The rate_per_key_per_hour= passed to mendwright.open() sets this cap.' in refused
    assert 'MENDWRIGHT_RATE' not in refused and 'rate/spend cap' in refused


def test_a_leader_waits_for_another_processes_lease_past_its_step_timeout(
        tmp_path, make_page, stub_provider, monkeypatch):
    # Another process holds the key's lease for this page and heals it. This process's leader
    # waits while that lease lives, though its step's timeout of 100 ms passed long before,
    # then uses the heal that process saved: no model call of its own.
    _warm(make_page, tmp_path, stub_provider)
    records = records_for(load_store(None, _store(tmp_path)))
    owner = 'other-host/1/abc'
    records.take(KEY, page_signature('/checkout', [CODE]), owner, time.time(), 15)
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        time.sleep(0.05)
        if len(pauses) == 6:        # 300 ms on: the other process saved its heal and is done
            load_store(None, _store(tmp_path)).add_candidate(KEY, HEALED)
            records.release(KEY, owner)
    monkeypatch.setattr(FLIGHTS, '_pause', pause)
    provider = stub_provider(None)
    page = make_page(V2, url=URL)
    heal = mendwright.open(page, provider=provider, store_path=_store(tmp_path), timeout_ms=100,
                           fingerprint=False)
    heal._clock = _instant()
    heal.locator(KEY, CODE).check()
    assert len(pauses) == 6 and provider.bundles == []
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert _count(_journal(tmp_path), 'heal.adopted') == 1


# The element the fingerprint finds again: the same label, a new id, name and test id.
FP_V1 = _html("<label><input id='accept-terms' name='terms' type='checkbox'>"
              "<span>I accept the terms and conditions</span></label>")
FP_V2 = _html("<label><input type='checkbox' name='agreement' data-testid='legal-consent'>"
              "<span>I accept the terms and conditions</span></label>")


def test_a_herd_heals_from_the_fingerprint_with_a_long_step_timeout(tmp_path, make_page,
                                                                    stub_provider):
    # The fleet harness's fp page with timeout_ms=10000: each lookup polls and looks at the
    # store again (the clocks step 0.5 s per read) before it heals, and the fingerprint the
    # warm-up saved still heals the key, once, with no model call; the others adopt it.
    page = make_page(FP_V1, url=URL, fingerprints=True)
    mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                    timeout_ms=0).locator(KEY, CODE, role='checkbox').check()
    assert json.loads((tmp_path / 'overrides.json').read_text(encoding='utf-8'))[
        'selectors'][KEY]['fingerprint']
    provider = stub_provider(None)
    workers = []
    for _ in range(N):
        page = make_page(FP_V2, url=URL, fingerprints=True)
        heal = mendwright.open(page, provider=provider, store_path=_store(tmp_path),
                               timeout_ms=10000)
        steps = itertools.count(0, 0.5)
        heal._clock = heal.store._clock = lambda steps=steps: float(next(steps))
        workers.append((page, heal))
    errors, polled = [], []

    def run(heal):
        try:
            heal.locator(KEY, CODE, poll_ms=1).check()
        except Exception as exc:        # noqa: BLE001 - reported below
            errors.append(exc)
        polled.append(heal._clock())
    threads = [threading.Thread(target=run, args=(heal,)) for _page, heal in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []
    assert provider.bundles == []
    events = _journal(tmp_path)
    [applied] = [e for e in events if e['event'] == 'heal.applied']
    assert applied['source'] == 'fingerprint'
    assert _count(events, 'heal.adopted') == N - 1
    assert all(page.actions[-1][:2] == ('xpath=' + applied['xpath'], 'check')
               for page, _h in workers)
    assert max(polled) >= 10.0      # the leader polled out its step on its clock


def test_a_nested_lookup_in_the_leaders_thread_does_not_wait_for_itself():
    flight, leader = FLIGHTS.begin(('store', 'k', 'sig'))
    try:
        assert leader and FLIGHTS.wait(flight, 60) is False      # at once, not after 60 s
    finally:
        FLIGHTS.finish(flight)
    assert FLIGHTS.waiting() == 0


# --- the page signature ----------------------------------------------------------------------

def test_the_page_signature_is_the_documented_digest():
    import hashlib
    expected = hashlib.sha256(
        b'27:mendwright-page-signature/1,9:/checkout,13:#accept-terms,'
        b"31://input[@id='accept-terms-old'],").hexdigest()
    assert page_signature('/checkout', ["//input[@id='accept-terms-old']", '#accept-terms',
                                        '#accept-terms']) == expected
    # No page path: the empty string.
    assert page_signature(None, []) == hashlib.sha256(
        b'27:mendwright-page-signature/1,0:,').hexdigest()
    # Selectors sort by their UTF-8 bytes: U+FF01 comes before an emoji there, after it in
    # UTF-16 code units (JavaScript's default sort).
    emoji, bang = chr(0x1F600), chr(0xFF01)
    assert page_signature('/p', [emoji, bang]) == hashlib.sha256(
        b'27:mendwright-page-signature/1,2:/p,3:' + bytes.fromhex('efbc81') + b',4:'
        + bytes.fromhex('f09f9880') + b',').hexdigest()
