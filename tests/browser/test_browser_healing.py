"""The heal loop on real browsers, through the public facade with a stub provider.

Each test runs on every engine (see conftest.py): Playwright Chromium, Firefox and WebKit,
patchright, camoufox, and Selenium with Chrome and with Firefox. The stub provider stands
in for the model; everything else (capture, the static and live gates, the store, the
journal, the retry and the canary) is the real code against a real page.
"""

import pytest

import mendwright
from mendwright import AttentionRequired, HealEvent
from mw_engines import (FAST_MS, HEALED, HIDDEN_PAGE, PAY_PAGE, POLL_MS, SIGNUP_V1, SIGNUP_V2,
                        STALE, TWO_BUTTONS, V2_CHECKBOX, country_page, journal_events,
                        neutral_button_page, page, saved_candidates, saved_entry)

pytestmark = pytest.mark.browser


def _open(tab, provider, store_file, **kwargs):
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def test_resolve_and_act(tab, store_file, stub_provider):
    tab.load(SIGNUP_V1)
    provider = stub_provider(HEALED)
    heal = _open(tab, provider, store_file)

    heal.locator('accept_terms', STALE, role='checkbox').check()

    assert tab.checked('#accept-terms') is True
    assert provider.bundles == []
    assert heal.heals == [] and heal.pending() == {}


def test_heal_after_a_redesign(tab, store_file, stub_provider):
    tab.load(SIGNUP_V1)
    provider = stub_provider(HEALED)
    heal = _open(tab, provider, store_file)
    heal.locator('accept_terms', STALE, role='checkbox').check()

    tab.load(SIGNUP_V2)   # the redesign: the checkbox lost its id
    proxy = heal.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                         poll_ms=POLL_MS)
    proxy.check(expect=lambda: tab.checked(V2_CHECKBOX))

    # One model call, the proposal passed both gates, and the action worked.
    assert tab.checked(V2_CHECKBOX) is True
    assert len(provider.bundles) == 1
    assert provider.bundles[0]['expectedRole'] == 'checkbox'
    assert 'legal-consent' in provider.bundles[0]['html']
    assert provider.outcomes == [('accept_terms', HEALED, True)]
    assert heal.heals == [HealEvent('accept_terms', (STALE,), HEALED, True)]
    assert heal.pending() == {}

    # The override file keeps the code selector and adds the heal.
    saved = saved_candidates(store_file, 'accept_terms')
    assert saved[STALE]['source'] == 'code'
    assert saved[HEALED]['source'] == 'heal' and saved[HEALED]['status'] == 'active'

    # The journal next to it records the whole heal.
    events = journal_events(store_file)
    names = [e['event'] for e in events]
    assert names == ['heal.requested', 'heal.proposed', 'heal.applied', 'heal.canary']
    applied = events[2]
    assert applied['key'] == 'accept_terms' and applied['xpath'] == HEALED
    assert applied['old'] == [STALE] and applied['provider'] == 'StubProvider'
    assert isinstance(applied['latency_ms'], int)
    assert events[3]['success'] is True

    # The next run on the same store uses the heal at once, with no model call.
    again = stub_provider(None)
    rerun = _open(tab, again, store_file)
    rerun.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                  poll_ms=POLL_MS).uncheck()
    assert again.bundles == []
    assert tab.checked(V2_CHECKBOX) is False


def test_a_heal_that_fails_its_check_is_quarantined(tab, store_file, stub_provider):
    tab.load(SIGNUP_V2)
    provider = stub_provider(HEALED)
    heal = _open(tab, provider, store_file)
    proxy = heal.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                         poll_ms=POLL_MS)
    proxy.check(expect=lambda: False)            # the step did not do what it should

    assert provider.outcomes == [('accept_terms', HEALED, False)]
    assert heal.heals[0].confirmed is False
    assert saved_candidates(store_file, 'accept_terms')[HEALED]['status'] == 'quarantined'
    assert [e['event'] for e in journal_events(store_file)][-1] == 'heal.quarantine'

    # The next run does not trust it again, even when the model proposes it again.
    again = stub_provider(HEALED)
    rerun = _open(tab, again, store_file)
    with pytest.raises(AttentionRequired) as info:
        rerun.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                      poll_ms=POLL_MS)
    assert 'previously quarantined' in str(info.value)
    assert len(again.bundles) == 2


