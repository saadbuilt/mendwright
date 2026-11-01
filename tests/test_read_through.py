"""Read-through: a Healer that is already open takes in what other workers saved to the store
file since it read it (SelectorStore.refresh), and adopts a heal another worker made after the
live check on its own page (HealCoordinator.adopt).

The worker scenarios are the fleet analysis's probes: when an open Healer sees another one's
heal (Q1), and whether a quarantine and the canary reach it (Q5). Each worker is a Healer of
its own (mendwright.open() gives each its own SelectorStore) on one store file, as threads,
processes and machines sharing a file are. Offline: conftest's lxml FakePage and StubProvider."""

import json
import logging
import os

import pytest

import mendwright
import mendwright.store as store_module
from mendwright import AttentionRequired, load_store
from mendwright.store import SelectorStore, StoreReadError, parsed_override

URL = 'https://shop.test/checkout'
KEY = 'accept_terms'
CODE = '#accept-terms'
HEALED = "//input[@data-testid='tos-optin']"


def _html(body):
    return ("<html><body><main><h1>Checkout</h1><form id='checkout'>"
            "<label for='email'>Email</label><input id='email' name='email' type='email'>"
            + body + "<button id='pay' type='submit'>Pay now</button></form></main></body></html>")


V1 = _html("<label><input id='accept-terms' type='checkbox'><span>I accept</span></label>")
V2 = _html("<label><input type='checkbox' data-testid='tos-optin'><span>I agree</span></label>")
DELETE = _html("<button type='button' data-testid='tos-optin'>Delete my account</button>")


def _store_path(tmp_path):
    return str(tmp_path / 'overrides.json')


def _open(make_page, tmp_path, provider, html=V2, **kwargs):
    page = make_page(html, url=URL, fingerprints=kwargs.pop('fingerprints', False))
    kwargs.setdefault('timeout_ms', 0)
    kwargs.setdefault('fingerprint', False)
    return page, mendwright.open(page, provider=provider, store_path=_store_path(tmp_path),
                                 **kwargs)


def _warm(make_page, tmp_path, stub_provider):
    """The store after one run on V1: the code selector, its role and its page."""
    _page, heal = _open(make_page, tmp_path, stub_provider(None), html=V1)
    heal.locator(KEY, CODE, role='checkbox').check()


def _journal(tmp_path, event):
    path = tmp_path / 'journal.jsonl'
    rows = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])
    return [r for r in rows if r['event'] == event]


def _disk(tmp_path):
    return json.loads((tmp_path / 'overrides.json').read_text(encoding='utf-8'))['selectors']


def _status_on_disk(tmp_path, xpath):
    return next(c.get('status', 'active') for c in _disk(tmp_path)[KEY]['candidates']
                if c['xpath'] == xpath)


# --- Q1: when does an open Healer see another worker's heal? ----------------------------------

