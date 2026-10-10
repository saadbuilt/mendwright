"""Hooks a test runner (such as a pytest plugin) uses: the live Healer registry,
Healer.pending(), Healer.confirm_all() and Healer.heals."""

import gc

import mendwright
from mendwright import HealEvent
from mendwright.facade import live_healers

V2 = "<form><input type='checkbox' id='new'><button id='go2'>Go</button></form>"


def _healer(tmp_path, make_page, stub_provider, proposal="//input[@type='checkbox']"):
    provider = stub_provider(proposal)
    healer = mendwright.open(make_page(V2), provider=provider,
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    return healer, provider


def test_open_registers_the_healer_while_it_is_alive(tmp_path, make_page, stub_provider):
    healer, _ = _healer(tmp_path, make_page, stub_provider)
    assert healer in live_healers()
    ident = id(healer)
    del healer
    gc.collect()
    assert ident not in [id(h) for h in live_healers()]


def test_pending_lists_unconfirmed_heals_as_a_copy(tmp_path, make_page, stub_provider):
    healer, _ = _healer(tmp_path, make_page, stub_provider)
    assert healer.pending() == {}
    healer.locator('terms', "//input[@id='old']", role='checkbox')
    pending = healer.pending()
    assert pending == {'terms': "//input[@type='checkbox']"}
    pending.clear()
    assert healer.pending() == {'terms': "//input[@type='checkbox']"}


def test_heals_record_old_new_and_the_canary_result(tmp_path, make_page, stub_provider):
    healer, _ = _healer(tmp_path, make_page, stub_provider)
    healer.locator('terms', "//input[@id='old']", role='checkbox')
    assert healer.heals == [HealEvent('terms', ("//input[@id='old']",),
                                      "//input[@type='checkbox']", None)]
    assert healer.confirm_all(True) == 1
    assert healer.heals[0].confirmed is True
    assert healer.pending() == {}
    assert healer.confirm_all(True) == 0


def test_confirm_all_false_quarantines_every_pending_heal(tmp_path, make_page, stub_provider):
    healer, provider = _healer(tmp_path, make_page, stub_provider)
    healer.locator('terms', "//input[@id='old']", role='checkbox')
    healer.coordinator.provider = stub_provider("//button[@id='go2']")
    healer.coordinator.provider.outcomes = provider.outcomes
    healer.locator('go', "//button[@id='go']", role='button')
    assert set(healer.pending()) == {'terms', 'go'}
    assert healer.confirm_all(False) == 2
    assert sorted(provider.outcomes) == [('go', "//button[@id='go2']", False),
                                         ('terms', "//input[@type='checkbox']", False)]
    assert [h.confirmed for h in healer.heals] == [False, False]
    assert healer.store.candidates('terms')[0]['xpath'] == "//input[@id='old']"


def test_an_action_confirmation_shows_up_in_heals(tmp_path, make_page, stub_provider):
    healer, _ = _healer(tmp_path, make_page, stub_provider)
    healer.locator('terms', "//input[@id='old']", role='checkbox').check(expect=lambda: True)
    assert healer.heals[0].confirmed is True
