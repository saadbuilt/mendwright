"""The override store: '~' in paths, write failures, and several stores (processes)
sharing one file."""

import json
import logging
import os

import pytest

from mendwright import SelectorStore, load_store
from mendwright.store import StoreReadError


def _load(tmp_path, seed=None):
    if seed is None:
        return load_store(None, str(tmp_path / 'o.json'))
    seed_path = tmp_path / 'seed.json'
    seed_path.write_text(json.dumps(seed), encoding='utf-8')
    return load_store(str(seed_path), str(tmp_path / 'o.json'))


def _saved(tmp_path):
    return json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))


def _xpaths(saved, key):
    return {c['xpath']: c for c in saved['selectors'][key]['candidates']}


# --- '~' in paths -----------------------------------------------------------------

def test_tilde_paths_expand_to_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cwd = tmp_path / 'cwd'
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.chdir(cwd)
    (home / 'seed.json').write_text(json.dumps({'selectors': [{'key': 'k', 'xpath': '//a'}]}))
    store = load_store('~/seed.json', '~/.myapp/overrides.json')
    assert [c['xpath'] for c in store.candidates('k')] == ['//a']   # seed found under ~
    store.add_candidate('k', '//b', source='heal')
    assert (home / '.myapp' / 'overrides.json').exists()
    assert not (cwd / '~').exists()


# --- write failures ---------------------------------------------------------------

def test_an_unwritable_store_warns_once_and_keeps_working(tmp_path, caplog):
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('x')
    store = load_store(None, str(blocker / 'overrides.json'))
    with caplog.at_level(logging.DEBUG, logger='mendwright'):
        store.add_candidate('k', '//a', source='code')
        store.add_candidate('k', '//b', source='heal')
        store.set_meta('k', role='button')
    assert [c['xpath'] for c in store.candidates('k')] == ['//a', '//b']
    assert store.meta('k')['expectedRole'] == 'button'
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'could not save selector changes' in warnings[0]
    assert 'store_path=' in warnings[0] and 'MENDWRIGHT_STORE' in warnings[0]


def test_replace_is_retried_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    store = _load(tmp_path)
    store._sleep = lambda seconds: None
    real_replace = os.replace
    failures = {'left': 2}

    def flaky_replace(src, dst):
        if failures['left']:
            failures['left'] -= 1
            raise PermissionError(13, 'The process cannot access the file')
        return real_replace(src, dst)

    monkeypatch.setattr(os, 'replace', flaky_replace)
    store.add_candidate('k', '//a', source='code')
    assert failures['left'] == 0
    assert '//a' in _xpaths(_saved(tmp_path), 'k')
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith('.sel-')] == []


# --- the lock file ----------------------------------------------------------------

def test_the_lock_file_is_removed_after_a_write(tmp_path):
    store = _load(tmp_path)
    store.add_candidate('k', '//a', source='code')
    assert not (tmp_path / 'o.json.lock').exists()


def test_a_stale_lock_is_broken(tmp_path, caplog):
    lock = tmp_path / 'o.json.lock'
    lock.write_text('12345')
    os.utime(lock, (0, 0))                     # written in 1970: its owner is long gone
    store = _load(tmp_path)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store.add_candidate('k', '//a', source='code')
    assert '//a' in _xpaths(_saved(tmp_path), 'k')
    assert not lock.exists()
    assert any('stale lock' in r.getMessage() for r in caplog.records)


