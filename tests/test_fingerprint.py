"""Fingerprint healing: the model-free first tier.

The module (mendwright.fingerprint): what a fingerprint records and never records, how it
scores, when it is confident, and the XPath it builds. Then the tier inside a heal, on
conftest's FakePage made with fingerprints=True: it records a fingerprint when a selector
matches, heals from it with no provider at all, goes through the same gate as a model's
proposal, and hands anything it is not sure of to the provider. Offline throughout.
"""

import json
import logging

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright import fingerprint as fp_mod
from mendwright.coordinator import HealCoordinator
from mendwright.facade import HealEvent
from mendwright.fingerprint import (FINGERPRINT_JS, find, from_raw, generated_id, of_element,
                                    raw_of_element, score, stable_classes, xpath_for)
from mendwright.store import SelectorStore

V1 = """<html><body><main><h1>Checkout</h1>
<form id='checkout'>
  <label for='email'>Email</label><input id='email' name='email' type='email' value='ada@example.com'>
  <div class='consent'><label><input id='accept-terms' name='terms' type='checkbox'>
    <span>I accept the terms and conditions</span></label></div>
  <label><input id='news' name='news' type='checkbox'> Send me the newsletter</label>
  <button id='pay' type='submit'>Pay now</button>
</form></main></body></html>"""

# A redesign: every id is gone, the classes are generated, the labels are the same.
V2 = """<html><body><main><h1>Checkout</h1>
<form id='checkout'>
  <label for='mail-1'>Email</label><input id='mail-1' name='email' type='email'>
  <fieldset class='legal'><ds-checkbox><label class='css-1x2y3z'>
    <input type='checkbox' name='agreement' data-testid='legal-consent'>
    <span>I accept the terms and conditions</span></label></ds-checkbox></fieldset>
  <label><input name='news' type='checkbox'> Send me the newsletter</label>
  <button class='ds-button' type='submit'>Pay now</button>
</form></main></body></html>"""

# Two look-alike checkboxes and nothing to tell which one the step meant.
AMBIGUOUS = """<html><body><main><form id='checkout'>
  <label><input type='checkbox' name='a'><span>I accept the terms</span></label>
  <label><input type='checkbox' name='b'><span>I accept the terms</span></label>
</form></main></body></html>"""


def _el(html, xpath):
    return lxml.html.fromstring(html).xpath(xpath)[0]


# --- what a fingerprint holds ------------------------------------------------------------------

def test_a_fingerprint_records_the_element_and_its_surroundings():
    fp = of_element(_el(V1, "//input[@id='accept-terms']"))
    assert fp['v'] == 1 and fp['tag'] == 'input'
    assert fp['attrs'] == {'id': 'accept-terms', 'name': 'terms', 'type': 'checkbox'}
    assert fp['label'] == 'I accept the terms and conditions'
    assert 'text' not in fp                      # a form field has no text of its own
    assert [a['tag'] for a in fp['ancestors']] == ['label', 'div', 'form', 'main']
    assert fp['ancestors'][1] == {'tag': 'div', 'classes': ['consent']}
    assert fp['ancestors'][2] == {'tag': 'form', 'id': 'checkout'}


def test_a_fingerprint_never_holds_an_input_value():
    el = _el(V1, "//input[@id='email']")
    raw = raw_of_element(el)
    assert 'value' not in raw['attrs']
    fp = of_element(el)
    assert 'ada@example.com' not in json.dumps(fp)
    assert fp['label'] == 'Email'                 # from <label for=...>


def test_a_fingerprint_reads_text_links_and_labels_the_way_it_stores_them():
    html = ("<html><body><nav aria-label='Main'><a class='Header_link__a1B2c nav-link' "
            "href='https://shop.example/pricing/?ref=1#top'>  Pricing\n plans </a>"
            "<button>Card 4111111111111111 saved</button>"
            "<input type='SUBMIT' role='Button'></nav></body></html>")
    link = of_element(_el(html, '//a'))
    assert link['attrs'] == {'href': '/pricing'}   # the path only: no host, query or slash
    assert link['text'] == 'Pricing plans'
    assert link['classes'] == ['nav-link']
    assert link['ancestors'] == [{'tag': 'nav'}]
    assert of_element(_el(html, '//button'))['text'] == 'Card # saved'   # long numbers masked
    assert of_element(_el(html, '//input'))['attrs'] == {'type': 'submit', 'role': 'button'}
    long_text = of_element(lxml.html.fromstring('<button>' + 'x' * 200 + '</button>'))
    assert len(long_text['text']) == fp_mod.TEXT_LIMIT


@pytest.mark.parametrize('raw', [None, [], 'input', {'tag': ''}, {'tag': 'a'},
                                 {'tag': 'a', 'attrs': {}, 'ancestors': ['div']},
                                 {'tag': 'button', 'html': '<button>x</button>'}])
