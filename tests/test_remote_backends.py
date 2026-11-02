"""A remote store (mendwright.remote) on the real backends: the reference HTTP server on 127.0.0.1
and S3 under moto (skipped without boto3 and moto). Machines share heals through the store; many
writers at once lose no heal; a store that refuses the credentials or cannot be reached when it
opens is never read as an empty one; and no credential reaches a log line, a journal event, an
error or a description. The machines in one process are stores of their own on one store, each
with a backend that names itself apart (Machine), so each has its own turn to write, its own
parse and its own outages, as separate machines do. Nothing leaves 127.0.0.1 or moto."""

import contextlib
import json
import logging
import threading
import uuid

import pytest

import mendwright
from mendwright import AttentionRequired, load_store
from mendwright.backends import OVERRIDES, Conflict
from mendwright.journal import Journal
from test_backends_contract import BUCKET, PREFIX, TOKEN, moto_s3
from test_backends_remote import free_port, need_boto3
from test_herd import CODE, HEALED, KEY, URL, V1, V2
from test_remote_store import _health_is_fresh  # noqa: F401  (an autouse fixture)

ROLE = 'checkbox'
WRONG = 'wrong-token-OTHERSECRET-9876543210'
AWS_SECRET = 'aws-secret-SECRETVALUE-key'
SECRETS = ('SECRETVALUE', 'OTHERSECRET')


class Remote:
    """One remote store for a test: url for store_path=, make() for a raw backend of it."""

    def __init__(self, kind, url, make):
        self.kind, self.url, self.make = kind, url, make

    def doc(self):
        read = self.make().get(OVERRIDES)
        return json.loads(read.body) if read.body is not None else None


class Machine:
    """A backend of another machine on the store: it delegates to backend and names itself
    apart, so its store has a place of its own (see remote.place_of)."""

    def __init__(self, backend, name=None):
        self.backend = backend
        self.name = name or 'machine-' + uuid.uuid4().hex[:8]
        self.conflicts = 0
        self.before_put = None

    def describe(self):
        return self.backend.describe() + '#' + self.name

    def get(self, name, *, if_none_match=None):
        return self.backend.get(name, if_none_match=if_none_match)

    def put(self, name, body, *, if_match=None, create_only=False):
        if self.before_put is not None:
            meddle, self.before_put = self.before_put, None
            meddle()
        try:
            return self.backend.put(name, body, if_match=if_match, create_only=create_only)
        except Conflict:
            self.conflicts += 1
            raise

    def delete(self, name, *, if_match=None):
        return self.backend.delete(name, if_match=if_match)


@contextlib.contextmanager
def _http(monkeypatch, tmp_path):
    from mendwright.backends.http import HttpBackend
    from mendwright.backends.server import StoreServer
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    with StoreServer(tmp_path / 'served', token=TOKEN) as server:
        yield Remote('http', server.url, lambda: HttpBackend(server.url))


@contextlib.contextmanager
def _s3(monkeypatch, tmp_path):
    with moto_s3(monkeypatch, tmp_path):
        monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'AKIASECRETVALUE0001')
        monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', AWS_SECRET)
        from mendwright.backends.s3 import S3Backend
        yield Remote('s3', S3Backend(BUCKET, PREFIX).describe(), lambda: S3Backend(BUCKET, PREFIX))


@pytest.fixture(params=['http', 's3'])
def remote(request, monkeypatch, tmp_path):
    with {'http': _http, 's3': _s3}[request.param](monkeypatch, tmp_path) as made:
        yield made


def _open(page, tmp_path, provider, name='machine', **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    kwargs.setdefault('fingerprint', False)
    return mendwright.open(page, provider=provider, journal=Journal(tmp_path / (name + '.jsonl')),
                           **kwargs)


def _events(tmp_path, name='machine'):
    path = tmp_path / (name + '.jsonl')
    return ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])


