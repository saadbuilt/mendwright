"""A store kept elsewhere than a local file (mendwright.remote): overrides.json of a StoreBackend,
read and written with the merge rules of a local store and a compare-and-swap. The machines here
are stores of their own on one MemoryBackend, each with a journal of its own. Offline: conftest's
lxml FakePage and fake providers; no model is called and nothing leaves the process."""

import json
import logging
import uuid

import pytest

import mendwright
from mendwright import AttentionRequired, load_store
from mendwright.backends import (OVERRIDES, BackendError, Conflict, Denied, MemoryBackend,
                                 Unavailable)
from mendwright.journal import Journal
from mendwright.remote import RemoteSelectorStore, _health, place_of
from mendwright.store import SOFT
from test_herd import CODE, HEALED, KEY, URL, V1, V2

ROLE = 'checkbox'


class Faulty:
    """A StoreBackend around another that counts calls and fails or meddles on demand: fail is a
    dict of operation -> an exception (or a list of them, one per call) to raise instead of
    calling through; before_put(name, body) runs before each put, as another writer would."""

    def __init__(self, inner=None):
        self.inner = inner or MemoryBackend()
        self.name = 'faulty:' + uuid.uuid4().hex[:8]
        self.calls = []
        self.fail = {}
        self.before_put = None

    def describe(self):
        return self.name

    def _maybe_fail(self, op):
        found = self.fail.get(op)
        if isinstance(found, list):
            found = found.pop(0) if found else None
        if found is not None:
            raise found

    def get(self, name, *, if_none_match=None):
        self.calls.append(('get', name))
        self._maybe_fail('get')
        return self.inner.get(name, if_none_match=if_none_match)

    def put(self, name, body, *, if_match=None, create_only=False):
        self.calls.append(('put', name, 'create' if create_only else 'match' if if_match else 'any'))
        self._maybe_fail('put')
        if self.before_put is not None:
            meddle, self.before_put = self.before_put, None
            meddle(name, body)
        return self.inner.put(name, body, if_match=if_match, create_only=create_only)

    def delete(self, name, *, if_match=None):
        self.calls.append(('delete', name))
        self._maybe_fail('delete')
        return self.inner.delete(name, if_match=if_match)

    def writes(self):
        return [c for c in self.calls if c[0] in ('put', 'delete')]


def _doc(backend):
    read = backend.get(OVERRIDES)
    return json.loads(read.body.decode('utf-8')) if read.body is not None else None


def _rows(backend, key=KEY):
    return {c['xpath']: c for c in _doc(backend)['selectors'][key]['candidates']}


def _open(page, store, tmp_path, provider, name='machine', **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    kwargs.setdefault('fingerprint', False)
    return mendwright.open(page, store=store, provider=provider,
                           journal=Journal(tmp_path / (name + '.jsonl')), **kwargs)


def _journal(tmp_path, name='machine'):
    path = tmp_path / (name + '.jsonl')
    return ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])


def _warm(backend, make_page, tmp_path, stub_provider):
    """overrides.json after a run on V1: the code selector, its role, page and address."""
    store = load_store(None, backend)
    _open(make_page(V1, url=URL), store, tmp_path, stub_provider(None), 'warm').locator(
        KEY, CODE, role=ROLE).check()
    return store


@pytest.fixture(autouse=True)
def _health_is_fresh():
    """The outage state of each place is the process's: no test starts in another's backoff."""
    from mendwright import remote
    remote._HEALTH.clear()
    remote._DOCS.clear()
    yield
    remote._HEALTH.clear()
    remote._DOCS.clear()


# --- loading -----------------------------------------------------------------------------------

def test_a_store_url_or_a_backend_is_a_remote_store():
    from_url = load_store(None, 'memory:')
    backend = MemoryBackend()
    from_backend = load_store(None, backend)
    assert isinstance(from_url, RemoteSelectorStore) and isinstance(from_backend, RemoteSelectorStore)
    assert from_url.unread is None and from_backend.remote_place == place_of(backend)
    # Every MemoryBackend is a store of its own, though each describes itself as 'memory'.
    assert from_url.remote_place != from_backend.remote_place
    assert all(p.startswith('memory:') for p in (from_url.remote_place, from_backend.remote_place))
    # A remote store has no file here.
    assert getattr(from_backend, '_override_path', None) is None


def test_a_missing_overrides_json_is_an_empty_store_and_the_first_write_creates_it():
    backend = Faulty()
    store = load_store(None, backend)
    assert store.unread is None and store.candidates(KEY) == []
    store.set_code_candidates(KEY, [CODE])
    assert backend.writes() == [('put', OVERRIDES, 'create')]
    assert set(_rows(backend)) == {CODE}
    store.set_meta(KEY, role=ROLE)
    assert backend.writes()[-1] == ('put', OVERRIDES, 'match')