def test_open_healers_use_one_heal_and_ask_no_model(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    providers = {name: stub_provider(HEALED) for name in 'abc'}
    _pa, a = _open(make_page, tmp_path, providers['a'])
    pb, b = _open(make_page, tmp_path, providers['b'])     # opened before A's heal
    _pc, c = _open(make_page, tmp_path, providers['c'])
    a.locator(KEY, CODE).check()
    assert [h.source for h in a.heals] == ['model']
    # B never wrote since it read the store: its lookup looks at the file at its first miss.
    b.locator(KEY, CODE).check()
    assert pb.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    # C made an unrelated write first, which took A's heal in.
    c.locator('email', '#email', role='textbox')
    c.locator(KEY, CODE).check()
    pd, d = _open(make_page, tmp_path, stub_provider(HEALED))    # opened after A's heal
    d.locator(KEY, CODE).check()
    assert [len(p.bundles) for p in providers.values()] == [1, 0, 0]
    # B and C adopted A's heal: pending for their canary. D loaded it with the store.
    assert b.pending() == {KEY: HEALED} and c.pending() == {KEY: HEALED} and d.pending() == {}
    assert b.heals == [] and c.heals == []                 # A lists the heal it made
    assert [(e['key'], e['xpath']) for e in _journal(tmp_path, 'heal.adopted')] == [
        (KEY, HEALED), (KEY, HEALED)]


def test_a_heal_saved_while_a_lookup_waits_is_used_without_waiting_out_the_timeout(
        tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(HEALED))
    pb, b = _open(make_page, tmp_path, stub_provider(None), timeout_ms=600000)
    clock = [0.0]
    b.store._clock = lambda: clock[0]        # refresh()'s max_age clock
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 1:
            a.locator(KEY, CODE).check()     # another worker heals meanwhile
        clock[0] += seconds

    import mendwright.resolver as resolver
    native = resolver.resolve(b.store, pb, KEY, timeout_ms=600000, poll_ms=200, sleep=sleep,
                              monotonic=lambda: clock[0])
    assert native is not None and b.store.last_success(KEY)['xpath'] == HEALED
    # One poll to see the miss, then about a second until the next look at the file.
    assert 1.0 <= sum(polls) <= 1.4


def test_the_heal_path_looks_at_the_file_before_its_fingerprint_tier(tmp_path, make_page,
                                                                    stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    provider_a, provider_b = stub_provider(HEALED), stub_provider(HEALED)
    _pa, a = _open(make_page, tmp_path, provider_a)
    healed = []

    def guard():
        # try_heal asks the guard just before it looks at the file: another worker heals
        # the key right then, after B's lookup missed.
        if not healed:
            healed.append(True)
            a.locator(KEY, CODE).check()
        return True
    pb, b = _open(make_page, tmp_path, provider_b, guard=guard)
    b.locator(KEY, CODE).check()
    assert pb.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert (len(provider_a.bundles), len(provider_b.bundles)) == (1, 0)
    assert b.pending() == {KEY: HEALED} and b.heals == []
    [event] = _journal(tmp_path, 'heal.adopted')
    assert event['old'] == [CODE]


def test_the_heal_path_looks_again_before_asking_a_model(tmp_path, make_page, stub_provider,
                                                         monkeypatch):
    _warm(make_page, tmp_path, stub_provider)
    provider_a, provider_b = stub_provider(HEALED), stub_provider(HEALED)
    _pa, a = _open(make_page, tmp_path, provider_a)
    pb, b = _open(make_page, tmp_path, provider_b, fingerprint=True)
    real = b.coordinator._fingerprint_tier

    def tier(*args):
        a.locator(KEY, CODE).check()     # another worker heals during B's fingerprint tier
        return real(*args)
    monkeypatch.setattr(b.coordinator, '_fingerprint_tier', tier)
    b.store.set_meta(KEY, fingerprint=_fingerprint())
    b.locator(KEY, CODE).check()
    assert (len(provider_a.bundles), len(provider_b.bundles)) == (1, 0)
    assert b.pending() == {KEY: HEALED}


def test_a_heal_saved_while_the_model_answers_is_adopted_not_saved_twice(tmp_path, make_page,
                                                                        stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(HEALED))

    class Racing:
        """A model that answers after another worker saved its heal of the key."""
        calls = 0

        def propose(self, bundle):
            Racing.calls += 1
            a.locator(KEY, CODE).check()
            return {'xpath': "//label[span]/input[@type='checkbox']", 'fallbacks': [],
                    'reasoning': 'slow'}

        def report_outcome(self, key, xpath, success):
            pass
    pb, b = _open(make_page, tmp_path, Racing())
    b.locator(KEY, CODE).check()
    assert Racing.calls == 1 and pb.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert b.pending() == {KEY: HEALED} and b.heals == []
    assert len(_journal(tmp_path, 'heal.applied')) == 1        # A's, and no second heal
    assert [c['xpath'] for c in _disk(tmp_path)[KEY]['candidates']] == [CODE, HEALED]


def _fingerprint():
    from mendwright.fingerprint import of_element
    import lxml.html
    doc = lxml.html.fromstring(V1)
    return of_element(doc.xpath("//input[@id='accept-terms']")[0])


def test_an_arrived_heal_is_adopted_only_after_the_live_check(tmp_path, make_page,
                                                              stub_provider, caplog):
    loose = "//*[@data-testid='tos-optin']"      # a hosted model may propose a wildcard
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(loose))
    provider_b = stub_provider(None)
    pb, b = _open(make_page, tmp_path, provider_b, html=DELETE)
    a.locator(KEY, CODE).check()
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        with pytest.raises(AttentionRequired, match='got button want checkbox'):
            b.locator(KEY, CODE).check()
    assert pb.actions == [] and b.pending() == {}
    assert not _journal(tmp_path, 'heal.adopted')
    assert len(_journal(tmp_path, 'heal.refused')) == 1
    assert len(provider_b.bundles) == 1          # it went on to its own attempt
    assert _status_on_disk(tmp_path, loose) == 'active'


def test_the_async_healer_adopts_another_workers_heal(tmp_path, make_page, stub_provider):
    import asyncio
    import mendwright.aio
    from test_aio import AsyncFakePage
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(HEALED))
    sync_page = make_page(V2, url=URL)
    provider_b = stub_provider(None)
    healed = []

    def guard():
        if not healed:                       # in B's heal thread, after its lookup missed
            healed.append(True)
            a.locator(KEY, CODE).check()
        return True

    async def main():
        b = await mendwright.aio.open(AsyncFakePage(sync_page), provider=provider_b,
                                      store_path=_store_path(tmp_path), timeout_ms=0,
                                      fingerprint=False, guard=guard)
        await b.locator(KEY, CODE).check()
        return b
    b = asyncio.run(main())
    assert sync_page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert provider_b.bundles == [] and b.pending() == {KEY: HEALED}
    assert len(_journal(tmp_path, 'heal.adopted')) == 1