def test_anything_that_is_not_the_raw_shape_gives_no_fingerprint(raw):
    assert from_raw(raw) is None


def test_generated_classes_and_ids_are_left_out_or_never_trusted():
    tokens = ('btn btn-primary form-group h-4 Header_cartLink__uIoob css-1x2y3z sc-bdVaJa '
              'jsx-1234567 ng-touched active c3pbtlh btn')
    assert stable_classes(tokens) == ['btn', 'btn-primary', 'form-group', 'h-4']
    assert stable_classes(None) == []
    for value in (':r3:', 'mui-generated-8842', 'ember123', 'x1n2onr6', ''):
        assert generated_id(value), value
    for value in ('email', 'signin-email', 'field-4', 'accept_terms'):
        assert not generated_id(value), value


# --- scoring -------------------------------------------------------------------------------------

def test_the_same_element_scores_one_and_is_a_strong_match():
    fp = of_element(_el(V1, '//button'))
    result = score(fp, fp)
    assert result.value == pytest.approx(1.0) and result.strong


def test_a_strong_match_needs_an_identity_signal_not_just_the_same_kind_of_element():
    old = of_element(_el(V1, "//input[@id='accept-terms']"))
    look_alike = of_element(lxml.html.fromstring(
        "<html><body><main><form id='checkout'><div class='consent'><label>"
        "<input type='checkbox' name='other'><span>Remember me</span></label></div>"
        "</form></main></body></html>").xpath('//input')[0])
    result = score(old, look_alike)
    assert result.value > 0.3       # same tag, type and surroundings
    assert not result.strong        # but nothing that says it is the same control


def test_a_field_name_can_move_between_a_label_and_aria_label():
    old = of_element(_el("<html><body><form><label for='o'>From</label>"
                         "<input id='o' name='origin'></form></body></html>", '//input'))
    new = of_element(_el("<html><body><form><input aria-label='From' name='from_airport'>"
                         "</form></body></html>", '//input'))
    assert score(old, new).strong


# --- finding the element -------------------------------------------------------------------------

def test_a_confident_match_gives_a_unique_xpath_without_a_position():
    old = of_element(_el(V1, "//input[@id='accept-terms']"))
    match = find(old, V2, 'checkbox')
    assert match.xpath == "//input[@data-testid='legal-consent']"
    assert match.score >= fp_mod.THRESHOLD and match.score - match.runner_up >= fp_mod.MARGIN
    assert match.reason == ''
    hits = lxml.html.fromstring(V2).xpath(match.xpath)
    assert len(hits) == 1 and hits[0].get('name') == 'agreement'


def test_two_look_alikes_are_not_told_apart():
    old = of_element(_el("<html><body><main><form id='checkout'><label><input type='checkbox'>"
                         "<span>I accept the terms</span></label></form></main></body></html>",
                         '//input'))
    match = find(old, AMBIGUOUS, 'checkbox')
    assert match.xpath is None
    assert 'two elements score alike' in match.reason


def test_nothing_similar_enough_is_not_a_match():
    old = of_element(_el(V1, '//button'))
    match = find(old, "<html><body><a href='/help'>Help</a><button>Delete account</button>"
                      "</body></html>", 'button')
    assert match.xpath is None
    assert match.reason.startswith('the best score was')


def test_only_elements_of_the_role_are_scored():
    old = of_element(_el(V1, '//button'))
    match = find(old, "<html><body><a href='/pay'>Pay now</a></body></html>", 'button')
    assert match.xpath is None
    assert match.reason == "no element on the page has the role 'button'"


def test_a_copy_the_page_hides_is_not_a_rival():
    old = of_element(_el(V1, '//button'))
    page = ("<html><body><form id='checkout'><button type='submit'>Pay now</button></form>"
            "<div style='display: none'><button type='submit'>Pay now</button></div>"
            "<div hidden><button type='submit'>Pay now</button></div></body></html>")
    match = find(old, page, 'button')
    assert match.xpath == "//form[@id='checkout']//button"


def test_a_copy_hidden_on_the_live_page_is_not_a_rival():
    old = of_element(_el(V1, '//button'))
    page = ("<html><body><form id='checkout'><button type='submit'>Pay now</button></form>"
            "<div data-testid='sticky'><button type='submit'>Pay now</button></div></body></html>")
    assert find(old, page, 'button').xpath is None          # without the live page: a tie
    checked = []

    def visible(xpath):
        checked.append(xpath)
        return 'sticky' not in xpath
    match = find(old, page, 'button', visible=visible)
    assert match.xpath == "//form[@id='checkout']//button"
    assert any('sticky' in x for x in checked)