def test_gate_rejects_a_proposal_that_is_not_unique(tab, store_file, stub_provider):
    tab.load(SIGNUP_V2)
    provider = stub_provider('//input')   # email, password and the checkbox
    heal = _open(tab, provider, store_file)

    with pytest.raises(AttentionRequired) as info:
        heal.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                     poll_ms=POLL_MS)

    assert 'not unique (3)' in str(info.value)
    assert len(provider.bundles) == 2                       # the proposal, then one re-prompt
    assert provider.bundles[1]['rejectReason'] == 'not unique (3)'
    assert '//input' not in saved_candidates(store_file, 'accept_terms')
    assert heal.pending() == {}


def test_gate_rejects_a_hidden_element(tab, store_file, stub_provider):
    tab.load(HIDDEN_PAGE)
    provider = stub_provider("//button[@id='ghost']")   # one match, but display:none
    heal = _open(tab, provider, store_file)

    with pytest.raises(AttentionRequired) as info:
        heal.locator('continue', "//button[@id='continue-old']", role='button',
                     timeout_ms=FAST_MS, poll_ms=POLL_MS)

    assert 'not uniquely present on the live page' in str(info.value)
    assert len(provider.bundles) == 2
    assert "//button[@id='ghost']" not in saved_candidates(store_file, 'continue')


@pytest.mark.parametrize('how', ['key_word', 'destructive_keys', 'destructive_arg'])
def test_a_destructive_control_is_never_healed(how, tab, store_file, stub_provider):
    if how == 'key_word':
        tab.load(PAY_PAGE)
        key, stale, fixed = 'pay_now', "//button[@id='pay-old']", "//button[@id='pay-now']"
    else:
        tab.load(neutral_button_page('go-on-v2'))
        key, stale, fixed = 'go_on', "//button[@id='go-on']", "//button[@id='go-on-v2']"
    provider = stub_provider(fixed)
    heal = _open(tab, provider, store_file,
                 destructive_keys={key} if how == 'destructive_keys' else frozenset())

    with pytest.raises(AttentionRequired) as info:
        heal.locator(key, stale, role='button', timeout_ms=FAST_MS, poll_ms=POLL_MS,
                     destructive=True if how == 'destructive_arg' else None)

    message = str(info.value)
    if how == 'key_word':
        assert "looks like a destructive control (its key contains 'pay')" in message
    else:
        assert 'is marked as a destructive control' in message and 'looks like' not in message
    assert provider.bundles == []                         # the model is never asked
    assert fixed not in saved_candidates(store_file, key)


def test_the_destructive_flag_survives_a_new_open(tab, store_file, stub_provider):
    tab.load(neutral_button_page('go-on'))
    first = _open(tab, stub_provider(None), store_file)
    first.locator('go_on', "//button[@id='go-on']", role='button', destructive=True).click()
    assert saved_entry(store_file, 'go_on')['destructive'] is True

    # Next run: the button was renamed, and this call says nothing about destructive.
    tab.load(neutral_button_page('go-on-v2'))
    provider = stub_provider("//button[@id='go-on-v2']")
    second = _open(tab, provider, store_file)
    with pytest.raises(AttentionRequired) as info:
        second.locator('go_on', "//button[@id='go-on']", role='button', timeout_ms=FAST_MS,
                       poll_ms=POLL_MS)

    assert 'is marked as a destructive control' in str(info.value)
    assert provider.bundles == []


@pytest.mark.parametrize('control,label,word', [
    ('<button type="button" id="{id}">Place order</button>', 'Place order', 'place order'),
    ('<input type="submit" id="{id}" value="Buy now">', 'Buy now', 'buy'),
], ids=['button-text', 'submit-value'])
def test_a_destructive_label_seen_on_the_page_is_remembered(control, label, word, tab,
                                                            store_file, stub_provider):
    """The key and the code say nothing, but the control's label on the page does."""
    tab.load(page('<form>' + control.format(id='place') + '</form>'))
    first = _open(tab, stub_provider(None), store_file)
    first.locator('go_on', "//*[@id='place']", role='button')
    assert saved_entry(store_file, 'go_on')['expectedText'] == [label]

    tab.load(page('<form>' + control.format(id='place-v2') + '</form>'))
    provider = stub_provider("//*[@id='place-v2']")
    second = _open(tab, provider, store_file)
    with pytest.raises(AttentionRequired) as info:
        second.locator('go_on', "//*[@id='place']", role='button', timeout_ms=FAST_MS,
                       poll_ms=POLL_MS)

    assert ("looks like a destructive control (its text " + repr(label) + " contains "
            + repr(word) + ")") in str(info.value)
    assert provider.bundles == []