def test_a_heal_reaches_the_other_machines(make_page, stub_provider, tmp_path):
    backend = MemoryBackend()
    first = _warm(backend, make_page, tmp_path, stub_provider)
    second = load_store(None, backend)
    page = make_page(V2, url=URL)
    _open(page, second, tmp_path, stub_provider(HEALED), 'second').locator(KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert set(_rows(backend)) == {CODE, HEALED}
    # A machine that loads now has it, and one that was open takes it in.
    assert [c['xpath'] for c in load_store(None, backend).candidates(KEY)][-1] == HEALED
    assert first.refresh(KEY) is True and first.arrived(KEY, HEALED)


def test_seed_files_and_destructive_keys_stay_local_and_win(tmp_path):
    backend = MemoryBackend()
    other = load_store(None, backend)
    other.set_code_candidates('pay', ['#pay'])
    other.add_candidate('pay', "//button[@id='pay-now']")
    other.set_meta('pay', role='button', destructive=False)
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'accept_terms': {
        'expectedRole': 'checkbox', 'candidates': [{'xpath': CODE, 'source': 'seed'}]}}}),
        encoding='utf-8')
    store = load_store(str(seed), backend, destructive_keys={'pay'})
    assert store.meta('pay')['destructive'] is True        # destructive_keys win
    assert store.meta(KEY)['expectedRole'] == 'checkbox'
    assert [c['xpath'] for c in store.candidates(KEY)] == [CODE]
    store.record_success(KEY, CODE)
    assert KEY not in _doc(backend)['selectors']          # a seed row is never written


def test_two_memory_stores_never_mix_their_overrides(tmp_path):
    one, two = MemoryBackend(), MemoryBackend()
    a, b = load_store(None, one), load_store(None, two)
    a.set_code_candidates(KEY, ['#from-one'])
    b.set_code_candidates(KEY, ['#from-two'])
    # Both backends are at version '1': the parse a process shares is per store, not per
    # version number.
    assert one.get(OVERRIDES).version == two.get(OVERRIDES).version
    c, d = load_store(None, one), load_store(None, two)
    c.refresh(KEY)
    d.refresh(KEY)
    assert [x['xpath'] for x in c.candidates(KEY)] == ['#from-one']
    assert [x['xpath'] for x in d.candidates(KEY)] == ['#from-two']


# --- writing: compare-and-swap --------------------------------------------------------------------

def test_a_write_another_writer_won_is_merged_and_made_again():
    backend = Faulty()
    store = load_store(None, backend)
    store.set_code_candidates(KEY, [CODE])

    def another_writer(name, body):
        rival = load_store(None, backend.inner)
        rival.add_candidate('cart', "//ul[@id='cart']/li")
    backend.before_put = another_writer
    store.add_candidate(KEY, HEALED)                 # its first put loses to the rival's
    puts = [c for c in backend.writes() if c[0] == 'put']
    assert len(puts) == 3                            # the selector, the lost try, the merge
    doc = _doc(backend)
    assert set(doc['selectors']) == {KEY, 'cart'}
    assert set(_rows(backend)) == {CODE, HEALED}


def test_each_try_merges_against_the_version_it_read_last():
    # A field another writer changed between two tries is that writer's change alone: the
    # retry must not take it for a change of both sides and put back the older value.
    backend = Faulty()
    setup = load_store(None, backend.inner)
    setup.set_code_candidates(KEY, [CODE])
    setup.set_meta(KEY, text='Accept')
    store = load_store(None, backend)                # it read text 'Accept'
    load_store(None, backend.inner).set_meta(KEY, text='I agree')     # another worker
    tries = []

    def a_third_writer(name, body):
        tries.append(json.loads(body)['selectors'][KEY]['expectedText'])
        load_store(None, backend.inner).set_meta(KEY, text='I accept the terms')
    backend.before_put = a_third_writer
    store.set_code_candidates('cart', ['#cart'])     # a change of its own, elsewhere
    assert tries == [['I agree']]                    # its first try took the second writer's
    assert _doc(backend)['selectors'][KEY]['expectedText'] == ['I accept the terms']
    assert store.meta(KEY)['expectedText'] == ['I accept the terms']
    assert '#cart' in {c['xpath'] for c in _doc(backend)['selectors']['cart']['candidates']}


def test_a_change_that_loses_every_try_waits_in_memory_for_the_next_write(caplog):
    backend = Faulty()
    store = load_store(None, backend)
    store.set_code_candidates(KEY, [CODE])
    backend.fail['put'] = [Conflict('lost')] * RemoteSelectorStore.CAS_TRIES
    store._sleep = lambda seconds: None
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store.add_candidate(KEY, HEALED)
    assert HEALED not in _rows(backend)
    assert 'kept changing under 5 writes' in caplog.text
    store.set_meta(KEY, role=ROLE)                   # the next write takes the heal along
    assert set(_rows(backend)) == {CODE, HEALED}
    assert _doc(backend)['selectors'][KEY]['expectedRole'] == ROLE