def test_a_held_lock_times_out_and_the_store_keeps_working(tmp_path, caplog):
    lock = tmp_path / 'o.json.lock'
    lock.write_text('12345')
    store = _load(tmp_path)
    now = {'t': 0.0}
    store._clock = lambda: now['t']
    store._wallclock = lambda: os.stat(lock).st_mtime     # the lock is always brand new
    store._sleep = lambda seconds: now.__setitem__('t', now['t'] + seconds)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store.add_candidate('k', '//a', source='code')
    assert now['t'] >= store.lock_timeout                  # it waited, then gave up
    assert not (tmp_path / 'o.json').exists()
    assert lock.exists()                                   # another process's lock is left alone
    assert [c['xpath'] for c in store.candidates('k')] == ['//a']
    assert any('held by another process' in r.getMessage() for r in caplog.records)
    lock.unlink()
    store.add_candidate('k', '//b', source='heal')         # the next write saves everything
    assert set(_xpaths(_saved(tmp_path), 'k')) == {'//a', '//b'}


# --- two stores, one file ---------------------------------------------------------

SEED = {'schema': 2, 'selectors': {'k': {'expectedRole': 'button', 'candidates': [
    {'xpath': '//seed', 'source': 'seed', 'status': 'active'}]}}}


def test_two_stores_do_not_lose_each_others_heals(tmp_path):
    a = _load(tmp_path, SEED)
    b = _load(tmp_path, SEED)
    a.add_candidate('terms', '//a-heal', source='heal')
    b.add_candidate('go', '//b-heal', source='heal')
    saved = _saved(tmp_path)
    assert set(saved['selectors']) == {'terms', 'go'}
    assert [c['xpath'] for c in b.candidates('terms')] == ['//a-heal']   # b took in a's heal


def test_a_quarantine_by_one_store_survives_a_write_by_the_other(tmp_path):
    a = _load(tmp_path, SEED)
    b = _load(tmp_path, SEED)
    a.add_candidate('k', '//healed', source='heal')
    b.add_candidate('other', '//x', source='heal')
    a.quarantine('k', '//healed')
    a.quarantine('k', '//seed')
    b.add_candidate('other', '//y', source='heal')        # b never touched k
    k = _xpaths(_saved(tmp_path), 'k')
    assert k['//healed']['status'] == 'quarantined'
    assert k['//seed']['status'] == 'quarantined'
    assert b.candidates('k') == []                        # b's memory caught up too


def test_when_both_stores_change_a_status_quarantine_wins(tmp_path):
    a = _load(tmp_path, SEED)
    b = _load(tmp_path, SEED)
    a.add_candidate('k', '//healed', source='heal')
    b.add_candidate('k', '//healed', source='heal')       # b added it too, then
    b.quarantine('k', '//healed')                         # quarantined it
    a.add_candidate('other', '//x', source='heal')        # a's stale 'active' must not win
    assert _xpaths(_saved(tmp_path), 'k')['//healed']['status'] == 'quarantined'


def test_resolving_a_heal_does_not_undo_another_stores_quarantine(tmp_path):
    a = _load(tmp_path, SEED)
    a.add_candidate('k', '//healed', source='heal')
    b = _load(tmp_path, SEED)
    a.record_success('k', '//healed')                     # a keeps using the heal
    b.quarantine('k', '//healed')                         # b's action with it failed
    a.add_candidate('other', '//x', source='heal')        # a writes for another key
    assert _xpaths(_saved(tmp_path), 'k')['//healed']['status'] == 'quarantined'
    assert '//healed' not in [c['xpath'] for c in a.candidates('k')]


def test_this_stores_own_change_is_kept(tmp_path):
    a = _load(tmp_path, SEED)
    a.add_candidate('k', '//healed', source='heal')
    b = _load(tmp_path, SEED)
    a.quarantine('k', '//healed')
    b.add_candidate('other', '//x', source='heal')        # b never changed //healed
    a.set_code_candidates('k', ['//healed'])              # a's code now uses it: code wins
    assert _xpaths(_saved(tmp_path), 'k')['//healed']['status'] == 'active'


def test_a_destructive_flag_set_elsewhere_survives(tmp_path):
    a = _load(tmp_path, SEED)
    b = _load(tmp_path, SEED)
    a.set_meta('go', destructive=False)
    b.set_meta('go', destructive=True)
    a.add_candidate('other', '//x', source='heal')
    assert _saved(tmp_path)['selectors']['go']['destructive'] is True
    assert a.meta('go')['destructive'] is True


