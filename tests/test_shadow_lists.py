"""A list inside an open shadow root. heal.locator_all() gives what the key's own selectors
match, as before (Playwright's CSS pierces open shadow roots), and its list fingerprint says
the items were inside a shadow root. With nonempty=True, a list whose selectors match nothing
is looked for in the page's own HTML (lists.find), which holds no shadow content: there it
could only find another list. So a list recorded inside a shadow root is never looked for."""

import json

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, lists
from mendwright.journal import NullJournal
from mendwright.store import load_store


def page(body):
    return '<html><head><title>t</title></head><body>' + body + '</body></html>'


def cards(cls):
    return ('<section><h2>Cart</h2><ul class="cart">' + ''.join(
        '<li class="' + cls + '"><a href="/p/' + n + '">' + n + '</a></li>' for n in ('A', 'B', 'C'))
        + '</ul></section>')


# The cart is in the shadow root of <my-app>; a look-alike list on the page holds the same
# items, as a cart's "Saved for later" can.
LOOKALIKE = '<section><h2>Saved</h2><ul class="saved">' + ''.join(
    '<li class="row"><a href="/p/' + n + '">' + n + '</a></li>' for n in ('A', 'B', 'C')) + '</ul></section>'


def app(inner):
    return '<my-app id="root"><template shadowrootmode="open">' + inner + '</template></my-app>'


V1 = page(LOOKALIKE + app(cards('card')))
V2 = page(LOOKALIKE + app(cards('item')))


def _open(page_, tmp_path):
    return mendwright.open(page_, store_path=str(tmp_path / 'o.json'), timeout_ms=0,
                           provider=None, journal=NullJournal())


def test_a_list_inside_a_shadow_root_is_given_and_recorded_as_one(make_shadow_page, tmp_path):
    page_ = make_shadow_page(V1)
    heal = _open(page_, tmp_path)
    assert len(heal.locator_all('cart', '.card', role='li', nonempty=True, poll_ms=0)) == 3
    assert lists.in_shadow(heal.store.meta('cart')['listFingerprint'])


def test_a_list_recorded_inside_a_shadow_root_is_never_looked_for(make_shadow_page, tmp_path):
    page_ = make_shadow_page(V1)
    _open(page_, tmp_path).locator_all('cart', '.card', role='li', nonempty=True, poll_ms=0)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    with pytest.raises(AttentionRequired, match='The list was inside a shadow root'):
        heal.locator_all('cart', '.card', role='li', nonempty=True, poll_ms=0)
    data = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert not [c for c in data['selectors']['cart']['candidates'] if c.get('list')]


def test_the_list_fingerprint_says_shadow_only_when_it_was_in_one():
    els = lxml.html.fromstring(cards('card')).xpath('//li')
    raw = lists.raw_of_elements(els, place=True)
    assert 'shadow' not in lists.from_raw(raw)
    assert lists.from_raw(dict(raw, shadow=True))['shadow'] is True
    assert lists.from_raw(dict(raw, shadow=False)).get('shadow') is None
    for script in (lists.LIST_JS, lists.LIST_PLACE_JS):
        assert 'shadow: shadow' in script


def test_a_list_lookup_never_runs_a_row_saved_inside_a_shadow_root(make_page, tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('cart', 'my-app >> .card', source='seed', shadow_hosts=['my-app'])
    store.add_candidate('cart', '.card', source='code')
    page_ = make_page(page(cards('card')))
    heal = mendwright.open(page_, store=store, timeout_ms=0, provider=None, journal=NullJournal())
    assert [c['xpath'] for c in heal._list_runnable('cart')] == ['.card']
