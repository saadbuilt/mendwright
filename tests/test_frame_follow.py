"""A key whose selector matches only inside an iframe is looked up there, and a key last
found inside an iframe heals inside it. Offline: TwoDocs plays a page with one iframe, as a
driver (the real-browser tests in tests/browser/test_browser_drivers.py cover Playwright
and Selenium)."""

import json

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright.facade import frame_spelling

FRAME = 'iframe[name="consent"]'
OUTER = "<main><h1>Sign up</h1><iframe name='consent'></iframe></main>"
INNER = "<label><input type='checkbox' id='inner-terms' name='agree'> I agree</label>"


def _find(doc, selector):
    body = selector
    for prefix in ('xpath=', 'css='):
        if body.startswith(prefix):
            body = body[len(prefix):]
    if body.startswith('#'):
        return doc.xpath('//*[@id=$i]', i=body[1:])
    if body == FRAME:
        return doc.xpath("//iframe[@name='consent']")
    try:
        return [e for e in doc.xpath(body) if hasattr(e, 'tag')]
    except Exception:
        return []


class Doc:
    """A driver over one lxml document; the inner document is reached through FRAME."""

    def __init__(self, html, inner=None, url='https://example.test/signup'):
        self.doc = lxml.html.fromstring(html)
        self.inner = inner
        self._url = url
        self.actions = []

    def _doc(self, frame_chain):
        if tuple(frame_chain) == (FRAME,):
            return self.inner.doc
        assert not frame_chain, frame_chain
        return self.doc

    def count(self, selector, frame_chain=()):
        return len(_find(self._doc(frame_chain), selector))

    def is_visible(self, selector, frame_chain=()):
        return self.count(selector, frame_chain) == 1

    def scroll_into_view(self, selector, frame_chain=()):
        pass

    def handle(self, selector, frame_chain=()):
        return (selector, tuple(frame_chain))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        self.actions.append((verb, selector, tuple(frame_chain)))

    def act_on(self, native, verb, *args, **kwargs):
        self.actions.append((verb,) + tuple(native))

    def url(self):
        return self._url

    def outer_html(self, scope=None):
        return lxml.html.tostring(self.doc, encoding='unicode')

    def describe(self, selector, frame_chain=()):
        found = _find(self._doc(frame_chain), selector)
        if len(found) != 1:
            return None
        el = found[0]
        role = 'checkbox' if el.get('type') == 'checkbox' else el.tag
        return {'tag': el.tag, 'role': role, 'text': el.text_content().strip(),
                'outer_html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}

    def inventory(self, limit=200):
        return []


class TwoDocs(Doc):
    def __init__(self, inner_html, **frame):
        super().__init__(OUTER, inner=Doc(inner_html))
        self.frame = dict({'name': 'consent', 'url': 'about:srcdoc', 'nested': False,
                           'selector': FRAME}, **frame)

    def frame_matches(self, selector):
        n = self.inner.count(selector)
        return [dict(self.frame, count=n)] if n else []

    def framed(self, frame_selector):
        return self.inner if frame_selector == FRAME else None


def _open(page, tmp_path, provider):
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))


def _saved(tmp_path, key):
    data = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))
    return {c['xpath']: c for c in data['selectors'][key]['candidates']}


def test_a_selector_found_only_in_an_iframe_is_looked_up_there(tmp_path, stub_provider):
    page = TwoDocs(INNER)
    provider = stub_provider(None)
    heal = _open(page, tmp_path, provider)
    proxy = heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    # Waiting for its canary like any heal (the pytest plugin confirms it, strict mode
    # counts it).
    assert heal.pending() == {'terms': 'css=#inner-terms'}
    proxy.check()
    assert page.actions == [('check', 'css=#inner-terms', (FRAME,))]
    assert provider.bundles == []
    assert [(h.source, h.old, h.new) for h in heal.heals] == [
        ('frame', ('#inner-terms',), 'css=#inner-terms')]
    assert _saved(tmp_path, 'terms')['css=#inner-terms']['frameXPath'] == FRAME
    events = [json.loads(line) for line in (tmp_path / 'journal.jsonl').read_text(
        encoding='utf-8').splitlines()]
    applied = [e for e in events if e['event'] == 'heal.applied']
    assert applied[0]['source'] == 'frame' and applied[0]['frame'] == FRAME


@pytest.mark.parametrize('frame, why', [
    ({'nested': True}, 'that iframe is inside another iframe'),
    ({'selector': None}, 'nothing tells that iframe apart'),
])
def test_some_frames_are_not_followed(frame, why, tmp_path, stub_provider):
    page = TwoDocs(INNER, **frame)
    provider = stub_provider("//input")
    heal = _open(page, tmp_path, provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert why in str(info.value) and provider.bundles == [] and heal.heals == []


def test_several_matches_in_the_iframe_are_not_followed(tmp_path, stub_provider):
    page = TwoDocs(INNER + INNER.replace('inner-terms', 'other'))
    heal = _open(page, tmp_path, stub_provider(None))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', "//input[@type='checkbox']", role='checkbox', timeout_ms=0, poll_ms=0)
    assert 'it matches 2 elements there' in str(info.value)


def test_a_destructive_key_is_not_followed(tmp_path, stub_provider):
    heal = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', destructive=True, timeout_ms=0,
                     poll_ms=0)
    assert 'marked as a destructive control' in str(info.value)


def test_the_page_guard_applies(tmp_path, stub_provider):
    page = TwoDocs(INNER)
    heal = _open(page, tmp_path, stub_provider(None))
    heal.store.set_last_page('terms', '/checkout')
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert 'change in the flow' in str(info.value) and heal.heals == []


def test_a_key_last_found_in_an_iframe_heals_inside_it(tmp_path, stub_provider):
    page = TwoDocs(INNER)
    first = _open(page, tmp_path, stub_provider(None))
    first.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()

    redesigned = TwoDocs(INNER.replace('inner-terms', 'terms-v2'))
    provider = stub_provider("//input[@name='agree']")
    heal = _open(redesigned, tmp_path, provider)
    heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
    assert redesigned.actions == [('check', "//input[@name='agree']", (FRAME,))]
    assert 'terms-v2' in provider.bundles[0]['html'] and 'Sign up' not in provider.bundles[0]['html']
    assert _saved(tmp_path, 'terms')["//input[@name='agree']"]['frameXPath'] == FRAME
    # The heal ran inside the iframe; the coordinator is back on the page.
    assert heal.coordinator.driver is not redesigned.inner and heal.coordinator.frame_xpath is None


@pytest.mark.parametrize('selector, spelled', [
    ('#inner', 'css=#inner'),
    ("//input[@id='a']", "xpath=//input[@id='a']"),
    ('xpath=html/body', 'xpath=(html/body)'),
    ('text=Save', None),
])
def test_frame_spelling_keeps_a_frame_candidate_apart(selector, spelled):
    assert frame_spelling(selector) == spelled


def test_the_stop_message_gives_selenium_advice_on_selenium():
    from mendwright.facade import _frame_message
    frames = [{'name': 'consent', 'url': 'about:srcdoc', 'selector': FRAME}]
    text = _frame_message('terms', '#inner-terms', frames, 'mendwright.open', 'why', selenium=True)
    assert "driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, 'iframe[name=\"consent\"]'))" in text
    assert 'page.frame' not in text
