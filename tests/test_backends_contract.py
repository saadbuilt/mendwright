"""The storage backend contract (the docstring of mendwright.backends), held against every
backend.

Each test takes `store`, a fresh empty store on one backend, and runs once for each entry
in BACKENDS. Adding a backend is one entry there: a context manager that yields a Store
whose new() makes another backend on the same place, as another worker would.

- memory: MemoryBackend, the reference. A test that fails on it is wrong.
- http: HttpBackend against the reference server (mendwright.backends.server) on
  127.0.0.1, on a free port, in a thread, with a token.
- s3: S3Backend under moto's mock_aws, which needs moto 5.1.22 or later (IfMatch on
  delete_object); skipped without it. S3 applies each request atomically, while moto applies
  one without a lock, so two threads could both pass one If-Match. The fixture runs moto's
  requests one at a time, as S3 would.

Nothing here reaches AWS or any other network service.

The reference server's versions and S3's are hashes of the body, so the same body has the
same version, while MemoryBackend's never repeat. The tests assume only what the contract
says, that a version changes when the body does, and a race writes a body of its own.
"""

import contextlib
import os
import threading

import pytest

from mendwright.backends import (OVERRIDES, Conflict, MemoryBackend, Read, StoreBackend,
                                 key_record_name)

TOKEN = 'contract-token-0123456789abcdef'
BUCKET = 'mendwright-contract'
PREFIX = 'team/store/'
MOTO = '5.1.22'
BOTO3 = '1.35.69'
THREADS = 16
JOIN = 60           # seconds a race may take before the test fails
OTHER_VERSION = '"0123456789abcdef0123456789abcdef"'    # a version no object here has


class Store:
    def __init__(self, kind, new):
        self.kind = kind
        self.new = new              # another backend on this store
        self.backend = new()


@pytest.fixture(autouse=True)
def _no_store_token(monkeypatch):
    for name in ('MENDWRIGHT_STORE_TOKEN', 'MENDWRIGHT_STORE_TOKEN_FILE'):
        monkeypatch.delenv(name, raising=False)


def aws_offline(monkeypatch, tmp_path):
    """Fake credentials, and none of what could send boto3 to AWS or to another account:
    no profile, role, endpoint or config file of the developer's, no instance metadata."""
    for name in list(os.environ):
        if name.upper().startswith('AWS_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED', 'true')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'no-aws-config'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'no-aws-credentials'))


@contextlib.contextmanager
def moto_s3(monkeypatch, tmp_path):
    pytest.importorskip(
        'boto3', minversion=BOTO3,
        reason='boto3 ' + BOTO3 + ' or later runs the S3 tests: pip install "mendwright[s3]" '
               '(the test extra has it)')
    moto = pytest.importorskip(
        'moto', minversion=MOTO,
        reason='moto ' + MOTO + ' or later runs the S3 tests offline: pip install "moto[s3]>=' + MOTO
               + '" (the test extra has it)')
    from moto.core.botocore_stubber import BotocoreStubber
    aws_offline(monkeypatch, tmp_path)
    one_at_a_time = threading.Lock()
    process = BotocoreStubber.process_request

    def atomic(self, request):
        with one_at_a_time:
            return process(self, request)

    monkeypatch.setattr(BotocoreStubber, 'process_request', atomic)
    with moto.mock_aws():
        import boto3
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket=BUCKET)
        yield


@contextlib.contextmanager
def _memory(request):
    backend = MemoryBackend()
    yield Store('memory', lambda: backend)


@contextlib.contextmanager
def _http(request):
    from mendwright.backends.http import HttpBackend
    from mendwright.backends.server import StoreServer
    request.getfixturevalue('monkeypatch').setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    with StoreServer(request.getfixturevalue('tmp_path') / 'store', token=TOKEN) as server:
        yield Store('http', lambda: HttpBackend(server.url))


@contextlib.contextmanager
def _s3(request):
    with moto_s3(request.getfixturevalue('monkeypatch'), request.getfixturevalue('tmp_path')):
        from mendwright.backends.s3 import S3Backend
        yield Store('s3', lambda: S3Backend(BUCKET, PREFIX))


# One entry per backend; memory first, since a failure there means the test is wrong.
BACKENDS = {'memory': _memory, 'http': _http, 's3': _s3}


