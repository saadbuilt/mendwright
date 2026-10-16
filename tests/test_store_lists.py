"""The store side of the list heals heal.locator_all(..., nonempty=True) proposes: proposed
and accepted rows are inactive, keep their evidence across writes and processes, have a cap
of their own, retire when the selector they replaced matches again, and are accepted or
rejected only by edit_list_heal() (mendwright lists), which changes nothing else in the
file."""

import json

import pytest

from mendwright import load_store
from mendwright.store import (MAX_LIST_HEALS, ListHealError, SelectorStore, _merge_status,
                              edit_list_heal)

CODE = '.cart-item'
ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]"
SAVED = "//div[contains(concat(' ', normalize-space(@class), ' '), ' saved-item ')]"


def evidence(id_='3f2a9c1d', at='2026-11-02T10:14:03+00:00', selectors=(CODE,)):
    """A proposal as lists.proposal_evidence() writes it, cut to what the store reads."""
    return {'id': id_, 'at': at, 'by': 'fingerprint', 'page': '/cart', 'count': 3, 'found': 3,
            'recorded': 3, 'score': 0.86, 'next': 0.31,
            'was': {'selectors': list(selectors), 'count': 3, 'label': 'Shopping cart',
                    'container': '<section class="cart">', 'items': ['Blue mug']},
            'now': {'label': 'Shopping cart', 'container': '<section class="cart">',
                    'alike': [1, 1], 'items': ['Blue mug']},
            'differs': []}


def _store(tmp_path, code=(CODE,)):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('cart', list(code))
    return store


def _file(tmp_path):
    return json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))


def _rows(tmp_path, key='cart'):
    return {c['xpath']: c for c in _file(tmp_path)['selectors'][key]['candidates']}


# --- proposing ------------------------------------------------------------------------------

def test_a_proposed_list_heal_is_saved_inactive_with_its_evidence(tmp_path):
    store = _store(tmp_path)
    row = store.propose_list_heal('cart', ROWS, css='div.cart-row', proposal=evidence(),
                                  now='2026-11-02T10:14:03+00:00')
    assert row == {'xpath': ROWS, 'source': 'heal', 'addedAt': '2026-11-02T10:14:03+00:00',
                   'lastSucceededAt': None, 'status': 'proposed', 'list': True,
                   'css': 'div.cart-row', 'proposal': evidence()}
    assert [c['xpath'] for c in store.candidates('cart')] == [CODE]     # never tried
    assert _rows(tmp_path)[ROWS] == row
    again = load_store(None, str(tmp_path / 'o.json'))
    assert [r['xpath'] for r in again.list_heals('cart')] == [ROWS]
    assert again.status('cart', ROWS) == 'proposed'
    assert [c['xpath'] for c in again.candidates('cart')] == [CODE]