def test_machines_share_heals_through_the_store(remote, make_page, stub_provider, tmp_path):
    _open(make_page(V1, url=URL), tmp_path, stub_provider(None), 'warm',
          store_path=remote.url).locator(KEY, CODE, role=ROLE).check()
    page = make_page(V2, url=URL)
    _open(page, tmp_path, stub_provider(HEALED), 'a', store_path=remote.url).locator(
        KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    rows = {c['xpath']: c for c in remote.doc()['selectors'][KEY]['candidates']}
    assert set(rows) == {CODE, HEALED} and rows[HEALED]['source'] == 'heal'
    # Another machine, with a place of its own: it reads the heal and uses it, no model call.
    other = stub_provider(None)
    page = make_page(V2, url=URL)
    _open(page, tmp_path, other, 'b', store=load_store(None, Machine(remote.make()))).locator(
        KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check') and other.bundles == []


def test_writers_at_once_lose_no_heal(remote):
    stores = [load_store(None, Machine(remote.make())) for _ in range(8)]
    start = threading.Barrier(len(stores))
    errors = []

    def write(i, store):
        try:
            start.wait()
            store.add_candidate('key%d' % i, "//input[@id='field%d']" % i)
            for _ in range(20):         # a change that lost every try waits for the next write
                if store.flush():
                    break
        except Exception as exc:        # noqa: BLE001 - reported below
            errors.append(exc)
    threads = [threading.Thread(target=write, args=(i, s)) for i, s in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert errors == [] and not any(t.is_alive() for t in threads)
    selectors = remote.doc()['selectors']
    assert {k: [c['xpath'] for c in e['candidates']] for k, e in selectors.items()} == {
        'key%d' % i: ["//input[@id='field%d']" % i] for i in range(8)}


def test_a_write_another_machine_won_is_merged_against_the_version_read(remote):
    setup = load_store(None, Machine(remote.make()))
    setup.set_code_candidates(KEY, [CODE])
    setup.set_meta(KEY, text='Accept')
    machine = Machine(remote.make())
    store = load_store(None, machine)
    rival = load_store(None, Machine(remote.make()))
    machine.before_put = lambda: rival.set_meta(KEY, text='I agree')
    store.add_candidate(KEY, HEALED)
    assert machine.conflicts == 1
    entry = remote.doc()['selectors'][KEY]
    assert entry['expectedText'] == ['I agree']
    assert {c['xpath'] for c in entry['candidates']} == {CODE, HEALED}


def _refused_heal(store, make_page, stub_provider, tmp_path):
    provider = stub_provider(HEALED)
    heal = _open(make_page(V2, url=URL), tmp_path, provider, 'refused', store=store)
    with pytest.raises(AttentionRequired) as info:
        heal.locator(KEY, CODE, role=ROLE).check()
    assert provider.bundles == []
    [declined] = [e for e in _events(tmp_path, 'refused') if e['event'] == 'heal.declined']
    assert declined['code'] == 'store'
    return str(info.value)


def test_a_store_that_refuses_the_credentials_is_never_read_as_empty(remote, make_page,
                                                                    stub_provider, tmp_path,
                                                                    monkeypatch, caplog):
    load_store(None, Machine(remote.make())).set_code_candidates(KEY, [CODE])
    if remote.kind == 'http':
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', WRONG)
        url, says = remote.url, 'refused the token (HTTP 401)'
    else:
        url, says = 's3://no-such-bucket-here/team/', 'does not exist (NoSuchBucket)'
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store = load_store(None, url)
        error = _refused_heal(store, make_page, stub_provider, tmp_path)
    assert store.unread_error == 'Denied' and says in store.unread
    assert 'could not read the store ' + store.remote_place in error
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    assert remote.doc()['selectors'][KEY]['candidates'][0]['xpath'] == CODE     # untouched


def test_a_store_that_cannot_be_reached_is_never_read_as_empty(make_page, stub_provider,
                                                               tmp_path, monkeypatch):
    url = 'http://127.0.0.1:%d/team/' % free_port()
    store = load_store(None, url)
    assert store.unread_error == 'Unavailable' and 'the connection was refused' in store.unread
    _refused_heal(store, make_page, stub_provider, tmp_path)


def test_an_s3_store_that_cannot_be_reached_is_never_read_as_empty(make_page, stub_provider,
                                                                   tmp_path, monkeypatch):
    need_boto3()
    from test_backends_remote import aws_offline
    aws_offline(monkeypatch, tmp_path)
    monkeypatch.setenv('AWS_ENDPOINT_URL_S3', 'http://127.0.0.1:%d' % free_port())
    store = load_store(None, 's3://team-bucket/mendwright/')
    assert store.unread_error == 'Unavailable' and 'could not reach S3' in store.unread
    _refused_heal(store, make_page, stub_provider, tmp_path)


def test_no_credential_reaches_a_log_a_journal_an_error_or_a_description(
        remote, make_page, stub_provider, tmp_path, monkeypatch, caplog):
    seen = []
    with caplog.at_level(logging.DEBUG, logger='mendwright'):
        # A heal, saved and adopted.
        _open(make_page(V1, url=URL), tmp_path, stub_provider(None), 'j',
              store_path=remote.url).locator(KEY, CODE, role=ROLE).check()
        heal = _open(make_page(V2, url=URL), tmp_path, stub_provider(HEALED), 'j',
                     store_path=remote.url)
        heal.locator(KEY, CODE).check()
        seen += [heal.store.backend.describe(), repr(heal.store), heal.store.remote_place]
        # Credentials refused when a store opens (a store of its own name: the stores of a
        # process on one store share what one of them read).
        if remote.kind == 'http':
            monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', WRONG)
            refused = remote.url.replace('127.0.0.1', 'localhost')
        else:
            refused = 's3://no-such-bucket-here/team/'
        store = load_store(None, refused)
        heal = _open(make_page(V2, url=URL), tmp_path, stub_provider(HEALED), 'j', store=store)
        try:
            heal.locator(KEY, CODE, role=ROLE).check()
        except AttentionRequired as exc:
            seen += [str(exc), repr(exc)]
        seen += [store.unread, store.backend.describe(), repr(store)]
    journal = (tmp_path / 'j.jsonl').read_text(encoding='utf-8')
    assert 'store.unavailable' in journal
    for text in seen + [caplog.text, journal]:
        assert not [s for s in SECRETS if s in text], text[:200]
