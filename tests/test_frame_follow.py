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
    if body.startswith('iframe['):
        return doc.xpath('//iframe')
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
        self.frame_sel = FRAME

    def _doc(self, frame_chain):
        if tuple(frame_chain) == (self.frame_sel,):
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
        self.frame_sel = self.frame['selector'] or FRAME

    def frame_matches(self, selector):
        n = self.inner.count(selector)
        return [dict(self.frame, count=n)] if n else []

    def framed(self, frame_selector):
        return self.inner if frame_selector == self.frame_sel else None


def _open(page, tmp_path, provider, **kwargs):
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'), **kwargs)


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
    # Saved with its prefix written out, so it never shares a row with the page.
    assert redesigned.actions == [('check', "xpath=//input[@name='agree']", (FRAME,))]
    assert 'terms-v2' in provider.bundles[0]['html'] and 'Sign up' not in provider.bundles[0]['html']
    assert _saved(tmp_path, 'terms')["xpath=//input[@name='agree']"]['frameXPath'] == FRAME
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


# --- what the pre-release review found ----------------------------------------------------

@pytest.mark.parametrize('how, why', [
    ({'dry_run': True}, 'dry-run mode is on'),
    ({'guard': lambda: False}, 'a transaction guard is currently blocking heals'),
])
def test_dry_run_and_a_blocking_guard_do_not_follow(how, why, tmp_path, stub_provider):
    heal = _open(TwoDocs(INNER), tmp_path, stub_provider(None), **how)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert why in str(info.value) and heal.heals == []
    saved = tmp_path / 'o.json'
    assert not saved.exists() or 'frameXPath' not in saved.read_text(encoding='utf-8')


def test_mendwright_disable_does_not_follow(tmp_path, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    heal = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert 'MENDWRIGHT_DISABLE=1' in str(info.value) and heal.heals == []


def test_a_quarantined_lookup_is_not_followed_again(tmp_path, stub_provider):
    first = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    first.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    first.coordinator.confirm_pending('terms', False)          # the action there failed
    heal = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert 'that lookup is quarantined' in str(info.value) and heal.heals == []


def test_an_iframe_that_changed_its_attributes_is_followed_to_its_new_selector(tmp_path,
                                                                              stub_provider):
    first = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    first.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
    page = TwoDocs(INNER, selector='iframe[title="Consent"]')
    heal = _open(page, tmp_path, stub_provider(None))
    heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
    assert page.actions == [('check', 'css=#inner-terms', ('iframe[title="Consent"]',))]
    assert _saved(tmp_path, 'terms')['css=#inner-terms']['frameXPath'] == 'iframe[title="Consent"]'


def test_a_later_selector_is_followed_when_the_first_cannot_be(tmp_path, stub_provider):
    page = TwoDocs(INNER + "<input type='checkbox' id='other'>")
    heal = _open(page, tmp_path, stub_provider(None))
    heal.locator('terms', ["//input[@type='checkbox']", '#inner-terms'], role='checkbox',
                 timeout_ms=0, poll_ms=0).check()
    assert page.actions == [('check', 'css=#inner-terms', (FRAME,))]


def test_a_store_without_the_new_keywords_still_takes_page_heals():
    from mendwright.client import add_heal

    class OldStore:
        def __init__(self):
            self.rows = []

        def add_candidate(self, key, xpath, *, source='heal'):
            self.rows.append((key, xpath, source))

    store = OldStore()
    assert add_heal(store, 'k', '//a', css='a.b', frame_xpath='') is True
    assert add_heal(store, 'k', '//b', frame_xpath='iframe[id="f"]') is False
    assert store.rows == [('k', '//a', 'heal')]


def test_the_store_moves_and_updates_a_candidate_with_the_same_text(tmp_path):
    from mendwright.store import load_store
    store = load_store(None, str(tmp_path / 's.json'))
    store.add_candidate('k', '//input', source='heal', frame_xpath='iframe[id="f"]', css='#a')
    store.add_candidate('k', '//input', source='heal', frame_xpath='', css='')
    again = load_store(None, str(tmp_path / 's.json'))
    [row] = [c for c in again.candidates('k') if c['xpath'] == '//input']
    assert not row.get('frameXPath') and not row.get('css')
    store.add_candidate('k', '//input', source='heal', frame_xpath='iframe[id="g"]')
    store.add_candidate('k', '//input', source='heal')         # None: leaves it where it is
    [row] = [c for c in store.candidates('k') if c['xpath'] == '//input']
    assert row['frameXPath'] == 'iframe[id="g"]'


def test_frame_selectors_match_the_src_whole_and_never_use_the_bare_tag():
    from mendwright.drivers.base import frame_selector
    tried = []

    def count(selector):
        tried.append(selector)
        return 1 if selector.startswith('iframe[src') else 2

    assert frame_selector({'tag': 'iframe', 'src': '/embed?token=abc'}, count) == 'iframe[src^="/embed?"]'
    assert frame_selector({'tag': 'iframe', 'src': '/embed'}, count) == 'iframe[src="/embed"]'
    assert frame_selector({'tag': 'iframe', 'src': ''}, count) is None
    assert 'iframe' not in tried and not any('token' in t for t in tried)



def test_a_store_with_the_0_5_signature_still_saves_iframe_heals():
    from mendwright.client import add_heal

    class Store05:
        def __init__(self):
            self.rows = []

        def add_candidate(self, key, xpath, *, source='heal', frame_xpath=None, now=None):
            self.rows.append((key, xpath, frame_xpath))

    store = Store05()
    assert add_heal(store, 'k', '//b', css='', frame_xpath='iframe[id="f"]') is True
    assert store.rows == [('k', '//b', 'iframe[id="f"]')]


def test_another_process_does_not_undo_a_move_to_the_page(tmp_path):
    from mendwright.store import load_store
    path = str(tmp_path / 's.json')
    first = load_store(None, path)
    first.add_candidate('k', '//b', source='heal', frame_xpath='iframe[id="f"]', css='b.x')
    a, b = load_store(None, path), load_store(None, path)
    a.add_candidate('k', '//b', source='heal', frame_xpath='', css='')   # healed on the page
    b.add_candidate('other', '//c', source='heal')                        # an unrelated write
    [row] = [c for c in load_store(None, path).candidates('k') if c['xpath'] == '//b']
    assert not row.get('frameXPath') and not row.get('css')


def test_a_seed_row_moved_to_the_page_stays_moved(tmp_path):
    from mendwright.store import load_store
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema': 2, 'selectors': {'k': {
        'expectedRole': 'checkbox', 'candidates': [
            {'xpath': '//b', 'source': 'seed', 'status': 'active', 'frameXPath': 'iframe[id="f"]'}]}}}),
        encoding='utf-8')
    path = str(tmp_path / 'o.json')
    load_store(str(seed), path).add_candidate('k', '//b', source='heal', frame_xpath='')
    second = load_store(str(seed), path)
    second.add_candidate('other', '//c', source='heal')                   # a later write
    [row] = [c for c in load_store(str(seed), path).candidates('k') if c['xpath'] == '//b']
    assert not row.get('frameXPath')



