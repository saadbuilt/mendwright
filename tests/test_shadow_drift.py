"""heal.drift() and mendwright check on a key inside an open shadow root: the same decision a
run makes (see tests/test_shadow_heal.py), on the fakes of tests/conftest.py, with nothing
saved."""

import json

import mendwright
from mendwright.journal import NullJournal
from mendwright.store import load_store


def shadow(inner, host='<my-app id="root">'):
    tag = host[1:].split()[0].rstrip('>')
    return host + '<template shadowrootmode="open">' + inner + '</template></' + tag + '>'


def page(body):
    return '<html><head><title>t</title></head><body>' + body + '</body></html>'


LOOKALIKE = '<button data-testid="save" type="button">Save</button>'
V1 = page(LOOKALIKE + shadow('<button id="save" data-testid="save" type="button">Save</button>'))
V2 = page(LOOKALIKE + shadow('<button id="save-2" data-testid="save" type="button">Save</button>'))
HOST = 'my-app[id="root"]'
CHAIN = HOST + ' >> [data-testid="save"]'


def _open(target, tmp_path, **kwargs):
    return mendwright.open(target, store_path=str(tmp_path / 'o.json'), timeout_ms=0,
                           provider=None, journal=NullJournal(), **kwargs)


def _sources(tmp_path):
    data = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    return [c['source'] for c in data['selectors']['save']['candidates']]


def _recorded(make_shadow_page, tmp_path):
    page_ = make_shadow_page(V1)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    return page_


def test_drift_says_a_run_would_heal_inside_the_shadow_root(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    key = _open(page_, tmp_path).drift(timeout_ms=0).keys[0]
    assert key.verdict == 'would-heal' and key.detail == 'fingerprint'
    assert key.heal['selector'] == CHAIN and key.heal['shadow'] == [HOST]
    assert 'heal it from memory inside the shadow root of ' + HOST in key.reason
    assert _sources(tmp_path) == ['code']         # nothing saved


def test_drift_refuses_as_a_run_does_when_the_shadow_root_is_gone(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(page(LOOKALIKE))
    key = _open(page_, tmp_path).drift(timeout_ms=0).keys[0]
    assert key.verdict == 'refused' and 'cannot enter that shadow root' in key.reason
    assert _sources(tmp_path) == ['code']


def test_drift_previews_the_follow_into_a_shadow_root(make_shadow_driver, tmp_path):
    driver = make_shadow_driver(V1)
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('save', ['#save'])
    store.set_meta('save', role='button')
    key = _open(driver, tmp_path).drift(['save'], timeout_ms=0).keys[0]
    assert key.verdict == 'would-heal' and key.detail == 'shadow'
    assert key.heal == {'source': 'shadow', 'selector': HOST + ' >> #save', 'frame': None,
                        'shadow': [HOST], 'score': None, 'runner_up': None}
    assert 'would heal into a shadow root' in mendwright.drift.counts_sentence([key])
    assert _sources(tmp_path) == ['code']


def test_drift_refuses_a_follow_a_run_would_refuse(make_shadow_driver, tmp_path):
    driver = make_shadow_driver(page(shadow('<button id="save">a</button><button id="save">b</button>')))
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_code_candidates('save', ['#save'])
    store.set_meta('save', role='button')
    key = _open(driver, tmp_path).drift(['save'], timeout_ms=0).keys[0]
    assert key.verdict == 'refused' and 'it matches 2 elements there' in key.reason


def test_drift_looks_up_a_saved_shadow_heal_inside_its_shadow_root(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    key = _open(page_, tmp_path).drift(timeout_ms=0).keys[0]
    assert key.verdict == 'ok-heal' and key.used == CHAIN and key.live_role == 'button'