def test_an_edited_inline_selector_wins_on_the_next_open(tab, store_file, stub_provider):
    tab.load(TWO_BUTTONS)
    first = _open(tab, stub_provider(None), store_file)
    assert first.locator('go', "//button[@id='a']", role='button').get_attribute('id') == 'a'

    # The next run, with the selector edited in code. Both buttons are on the page, so the
    # old selector would still match: the code has to win.
    provider = stub_provider(None)
    second = _open(tab, provider, store_file)
    assert second.locator('go', '#b', role='button').get_attribute('id') == 'b'

    assert provider.bundles == []
    saved = saved_candidates(store_file, 'go')
    assert saved["//button[@id='a']"]['status'] == 'superseded'
    assert saved['#b']['status'] == 'active' and saved['#b']['source'] == 'code'


def test_the_role_is_read_from_the_live_element_and_used_for_a_heal(tab, store_file,
                                                                     stub_provider):
    # A CSS id selector says nothing about the element, so the role can only come from
    # the page.
    tab.load(country_page('country'))
    first = _open(tab, stub_provider(None), store_file)
    first.locator('country', '#country')
    assert saved_entry(store_file, 'country')['expectedRole'] == 'combobox'

    tab.load(country_page('country-v2'))
    provider = stub_provider("//select[@id='country-v2']")
    second = _open(tab, provider, store_file)
    second.locator('country', '#country', timeout_ms=FAST_MS, poll_ms=POLL_MS).select_option('de')

    assert len(provider.bundles) == 1
    assert provider.bundles[0]['expectedRole'] == 'combobox'
    assert tab.value('#country-v2') == 'de'
    assert second.heals[0].new == "//select[@id='country-v2']"


@pytest.mark.engine_family('playwright')
def test_a_selector_inside_an_iframe_says_so_without_a_model_call(tab, store_file,
                                                                  stub_provider):
    tab.load(page("<h1>Sign up</h1><iframe name='consent' srcdoc=\"<label><input "
                  "type='checkbox' id='inner-terms'> I agree</label>\"></iframe>"))
    tab.page.frame_locator("iframe[name='consent']").locator('#inner-terms').wait_for(
        state='attached')
    provider = stub_provider("//input[@type='checkbox']")
    heal = _open(tab, provider, store_file)

    with pytest.raises(AttentionRequired) as info:
        heal.locator('frame_terms', '#inner-terms', role='checkbox', timeout_ms=FAST_MS,
                     poll_ms=POLL_MS)
    assert "matches inside the iframe named 'consent'" in str(info.value)
    assert "heal = mendwright.open(page.frame(name='consent'))" in str(info.value)
    assert provider.bundles == []

    # The advice works: on the Frame, the selector matches at once.
    framed = mendwright.open(tab.page.frame(name='consent'), provider=provider,
                             store_path=store_file)
    framed.locator('frame_terms', '#inner-terms', role='checkbox').check()
    assert provider.bundles == []


def test_mendwright_disable_resolves_but_never_heals(tab, store_file, stub_provider,
                                                     monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    tab.load(SIGNUP_V2)
    provider = stub_provider(HEALED)
    heal = _open(tab, provider, store_file)

    heal.locator('email', '#email', role='input').fill('ada@example.test')
    assert tab.value('#email') == 'ada@example.test'

    with pytest.raises(AttentionRequired) as info:
        heal.locator('accept_terms', STALE, role='checkbox', timeout_ms=FAST_MS,
                     poll_ms=POLL_MS)
    assert 'MENDWRIGHT_DISABLE' in str(info.value)
    assert provider.bundles == []
    assert HEALED not in saved_candidates(store_file, 'accept_terms')