# --- a quarantine belongs to the page or to the iframe it was made in ----------------------

AGREE = "//input[@name='agree']"
PAGE_WITH_AGREE = ("<main><h1>Sign up</h1><label><input type='checkbox' name='agree'> I agree"
                   "</label></main>")


def _quarantined_in_frame(tmp_path, stub_provider):
    """Run 1 follows '#inner-terms' into the iframe; run 2, after a redesign there, heals it
    inside the iframe to AGREE, and that heal's action fails, so it is quarantined."""
    first = _open(TwoDocs(INNER), tmp_path, stub_provider(None))
    first.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    first.coordinator.confirm_pending('terms', True)
    second = _open(TwoDocs(INNER.replace('inner-terms', 'v2')), tmp_path, stub_provider(AGREE),
                   fingerprint=False)
    second.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    second.coordinator.confirm_pending('terms', False)
    assert _saved(tmp_path, 'terms')['xpath=' + AGREE]['status'] == 'quarantined'


def test_a_quarantine_inside_an_iframe_does_not_block_the_page(tmp_path, stub_provider):
    _quarantined_in_frame(tmp_path, stub_provider)
    # The iframe is gone and the form is on the page: the same XPath is fine there.
    page = Doc(PAGE_WITH_AGREE)
    provider = stub_provider(AGREE)
    heal = _open(page, tmp_path, provider, fingerprint=False)
    heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
    assert page.actions == [('check', AGREE, ())]
    rows = _saved(tmp_path, 'terms')
    assert rows[AGREE]['status'] == 'active' and not rows[AGREE].get('frameXPath')
    assert rows['xpath=' + AGREE]['status'] == 'quarantined'


def test_a_quarantine_inside_an_iframe_still_blocks_that_element_there(tmp_path, stub_provider):
    _quarantined_in_frame(tmp_path, stub_provider)
    provider = stub_provider("(//input[@name='agree'])[1]")      # the same element, respelled
    heal = _open(TwoDocs(INNER.replace('inner-terms', 'v2')), tmp_path, provider,
                 fingerprint=False)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0)
    assert 'quarantined' in str(info.value)