# --- Q5: a quarantine and the canary reach Healers that are already open --------------------

def test_a_quarantine_reaches_a_healer_that_is_already_open(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(HEALED))
    a.locator(KEY, CODE).check()
    pb, b = _open(make_page, tmp_path, stub_provider(None))      # loads A's heal, active
    assert b.store.status(KEY, HEALED) == 'active'
    a.coordinator.confirm_pending(KEY, False, by='action')       # A's canary fails
    assert _status_on_disk(tmp_path, HEALED) == 'quarantined'
    with pytest.raises(AttentionRequired):
        b.locator(KEY, CODE).check()
    assert pb.actions == []                      # B did not act through the quarantined heal
    assert b.store.status(KEY, HEALED) == 'quarantined'


def test_the_canary_of_an_adopted_heal_quarantines_it_for_everyone(tmp_path, make_page,
                                                                    stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    provider_a = stub_provider(HEALED)
    _pa, a = _open(make_page, tmp_path, provider_a)
    provider_b = stub_provider(None)
    pb, b = _open(make_page, tmp_path, provider_b)
    a.locator(KEY, CODE)
    pb.fail['check'] = RuntimeError('Locator.check: element is outside of the viewport')
    with pytest.raises(RuntimeError):
        b.locator(KEY, CODE).check()
    assert _status_on_disk(tmp_path, HEALED) == 'quarantined'
    assert b.store.status(KEY, HEALED) == 'quarantined'
    [canary] = _journal(tmp_path, 'heal.canary')
    assert (canary['xpath'], canary['success'], canary['by']) == (HEALED, False, 'action')
    # Neither provider is told: B's did not propose it, and A's heal is not A's canary.
    assert provider_a.outcomes == [] and provider_b.outcomes == []


def test_no_fingerprint_is_recorded_through_an_unconfirmed_adopted_heal(tmp_path, make_page,
                                                                         stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    _pa, a = _open(make_page, tmp_path, stub_provider(HEALED))
    pb, b = _open(make_page, tmp_path, stub_provider(None), fingerprint=True, fingerprints=True)
    a.locator(KEY, CODE)
    reads = pb.fingerprint_reads
    box = b.locator(KEY, CODE)
    assert pb.fingerprint_reads == reads and b.pending() == {KEY: HEALED}
    box.check(expect=lambda: True)               # the canary confirms it
    assert b.pending() == {}
    b.locator(KEY, CODE)
    assert pb.fingerprint_reads == reads + 1     # read through the confirmed heal


# --- SelectorStore.refresh() ------------------------------------------------------------------

def _write_entry(path, key, candidates, **meta):
    data = json.loads(open(path, encoding='utf-8').read()) if os.path.exists(path) else {
        'schema': 2, 'selectors': {}}
    data['selectors'][key] = dict(meta, candidates=candidates)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _row(xpath, source='heal', status='active', at=None):
    return {'xpath': xpath, 'source': source, 'addedAt': '2026-10-01T00:00:00+00:00',
            'lastSucceededAt': at, 'status': status}


@pytest.fixture
def parses(monkeypatch):
    count = []
    real = store_module._read_versioned

    def counting(*args, **kwargs):
        count.append(args[0])
        return real(*args, **kwargs)
    monkeypatch.setattr(store_module, '_read_versioned', counting)
    store_module._PARSED.clear()
    return count


def test_refresh_costs_one_stat_when_nothing_changed(tmp_path, parses):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code')], expectedRole='button')
    store = load_store(None, path)
    parses.clear()
    assert store.refresh('k') is False and parses == []


def test_one_parse_per_file_version_for_every_store_in_the_process(tmp_path, parses):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code')], expectedRole='button')
    stores = [load_store(None, path) for _ in range(5)]
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//b')], expectedRole='button')
    parses.clear()
    assert [s.refresh('k') for s in stores] == [True] * 5
    assert len(parses) == 1
    assert all(s.arrived('k', '//b') for s in stores)
    assert [s.refresh('k') for s in stores] == [False] * 5 and len(parses) == 1


def test_refresh_merges_only_its_key_with_the_merge_rules_of_a_write(tmp_path):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code', at='2026-10-01T00:00:00+00:00'), _row('//h')],
                 expectedRole='button')
    _write_entry(path, 'other', [_row('//o', 'code')], expectedRole='button')
    store = load_store(None, path)
    store.record_success('k', '//h', now='2026-10-05T00:00:00+00:00')
    # Another worker: quarantines //h, marks k destructive, records an older success.
    _write_entry(path, 'k', [_row('//a', 'code', at='2026-10-03T00:00:00+00:00'),
                             _row('//h', status='quarantined', at='2026-10-02T00:00:00+00:00')],
                 expectedRole='button', destructive=True)
    _write_entry(path, 'other', [_row('//o', 'code'), _row('//o2')], expectedRole='button')
    assert store.refresh('k') is True
    assert store.status('k', '//h') == 'quarantined'                  # quarantine wins
    assert store.meta('k')['destructive'] is True                     # destructive wins
    rows = {c['xpath']: c for c in store._data['selectors']['k']['candidates']}
    assert rows['//h']['lastSucceededAt'] == '2026-10-05T00:00:00+00:00'   # newest wins
    assert rows['//a']['lastSucceededAt'] == '2026-10-03T00:00:00+00:00'
    assert store.status('other', '//o2') is None                      # only k was merged


def test_refresh_keeps_the_merge_base_of_every_other_key(tmp_path):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code')], expectedRole='button')
    _write_entry(path, 'j', [_row('//j', 'code'), _row('//jh')], expectedRole='button')
    store = load_store(None, path)
    # Another worker quarantines j's heal; this store refreshes k only, then writes.
    _write_entry(path, 'j', [_row('//j', 'code'), _row('//jh', status='quarantined')],
                 expectedRole='button')
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//kh')], expectedRole='button')
    store.refresh('k')
    store.add_candidate('k', '//mine')
    assert {c['xpath']: c['status'] for c in _disk_at(path, 'j')} == {
        '//j': 'active', '//jh': 'quarantined'}
    assert [c['xpath'] for c in _disk_at(path, 'k')] == ['//a', '//kh', '//mine']


def _disk_at(path, key):
    return json.loads(open(path, encoding='utf-8').read())['selectors'][key]['candidates']


def test_an_unreadable_file_is_never_taken_for_an_empty_store(tmp_path):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//h')], expectedRole='button')
    store = load_store(None, path)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('{"schema": 2, "selectors": {')          # a merge conflict, say
    assert store.refresh('k') is False
    assert [c['xpath'] for c in store.candidates('k')] == ['//a', '//h']
    with pytest.raises(StoreReadError):
        parsed_override(path, store_module.stat_version(path))


def test_refresh_of_a_read_only_store_or_its_snapshot_reads_nothing(tmp_path, parses):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code')], expectedRole='button')
    store = load_store(None, path)
    snapshot = store.snapshot()
    read_only = load_store(None, path, read_only=True)
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//b')], expectedRole='button')
    parses.clear()
    assert snapshot.refresh('k') is False and read_only.refresh('k') is False
    assert parses == [] and snapshot.status('k', '//b') is None


def test_max_age_skips_the_look_within_that_time(tmp_path, parses):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code')], expectedRole='button')
    store = load_store(None, path)
    clock = [100.0]
    store._clock = lambda: clock[0]
    assert store.refresh('k', max_age=1.0) is False
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//b')], expectedRole='button')
    clock[0] += 0.5
    assert store.refresh('k', max_age=1.0) is False          # looked 0.5 s ago
    clock[0] += 0.5
    assert store.refresh('k', max_age=1.0) is True


def test_a_store_made_without_reading_the_file_takes_it_in_on_refresh(tmp_path):
    path = _store_path(tmp_path)
    _write_entry(path, 'k', [_row('//a', 'code'), _row('//h')], expectedRole='button')
    store = SelectorStore({'schema': 2, 'selectors': {}}, path)
    assert store.refresh('k') is True
    assert [c['xpath'] for c in store.candidates('k')] == ['//a', '//h']
