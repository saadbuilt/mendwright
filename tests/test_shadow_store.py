"""What the store and the resolver do with a selector saved inside an open shadow root (see
mendwright.shadow): the candidate's and the key's shadowHosts, merged between processes, and
quarantines listed per place; a candidate that does not add up, or a driver that does not
reach into shadow roots, never runs one."""

import json

import pytest

from mendwright import shadow
from mendwright.resolver import _split_runnable, _target
from mendwright.selectors import InvalidSelector
from mendwright.shadow import ShadowHost
from mendwright.store import load_store

HOST = 'my-app[id="root"]'


# --- the store ------------------------------------------------------------------------------------

def test_a_candidate_keeps_its_shadow_hosts_and_moves_out_of_a_shadow_root(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.add_candidate('k', HOST + ' >> #a', source='heal', shadow_hosts=[HOST])
    row = json.loads(path.read_text())['selectors']['k']['candidates'][0]
    assert row['shadowHosts'] == [HOST]
    again = load_store(None, str(path))
    assert again.candidates('k')[0]['shadowHosts'] == [HOST]
    again.add_candidate('k', HOST + ' >> #a', source='heal', shadow_hosts=[])
    row = json.loads(path.read_text())['selectors']['k']['candidates'][0]
    assert 'shadowHosts' not in row
    assert shadow.hosts_of(again.candidates('k')[0]) == ()


def test_a_shadow_root_a_process_moved_a_row_to_is_merged_into_another_store(tmp_path):
    path = tmp_path / 'o.json'
    first = load_store(None, str(path))
    first.add_candidate('k', '#a', source='code')
    second = load_store(None, str(path))
    first.add_candidate('k', HOST + ' >> #a', source='heal', shadow_hosts=[HOST])
    second.set_meta('k', role='button')           # merges what first wrote, then writes
    row = [c for c in json.loads(path.read_text())['selectors']['k']['candidates']
           if c['source'] == 'heal'][0]
    assert row['shadowHosts'] == [HOST]
    assert shadow.hosts_of(second._find('k', HOST + ' >> #a')) == (HOST,)


def test_where_a_key_was_found_is_saved_and_cleared(tmp_path):
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.set_meta('k', role='button', shadow_hosts=[HOST, None])
    assert store.shadow_hosts('k') == [HOST, None]
    assert json.loads(path.read_text())['selectors']['k']['shadowHosts'] == [HOST, None]
    assert load_store(None, str(path)).shadow_hosts('k') == [HOST, None]
    store.set_meta('k', shadow_hosts=[])
    assert store.shadow_hosts('k') == [] and 'shadowHosts' not in json.loads(path.read_text())[
        'selectors']['k']
    with pytest.raises(ValueError):
        store.set_meta('k', shadow_hosts='my-app')


def test_where_a_key_was_found_is_cleared_in_another_store_too(tmp_path):
    path = tmp_path / 'o.json'
    first = load_store(None, str(path))
    first.set_meta('k', role='button', shadow_hosts=[HOST])
    second = load_store(None, str(path))
    first.set_meta('k', shadow_hosts=[])
    second.set_meta('k', text='Save')             # merges first's write
    assert second.shadow_hosts('k') == []
    assert 'shadowHosts' not in json.loads(path.read_text())['selectors']['k']


def test_a_malformed_saved_place_keeps_a_host_nothing_tells_apart(tmp_path):
    path = tmp_path / 'o.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'shadowHosts': 'my-app', 'candidates': [{'xpath': '#a', 'source': 'code'}]}}}))
    assert load_store(None, str(path)).shadow_hosts('k') == [None]


def test_quarantines_are_listed_per_place(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('k', '//page', source='heal')
    store.add_candidate('k', HOST + ' >> #in', source='heal', shadow_hosts=[HOST])
    store.add_candidate('k', 'xpath=//f', source='heal', frame_xpath='iframe')
    for xpath in ('//page', HOST + ' >> #in', 'xpath=//f'):
        store.quarantine('k', xpath)
    assert store.quarantined('k', frame_xpath='') == ['//page']
    assert store.quarantined('k', frame_xpath='', shadow_hosts=[HOST]) == [HOST + ' >> #in']
    assert store.quarantined('k', frame_xpath='iframe') == ['xpath=//f']
    assert len(store.quarantined('k')) == 3


def test_code_that_takes_over_a_shadow_heal_runs_it_as_written(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('k', HOST + ' >> #a', source='heal', shadow_hosts=[HOST])
    store.set_code_candidates('k', [HOST + ' >> #a'])     # mendwright fix wrote it
    row = store.candidates('k')[0]
    assert row['source'] == 'code' and shadow.hosts_of(row) == ()


# --- the resolver ---------------------------------------------------------------------------------

@pytest.mark.parametrize('row', [
    {'xpath': '#save', 'shadowHosts': [HOST]},                       # not the chain
    {'xpath': 'other-app >> #save', 'shadowHosts': [HOST]},         # another host
    {'xpath': HOST + " >> //button", 'shadowHosts': [HOST]},        # not CSS
    {'xpath': HOST + ' >> #save', 'shadowHosts': [None]},           # a host not saved
    {'xpath': "xpath=//a >> #save", 'shadowHosts': ['xpath=//a']},  # a host not CSS
], ids=['not-a-chain', 'other-host', 'xpath-inside', 'host-none', 'host-xpath'])
def test_a_shadow_row_that_does_not_add_up_is_never_run(row):
    runnable, errors = _split_runnable('k', [row])
    assert runnable == [] and len(errors) == 1


class NoShadowDriver:
    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def handle(self, selector, frame_chain=()):
        return object()


def test_a_shadow_heal_cannot_run_on_a_driver_that_does_not_reach_into_shadow_roots():
    row = {'xpath': HOST + ' >> #save', 'shadowHosts': [HOST]}
    runnable, errors = _split_runnable('k', [row], NoShadowDriver())
    assert runnable == [] and 'does not reach into shadow roots' in str(errors[0])
    assert _split_runnable('k', [row])[0] == [row]          # no driver: syntax only
    assert _target(row) == ('#save', (ShadowHost(HOST),))


def test_a_shadow_heal_on_a_custom_driver_is_never_run_on_the_page(tmp_path):
    from mendwright.resolver import resolve
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('k', HOST + ' >> #save', source='heal', shadow_hosts=[HOST])
    with pytest.raises(InvalidSelector, match='does not reach into shadow roots'):
        resolve(store, NoShadowDriver(), 'k', timeout_ms=0)