def test_an_explicit_false_still_clears_a_flag_set_earlier(tmp_path):
    a = _load(tmp_path, SEED)
    a.set_meta('go', destructive=True)
    b = _load(tmp_path, SEED)
    b.set_meta('go', destructive=False)                   # only b changed it since loading
    a.add_candidate('other', '//x', source='heal')
    assert _saved(tmp_path)['selectors']['go']['destructive'] is False


def test_metadata_from_another_store_is_kept(tmp_path):
    a = _load(tmp_path, SEED)
    b = _load(tmp_path, SEED)
    a.set_meta('pay', role='button', destructive=True)
    b.add_candidate('go', '//go', source='code')
    saved = _saved(tmp_path)
    assert saved['selectors']['pay']['destructive'] is True
    assert b.meta('pay')['destructive'] is True


def test_newest_success_time_wins(tmp_path):
    a = _load(tmp_path, SEED)
    a.add_candidate('k', '//one', source='heal')
    a.add_candidate('k', '//two', source='heal')
    b = _load(tmp_path, SEED)
    a.record_success('k', '//two', now='2026-10-10T10:00:00+00:00')
    b.record_success('k', '//one', now='2026-10-10T09:00:00+00:00')
    k = _xpaths(_saved(tmp_path), 'k')
    assert k['//two']['lastSucceededAt'] == '2026-10-10T10:00:00+00:00'
    assert k['//one']['lastSucceededAt'] == '2026-10-10T09:00:00+00:00'


# --- the cap ----------------------------------------------------------------------

def test_inactive_candidates_do_not_push_out_active_ones(tmp_path):
    data = {'schema': 2, 'selectors': {'k': {'expectedRole': 'button', 'maxCandidates': 2,
                                             'candidates': []}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    for i in range(4):
        store.add_candidate('k', '//old%d' % i, source='heal', now='2026-01-0%dT00:00:00' % (i + 1))
        store.quarantine('k', '//old%d' % i)
    store.add_candidate('k', '//new', source='code')       # never succeeded yet
    saved = _xpaths(_saved(tmp_path), 'k')
    assert saved['//new']['status'] == 'active'
    assert len([x for x in saved if x.startswith('//old')]) == 2   # inactive rows have their own cap


# --- only a missing file is an empty store ----------------------------------------------

SAVED = {'schema': 2, 'selectors': {'wire_go': {
    'expectedRole': 'button', 'destructive': True, 'candidates': [
        {'xpath': '//old', 'source': 'heal', 'status': 'quarantined'}]}}}


@pytest.mark.parametrize('text, says', [
    ('{"selectors": [{"key": "pay", "xpath": "//a"},]}', 'is not valid JSON'),
    ('<<<<<<< HEAD\n' + json.dumps(SAVED) + '\n=======\n{}\n>>>>>>> branch\n', 'is not valid JSON'),
    ('[]', 'is not a selector file'),
    ('null', 'is not a selector file'),
    ('{"schema": 2}', 'is not a selector file'),
    ('{"selectors": {"k": {}}}', 'is not a selector file'),
])
def test_a_seed_or_override_that_cannot_be_read_raises(text, says, tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text(text, encoding='utf-8')
    for seed, override in ((str(bad), str(tmp_path / 'o.json')), (None, str(bad))):
        with pytest.raises(StoreReadError) as caught:
            load_store(seed, override)
        assert str(bad) in str(caught.value) and says in str(caught.value)
    assert bad.read_text(encoding='utf-8') == text


def test_a_utf16_seed_raises(tmp_path):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'selectors': [{'key': 'pay', 'xpath': '//a'}]}), encoding='utf-16')
    with pytest.raises(StoreReadError, match='is not UTF-8 text'):
        load_store(str(seed), str(tmp_path / 'o.json'))


