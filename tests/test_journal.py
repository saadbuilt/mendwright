"""The observability side-channel: Journal appends redacted heal events as JSONL, and a
write that fails is swallowed so it can never break a heal. Fully offline/deterministic:
a fixed clock is injected and no field uses wall-clock time or randomness."""

import json
import os

import pytest

from mendwright.journal import (
    ACCEPTED,
    APPLIED,
    CANARY,
    DEFAULT_PATH,
    HELD,
    PROPOSED,
    QUARANTINE,
    REJECTED,
    REQUESTED,
    Journal,
    NullJournal,
)

FIXED = '2026-01-01T00:00:00+00:00'


def _journal(tmp_path):
    # Nested under a not-yet-existing dir so we also exercise parent mkdir.
    return Journal(tmp_path / '.mendwright' / 'journal.jsonl', clock=lambda: FIXED)


def _read(tmp_path):
    path = tmp_path / '.mendwright' / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_default_path_is_relative_dot_mendwright():
    assert DEFAULT_PATH == '.mendwright/journal.jsonl'


def test_writes_events_and_reads_back_jsonl(tmp_path):
    j = _journal(tmp_path)
    j.requested('terms', provider='anthropic')
    j.proposed('terms', provider='anthropic', model='some-model-id',
               latency_ms=812, tokens={'input': 1300, 'output': 42},
               xpath="//input[@type='checkbox']")
    j.rejected('terms', xpath='//bad', reason='no element matched')
    j.applied('terms', xpath="//input[@type='checkbox']", source='heal')
    j.canary('terms', xpath="//input[@type='checkbox']", success=False)
    j.quarantine('terms', xpath="//input[@type='checkbox']")

    rows = _read(tmp_path)
    assert [r['event'] for r in rows] == [
        REQUESTED, PROPOSED, REJECTED, APPLIED, CANARY, QUARANTINE]
    # Every line carries the frame + the selector key.
    assert all(r['ts'] == FIXED and r['key'] == 'terms' for r in rows)

    proposed = rows[1]
    assert proposed['provider'] == 'anthropic'
    assert proposed['model'] == 'some-model-id'
    assert proposed['latency_ms'] == 812
    assert proposed['tokens'] == {'input': 1300, 'output': 42}  # nested dict survives
    assert proposed['xpath'] == "//input[@type='checkbox']"

    # "when known": fields not supplied are omitted, not written as null.
    assert 'model' not in rows[0] and 'latency_ms' not in rows[0] and 'tokens' not in rows[0]
    # False is real data, not "unknown" -> it must be kept.
    assert rows[4]['success'] is False


def test_list_heals_held_and_accepted_have_events_of_their_own(tmp_path):
    j = _journal(tmp_path)
    j.held('cart', provider='fingerprint', xpath='//li', id='3f2a9c1d', list=True, css=None)
    j.accepted('cart', xpath='//li', id='3f2a9c1d', list=True)
    rows = _read(tmp_path)
    assert (HELD, ACCEPTED) == ('heal.held', 'heal.accepted')
    assert [r['event'] for r in rows] == [HELD, ACCEPTED]
    assert rows[0] == {'ts': FIXED, 'event': HELD, 'key': 'cart', 'provider': 'fingerprint',
                       'xpath': '//li', 'id': '3f2a9c1d', 'list': True}
    NullJournal().held('cart')
    NullJournal().accepted('cart')


def test_appends_without_truncating(tmp_path):
    j = _journal(tmp_path)
    j.requested('a')
    j.requested('b')
    j.requested('c')
    rows = _read(tmp_path)
    assert [r['key'] for r in rows] == ['a', 'b', 'c']


def test_parent_directory_is_created(tmp_path):
    j = _journal(tmp_path)
    assert not (tmp_path / '.mendwright').exists()
    j.requested('terms')
    assert (tmp_path / '.mendwright' / 'journal.jsonl').exists()


def test_no_crlf_translation_on_disk(tmp_path):
    # Verifies O_BINARY: the Windows CRT must not rewrite '\n' as '\r\n'. One event ->
    # exactly one LF-terminated line and no CR anywhere.
    j = _journal(tmp_path)
    j.requested('terms')
    data = (tmp_path / '.mendwright' / 'journal.jsonl').read_bytes()
    assert b'\r' not in data
    assert data.endswith(b'\n')
    assert data.count(b'\n') == 1