def test_a_stored_fingerprint_of_another_version_is_not_read():
    old = of_element(_el(V1, '//button'))
    old['v'] = 99
    assert find(old, V2, 'button').reason == 'the stored fingerprint is not one this version reads'


def test_the_xpath_never_uses_a_position_and_is_none_when_nothing_is_unique():
    page = lxml.html.fromstring("<html><body><ul><li><b>x</b></li><li><b>x</b></li></ul>"
                                "</body></html>")
    assert xpath_for(page.xpath('//b')[1]) is None
    page = lxml.html.fromstring("<html><body><p>It's \"quoted\"</p></body></html>")
    xp = xpath_for(page.xpath('//p')[0])
    assert xp.startswith('//p[normalize-space()=concat(') and '[1]' not in xp
    assert len(page.xpath(xp)) == 1


def test_the_fingerprint_script_is_one_function_that_reads_no_value():
    assert FINGERPRINT_JS.lstrip().startswith('(els) =>')
    assert "'value'" not in FINGERPRINT_JS and '.value' not in FINGERPRINT_JS


# --- the tier inside a heal ------------------------------------------------------------------------

def _open(page, tmp_path, provider=None, **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'), **kwargs)


def _journal(tmp_path):
    path = tmp_path / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _broken(make_page, tmp_path, provider=None, html=V2, **kwargs):
    """A Healer that matched #accept-terms on V1, on a page that now shows ``html``."""
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path, provider, **kwargs)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    page.set_html(html)
    return page, heal


def test_a_match_records_the_fingerprint_in_the_store(make_page, tmp_path):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    stored = heal.store.meta('terms')['fingerprint']
    assert stored == of_element(_el(V1, "//input[@id='accept-terms']"))
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert saved['selectors']['terms']['fingerprint'] == stored
    heal.locator('email', '#email', poll_ms=0)
    assert 'ada@example.com' not in (tmp_path / 'o.json').read_text(encoding='utf-8')


def test_the_fingerprint_is_read_once_per_selector_per_healer(make_page, tmp_path):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    for _ in range(3):
        heal.locator('terms', '#accept-terms', poll_ms=0)
    assert page.fingerprint_reads == 1


def test_it_heals_with_no_provider_at_all(make_page, tmp_path, caplog):
    page, heal = _broken(make_page, tmp_path)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        box = heal.locator('terms', '#accept-terms', poll_ms=0)
    new = "//input[@data-testid='legal-consent']"
    assert heal.heals == [HealEvent('terms', ('#accept-terms',), new, None, 'fingerprint')]
    assert heal.pending() == {'terms': new}
    box.check(expect=lambda: True)
    assert heal.heals[0].confirmed is True
    assert page.actions[-1][:2] == ('xpath=' + new, 'check')
    assert "healed 'terms': #accept-terms -> " + new + ' (fingerprint, 0.0s)' in caplog.text
    events = [(e['event'], e.get('source'), e.get('provider')) for e in _journal(tmp_path)
              if e['event'].startswith('heal.')]
    assert events == [('heal.requested', None, None), ('heal.proposed', 'fingerprint', None),
                      ('heal.applied', 'fingerprint', 'fingerprint'),
                      ('heal.canary', None, None)]
    proposed = [e for e in _journal(tmp_path) if e['event'] == 'heal.proposed'][0]
    assert proposed['reasoning'].startswith('fingerprint score ')


def test_a_confident_match_never_asks_the_provider(make_page, tmp_path, stub_provider):
    provider = stub_provider("//input[@name='agreement']")
    page, heal = _broken(make_page, tmp_path, provider)
    box = heal.locator('terms', '#accept-terms', poll_ms=0)
    assert provider.bundles == []
    box.confirm(True)
    assert provider.outcomes == []          # the provider did not propose it


def test_what_the_tier_is_not_sure_of_goes_to_the_provider(make_page, tmp_path, stub_provider):
    provider = stub_provider("//input[@name='a']")
    page, heal = _broken(make_page, tmp_path, provider, html=AMBIGUOUS)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    assert len(provider.bundles) == 1
    assert heal.heals[-1].source == 'model'
    assert 'fingerprint' not in provider.bundles[0]   # the model never sees it
    events = [e['event'] for e in _journal(tmp_path) if e['event'].startswith('heal.')]
    assert events.count('heal.requested') == 1


def test_without_a_provider_an_unsure_match_says_both_things_to_fix(make_page, tmp_path):
    page, heal = _broken(make_page, tmp_path, html=AMBIGUOUS)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#accept-terms', poll_ms=0)
    message = str(info.value)
    assert ("-> the fingerprint match for 'terms' was not confident: two elements score alike"
            in message)
    assert 'and no model provider is configured to try instead.' in message
    assert 'ANTHROPIC_API_KEY' in message       # the provider part names what to set
    assert heal.heals == []


