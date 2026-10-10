"""apply() refuses a proposal the store already holds as quarantined or superseded, and
stores non-'/' XPath proposals so the drivers run them as XPath."""

import json

import pytest

from mendwright import HealCoordinator, apply, load_store
from mendwright.selectors import classify

HTML = ("<html><body><form><label><input type='checkbox' id='a'>accept</label>"
        "<input type='checkbox' id='news'>"
        "<input type='text' id='email'></form></body></html>")
CHECKBOX = "//input[@id='news']"
LABELLED = "//label[contains(.,'accept')]/input"
META = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}


def _store(tmp_path, status='quarantined'):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_meta('terms', role='checkbox')
    store.add_candidate('terms', "//input[@id='old']", source='code')
    store.add_candidate('terms', CHECKBOX, source='heal')
    if status == 'quarantined':
        store.quarantine('terms', CHECKBOX)
    else:
        store._find('terms', CHECKBOX)['status'] = status
    return store


def test_status_reports_each_candidate(tmp_path):
    store = _store(tmp_path)
    assert store.status('terms', CHECKBOX) == 'quarantined'
    assert store.status('terms', "//input[@id='old']") == 'active'
    assert store.status('terms', '//missing') is None
    assert store.status('nokey', '//x') is None


def test_quarantined_proposal_is_rejected(tmp_path):
    store = _store(tmp_path)
    applied, reason = apply(store, 'terms', {'xpath': CHECKBOX}, HTML, META)
    assert applied is None
    assert reason == 'previously quarantined'
    assert store.status('terms', CHECKBOX) == 'quarantined'


def test_superseded_proposal_is_rejected(tmp_path):
    store = _store(tmp_path, status='superseded')
    applied, reason = apply(store, 'terms', {'xpath': CHECKBOX}, HTML, META)
    assert (applied, reason) == (None, 'previously superseded')


def test_a_fallback_after_a_quarantined_xpath_still_applies(tmp_path):
    store = _store(tmp_path)
    applied, reason = apply(store, 'terms', {'xpath': CHECKBOX, 'fallbacks': [LABELLED]}, HTML, META)
    assert (applied, reason) == (LABELLED, 'ok')
    assert LABELLED in [c['xpath'] for c in store.candidates('terms')]


class Repeating:
    """Proposes the quarantined xpath first, then a fresh one on the re-prompt."""

    def __init__(self):
        self.bundles = []

    def propose(self, bundle):
        self.bundles.append(bundle)
        return {'xpath': CHECKBOX if len(self.bundles) == 1 else LABELLED}

    def report_outcome(self, *a):
        pass


def test_reprompt_tells_the_model_why_and_heals_with_the_next_answer(tmp_path):
    store = _store(tmp_path)
    provider = Repeating()
    coord = HealCoordinator(store, provider=provider, html_provider=lambda: HTML,
                            inventory_provider=lambda: [], guard=lambda: True)
    applied = coord.try_heal('terms', META, store.candidates('terms'), {})
    assert applied == LABELLED
    assert provider.bundles[1]['rejectedXpath'] == CHECKBOX
    assert provider.bundles[1]['rejectReason'] == 'previously quarantined'


def test_stores_without_status_still_work():
    class Minimal:
        def __init__(self):
            self.added = []

        def add_candidate(self, key, xpath, **kw):
            self.added.append(xpath)

    store = Minimal()
    assert apply(store, 'terms', {'xpath': CHECKBOX}, HTML, META) == (CHECKBOX, 'ok')
    assert store.added == [CHECKBOX]


def test_xpath_without_a_leading_slash_is_stored_with_the_prefix(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    meta = {'expectedRole': 'input', 'expectedText': [], 'destructive': False}
    applied, reason = apply(store, 'email', {'xpath': "descendant::input[@id='email']"}, HTML, meta)
    assert reason == 'ok'
    assert applied == "xpath=descendant::input[@id='email']"
    assert classify(applied) == 'xpath'
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert saved['selectors']['email']['candidates'][0]['xpath'] == applied


def test_a_redundant_xpath_prefix_is_dropped(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    applied, reason = apply(store, 'terms', {'xpath': 'xpath=' + CHECKBOX}, HTML, META)
    assert (applied, reason) == (CHECKBOX, 'ok')


def test_non_string_proposal_is_rejected():
    applied, reason = apply(object(), 'terms', {'xpath': 42}, HTML, META)
    assert (applied, reason) == (None, 'proposal is not a string')


@pytest.mark.parametrize('spelling', [
    '//input[@id="news"]', "(//input[@id='news'])[1]", "//*[@id='news']",
    "//form/input[@id='news']", "//input[@type='checkbox' and @id='news']",
])
def test_another_spelling_of_a_quarantined_element_is_rejected(spelling, tmp_path):
    store = _store(tmp_path)
    applied, reason = apply(store, 'terms', {'xpath': spelling}, HTML, META)
    assert applied is None
    assert reason.startswith('previously quarantined (the same element')
    assert spelling not in [c['xpath'] for c in store.candidates('terms')]


def test_a_different_element_still_heals_after_a_quarantine(tmp_path):
    store = _store(tmp_path)
    assert apply(store, 'terms', {'xpath': "//input[@id='a']"}, HTML, META) == (
        "//input[@id='a']", 'ok')


def test_a_superseded_selector_does_not_block_its_element(tmp_path):
    # An old code selector can point at the right element; only a quarantine blocks it.
    store = _store(tmp_path, status='superseded')
    assert apply(store, 'terms', {'xpath': '//input[@id="news"]'}, HTML, META) == (
        '//input[@id="news"]', 'ok')


def test_the_store_lists_quarantined_selectors(tmp_path):
    store = _store(tmp_path)
    assert store.quarantined('terms') == [CHECKBOX]
    assert store.quarantined('missing') == []
