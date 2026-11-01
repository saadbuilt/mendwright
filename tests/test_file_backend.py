"""The storage backend contract (mendwright.backends), held for the memory backend and for
backends.file.FileBackend, the files of a folder on this machine: versioned reads, writes that
compare a version first (if_match) or create only (create_only), deletes, and racing writers
that never lose each other's changes."""

import os
import threading

import pytest

from mendwright.backends import Conflict, MemoryBackend, Read, StoreBackend, Unavailable
from mendwright.backends.file import FileBackend


@pytest.fixture(params=['memory', 'file'])
def backend(request, tmp_path):
    return MemoryBackend() if request.param == 'memory' else FileBackend(tmp_path)


def test_it_keeps_the_protocol(backend):
    assert isinstance(backend, StoreBackend) and isinstance(backend.describe(), str)


def test_a_missing_object_reads_as_none(backend):
    assert backend.get('keys/a.json') == Read(None, None, False)
    assert backend.get('keys/a.json', if_none_match='1') == Read(None, None, False)


def test_a_read_gives_the_body_and_its_version(backend):
    version = backend.put('keys/a.json', b'{"n": 1}')
    assert backend.get('keys/a.json') == Read(b'{"n": 1}', version, False)
    assert backend.get('keys/a.json', if_none_match=version) == Read(None, version, True)
    assert backend.get('keys/a.json', if_none_match='other') == Read(b'{"n": 1}', version, False)


def test_every_change_of_the_body_gives_a_new_version(backend):
    first = backend.put('o', b'aaaa')
    second = backend.put('o', b'bbbb')                  # the same size
    assert first != second
    assert backend.get('o', if_none_match=first).not_modified is False


def test_create_only_writes_only_what_does_not_exist(backend):
    version = backend.put('o', b'one', create_only=True)
    with pytest.raises(Conflict):
        backend.put('o', b'two', create_only=True)
    assert backend.get('o') == Read(b'one', version, False)


def test_if_match_writes_only_over_that_version(backend):
    first = backend.put('o', b'one')
    second = backend.put('o', b'two', if_match=first)
    with pytest.raises(Conflict):
        backend.put('o', b'three', if_match=first)      # changed since
    with pytest.raises(Conflict):
        backend.put('gone', b'x', if_match=first)       # does not exist
    assert backend.get('o') == Read(b'two', second, False)


def test_bad_arguments_are_refused(backend):
    with pytest.raises(ValueError):
        backend.put('o', b'x', if_match='1', create_only=True)
    with pytest.raises(TypeError):
        backend.put('o', 'text')


def test_delete_compares_the_version_and_a_gone_object_is_no_error(backend):
    version = backend.put('o', b'one')
    with pytest.raises(Conflict):
        backend.delete('o', if_match='stale')
    backend.delete('o', if_match=version)
    assert backend.get('o').body is None
    backend.delete('o')
    backend.delete('o', if_match=version)


def test_racing_creators_one_wins(backend):
    wins, start = [], threading.Barrier(16)

    def create(i):
        start.wait()
        try:
            backend.put('lease', str(i).encode(), create_only=True)
            wins.append(i)
        except Conflict:
            pass
    threads = [threading.Thread(target=create, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(wins) == 1 and backend.get('lease').body == str(wins[0]).encode()


def test_racing_compare_and_swap_writers_lose_no_change(backend):
    backend.put('count', b'0')
    start = threading.Barrier(8)

    def add():
        start.wait()
        for _ in range(10):
            while True:
                found = backend.get('count')
                try:
                    backend.put('count', str(int(found.body) + 1).encode(), if_match=found.version)
                    break
                except Conflict:
                    continue
    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert backend.get('count').body == b'80'


# --- the files of a folder ---------------------------------------------------------------------

@pytest.mark.parametrize('name', ['../out.json', '/abs.json', 'keys/../../x', 'a\\b', 'c:x', '',
                                  'keys//a.json'])
def test_a_name_never_leaves_the_folder(tmp_path, name):
    with pytest.raises(ValueError):
        FileBackend(tmp_path / 'store').put(name, b'x')


def test_objects_are_files_in_the_folder_and_versions_their_stat(tmp_path):
    backend = FileBackend(tmp_path)
    version = backend.put('keys/0a.json', b'{}')
    path = tmp_path / 'keys' / '0a.json'
    st = os.stat(path)
    assert path.read_bytes() == b'{}'
    assert version == '%d-%d-%d' % (st.st_ino, st.st_mtime_ns, st.st_size)
    assert not list((tmp_path / 'keys').glob('*.lock')) and not list(
        (tmp_path / 'keys').glob('*.tmp'))


def test_a_lock_held_by_another_writer_is_unavailable(tmp_path):
    backend = FileBackend(tmp_path)
    backend.lock_timeout, backend.lock_stale_after = 0.2, 3600
    backend.put('o', b'one')
    (tmp_path / 'o.lock').write_text('999999', encoding='ascii')
    with pytest.raises(Unavailable, match='held by another writer'):
        backend.put('o', b'two')
    assert backend.get('o').body == b'one'


def test_a_replace_the_os_refuses_for_a_while_is_tried_again(tmp_path, monkeypatch):
    # Windows refuses the replace while a reader holds the file; readers that keep reading
    # leave only short gaps, so a write keeps trying, more often than a read does.
    slept = []
    backend = FileBackend(tmp_path, sleep=slept.append)
    backend.put('o', b'one')
    real, refusals = os.replace, [30]

    def replace(src, dst):
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(13, 'Access is denied')
        return real(src, dst)

    monkeypatch.setattr(os, 'replace', replace)
    backend.put('o', b'two')
    assert backend.get('o').body == b'two'
    assert len(slept) == 30 and max(slept) <= 0.02 and sum(slept) < backend.lock_timeout
    assert not list(tmp_path.glob('*.tmp'))


def test_a_replace_the_os_keeps_refusing_is_unavailable_after_lock_timeout(tmp_path,
                                                                          monkeypatch):
    slept = []
    backend = FileBackend(tmp_path, sleep=slept.append)
    backend.put('o', b'one')

    def replace(src, dst):
        raise PermissionError(13, 'Access is denied')

    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(Unavailable, match='could not write o'):
        backend.put('o', b'two')
    assert backend.lock_timeout <= sum(slept) < backend.lock_timeout + 0.02
    monkeypatch.undo()
    assert backend.get('o').body == b'one'
    assert not list(tmp_path.glob('*.tmp')) and not list(tmp_path.glob('*.lock'))


def test_readers_that_keep_reading_see_whole_bodies_and_never_stop_a_write(tmp_path):
    backend = FileBackend(tmp_path)
    small, large = b's' * 200_000, b'L' * 300_001
    backend.put('keys/big.json', small)
    writing_done, start = threading.Event(), threading.Barrier(4)
    failures, lengths = [], []

    def write():
        try:
            start.wait()
            for i in range(20):
                backend.put('keys/big.json', large if i % 2 == 0 else small)
        except Exception as exc:  # noqa: BLE001 - reported below
            failures.append(exc)
        finally:
            writing_done.set()

    def read():
        reader = FileBackend(tmp_path)
        try:
            start.wait()
            while True:
                last = writing_done.is_set()
                body = reader.get('keys/big.json').body
                if body not in (small, large):
                    failures.append('a read of %d bytes is neither body' % len(body))
                lengths.append(len(body))
                if last:
                    return
        except Exception as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    threads = [threading.Thread(target=write)] + [threading.Thread(target=read)
                                                    for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert backend.get('keys/big.json').body == small and len(lengths) >= 3