def test_without_a_provider_or_a_fingerprint_the_message_says_so(make_page, tmp_path):
    page = make_page(V1)                         # this page answers no fingerprint read
    heal = _open(page, tmp_path)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    page.set_html(V2)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#accept-terms', poll_ms=0)
    assert ("-> 'terms' has no fingerprint yet (mendwright records one each time its selector "
            'matches), and no model provider is configured to try instead.') in str(info.value)


def test_a_match_the_gate_rejects_is_not_applied(make_page, tmp_path):
    page, heal = _broken(make_page, tmp_path)
    heal.store.add_candidate('terms', "//input[@name='agreement']")
    heal.store.quarantine('terms', "//input[@name='agreement']")    # the same element
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#accept-terms', poll_ms=0)
    assert ("the fingerprint match for 'terms', //input[@data-testid='legal-consent'], failed "
            "mendwright's checks (previously quarantined") in str(info.value)
    rejected = [e for e in _journal(tmp_path) if e['event'] == 'heal.rejected']
    assert rejected[-1]['source'] == 'fingerprint'


def test_fingerprint_false_and_the_variable_turn_it_off(make_page, tmp_path, stub_provider,
                                                        monkeypatch):
    provider = stub_provider("//input[@name='agreement']")
    page, heal = _broken(make_page, tmp_path, provider, fingerprint=False)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    assert heal.store.meta('terms')['fingerprint'] is None
    assert page.fingerprint_reads == 0 and len(provider.bundles) == 1

    monkeypatch.setenv('MENDWRIGHT_FINGERPRINT', '0')
    other = tmp_path / 'b'
    page, heal = _broken(make_page, other, stub_provider("//input[@name='agreement']"))
    assert heal.coordinator.fingerprint is False and page.fingerprint_reads == 0
    # An argument wins over the variable.
    assert _open(make_page(V1), tmp_path, fingerprint=True).coordinator.fingerprint is True
    monkeypatch.setenv('MENDWRIGHT_FINGERPRINT', 'sometimes')
    with pytest.raises(ValueError, match='MENDWRIGHT_FINGERPRINT must be 1 or 0'):
        _open(make_page(V1), tmp_path)


def test_disable_turns_the_tier_off_too(make_page, tmp_path, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    page, heal = _broken(make_page, tmp_path, fingerprint=True)
    with pytest.raises(AttentionRequired, match='MENDWRIGHT_DISABLE=1'):
        heal.locator('terms', '#accept-terms', poll_ms=0)
    assert page.fingerprint_reads == 0 and heal.heals == []


def test_dry_run_checks_the_match_and_applies_nothing(make_page, tmp_path, stub_provider):
    provider = stub_provider("//input[@name='agreement']")
    page, heal = _broken(make_page, tmp_path, provider, dry_run=True)
    with pytest.raises(AttentionRequired, match='the fingerprint match for .terms. was checked'):
        heal.locator('terms', '#accept-terms', poll_ms=0)
    assert provider.bundles == [] and heal.heals == []
    assert "//input[@data-testid='legal-consent']" not in json.dumps(
        heal.store.candidates('terms'))


def test_a_destructive_key_is_neither_fingerprinted_nor_healed(make_page, tmp_path):
    page = make_page(V1, fingerprints=True)
    heal = _open(page, tmp_path)
    heal.locator('pay', '#pay', destructive=True, poll_ms=0)
    assert page.fingerprint_reads == 0
    assert heal.store.meta('pay')['fingerprint'] is None


def test_an_unconfirmed_heal_does_not_replace_the_fingerprint(make_page, tmp_path):
    page, heal = _broken(make_page, tmp_path)
    before = heal.store.meta('terms')['fingerprint']
    box = heal.locator('terms', '#accept-terms', poll_ms=0)    # healed, not confirmed
    heal.locator('terms', '#accept-terms', poll_ms=0)          # matches through the heal
    assert heal.store.meta('terms')['fingerprint'] == before
    box.confirm(True)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    after = heal.store.meta('terms')['fingerprint']
    assert after['attrs'].get('data-testid') == 'legal-consent'


def test_the_fingerprint_tier_does_not_spend_the_rate_limit(make_page, tmp_path, stub_provider):
    provider = stub_provider(None)
    page, heal = _broken(make_page, tmp_path, provider, rate_per_key_per_hour=0)
    heal.locator('terms', '#accept-terms', poll_ms=0)
    assert heal.heals[-1].source == 'fingerprint'


def test_the_low_level_coordinator_leaves_the_tier_off(make_page):
    page = make_page(V2, fingerprints=True)
    store = SelectorStore({'schema': 2, 'selectors': {}}, 'unused.json')
    coordinator = HealCoordinator(store, provider=object(), html_provider=page.content,
                                  inventory_provider=list, guard=lambda: True)
    assert coordinator.fingerprint is False
