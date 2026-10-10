"""The selector in your code wins: an edited inline selector is used at once, the old
one is retired, heals stay as fallbacks, and a list gives ordered zero-AI fallbacks."""

import json
import os

import pytest

import mendwright
from mendwright import InvalidSelector, load_store

PAGE = "<form><button id='a'>A</button><button id='b'>B</button><button id='c'>C</button></form>"
A, B, C = "//button[@id='a']", "//button[@id='b']", "//button[@id='c']"


def _open(make_page, stub_provider, sp, html=PAGE, proposal=None):
    page = make_page(html)
    provider = stub_provider(proposal)
    return mendwright.open(page, provider=provider, store_path=sp), page, provider


def _saved(sp):
    data = json.loads(open(sp, encoding='utf-8').read())
    return {c['xpath']: c for c in data['selectors']['go']['candidates']}


def _id(proxy):
    return proxy.get_attribute('id')


def test_an_edited_selector_is_used_and_the_old_one_retired(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _, _ = _open(make_page, stub_provider, sp)
    assert _id(first.locator('go', A, role='button', timeout_ms=0, poll_ms=0)) == 'a'

    second, _, provider = _open(make_page, stub_provider, sp)      # the next run, code edited
    assert _id(second.locator('go', B, role='button', timeout_ms=0, poll_ms=0)) == 'b'
    assert provider.bundles == []
    saved = _saved(sp)
    assert saved[A]['status'] == 'superseded'
    assert saved[B]['status'] == 'active' and saved[B]['source'] == 'code'
    assert [c['xpath'] for c in second.store.candidates('go')] == [B]


def test_heals_stay_as_fallbacks_behind_the_code(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    healed = "//button[text()='C']"
    no_a = PAGE.replace("id='a'", "id='a2'")
    first, _, provider = _open(make_page, stub_provider, sp, html=no_a, proposal=healed)
    assert _id(first.locator('go', A, role='button', timeout_ms=0, poll_ms=0)) == 'c'
    assert len(provider.bundles) == 1

    # Code unchanged and still broken: the heal is used, no model call.
    second, page, provider = _open(make_page, stub_provider, sp, html=no_a)
    assert _id(second.locator('go', A, role='button', timeout_ms=0, poll_ms=0)) == 'c'
    assert provider.bundles == []
    assert page.queries[0] == 'xpath=' + A          # the code selector is still tried first

    # The developer fixes the code: it wins over the heal, which stays as a fallback.
    third, _, _ = _open(make_page, stub_provider, sp, html=no_a)
    assert _id(third.locator('go', B, role='button', timeout_ms=0, poll_ms=0)) == 'b'
    assert [c['xpath'] for c in third.store.candidates('go')] == [B, healed]
    assert _saved(sp)[A]['status'] == 'superseded'


def test_a_new_code_selector_beats_an_older_heal_that_still_matches(tmp_path, make_page,
                                                                    stub_provider):
    sp = str(tmp_path / 'o.json')
    store = load_store(None, sp)
    store.set_meta('go', role='button')
    store.add_candidate('go', C, source='heal')
    store.record_success('go', C, now='2026-10-01T00:00:00+00:00')   # a recent, working heal
    healer, _, _ = _open(make_page, stub_provider, sp)
    assert _id(healer.locator('go', A, timeout_ms=0, poll_ms=0)) == 'a'


def test_a_list_gives_ordered_fallbacks_without_a_model(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    healer, _, provider = _open(make_page, stub_provider, sp)
    proxy = healer.locator('go', ["//button[@id='gone']", B, C], role='button',
                           timeout_ms=0, poll_ms=0)
    assert _id(proxy) == 'b'
    assert provider.bundles == []
    assert [c['xpath'] for c in healer.store.candidates('go')][:3] == ["//button[@id='gone']", B, C]
    assert {x: c['source'] for x, c in _saved(sp).items()} == {
        "//button[@id='gone']": 'code', B: 'code', C: 'code'}


def test_going_back_to_an_old_selector_makes_it_active_again(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    healer, _, _ = _open(make_page, stub_provider, sp)
    healer.locator('go', A, role='button', timeout_ms=0, poll_ms=0)
    healer.locator('go', B, role='button', timeout_ms=0, poll_ms=0)
    assert _id(healer.locator('go', A, role='button', timeout_ms=0, poll_ms=0)) == 'a'
    saved = _saved(sp)
    assert saved[A]['status'] == 'active'
    assert saved[B]['status'] == 'superseded'


def test_a_healed_selector_copied_into_code_becomes_a_code_candidate(tmp_path, make_page,
                                                                     stub_provider):
    sp = str(tmp_path / 'o.json')
    store = load_store(None, sp)
    store.add_candidate('go', C, source='heal')
    healer, _, _ = _open(make_page, stub_provider, sp)
    healer.locator('go', C, role='button', timeout_ms=0, poll_ms=0)
    assert _saved(sp)[C]['source'] == 'code'


def test_prefixes_are_normalized_before_storing(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    healer, _, _ = _open(make_page, stub_provider, sp)
    healer.locator('go', 'xpath=' + B, role='button', timeout_ms=0, poll_ms=0)
    healer.locator('go', '  ' + B + ' ', role='button', timeout_ms=0, poll_ms=0)
    assert list(_saved(sp)) == [B]


def test_css_selectors_work_inline(tmp_path, make_page, stub_provider):
    healer, _, _ = _open(make_page, stub_provider, str(tmp_path / 'o.json'))
    assert _id(healer.locator('go', 'css=#c', timeout_ms=0, poll_ms=0)) == 'c'
    assert healer.store.candidates('go')[0]['xpath'] == '#c'


def test_bad_selectors_are_rejected_before_the_page_is_touched(tmp_path, make_page,
                                                               stub_provider):
    healer, page, _ = _open(make_page, stub_provider, str(tmp_path / 'o.json'))
    with pytest.raises(InvalidSelector, match="the selector for 'go' is not usable"):
        healer.locator('go', "//button[", timeout_ms=0, poll_ms=0)
    with pytest.raises(TypeError, match='Locator objects'):
        healer.locator('go', object(), timeout_ms=0, poll_ms=0)
    with pytest.raises(ValueError, match='empty list'):
        healer.locator('go', [], timeout_ms=0, poll_ms=0)
    assert page.queries == []
    assert healer.store.candidates('go') == []


def test_without_a_selector_the_stored_order_is_used(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first, _, _ = _open(make_page, stub_provider, sp)
    first.locator('go', [A, B], role='button', timeout_ms=0, poll_ms=0)
    second, _, _ = _open(make_page, stub_provider, sp)
    assert _id(second.locator('go', timeout_ms=0, poll_ms=0)) in ('a', 'b')


def test_a_heal_behind_a_broken_code_selector_does_not_rewrite_the_file(tmp_path, make_page,
                                                                         stub_provider):
    sp = str(tmp_path / 'o.json')
    no_a = PAGE.replace("id='a'", "id='a2'")
    healer, _, _ = _open(make_page, stub_provider, sp, html=no_a, proposal=C)
    healer.locator('go', A, role='button', timeout_ms=0, poll_ms=0)        # heals to C
    healer.locator('go', A, role='button', timeout_ms=0, poll_ms=0)        # C becomes newest
    os.remove(sp)
    for _ in range(3):
        assert _id(healer.locator('go', A, role='button', timeout_ms=0, poll_ms=0)) == 'c'
    assert not os.path.exists(sp)          # nothing changed, so nothing was written


def test_the_old_inline_xpath_keyword_still_works(tmp_path, make_page, stub_provider):
    healer, _, _ = _open(make_page, stub_provider, str(tmp_path / 'o.json'))
    assert _id(healer.locator('go', inline_xpath=B, role='button', timeout_ms=0, poll_ms=0)) == 'b'
    with pytest.raises(TypeError, match='both selector and inline_xpath'):
        healer.locator('go', A, inline_xpath=B)