def test_soft_changes_go_to_the_backend_when_due_and_at_flush(soft_clock):
    backend = MemoryBackend()
    store = load_store(None, backend)
    store.set_code_candidates(KEY, [CODE, '#other'])
    store.record_success(KEY, CODE, now='2026-10-01T00:00:00+00:00')        # first ones:
    store.record_success(KEY, '#other', now='2026-10-01T00:00:05+00:00')    # written at once
    store.record_success(KEY, CODE, now='2026-10-01T00:00:09+00:00')        # a new order waits
    assert _rows(backend)[CODE]['lastSucceededAt'] == '2026-10-01T00:00:00+00:00'
    assert store.flush() is True
    assert _rows(backend)[CODE]['lastSucceededAt'] == '2026-10-01T00:00:09+00:00'
    # A store dropped before it wrote: SOFT writes its changes at exit, to the backend.
    other = load_store(None, backend)
    other.record_success(KEY, '#other', now='2026-10-02T00:00:00+00:00')
    SOFT.flush_all()
    assert _rows(backend)['#other']['lastSucceededAt'] == '2026-10-02T00:00:00+00:00'


# --- a store that cannot be read is never an empty one ------------------------------------------

@pytest.mark.parametrize('error', [
    Unavailable('memory: could not reach the store'),
    Denied('memory: the credentials were refused'),
    BackendError('memory: the server answered HTTP 400 to GET'),
], ids=['unavailable', 'denied', 'any-backend-error'])
def test_a_store_that_cannot_be_read_refuses_heals_and_code_selectors_still_work(
        error, make_page, stub_provider, tmp_path):
    backend = Faulty()
    backend.fail['get'] = error
    store = load_store(None, backend)
    assert store.remote_place in store.unread and type(error).__name__ in store.unread
    provider = stub_provider(HEALED)
    page = make_page(V1, url=URL)
    heal = _open(page, store, tmp_path, provider)
    heal.locator(KEY, CODE, role=ROLE).check()          # the code's selector resolves
    assert page.actions[-1][:2] == ('css=' + CODE, 'check')
    broken = make_page(V2, url=URL)
    heal = _open(broken, store, tmp_path, provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator(KEY, CODE).check()
    assert 'could not read the store ' + store.remote_place in str(info.value)
    assert provider.bundles == []
    [declined] = [e for e in _journal(tmp_path) if e['event'] == 'heal.declined']
    assert declined['code'] == 'store'
    assert backend.writes() == []                       # never written over


def test_a_body_that_is_not_a_selector_file_is_not_an_empty_store(make_page, stub_provider,
                                                                   tmp_path):
    backend = MemoryBackend()
    backend.put(OVERRIDES, b'{"selectors": "not a list or a dict"')
    store = load_store(None, backend)
    assert 'is not valid JSON' in store.unread
    heal = _open(make_page(V2, url=URL), store, tmp_path, stub_provider(HEALED))
    with pytest.raises(AttentionRequired):
        heal.locator(KEY, CODE, role=ROLE).check()
    assert backend.get(OVERRIDES).body == b'{"selectors": "not a list or a dict"'


def test_a_store_read_later_takes_in_all_it_holds_and_heals_again(make_page, stub_provider,
                                                                  tmp_path):
    backend = Faulty()
    _warm(backend.inner, make_page, tmp_path, stub_provider)
    quarantined = load_store(None, backend.inner)
    quarantined.add_candidate(KEY, "//input[@name='bad']")
    quarantined.quarantine(KEY, "//input[@name='bad']")
    backend.fail['get'] = [Unavailable('memory: not now')]
    store = load_store(None, backend)
    assert store.unread
    _health(store.remote_place, 'read').retry_at = 0.0     # its wait after the failure is over
    provider = stub_provider(HEALED)
    page = make_page(V2, url=URL)
    _open(page, store, tmp_path, provider).locator(KEY, CODE).check()
    assert store.unread is None and len(provider.bundles) == 1
    assert store.status(KEY, "//input[@name='bad']") == 'quarantined'
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert set(_rows(backend)) == {CODE, HEALED, "//input[@name='bad']"}


def test_a_backend_that_cannot_be_made_is_an_unread_store(monkeypatch, tmp_path):
    # An HTTP store whose token file cannot be read: backend_for raises Denied.
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN_FILE', str(tmp_path / 'no-such-token'))
    store = load_store(None, 'https://store.example/team/')
    assert store.remote_place == 'https://store.example/team/'
    assert 'the token file does not exist' in store.unread
    assert store.heal_refusal(KEY)


def test_a_read_gives_the_last_copy_when_the_store_stops_answering(make_page, stub_provider,
                                                                   tmp_path):
    backend = Faulty()
    _warm(backend.inner, make_page, tmp_path, stub_provider)
    store = load_store(None, backend)
    load_store(None, backend.inner).add_candidate(KEY, HEALED)
    backend.fail['get'] = Unavailable('memory: gone for now')
    assert store.refresh(KEY) is False
    assert [c['xpath'] for c in store.candidates(KEY)] == [CODE]
    assert store.unread is None and store.heal_refusal(KEY) == ''

