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