def test_a_quarantine_on_the_page_does_not_block_an_iframe():
    from mendwright.client import apply

    class Store:
        def __init__(self):
            self.rows = [{'xpath': AGREE, 'status': 'quarantined', 'source': 'heal'}]
            self.added = []

        def candidates(self, key):
            return []

        def quarantined(self, key, frame_xpath=None):
            return [r['xpath'] for r in self.rows
                    if frame_xpath is None or (r.get('frameXPath') or '') == frame_xpath]

        def status(self, key, xpath):
            return next((r['status'] for r in self.rows if r['xpath'] == xpath), None)

        def add_candidate(self, key, xpath, **kw):
            self.added.append((xpath, kw.get('frame_xpath')))

    meta = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}
    store = Store()
    proposal = {'xpath': "(//input[@name='agree'])[1]", 'fallbacks': []}
    applied, _ = apply(store, 'terms', proposal, INNER, meta, frame_xpath=FRAME)
    assert applied == "xpath=(//input[@name='agree'])[1]"
    assert store.added == [(applied, FRAME)]
    applied, reason = apply(store, 'terms', proposal, PAGE_WITH_AGREE, meta)
    assert applied is None and 'quarantined' in reason


def test_a_store_whose_quarantined_takes_no_place_still_blocks_everywhere():
    from mendwright.client import _quarantined_paths

    class OldStore:
        def quarantined(self, key):
            return [AGREE]

    _doc, paths = _quarantined_paths(OldStore(), 'terms', PAGE_WITH_AGREE, FRAME)
    assert len(paths) == 1


def test_the_store_lists_the_quarantines_of_one_place(tmp_path):
    from mendwright.store import load_store
    store = load_store(None, str(tmp_path / 's.json'))
    store.add_candidate('k', '//a', source='heal')
    store.add_candidate('k', 'xpath=//a', source='heal', frame_xpath=FRAME)
    store.quarantine('k', '//a')
    store.quarantine('k', 'xpath=//a')
    assert store.quarantined('k') == ['//a', 'xpath=//a']
    assert store.quarantined('k', frame_xpath='') == ['//a']
    assert store.quarantined('k', frame_xpath=FRAME) == ['xpath=//a']



def test_an_iframe_quarantine_saved_by_0_6_0_no_longer_blocks_the_page(tmp_path, stub_provider):
    # 0.6.0 saved a heal inside an iframe as the plain XPath. Reading the store respells it,
    # so its quarantine stays in the iframe.
    (tmp_path / 'o.json').write_text(json.dumps({'schema': 2, 'selectors': {'terms': {
        'expectedRole': 'checkbox', 'candidates': [
            {'xpath': '#inner-terms', 'source': 'code', 'status': 'active'},
            {'xpath': AGREE, 'source': 'heal', 'status': 'quarantined', 'frameXPath': FRAME}]}}}),
        encoding='utf-8')
    page = Doc(PAGE_WITH_AGREE)
    heal = _open(page, tmp_path, stub_provider(AGREE), fingerprint=False)
    heal.locator('terms', '#inner-terms', role='checkbox', timeout_ms=0, poll_ms=0).check()
    assert page.actions == [('check', AGREE, ())]
    rows = _saved(tmp_path, 'terms')
    assert rows[AGREE]['status'] == 'active' and not rows[AGREE].get('frameXPath')
    assert rows['xpath=' + AGREE]['status'] == 'quarantined'
    assert rows['xpath=' + AGREE]['frameXPath'] == FRAME


def test_respelling_leaves_other_rows_alone():
    from mendwright.store import _respell_frame_heals
    data = {'schema': 2, 'selectors': {'k': {'candidates': [
        {'xpath': '//a', 'source': 'heal', 'frameXPath': FRAME},
        {'xpath': '//b', 'source': 'code', 'frameXPath': FRAME},
        {'xpath': '//c', 'source': 'heal'},
        {'xpath': 'css=#d', 'source': 'heal', 'frameXPath': FRAME},
        {'xpath': '//e', 'source': 'heal', 'frameXPath': FRAME},
        {'xpath': 'xpath=//e', 'source': 'heal', 'frameXPath': FRAME}]}}}
    rows = _respell_frame_heals(data)['selectors']['k']['candidates']
    assert [r['xpath'] for r in rows] == ['xpath=//a', '//b', '//c', 'css=#d', '//e', 'xpath=//e']


def test_xpath_compilation_is_cached():
    from mendwright import selectors
    assert hasattr(selectors._compile_xpath, 'cache_info')
