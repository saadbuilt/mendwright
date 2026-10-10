"""Only keys you mark are destructive: heal.locator(..., destructive=True),
open(destructive_keys=...) and "destructive": true in a seed file. mendwright does not
guess it from the key, from text= or from the label of the element.

These tests cover the sync facade: words alone no longer block a heal, and a heal saved
earlier is never acted on once its key is marked. That the flags survive into the next
process is in test_store_meta.py, and the async facade is in test_aio.py."""

import json

import pytest

import mendwright
from mendwright import AttentionRequired, load_store

BROKEN = "//button[@id='gone']"
PAY = "//button[@id='p']"


@pytest.mark.parametrize('key, kw, button', [
    ('pay', {}, "<button id='p'>Go</button>"),
    ('placeOrder_btn', {}, "<button id='p'>Go</button>"),
    ('next_step', {'text': 'Pay now'}, "<button id='p'>Pay now</button>"),
    ('next_step', {}, "<button id='p' aria-label='Delete account'>&#215;</button>"),
    ('next_step', {}, "<input type='submit' id='p' value='Complete order'>"),
])
def test_words_in_the_key_text_or_label_do_not_block_a_heal(key, kw, button, tmp_path,
                                                             make_page, stub_provider):
    xpath = PAY if button.startswith('<button') else "//input[@id='p']"
    page = make_page('<form>' + button + '</form>')
    provider = stub_provider(xpath)
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    heal.locator(key, BROKEN, role='button', timeout_ms=0, poll_ms=0, **kw).click()
    assert len(provider.bundles) == 1
    assert page.actions[-1][:2] == ('xpath=' + xpath, 'click')
    assert heal.store.meta(key)['destructive'] is False


def test_a_matched_label_is_not_saved_as_expected_text(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    page = make_page("<form><button id='p'>Pay now</button></form>")
    heal = mendwright.open(page, provider=stub_provider(None), store_path=sp)
    heal.locator('next_step', PAY, timeout_ms=0, poll_ms=0)
    assert heal.store.meta('next_step')['expectedText'] == []
    saved = json.loads(open(sp, encoding='utf-8').read())['selectors']['next_step']
    assert 'expectedText' not in saved and 'destructive' not in saved


# --- a heal saved earlier, on a key that is marked now ------------------------------------

def _saved_heal(override, seed=None):
    """An override file holding a heal for 'next_step', and optionally a seed file that
    marks the key destructive."""
    override.write_text(json.dumps({'schema': 2, 'selectors': {'next_step': {
        'expectedRole': 'button', 'candidates': [
            {'xpath': '#continue', 'source': 'code', 'status': 'active'},
            {'xpath': 'xpath=' + PAY, 'source': 'heal', 'status': 'active',
             'lastSucceededAt': '2026-10-01T00:00:00+00:00'}]}}}), encoding='utf-8')
    if seed is not None:
        seed.write_text(json.dumps({'schema': 2, 'selectors': {'next_step': {
            'expectedRole': 'button', 'destructive': True, 'candidates': [
                {'xpath': '#continue', 'source': 'seed', 'status': 'active'}]}}}),
            encoding='utf-8')


@pytest.mark.parametrize('how', ['destructive_arg', 'destructive_keys', 'seed'])
def test_a_saved_heal_is_quarantined_once_the_key_is_marked(how, tmp_path, make_page,
                                                            stub_provider):
    override, seed = tmp_path / 'o.json', tmp_path / 'seed.json'
    _saved_heal(override, seed if how == 'seed' else None)
    page = make_page("<form><button id='p'>Continue</button></form>")
    provider = stub_provider(PAY)
    if how == 'seed':
        heal = mendwright.open(page, provider=provider, store=load_store(str(seed), str(override)))
    else:
        heal = mendwright.open(page, provider=provider, store_path=str(override),
                               destructive_keys={'next_step'} if how == 'destructive_keys'
                               else frozenset())

    with pytest.raises(AttentionRequired) as info:
        heal.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0,
                     destructive=True if how == 'destructive_arg' else None).click()

    message = str(info.value)
    assert "'next_step' is marked as a destructive control" in message
    assert 'quarantined that selector' in message
    if how == 'destructive_arg':
        assert 'pass destructive=False' in message     # a flag the facade saved can be cleared
    else:
        assert 'Fix the selector in your code or seed file.' in message
    assert page.actions == [] and provider.bundles == []
    saved = json.loads(override.read_text(encoding='utf-8'))['selectors']['next_step']
    assert {c['xpath']: c['status'] for c in saved['candidates']}['xpath=' + PAY] == 'quarantined'
    events = [json.loads(line) for line in
              (tmp_path / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
    assert events[-1]['event'] == 'heal.quarantine'


def test_a_saved_heal_on_a_pay_button_is_used_when_the_key_is_not_marked(tmp_path, make_page,
                                                                          stub_provider):
    _saved_heal(tmp_path / 'o.json')
    page = make_page("<form><button id='p'>Pay now</button></form>")
    heal = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    heal.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0).click()
    assert page.actions[-1][:2] == ('xpath=' + PAY, 'click')


def test_marking_a_key_after_its_heal_in_the_same_run_fails_the_heal(tmp_path, make_page,
                                                                     stub_provider):
    page = make_page("<form><button id='p'>Continue</button></form>")
    provider = stub_provider(PAY)
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    heal.locator('next_step', BROKEN, role='button', timeout_ms=0, poll_ms=0)
    assert heal.pending() == {'next_step': PAY}

    with pytest.raises(AttentionRequired, match="'next_step' is marked as a destructive control"):
        heal.locator('next_step', BROKEN, role='button', destructive=True, timeout_ms=0,
                     poll_ms=0)
    assert heal.pending() == {}
    assert provider.outcomes == [('next_step', PAY, False)]
    assert heal.store.status('next_step', PAY) == 'quarantined'
    assert len(provider.bundles) == 1                    # only the first, unmarked call