def test_the_same_xpath_found_again_refreshes_its_row(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    store.propose_list_heal('cart', ROWS, css='div.cart-row',
                            proposal=evidence('9b8c7d6e', '2026-11-03T00:00:00+00:00'))
    rows = store.list_heals('cart')
    assert len(rows) == 1 and rows[0]['proposal']['id'] == '9b8c7d6e'
    assert rows[0]['css'] == 'div.cart-row'
    store.retire_list_heals('cart', CODE)                               # superseded ...
    store.propose_list_heal('cart', ROWS, proposal=evidence())         # ... and proposed again
    assert store.status('cart', ROWS) == 'proposed'
    for status in ('accepted', 'quarantined'):
        store._find('cart', ROWS)['status'] = status
        with pytest.raises(ValueError):
            store.propose_list_heal('cart', ROWS, proposal=evidence())
    with pytest.raises(ValueError):
        store.propose_list_heal('cart', CODE, proposal=evidence())     # the code's own


def test_a_list_heal_of_a_build_of_0_8_0_before_its_release_becomes_a_proposal(tmp_path):
    store = _store(tmp_path)
    store.add_candidate('cart', SAVED, list_heal=True)
    store.propose_list_heal('cart', SAVED, proposal=evidence())
    assert store.status('cart', SAVED) == 'proposed'
    assert store.list_heals('cart')[0]['proposal']['id'] == '3f2a9c1d'


def test_list_heals_are_newest_first_and_capped_apart_from_other_rows(tmp_path):
    store = _store(tmp_path)
    for i in range(6):                                  # code selectors that changed
        store.set_code_candidates('cart', ['.old-' + str(i)])
    store.set_code_candidates('cart', [CODE])
    for i in range(MAX_LIST_HEALS + 1):
        store.propose_list_heal('cart', '//li[@class="l' + str(i) + '"]',
                                proposal=evidence('0000000' + str(i),
                                                  '2026-11-0' + str(i + 1) + 'T00:00:00+00:00'))
    rows = _rows(tmp_path)
    kept = [x for x, c in rows.items() if c['status'] == 'proposed']
    assert len(kept) == MAX_LIST_HEALS == 3 and '//li[@class="l0"]' not in kept
    assert len([c for c in rows.values() if c['status'] == 'superseded']) == 5
    order = [r['proposal']['id'] for r in load_store(None, str(tmp_path / 'o.json')).list_heals('cart')]
    assert order == ['00000003', '00000002', '00000001']


# --- several processes on one file -------------------------------------------------------------

def test_two_processes_keep_both_proposals_and_the_newer_evidence(tmp_path):
    one, two = _store(tmp_path), _store(tmp_path)
    one.propose_list_heal('cart', ROWS, proposal=evidence('aaaaaaaa', '2026-11-02T10:00:00+00:00'))
    two.propose_list_heal('cart', ROWS, proposal=evidence('bbbbbbbb', '2026-11-02T11:00:00+00:00'))
    two.propose_list_heal('cart', SAVED, proposal=evidence('cccccccc', '2026-11-02T09:00:00+00:00'))
    one.propose_list_heal('cart', ROWS, proposal=evidence('aaaaaaaa', '2026-11-02T10:00:00+00:00'))
    rows = _rows(tmp_path)
    assert rows[ROWS]['proposal']['id'] == 'bbbbbbbb'                  # the later 'at'
    assert rows[SAVED]['proposal']['id'] == 'cccccccc'


def test_an_acceptance_wins_over_a_stale_status():
    assert _merge_status('superseded', 'accepted', 'proposed') == 'accepted'
    assert _merge_status('accepted', 'proposed', 'superseded') == 'accepted'
    assert _merge_status('accepted', 'quarantined', 'proposed') == 'quarantined'
    assert _merge_status('proposed', 'accepted', 'proposed') == 'accepted'   # only the file


def test_a_process_that_loaded_the_proposal_keeps_its_acceptance(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    running = load_store(None, str(tmp_path / 'o.json'))                # a test run
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '3f2a9c1d', now='2026-11-02T10:20:00+00:00')
    running.set_meta('cart', role='div')                                # it writes later
    row = _rows(tmp_path)[ROWS]
    assert row['status'] == 'accepted' and row['acceptedAt'] == '2026-11-02T10:20:00+00:00'
    assert row['proposal']['id'] == '3f2a9c1d'


def test_a_write_by_mendwright_0_8_0_drops_the_evidence_and_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    store.propose_list_heal('cart', SAVED, proposal=evidence('9b8c7d6e', selectors=('.other',)))
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '9b8c7d6e')
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    # 0.8.0's _payload keeps xpath, source, addedAt, lastSucceededAt, status, frameXPath, css
    # and list of a row.
    data = _file(tmp_path)
    for c in data['selectors']['cart']['candidates']:
        for name in ('proposal', 'acceptedAt'):
            c.pop(name, None)
    (tmp_path / 'o.json').write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ListHealError, match='has no saved evidence, because an older mendwright'):
        edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '3f2a9c1d')
    after = load_store(None, str(tmp_path / 'o.json'))
    assert after.status('cart', SAVED) == 'accepted'                    # fix still writes it
    after.set_code_candidates('cart', [CODE])
    assert after.retire_list_heals('cart', '.unrelated') == []
    assert after.retire_list_heals('cart', CODE) == [(ROWS, 'proposed', 'superseded'),
                                                      (SAVED, 'accepted', 'quarantined')]


