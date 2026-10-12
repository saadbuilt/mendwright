"""locator(intent=..., role=...) with no selector, on every engine: the stub provider
stands in for the model that finds the element, and the real gate checks it on the page."""

import pytest

import mendwright
from mendwright import AttentionRequired
from mw_engines import FAST_MS, HEALED, POLL_MS, SIGNUP_V2, V2_CHECKBOX, saved_entry

pytestmark = pytest.mark.browser

INTENT = 'the terms checkbox in the signup form'


def test_a_model_finds_the_element_from_its_intent(tab, store_file, stub_provider):
    tab.load(SIGNUP_V2)
    provider = stub_provider(HEALED)
    heal = mendwright.open(tab.target, provider=provider, store_path=store_file)
    heal.locator('terms', intent=INTENT, role='checkbox', timeout_ms=FAST_MS,
                 poll_ms=POLL_MS).check(expect=lambda: tab.checked(V2_CHECKBOX))
    assert tab.checked(V2_CHECKBOX) is True
    assert provider.bundles[0]['intent'] == INTENT
    assert heal.heals[0].source == 'intent' and heal.heals[0].confirmed is True
    entry = saved_entry(store_file, 'terms')
    assert entry['intent'] == INTENT and entry['candidates'][0]['xpath'] == HEALED


def test_what_the_model_finds_from_an_intent_must_be_on_the_live_page(tab, store_file,
                                                                      stub_provider):
    tab.load(SIGNUP_V2)
    provider = stub_provider("//input[@type='checkbox' and @name='not-there']")
    heal = mendwright.open(tab.target, provider=provider, store_path=store_file)
    with pytest.raises(AttentionRequired, match='no match'):
        heal.locator('terms', intent=INTENT, role='checkbox', timeout_ms=FAST_MS,
                     poll_ms=POLL_MS)
    assert heal.heals == []
