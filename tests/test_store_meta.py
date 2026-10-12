"""Per-key metadata (expectedRole, expectedText, destructive) survives into the next
process. Each test opens the same files twice, the way two runs of a script would."""

import json

import pytest

import mendwright
from mendwright import AttentionRequired, SelectorStore, load_store

V1 = "<form><button id='go'>Go</button></form>"
V2 = "<form><button id='renamed'>Go</button></form>"   # the id in code is stale
OLD = "//button[@id='go']"


def _run(make_page, stub_provider, store_path, html, **open_kw):
    provider = stub_provider('//button')
    healer = mendwright.open(make_page(html), provider=provider, store_path=store_path,
                             **open_kw)
    return healer, provider


def _locate(healer, **kw):
    return healer.locator('go', OLD, role='button', timeout_ms=0, poll_ms=0, **kw)


def test_destructive_keys_still_protect_a_key_in_the_next_process(tmp_path, make_page,
                                                                  stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _ = _run(make_page, stub_provider, sp, V1, destructive_keys={'go'})
    _locate(first)
    second, provider = _run(make_page, stub_provider, sp, V2, destructive_keys={'go'})
    with pytest.raises(AttentionRequired, match='destructive control'):
        _locate(second)
    assert provider.bundles == []


def test_destructive_true_on_the_first_run_only_still_protects(tmp_path, make_page,
                                                               stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _ = _run(make_page, stub_provider, sp, V1)
    _locate(first, destructive=True)
    second, provider = _run(make_page, stub_provider, sp, V2)
    with pytest.raises(AttentionRequired, match='destructive control'):
        _locate(second)                       # destructive omitted: None never clears it
    assert provider.bundles == []
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert saved['selectors']['go']['destructive'] is True


def test_explicit_false_clears_a_flag_the_facade_saved(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _ = _run(make_page, stub_provider, sp, V1)
    _locate(first, destructive=True)
    second, provider = _run(make_page, stub_provider, sp, V2)
    loc = _locate(second, destructive=False)
    assert loc.count() == 1
    assert len(provider.bundles) == 1
    assert load_store(None, sp).meta('go')['destructive'] is False


def test_explicit_false_cannot_clear_destructive_keys(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _ = _run(make_page, stub_provider, sp, V1, destructive_keys={'go'})
    _locate(first, destructive=False)
    second, provider = _run(make_page, stub_provider, sp, V2, destructive_keys={'go'})
    with pytest.raises(AttentionRequired, match='destructive control'):
        _locate(second, destructive=False)
    assert provider.bundles == []


def test_explicit_false_cannot_clear_a_seed_flag(tmp_path, make_page, stub_provider):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'go': {
        'expectedRole': 'button', 'expectedText': ['Go'], 'destructive': True,
        'candidates': [{'xpath': OLD, 'source': 'seed', 'status': 'active'}]}}}))
    sp = str(tmp_path / 'o.json')
    for html in (V1, V2):
        provider = stub_provider('//button')
        healer = mendwright.open(make_page(html), provider=provider,
                                 store=load_store(str(seed), sp))
        if html == V1:
            _locate(healer, destructive=False)
            continue
        with pytest.raises(AttentionRequired, match='destructive control'):
            _locate(healer, destructive=False)
        assert provider.bundles == []


def test_role_and_text_survive_into_the_next_process(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _ = _run(make_page, stub_provider, sp, V1)
    first.locator('go', OLD, role='button', text='Go', timeout_ms=0, poll_ms=0)
    meta = load_store(None, sp).meta('go')
    assert meta['expectedRole'] == 'button'
    assert meta['expectedText'] == ['Go']


def test_the_pages_a_key_was_found_on_are_saved_and_read_back(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.add_candidate('next', '#next', source='code')
    for page in ('/signup', '/checkout', '/signup', '/a', '/b', '/c', '/d'):
        store.set_last_page('next', page)
    assert store.meta('next')['lastPage'] == '/d'
    assert store.meta('next')['pages'] == ['/d', '/c', '/b', '/a', '/signup']   # at most 5
    saved = json.loads(path.read_text(encoding='utf-8'))['selectors']['next']
    assert saved['lastPage'] == '/d' and saved['pages'] == ['/d', '/c', '/b', '/a', '/signup']
    again = load_store(None, str(path))
    assert again.meta('next')['lastPage'] == '/d'
    assert again.meta('next')['pages'] == ['/d', '/c', '/b', '/a', '/signup']


def test_set_last_page_writes_only_when_it_changes(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.add_candidate('next', '#next', source='code')
    store.set_last_page('next', '/signup')
    writes = []
    store._persist_override = lambda: writes.append(1)
    store.set_last_page('next', '/signup')
    store.set_last_page('next', None)
    store.set_last_page('next', '')
    assert writes == []
    store.set_last_page('next', '/checkout')
    assert writes == [1]
    # Back and forth between known pages: the order changes in memory, the file waits.
    store.set_last_page('next', '/signup')
    store.set_last_page('next', '/checkout')
    assert writes == [1]
    assert store.meta('next')['lastPage'] == '/checkout'


def test_a_reordered_page_list_is_saved_with_the_next_write(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.add_candidate('next', '#next', source='code')
    store.set_last_page('next', '/signup')
    store.set_last_page('next', '/checkout')
    store.set_last_page('next', '/signup')              # a reorder: not written yet
    store.set_meta('next', role='button')               # any later write carries it
    saved = json.loads(path.read_text(encoding='utf-8'))['selectors']['next']
    assert saved['lastPage'] == '/signup' and saved['pages'] == ['/signup', '/checkout']


def test_two_processes_keep_the_pages_both_found_a_key_on(tmp_path):
    path = str(tmp_path / 'o.json')
    first = load_store(None, path)
    first.add_candidate('search', '#q', source='code')
    first.set_meta('search', role='input')
    a = load_store(None, path)
    b = load_store(None, path)
    a.set_last_page('search', '/search')
    b.set_last_page('search', '/results')
    meta = load_store(None, path).meta('search')
    assert meta['pages'] == ['/results', '/search']
    assert meta['lastPage'] == '/results'


def test_malformed_saved_pages_are_ignored(tmp_path):
    path = tmp_path / 'o.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'lastPage': 5, 'pages': ['/a', 3], 'candidates': [
            {'xpath': '#k', 'source': 'code', 'status': 'active'}]}}}), encoding='utf-8')
    meta = load_store(None, str(path)).meta('k')
    assert meta['lastPage'] is None and meta['pages'] == []