@pytest.fixture(params=list(BACKENDS))
def store(request):
    with BACKENDS[request.param](request) as made:
        yield made


def run_together(jobs):
    """Run each job in a thread of its own, started together, and raise the first error one
    of them raised."""
    start = threading.Barrier(len(jobs))
    errors = []

    def thread(job):
        def run():
            try:
                start.wait(JOIN)
                job()
            except Exception as exc:        # raised again in the test's own thread
                errors.append(exc)
        return threading.Thread(target=run, daemon=True)

    threads = [thread(job) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN)
    assert not [t for t in threads if t.is_alive()], 'a thread was still running after %d s' % JOIN
    if errors:
        raise errors[0]


# --- reads ------------------------------------------------------------------------------------

def test_the_backend_keeps_the_protocol(store):
    assert isinstance(store.backend, StoreBackend)
    place = store.backend.describe()
    assert isinstance(place, str) and place


def test_a_missing_object_reads_as_nothing(store):
    assert store.backend.get(OVERRIDES) == Read(None, None, False)
    assert store.backend.get(OVERRIDES, if_none_match=OTHER_VERSION) == Read(None, None, False)


def test_get_gives_the_body_and_the_version_put_returned(store):
    version = store.backend.put(OVERRIDES, b'{"a": 1}')
    assert isinstance(version, str) and version
    assert store.backend.get(OVERRIDES) == Read(b'{"a": 1}', version, False)


def test_get_if_none_match_the_current_version_is_not_modified(store):
    version = store.backend.put(OVERRIDES, b'{"a": 1}')
    assert store.backend.get(OVERRIDES, if_none_match=version) == Read(None, version, True)


def test_get_if_none_match_an_older_version_gives_the_body(store):
    old = store.backend.put(OVERRIDES, b'old')
    new = store.backend.put(OVERRIDES, b'new')
    assert store.backend.get(OVERRIDES, if_none_match=old) == Read(b'new', new, False)
    assert store.backend.get(OVERRIDES, if_none_match=OTHER_VERSION) == Read(b'new', new, False)


def test_a_version_changes_whenever_the_body_changes(store):
    versions = [store.backend.put(OVERRIDES, body) for body in (b'one', b'two', b'three', b'')]
    assert all(isinstance(v, str) and v for v in versions)
    assert len(set(versions)) == 4


# --- put --------------------------------------------------------------------------------------

def test_put_without_a_condition_creates_or_replaces(store):
    store.backend.put(OVERRIDES, b'first')
    version = store.backend.put(OVERRIDES, b'second')
    assert store.backend.get(OVERRIDES) == Read(b'second', version, False)


def test_put_if_match_the_current_version_writes(store):
    old = store.backend.put(OVERRIDES, b'old')
    new = store.backend.put(OVERRIDES, b'new', if_match=old)
    assert new != old
    assert store.backend.get(OVERRIDES) == Read(b'new', new, False)


def test_put_if_match_an_older_version_conflicts_and_writes_nothing(store):
    old = store.backend.put(OVERRIDES, b'old')
    new = store.backend.put(OVERRIDES, b'new', if_match=old)
    with pytest.raises(Conflict):
        store.backend.put(OVERRIDES, b'lost', if_match=old)
    with pytest.raises(Conflict):
        store.backend.put(OVERRIDES, b'lost', if_match=OTHER_VERSION)
    assert store.backend.get(OVERRIDES) == Read(b'new', new, False)


def test_put_if_match_on_an_object_that_is_gone_conflicts(store):
    version = store.backend.put(OVERRIDES, b'here')
    store.backend.delete(OVERRIDES)
    with pytest.raises(Conflict):
        store.backend.put(OVERRIDES, b'lost', if_match=version)
    with pytest.raises(Conflict):
        store.backend.put(key_record_name('never'), b'lost', if_match=version)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)
    assert store.backend.get(key_record_name('never')) == Read(None, None, False)


def test_create_only_writes_an_object_that_does_not_exist(store):
    version = store.backend.put(OVERRIDES, b'new', create_only=True)
    assert store.backend.get(OVERRIDES) == Read(b'new', version, False)