def test_stray_frame_fields_do_not_override_the_frame(tmp_path):
    j = _journal(tmp_path)
    # ts=/event= as caller fields must not clobber the real frame; a field literally named
    # "name" is data (not the positional event name) and is recorded as-is.
    j.event('heal.requested', ts='HACK', event='HACK', key='k', name='widget')
    row = _read(tmp_path)[0]
    assert row['ts'] == FIXED
    assert row['event'] == 'heal.requested'
    assert row['name'] == 'widget'


def test_exotic_value_is_stringified_not_dropped(tmp_path):
    # A non-JSON-native value must degrade to a string via default=str rather than raise
    # and lose the whole line.
    j = _journal(tmp_path)
    j.proposed('terms', tokens={'a', 'b'})  # a set is not JSON-serializable
    row = _read(tmp_path)[0]
    assert row['event'] == PROPOSED
    assert isinstance(row['tokens'], str)


def test_null_journal_records_nothing(tmp_path, monkeypatch):
    # Default no-op sink: no return value, and nothing written even near the default path.
    monkeypatch.chdir(tmp_path)
    nj = NullJournal()
    assert nj.requested('terms', provider='anthropic') is None
    assert nj.proposed('terms') is None
    assert nj.event('heal.requested', key='terms') is None
    assert not (tmp_path / '.mendwright').exists()


def test_write_failure_is_swallowed_when_parent_is_a_file(tmp_path):
    # mkdir of the parent fails because a regular file sits where the dir would go.
    blocker = tmp_path / 'blocker'
    blocker.write_text('x', encoding='utf-8')
    j = Journal(blocker / 'journal.jsonl', clock=lambda: FIXED)
    assert j.requested('terms') is None  # does not raise
    assert blocker.is_file()             # untouched


def test_write_failure_is_swallowed_when_path_is_a_directory(tmp_path):
    # The journal path itself is a directory, so the open()/write() step fails on both
    # POSIX and Windows. Exercises the I/O path, not just mkdir.
    target = tmp_path / 'journal.jsonl'
    target.mkdir()
    j = Journal(target, clock=lambda: FIXED)
    assert j.requested('terms') is None  # does not raise
    assert j.event('heal.applied', key='terms', xpath='//x') is None


def test_clock_failure_is_swallowed(tmp_path):
    def boom():
        raise RuntimeError('no clock')

    j = Journal(tmp_path / '.mendwright' / 'journal.jsonl', clock=boom)
    assert j.requested('terms') is None  # clock error swallowed, nothing written
    assert not (tmp_path / '.mendwright' / 'journal.jsonl').exists()


_WRITER = '''
import sys, time
from mendwright.journal import Journal
path, who, start, count = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
journal = Journal(path)
while time.time() < start:
    pass
for seq in range(count):
    journal.proposed(who, seq=seq, pad='x' * (50 + (seq * 37) % 700))
'''


def test_several_processes_appending_at_once_lose_no_line(tmp_path):
    # pytest -n auto shares one journal between workers. On Windows, O_APPEND alone is a
    # seek then a write, and lines from different processes overwrote each other.
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'journal.jsonl'
    env = dict(os.environ, PYTHONPATH=str(root))
    start, writers, count = time.time() + 2.0, 4, 400
    procs = [subprocess.Popen([sys.executable, '-c', _WRITER, str(path), 'w%d' % n,
                               repr(start), str(count)], cwd=str(root), env=env)
             for n in range(writers)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    seen, broken = set(), 0
    for line in path.read_bytes().split(b'\n'):
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            broken += 1
            continue
        seen.add((event['key'], event['seq']))
    assert broken == 0
    assert seen == {('w%d' % n, seq) for n in range(writers) for seq in range(count)}


@pytest.mark.skipif(os.name != 'posix', reason='0o600 mode bits are POSIX-only')
def test_file_permissions_are_0o600(tmp_path):
    j = _journal(tmp_path)
    j.requested('terms')
    mode = (tmp_path / '.mendwright' / 'journal.jsonl').stat().st_mode & 0o777
    assert mode == 0o600
