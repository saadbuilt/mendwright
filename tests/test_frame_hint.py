"""A selector that misses on the page but matches inside an iframe stops with a message
that says so, before any model call. Offline: conftest's FakePage plays the page and its
frames."""

import json

import pytest

import mendwright
from mendwright import AttentionRequired

OUTER = "<main><h1>Sign up</h1><iframe name='consent'></iframe></main>"
INNER = "<label><input type='checkbox' id='inner-terms'> I agree</label>"


class FakeFrame:
    """A Playwright Frame over its own FakePage document."""

    def __init__(self, page, name='', url='about:srcdoc', parent=None):
        self._page = page
        self.name = name
        self.url = url
        self.parent_frame = parent
        self.child_frames = []

    def locator(self, selector):
        return self._page.locator(selector)


def _framed_page(make_page, name='consent', url='about:srcdoc'):
    page = make_page(OUTER)
    main = FakeFrame(page)
    child = FakeFrame(make_page(INNER), name=name, url=url, parent=main)
    page.frames = [main, child]
    return page


def test_a_selector_inside_an_iframe_stops_before_any_model_call(tmp_path, make_page,
                                                                 stub_provider):
    page = _framed_page(make_page)
    provider = stub_provider("//input[@type='checkbox']")
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('frame_terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    message = str(info.value)
    assert message.startswith('No matching element for: frame_terms')
    assert ("'frame_terms' is inside an iframe: #inner-terms matches nothing in the page "
            "mendwright was opened on, but matches inside the iframe named 'consent'") in message
    assert "heal = mendwright.open(page.frame(name='consent'))" in message
    assert provider.bundles == []
    assert heal.coordinator.explain('frame_terms') in message
    journal = tmp_path / 'journal.jsonl'
    assert not journal.exists() or 'heal.requested' not in journal.read_text(encoding='utf-8')


def test_an_unnamed_frame_is_named_by_its_url_without_the_query(tmp_path, make_page,
                                                                 stub_provider):
    page = _framed_page(make_page, name='', url='https://user:pw@consent.example/c?token=t#x')
    heal = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('frame_terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    message = str(info.value)
    assert "page.frame(url='https://consent.example/c')" in message
    assert 'token' not in message and 'pw' not in message


def test_a_page_without_frames_heals_as_before(tmp_path, make_page, stub_provider):
    page = make_page("<form><input type='checkbox' id='new'></form>")
    page.frames = [FakeFrame(page)]
    provider = stub_provider("//input[@id='new']")
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    heal.locator('terms', '#old', role='checkbox', timeout_ms=0, poll_ms=0).check()
    assert len(provider.bundles) == 1
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    assert "//input[@id='new']" in [c['xpath'] for c in saved['selectors']['terms']['candidates']]