def test_create_only_conflicts_when_the_object_exists_and_writes_nothing(store):
    version = store.backend.put(OVERRIDES, b'first')
    with pytest.raises(Conflict):
        store.backend.put(OVERRIDES, b'second', create_only=True)
    assert store.backend.get(OVERRIDES) == Read(b'first', version, False)


def test_create_only_writes_again_after_a_delete(store):
    store.backend.put(OVERRIDES, b'first', create_only=True)
    store.backend.delete(OVERRIDES)
    version = store.backend.put(OVERRIDES, b'again', create_only=True)
    assert store.backend.get(OVERRIDES) == Read(b'again', version, False)


def test_put_takes_if_match_or_create_only_not_both(store):
    version = store.backend.put(key_record_name('k'), b'x')
    with pytest.raises(ValueError):
        store.backend.put(OVERRIDES, b'x', if_match=version, create_only=True)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)


def test_put_takes_the_body_as_bytes(store):
    with pytest.raises(TypeError):
        store.backend.put(OVERRIDES, '{"a": 1}')
    assert store.backend.get(OVERRIDES) == Read(None, None, False)
    version = store.backend.put(OVERRIDES, bytearray(b'{"a": 1}'))
    assert store.backend.get(OVERRIDES) == Read(b'{"a": 1}', version, False)


def test_an_empty_body_is_an_object(store):
    version = store.backend.put(OVERRIDES, b'')
    assert store.backend.get(OVERRIDES) == Read(b'', version, False)
    assert store.backend.get(OVERRIDES, if_none_match=version) == Read(None, version, True)


def test_any_bytes_come_back_as_they_went(store):
    body = bytes(range(256)) * 64 + b'\x00\r\n\xff'
    version = store.backend.put(OVERRIDES, body)
    assert store.backend.get(OVERRIDES) == Read(body, version, False)
    big = os.urandom(2 * 1024 * 1024 + 3)
    version = store.backend.put(OVERRIDES, big, if_match=version)
    assert store.backend.get(OVERRIDES) == Read(big, version, False)


# --- delete -----------------------------------------------------------------------------------

def test_delete_if_match_the_current_version_removes_the_object(store):
    version = store.backend.put(OVERRIDES, b'x')
    store.backend.delete(OVERRIDES, if_match=version)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)


def test_delete_if_match_an_older_version_conflicts_and_removes_nothing(store):
    old = store.backend.put(OVERRIDES, b'old')
    new = store.backend.put(OVERRIDES, b'new')
    with pytest.raises(Conflict):
        store.backend.delete(OVERRIDES, if_match=old)
    assert store.backend.get(OVERRIDES) == Read(b'new', new, False)


def test_delete_without_a_condition_removes_the_object(store):
    store.backend.put(OVERRIDES, b'x')
    store.backend.delete(OVERRIDES)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)


def test_deleting_an_object_that_is_gone_is_not_an_error(store):
    store.backend.delete(OVERRIDES)
    store.backend.delete(OVERRIDES, if_match=OTHER_VERSION)
    version = store.backend.put(OVERRIDES, b'x')
    store.backend.delete(OVERRIDES, if_match=version)
    store.backend.delete(OVERRIDES, if_match=version)
    store.backend.delete(OVERRIDES)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)


# --- names and places -------------------------------------------------------------------------

def test_each_name_is_an_object_of_its_own(store):
    first, second = key_record_name('checkout.pay'), key_record_name('login.submit')
    v_overrides = store.backend.put(OVERRIDES, b'{}')
    v_first = store.backend.put(first, b'{"lease": 1}')
    v_second = store.backend.put(second, b'{"lease": 2}')
    store.backend.delete(first, if_match=v_first)
    store.backend.put(second, b'{"lease": 3}', if_match=v_second)
    assert store.backend.get(OVERRIDES) == Read(b'{}', v_overrides, False)
    assert store.backend.get(first) == Read(None, None, False)
    assert store.backend.get(second).body == b'{"lease": 3}'


def test_another_backend_on_the_same_store_sees_every_write(store):
    other = store.new()
    first = store.backend.put(OVERRIDES, b'first')
    assert other.get(OVERRIDES) == Read(b'first', first, False)
    second = other.put(OVERRIDES, b'second', if_match=first)
    with pytest.raises(Conflict):
        store.backend.put(OVERRIDES, b'lost', if_match=first)
    assert store.backend.get(OVERRIDES, if_none_match=second) == Read(None, second, True)
    other.delete(OVERRIDES, if_match=second)
    assert store.backend.get(OVERRIDES) == Read(None, None, False)