def test_taking_an_accepted_list_heal_over_as_code_makes_it_a_plain_code_row(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '3f2a9c1d')
    run = load_store(None, str(tmp_path / 'o.json'))                    # after mendwright fix
    run.set_code_candidates('cart', [ROWS])
    row = _rows(tmp_path)[ROWS]
    assert row['source'] == 'code' and row['status'] == 'active'
    assert not {'list', 'proposal', 'acceptedAt'} & set(row)
    run.record_success('cart', ROWS)                                    # a later write
    assert not {'list', 'proposal', 'acceptedAt'} & set(_rows(tmp_path)[ROWS])


# --- retiring ---------------------------------------------------------------------------------

def test_the_selector_a_list_heal_replaced_retires_it(tmp_path):
    store = _store(tmp_path, code=(CODE, '.basket-item'))
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    store.propose_list_heal('cart', SAVED, proposal=evidence('9b8c7d6e'))
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '9b8c7d6e')
    store = load_store(None, str(tmp_path / 'o.json'))
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    store.add_candidate('cart', '//li', list_heal=True)                 # 0.8.0's own: left alone
    assert store.retire_list_heals('cart', '.basket-item') == []        # another page's selector
    assert store.retire_list_heals('cart', CODE) == [(ROWS, 'proposed', 'superseded'),
                                                      (SAVED, 'accepted', 'quarantined')]
    assert store.status('cart', '//li') == 'active'
    assert store.list_heals('cart') == [] and store.retire_list_heals('cart', CODE) == []
    assert store.quarantined('cart') == [SAVED]


# --- mendwright lists accept / reject -----------------------------------------------------------

def test_accept_changes_only_its_rows_and_keeps_a_pinned_seed_quarantine(tmp_path):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'pay': {'candidates': [
        {'xpath': '#pay', 'source': 'seed', 'status': 'active'}]}}}), encoding='utf-8')
    store = load_store(str(seed), str(tmp_path / 'o.json'))
    store.quarantine('pay', '#pay')                                     # pinned in the file
    store.set_code_candidates('cart', [CODE])
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    store.propose_list_heal('cart', SAVED, proposal=evidence('9b8c7d6e'))
    before = (tmp_path / 'o.json').read_text(encoding='utf-8')
    done = edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '3f2a9c1d',
                          now='2026-11-02T10:20:00+00:00')
    assert (done.key, done.action, done.xpath, done.id, done.superseded) == (
        'cart', 'accept', ROWS, '3f2a9c1d', [SAVED])
    after = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    old = json.loads(before)
    assert after['selectors']['pay'] == old['selectors']['pay']          # the seed quarantine
    rows = {c['xpath']: c for c in after['selectors']['cart']['candidates']}
    assert rows[ROWS]['status'] == 'accepted' and rows[SAVED]['status'] == 'superseded'
    old_rows = {c['xpath']: c for c in old['selectors']['cart']['candidates']}
    assert rows[CODE] == old_rows[CODE]
    events = [json.loads(line) for line in (tmp_path / 'journal.jsonl').read_text().splitlines()]
    assert events[-1]['event'] == 'heal.accepted'
    assert {k: events[-1][k] for k in ('key', 'xpath', 'id', 'list')} == {
        'key': 'cart', 'xpath': ROWS, 'id': '3f2a9c1d', 'list': True}