def test_a_missing_seed_raises_and_none_means_no_seed(tmp_path):
    with pytest.raises(FileNotFoundError, match='seed_path=None'):
        load_store(str(tmp_path / 'missing.json'), str(tmp_path / 'o.json'))
    assert load_store(None, str(tmp_path / 'o.json')).candidates('k') == []


@pytest.mark.parametrize('text', ['', '  \n', '{}'])
def test_an_empty_override_is_an_empty_store(text, tmp_path):
    (tmp_path / 'o.json').write_text(text, encoding='utf-8')
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('k', '//a', source='code')
    assert list(_saved(tmp_path)['selectors']) == ['k']


def test_open_refuses_a_conflicted_store_before_any_heal(tmp_path, make_page, stub_provider):
    import mendwright
    sp = tmp_path / 'o.json'
    text = '<<<<<<< HEAD\n' + json.dumps(SAVED) + '\n=======\n{}\n>>>>>>> other\n'
    sp.write_text(text, encoding='utf-8')
    provider = stub_provider("//button[@id='go']")
    with pytest.raises(ValueError, match='is not valid JSON'):
        mendwright.open(make_page("<button id='go'>Send</button>"), provider=provider,
                        store_path=str(sp))
    assert provider.bundles == []
    assert sp.read_text(encoding='utf-8') == text


def test_a_file_that_breaks_mid_run_is_not_overwritten(tmp_path, caplog):
    store = _load(tmp_path)
    store.add_candidate('k', '//a', source='code')
    (tmp_path / 'o.json').write_text('{"schema": 2, "selectors": {', encoding='utf-8')
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        store.add_candidate('k', '//b', source='heal')
        store.add_candidate('k', '//c', source='heal')
    assert (tmp_path / 'o.json').read_text(encoding='utf-8') == '{"schema": 2, "selectors": {'
    assert [c['xpath'] for c in store.candidates('k')] == ['//a', '//b', '//c']
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'is not saving' in warnings[0] and 'Fix or delete the file' in warnings[0]


def _flaky_reads(monkeypatch, failures):
    """Path.read_text raises PermissionError ``failures`` times, as Windows does while
    another process replaces the file."""
    from pathlib import Path
    real = Path.read_text
    left = [failures]

    def read_text(self, *args, **kwargs):
        if self.name == 'o.json' and left[0] > 0:
            left[0] -= 1
            raise PermissionError(13, 'The process cannot access the file')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    monkeypatch.setattr('time.sleep', lambda s: None)
    return left


def test_a_read_refused_for_a_moment_is_retried(tmp_path, monkeypatch):
    _load(tmp_path).add_candidate('k', '//a', source='code')
    left = _flaky_reads(monkeypatch, 2)
    store = load_store(None, str(tmp_path / 'o.json'))
    assert left == [0]
    assert [c['xpath'] for c in store.candidates('k')] == ['//a']


def test_a_read_that_stays_refused_raises_instead_of_reading_empty(tmp_path, monkeypatch):
    _load(tmp_path).add_candidate('k', '//a', source='code')
    _flaky_reads(monkeypatch, 100)
    with pytest.raises(PermissionError):
        load_store(None, str(tmp_path / 'o.json'))


def test_a_write_whose_read_stays_refused_leaves_the_file_alone(tmp_path, monkeypatch, caplog):
    first = _load(tmp_path)
    first.add_candidate('k', '//a', source='code')
    before = (tmp_path / 'o.json').read_text(encoding='utf-8')
    other = _load(tmp_path)
    other._sleep = lambda s: None
    _flaky_reads(monkeypatch, 100)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        other.add_candidate('mine', '//m', source='code')
    monkeypatch.undo()
    assert (tmp_path / 'o.json').read_text(encoding='utf-8') == before
    assert any('could not save' in r.getMessage() for r in caplog.records)