# --- races ------------------------------------------------------------------------------------

def test_concurrent_increments_lose_no_update(store):
    """N workers each read a counter and write it back one higher with if_match, reading
    again on a Conflict: the counter ends at exactly N."""
    store.backend.put('counter', b'0')
    backends = [store.new() for _ in range(THREADS)]    # made here: boto3 is not safe to set up in threads
    conflicts = []

    def increment(backend):
        def job():
            for _ in range(50 * THREADS):
                read = backend.get('counter')
                try:
                    backend.put('counter', b'%d' % (int(read.body) + 1), if_match=read.version)
                    return
                except Conflict:
                    conflicts.append(1)
            raise AssertionError('no write won after %d tries' % (50 * THREADS))
        return job

    run_together([increment(b) for b in backends])
    assert store.backend.get('counter').body == b'%d' % THREADS


def test_of_racing_create_only_writes_exactly_one_wins(store):
    name = key_record_name('lease')
    backend = store.backend             # one backend shared by every thread
    won, lost = [], []

    def create(i):
        def job():
            try:
                won.append((i, backend.put(name, b'worker %d' % i, create_only=True)))
            except Conflict:
                lost.append(i)
        return job

    run_together([create(i) for i in range(THREADS)])
    assert len(won) == 1 and len(lost) == THREADS - 1
    winner, version = won[0]
    assert store.backend.get(name) == Read(b'worker %d' % winner, version, False)


def test_racing_deletes_of_one_version_all_succeed_and_the_object_is_gone(store):
    name = key_record_name('lease')
    version = store.backend.put(name, b'held')
    backends = [store.new() for _ in range(THREADS)]
    run_together([lambda b=b: b.delete(name, if_match=version) for b in backends])
    assert store.backend.get(name) == Read(None, None, False)


def test_racing_deletes_and_writes_of_one_version_never_both_win(store):
    """Half the workers delete with if_match=v, half write with if_match=v. Either one write
    won, and every other write and every delete conflicted, or a delete won: the object is
    gone, every write conflicted, and no delete failed (gone is not an error)."""
    name = key_record_name('lease')
    version = store.backend.put(name, b'held')
    backends = [store.new() for _ in range(THREADS)]
    results = {}

    def worker(i, backend):
        def job():
            try:
                if i % 2:
                    results[i] = ('wrote', backend.put(name, b'writer %d' % i, if_match=version))
                else:
                    backend.delete(name, if_match=version)
                    results[i] = ('deleted', None)
            except Conflict:
                results[i] = ('conflict', None)
        return job

    run_together([worker(i, b) for i, b in enumerate(backends)])
    writers = [i for i in results if i % 2]
    deleters = [i for i in results if not i % 2]
    wrote = [i for i in writers if results[i][0] == 'wrote']
    final = store.backend.get(name)
    if wrote:
        assert len(wrote) == 1, results
        assert final == Read(b'writer %d' % wrote[0], results[wrote[0]][1], False)
        assert all(results[i][0] == 'conflict' for i in deleters), results
    else:
        assert final == Read(None, None, False)
        assert all(results[i][0] == 'conflict' for i in writers), results
        assert all(results[i][0] == 'deleted' for i in deleters), results


def test_a_reader_sees_the_old_body_or_the_new_one_never_a_mix(store):
    name = key_record_name('big')
    small, large = b'a' * 200_000, b'b' * 300_001
    store.backend.put(name, small)
    readers = [store.new() for _ in range(3)]
    done = threading.Event()
    seen = []

    def write():
        try:
            for i in range(20):
                store.backend.put(name, large if i % 2 == 0 else small)
        finally:
            done.set()

    def read(backend):
        def job():
            while True:
                finished = done.is_set()
                body = backend.get(name).body
                assert body in (small, large), 'a read of %d bytes is neither body' % len(body)
                seen.append(len(body))
                if finished:
                    return
        return job

    run_together([write] + [read(b) for b in readers])
    assert seen