def test_reject_quarantines_the_one_waiting(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    done = edit_list_heal(tmp_path / 'o.json', 'cart', 'reject')
    assert done.xpath == ROWS and _rows(tmp_path)[ROWS]['status'] == 'quarantined'
    events = [json.loads(line) for line in (tmp_path / 'journal.jsonl').read_text().splitlines()]
    assert events[-1]['event'] == 'heal.quarantine'
    assert events[-1]['reason'] == 'rejected with mendwright lists reject'
    # An accepted one is rejected when nothing is proposed.
    store = load_store(None, str(tmp_path / 'o.json'))
    store.propose_list_heal('cart', SAVED, proposal=evidence('9b8c7d6e'))
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '9b8c7d6e')
    assert edit_list_heal(tmp_path / 'o.json', 'cart', 'reject').xpath == SAVED
    assert _rows(tmp_path)[SAVED]['status'] == 'quarantined'


def test_what_mendwright_lists_accept_and_reject_refuse(tmp_path, monkeypatch):
    path = tmp_path / 'o.json'
    flag = ' --store "' + str(path) + '"'
    store = _store(tmp_path)
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'nope', 'accept', 'x')
    assert str(info.value) == ("no key 'nope' in " + str(path) + '. Run mendwright lists' + flag
                               + ' to see the keys with a list heal to check.')
    with pytest.raises(ListHealError, match="^'cart' has no list heal to check.$"):
        edit_list_heal(path, 'cart', 'accept', 'x')
    store.propose_list_heal('cart', ROWS, proposal=evidence())
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'cart', 'accept', '9b8c7d6e')
    assert str(info.value) == ("'cart' has no list heal with id 9b8c7d6e. It changed since you "
                               'looked: run mendwright lists' + flag + ' to see it again.')
    store.propose_list_heal('cart', SAVED, proposal=evidence('1a2b3c4d'))
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'cart', 'accept', '9b8c7d6e')
    assert 'The ones waiting are 3f2a9c1d and 1a2b3c4d: run mendwright lists' in str(info.value)
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'cart', 'reject')
    assert str(info.value) == ("'cart' has 2 list heals waiting: give the id of the one to "
                               'reject (3f2a9c1d, 1a2b3c4d).')
    edit_list_heal(path, 'cart', 'accept', '1a2b3c4d', now='2026-10-30T08:00:00+00:00')
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'cart', 'accept', '1a2b3c4d')
    assert str(info.value) == ("'cart' has no list heal to check. Its list heal was accepted on "
                               '2026-10-30 08:00:00 UTC and waits for mendwright fix --apply; to '
                               'drop it, run: mendwright lists reject cart' + flag)
    # The lock of a test run that holds the file.
    monkeypatch.setattr(SelectorStore, 'lock_timeout', 0.05)
    lock = tmp_path / 'o.json.lock'
    lock.write_text('12345')
    with pytest.raises(ListHealError) as info:
        edit_list_heal(path, 'cart', 'reject')
    assert str(info.value) == ('could not save the change to ' + str(path) + ': the lock file '
                               + str(path) + '.lock is held by another process. Try again when '
                               'your tests have finished.')
    lock.unlink()
    path.write_text(json.dumps({'selectors': [{'key': 'cart', 'xpath': CODE}]}), encoding='utf-8')
    with pytest.raises(ListHealError, match=r'written by an older mendwright \(schema 1\)'):
        edit_list_heal(path, 'cart', 'reject')


def test_an_accepted_list_heal_whose_css_form_the_code_passes_is_done(tmp_path):
    store = _store(tmp_path)
    store.propose_list_heal('cart', ROWS, css='div.cart-row', proposal=evidence())
    edit_list_heal(tmp_path / 'o.json', 'cart', 'accept', '3f2a9c1d')
    run = load_store(None, str(tmp_path / 'o.json'))       # mendwright fix wrote div.cart-row
    run.set_code_candidates('cart', ['div.cart-row'])
    assert run.list_heals('cart') == [] and _rows(tmp_path)[ROWS]['status'] == 'superseded'
    assert [c['xpath'] for c in run.candidates('cart')] == ['div.cart-row']