def test_set_meta_writes_even_when_no_candidate_changes(tmp_path):
    sp = tmp_path / 'o.json'
    store = load_store(None, str(sp))
    store.set_meta('k', role='link', destructive=True)
    saved = json.loads(sp.read_text(encoding='utf-8'))
    assert saved == {'schema': 2, 'selectors': {'k': {
        'expectedRole': 'link', 'destructive': True, 'candidates': []}}}
    again = load_store(None, str(sp))
    assert again.meta('k')['destructive'] is True
    assert again.destructive_choice('k') is True


def test_set_meta_with_unchanged_values_does_not_rewrite(tmp_path):
    sp = tmp_path / 'o.json'
    store = load_store(None, str(sp))
    store.set_meta('k', role='link')
    sp.unlink()
    store.set_meta('k', role='link')
    assert not sp.exists()


def test_low_level_store_without_set_meta_writes_candidates_only(tmp_path):
    data = {'schema': 2, 'selectors': {'k': {'expectedRole': 'button', 'candidates': []}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    store.add_candidate('k', '//healed', source='heal')
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert set(saved['selectors']['k']) == {'candidates'}


def test_a_known_seed_role_wins_over_a_saved_role(tmp_path):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'checkbox', 'candidates': [{'xpath': '//a', 'source': 'seed'}]}}}))
    override = tmp_path / 'o.json'
    override.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'candidates': []}}}))
    assert load_store(str(seed), str(override)).meta('k')['expectedRole'] == 'checkbox'


def test_a_saved_role_fills_an_unknown_seed_role(tmp_path):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'selectors': [{'key': 'k', 'xpath': "//*[@id='x']"}]}))
    override = tmp_path / 'o.json'
    override.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'candidates': []}}}))
    assert load_store(str(seed), str(override)).meta('k')['expectedRole'] == 'button'


def test_malformed_saved_metadata_is_ignored(tmp_path):
    override = tmp_path / 'o.json'
    override.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'destructive': 'yes', 'expectedText': 'Pay', 'candidates': [
            {'xpath': '//a', 'source': 'code', 'status': 'active'}]}}}))
    store = load_store(None, str(override))
    assert store.destructive_choice('k') is None
    assert store.candidates('k')[0]['xpath'] == '//a'


def test_destructive_keys_apply_to_keys_only_in_the_override(tmp_path):
    override = tmp_path / 'o.json'
    override.write_text(json.dumps({'schema': 2, 'selectors': {'pay': {
        'candidates': [{'xpath': '//button', 'source': 'heal', 'status': 'active'}]}}}))
    store = load_store(None, str(override), frozenset({'pay'}))
    assert store.meta('pay')['destructive'] is True
    store.set_meta('pay', destructive=False)
    assert store.meta('pay')['destructive'] is True
    assert store.is_locked_destructive('pay')


def test_a_seed_rows_first_success_does_not_pin_it_active(tmp_path):
    # The seed is the source of truth for its own rows: a quarantine published to the seed
    # later must still apply, so a first success alone does not copy the row to the file.
    seed = tmp_path / 'seed.json'
    row = {'xpath': '//a', 'source': 'seed', 'addedAt': None, 'lastSucceededAt': None,
           'status': 'active'}
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'candidates': [row]}}}), encoding='utf-8')
    store = load_store(str(seed), str(tmp_path / 'o.json'))
    store.record_success('k', '//a')
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'button', 'candidates': [dict(row, status='quarantined')]}}}),
        encoding='utf-8')
    assert load_store(str(seed), str(tmp_path / 'o.json')).status('k', '//a') == 'quarantined'


def test_the_first_success_of_a_key_with_role_given_is_saved(tmp_path, make_page,
                                                             stub_provider):
    # With role= given nothing else is learned, so only the success itself can be written.
    sp = str(tmp_path / 'o.json')
    healer, _ = _run(make_page, stub_provider, sp, V1)
    healer.locator('go', OLD, role='button', timeout_ms=0, poll_ms=0)
    saved = json.loads(open(sp, encoding='utf-8').read())['selectors']['go']['candidates']
    assert saved[0]['xpath'] == OLD and saved[0]['lastSucceededAt']
