"""Heals of an element inside an open shadow root, through mendwright.open() on the fakes of
tests/conftest.py (ShadowPage, Playwright-shaped, and ShadowDriver, Selenium-shaped), which
run the real drivers. lxml has no shadow roots; those fakes keep each shadow root's content
in an lxml document of its own, so the page's HTML has none of it, as in a browser.

The safety rules each have a test that fails without the rule: a key last found inside a
shadow root heals only there, never onto a look-alike on the page outside its host; when that
shadow root cannot be entered, the heal is refused, not run on the page; a selector saved
inside a shadow root matches only there; a quarantine counts in its own place; and the follow
of a key's own selector into a shadow root refuses what it cannot tell apart.
"""

import asyncio
import json

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, facade
from mendwright.drivers.base import PLACE_JS, SHADOW_HTML_JS
from mendwright.journal import Journal, NullJournal
from mendwright.store import load_store


def shadow(inner, host='<my-app id="root">', mode='open', light=''):
    """A host with a declarative shadow root (see conftest.ShadowDOM), and light DOM after
    it."""
    tag = host[1:].split()[0].rstrip('>')
    return (host + '<template shadowrootmode="' + mode + '">' + inner + '</template>' + light
            + '</' + tag + '>')


def page(body):
    return '<html><head><title>t</title></head><body>' + body + '</body></html>'


# The Save button lives in the shadow root of <my-app id="root">. Outside the host, on the
# page, there is a look-alike with the same test id and the same text. A heal that looked at
# the page's own HTML would find only the look-alike, and it scores as the recorded element.
LOOKALIKE = '<button data-testid="save" type="button">Save</button>'
SAVE_V1 = '<div class="panel"><button id="save" data-testid="save" type="button">Save</button></div>'
SAVE_V2 = '<div class="panel"><button id="save-2" data-testid="save" type="button">Save</button></div>'
V1 = page(LOOKALIKE + shadow(SAVE_V1))
V2 = page(LOOKALIKE + shadow(SAVE_V2))
HOST = 'my-app[id="root"]'
CHAIN = HOST + ' >> [data-testid="save"]'


def _open(target, tmp_path, **kwargs):
    kwargs.setdefault('provider', None)
    kwargs.setdefault('journal', NullJournal())
    return mendwright.open(target, store_path=str(tmp_path / 'o.json'), timeout_ms=0, **kwargs)


def _store(tmp_path):
    return json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))


def _rows(tmp_path, key='save'):
    return _store(tmp_path)['selectors'][key]['candidates']


def _recorded(make_shadow_page, tmp_path, v1=V1, selector='#save'):
    """A ShadowPage on v1 where 'save' matched once, so its fingerprint and hosts are saved."""
    page_ = make_shadow_page(v1)
    _open(page_, tmp_path).locator('save', selector, role='button', poll_ms=0)
    return page_


def _clicked(page_):
    return page_.actions[-1][0] if page_.actions else None


# --- where the element is -------------------------------------------------------------------

def test_a_match_inside_a_shadow_root_records_its_hosts(make_shadow_page, tmp_path):
    page_ = make_shadow_page(V1)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert heal.store.shadow_hosts('save') == [HOST]
    assert _store(tmp_path)['selectors']['save']['shadowHosts'] == [HOST]
    # The fingerprint is saved without them, and meta() keeps its keys.
    assert 'shadowHosts' not in heal.store.meta('save')['fingerprint']
    assert 'shadowHosts' not in heal.store.meta('save')


def test_a_match_on_the_page_drops_the_hosts_again(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(page('<button id="save" type="button">Save</button>'))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    assert heal.store.shadow_hosts('save') == []
    assert 'shadowHosts' not in _store(tmp_path)['selectors']['save']


def test_nested_shadow_roots_record_every_host_outermost_first(make_shadow_page, tmp_path):
    inner = shadow('<button id="deep" data-testid="deep">Deep</button>',
                   host='<settings-panel>')
    page_ = make_shadow_page(page(shadow(inner)))
    heal = _open(page_, tmp_path)
    heal.locator('deep', '#deep', role='button', poll_ms=0)
    assert heal.store.shadow_hosts('deep') == [HOST, 'settings-panel']


def test_a_host_nothing_tells_apart_is_recorded_as_none(make_shadow_page, tmp_path):
    two = page(shadow(SAVE_V1, host='<div>') + '<div></div>')
    page_ = make_shadow_page(two)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    assert heal.store.shadow_hosts('save') == [None]


# --- a heal inside the shadow root --------------------------------------------------------------

def test_a_key_last_found_inside_a_shadow_root_heals_there_and_not_on_the_look_alike(
        make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert _clicked(page_).get('id') == 'save-2'       # the button inside the shadow root
    assert heal.heals[-1].new == CHAIN and heal.heals[-1].source == 'fingerprint'
    row = [c for c in _rows(tmp_path) if c['source'] == 'heal'][0]
    assert row['xpath'] == CHAIN and row['shadowHosts'] == [HOST]
    # The tier saw the shadow root's HTML and checked the CSS inside it, through its hosts.
    assert SHADOW_HTML_JS in page_.scripts and PLACE_JS in page_.scripts


def test_without_the_shadow_scope_the_heal_would_pick_the_look_alike(make_shadow_page, tmp_path,
                                                                    monkeypatch):
    # What the rule above prevents: healed on the page, the key goes to the look-alike.
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    monkeypatch.setattr(facade, 'heal_place', lambda store, key: ('', (), ''))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert heal.heals[-1].new == "//button[@data-testid='save']"
    assert page_.dom.owner[_clicked(page_)] is page_.dom.document


def test_a_heal_inside_nested_shadow_roots_stays_in_the_inner_one(make_shadow_page, tmp_path):
    def nested(button):
        # A look-alike in the outer shadow root, and the button in the inner one.
        inner = shadow(button, host='<settings-panel>')
        return page(LOOKALIKE + shadow(LOOKALIKE + inner))
    page_ = _recorded(make_shadow_page, tmp_path, v1=nested(
        '<button id="save" data-testid="save" type="button">Save</button>'))
    page_.set_html(nested('<button id="save-2" data-testid="save" type="button">Save</button>'))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert _clicked(page_).get('id') == 'save-2'
    assert heal.heals[-1].new == HOST + ' >> settings-panel >> [data-testid="save"]'


def test_a_test_id_reused_in_a_nested_shadow_root_does_not_hide_the_element(make_shadow_page,
                                                                            tmp_path):
    # A component inside the shadow root has a button with the same test id in its own
    # shadow root. Playwright's CSS pierces into it, so [data-testid="save"] matches 2 there:
    # it says nothing of whether the renamed button is visible, and #save-2 does. The other
    # Save button in the shadow root must not take its place.
    def app(ident):
        return page(shadow(
            '<button type="button" id="' + ident + '" data-testid="save">Save</button>'
            '<button type="button" id="footer-save" class="link">Save</button>'
            + shadow('<button type="button" id="bar-save" data-testid="save">Save</button>',
                     host='<save-bar>')))
    page_ = _recorded(make_shadow_page, tmp_path, v1=app('save'))
    page_.set_html(app('save-2'))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert _clicked(page_).get('id') == 'save-2'
    assert heal.heals[-1].new == HOST + ' >> #save-2'


def test_a_heal_inside_a_shadow_root_stays_there_after_a_0_9_0_process_wrote_the_store(
        make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0).click()
    # A 0.9.0 process runs the heal's chain, as Playwright does, and writes the store: it
    # drops every shadowHosts, the key's and the heal's.
    data = _store(tmp_path)
    entry = data['selectors']['save']
    entry.pop('shadowHosts', None)
    for row in entry['candidates']:
        row.pop('shadowHosts', None)
        row['lastSucceededAt'] = ('2026-01-02T00:00:00+00:00' if row['xpath'] == CHAIN
                                  else '2026-01-01T00:00:00+00:00')
    (tmp_path / 'o.json').write_text(json.dumps(data), encoding='utf-8')
    # The button is renamed again; outside the host the look-alike keeps the old test id.
    page_.set_html(page(LOOKALIKE + shadow(
        '<button id="save-3" data-testid="save-btn" type="button">Save</button>')))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert page_.dom.owner[_clicked(page_)] is not page_.dom.document
    assert heal.heals[-1].new == HOST + ' >> [data-testid="save-btn"]'


@pytest.mark.parametrize('v2, why', [
    (page(LOOKALIKE), 'is not on the page, matches more than one element'),
    (page(LOOKALIKE + shadow(SAVE_V2) + shadow(SAVE_V2)), 'matches more than one element'),
    (page(LOOKALIKE + shadow(SAVE_V2, mode='closed')), 'or has a closed shadow root'),
], ids=['host-gone', 'two-hosts', 'closed-root'])
def test_a_shadow_root_that_cannot_be_entered_is_never_healed_on_the_page(
        make_shadow_page, tmp_path, stub_provider, v2, why):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(v2)
    provider = stub_provider("//button[@data-testid='save']")   # would find the look-alike
    heal = _open(page_, tmp_path, provider=provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=0)
    message = str(info.value)
    assert "'save' was last found inside the shadow root of " + HOST in message
    assert why in message and 'never on the page outside it' in message
    assert provider.bundles == [] and heal.heals == [] and page_.actions == []
    assert [c['source'] for c in _rows(tmp_path)] == ['code']


def test_a_host_nothing_tells_apart_refuses_every_heal(make_shadow_page, tmp_path, stub_provider):
    two = page(LOOKALIKE + shadow(SAVE_V1, host='<div>') + '<div></div>')
    page_ = _recorded(make_shadow_page, tmp_path, v1=two)
    page_.set_html(page(LOOKALIKE + shadow(SAVE_V2, host='<div>') + '<div></div>'))
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(page_, tmp_path, provider=provider)
    with pytest.raises(AttentionRequired, match='whose host nothing tells apart'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert provider.bundles == [] and page_.actions == []


def test_a_shadow_root_inside_an_iframe_is_refused(make_shadow_page, tmp_path, stub_provider):
    path = tmp_path / 'o.json'
    path.write_text(json.dumps({'schema': 2, 'selectors': {'save': {
        'expectedRole': 'button', 'shadowHosts': [HOST], 'candidates': [
            {'xpath': "xpath=//button[@id='gone']", 'source': 'heal', 'status': 'active',
             'frameXPath': 'iframe[id="f"]', 'lastSucceededAt': '2026-10-01T00:00:00+00:00'}]}}}),
        encoding='utf-8')
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(make_shadow_page(V2), tmp_path, provider=provider)
    with pytest.raises(AttentionRequired, match='inside a shadow root in the iframe'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert provider.bundles == []


def test_a_model_heal_inside_a_shadow_root_gets_that_root_only(make_shadow_page, tmp_path,
                                                                stub_provider):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    provider = stub_provider("//button[@id='save-2']")
    heal = _open(page_, tmp_path, provider=provider, fingerprint=False)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    sent = provider.bundles[0]['html']
    assert 'save-2' in sent and 'Save</button>' in sent
    assert sent.count('data-testid="save"') == 1        # no look-alike from the page
    assert [row['id'] for row in provider.bundles[0]['inventory']] == ['save-2']
    assert heal.heals[-1].new == CHAIN and heal.heals[-1].source == 'model'
    assert _clicked(page_).get('id') == 'save-2'


def test_an_element_only_its_text_tells_apart_has_no_css_and_is_refused(make_shadow_page,
                                                                        tmp_path):
    def two(cls):
        return page(shadow('<button type="button"' + cls + '>Save</button>'
                           '<button type="button">Cancel</button>'))
    page_ = _recorded(make_shadow_page, tmp_path, v1=two(' class="primary"'), selector='.primary')
    page_.set_html(two(''))
    heal = _open(page_, tmp_path)
    with pytest.raises(AttentionRequired, match='no CSS selector tells its element apart'):
        heal.locator('save', '.primary', role='button', poll_ms=0)
    assert page_.actions == []


def test_a_heal_inside_a_shadow_root_must_be_visible_there(make_shadow_page, tmp_path,
                                                           stub_provider):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(page(LOOKALIKE + shadow(
        '<button id="save-2" data-testid="save" type="button" data-invisible>Save</button>')))
    provider = stub_provider("//button[@id='save-2']")
    heal = _open(page_, tmp_path, provider=provider, fingerprint=False)
    with pytest.raises(AttentionRequired, match='not uniquely present on the live page'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert page_.actions == []


def test_a_saved_shadow_heal_matches_nothing_outside_its_shadow_root(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    # The button leaves the shadow root; a look-alike sits in the host's light DOM, and one
    # more in another host. Playwright's CSS would reach both through the chain.
    page_.set_html(page(LOOKALIKE + shadow('<p>gone</p>', light=LOOKALIKE)))
    assert heal.present('save') is False
    with pytest.raises(AttentionRequired):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert len(page_.actions) == 0


# The component re-renders after the lookup: its button leaves the shadow root, and the page
# slots one with the same test id into the host's light DOM.
RERENDERED = page(shadow('<p>loading</p><slot></slot>', light=LOOKALIKE))


def test_an_action_after_a_re_render_never_reaches_outside_the_shadow_root(make_shadow_page,
                                                                          tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    proxy = heal.locator('save', '#save', role='button', poll_ms=0)    # healed inside it
    assert heal.heals[-1].new == CHAIN
    page_.set_html(RERENDERED)
    with pytest.raises(AttentionRequired, match=r"'save' was found inside the shadow root of "
                       r'my-app\[id="root"\], but when it came to click\(\)'):
        proxy.click()
    with pytest.raises(AttentionRequired, match=r'when it came to is_visible\(\)'):
        proxy.is_visible()
    assert page_.actions == []
    # Nothing says the heal was wrong: it stays active.
    assert [c['status'] for c in _rows(tmp_path) if c['xpath'] == CHAIN] == ['active']
    # While the button is inside the shadow root, the action runs.
    page_.set_html(V2)
    proxy.click()
    assert _clicked(page_).get('id') == 'save-2'


def test_the_async_healer_never_acts_outside_the_shadow_root_after_a_re_render(
        make_shadow_page, tmp_path):
    sync = make_shadow_page(V1)

    async def lookup():
        heal = await mendwright.aio.open(AsyncShadowPage(sync), provider=None, timeout_ms=0,
                                         store_path=str(tmp_path / 'o.json'),
                                         journal=NullJournal())
        return await heal.locator('save', '#save', role='button', poll_ms=0)

    asyncio.run(lookup())
    sync.set_html(V2)

    async def main():
        proxy = await lookup()
        sync.set_html(RERENDERED)
        await proxy.click()

    with pytest.raises(AttentionRequired, match=r'when it came to click\(\)'):
        asyncio.run(main())
    assert sync.actions == []


def test_two_matches_in_the_shadow_root_match_nothing(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(page(shadow(SAVE_V2 + SAVE_V2)))
    assert heal.present('save') is False


# --- quarantines count in their own place -------------------------------------------------------

def test_a_quarantined_shadow_heal_blocks_its_element_under_another_css_form(make_shadow_page,
                                                                           tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    heal.confirm_all(False)
    assert {c['xpath']: c['status'] for c in _rows(tmp_path)}[CHAIN] == 'quarantined'
    heal = _open(page_, tmp_path)
    with pytest.raises(AttentionRequired, match='previously quarantined'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    # Not saved as HOST >> #save-2 or HOST >> button[...] either.
    assert [c['xpath'] for c in _rows(tmp_path) if c['source'] == 'heal'] == [CHAIN]


def test_a_quarantine_on_the_page_does_not_block_the_shadow_root(make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('save', "//button[@data-testid='save']", source='heal')
    store.quarantine('save', "//button[@data-testid='save']")
    page_.set_html(V2)
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert heal.heals[-1].new == CHAIN and _clicked(page_).get('id') == 'save-2'


# --- drivers that do not reach into shadow roots -------------------------------------------------

class PlainDriver:
    """A custom driver: the Playwright driver of a ShadowPage without the shadow root reads."""

    def __init__(self, page_):
        from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
        self._d = PlaywrightSyncDriver(page_)

    def __getattr__(self, name):
        if name.startswith('shadow_'):
            raise AttributeError(name)
        return getattr(self._d, name)


def test_a_driver_that_does_not_reach_into_shadow_roots_never_heals_there(make_shadow_page,
                                                                         tmp_path, stub_provider):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(PlainDriver(page_), tmp_path, provider=provider)
    with pytest.raises(AttentionRequired, match='this driver does not reach into shadow roots'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert provider.bundles == [] and page_.actions == []


def test_a_driver_that_does_not_reach_into_shadow_roots_never_runs_a_shadow_heal(
        make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(page(LOOKALIKE.replace('type="button"', 'type="button" id="x"')))
    heal = _open(PlainDriver(page_), tmp_path)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert 'does not reach into shadow roots' in str(info.value)
    assert page_.actions == []


# --- Selenium -------------------------------------------------------------------------------------

def test_on_selenium_a_key_in_a_shadow_root_heals_there(make_shadow_driver, tmp_path):
    driver = make_shadow_driver(V1)
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_meta('save', role='button', shadow_hosts=[HOST])
    _open(make_shadow_driver(V1), tmp_path)    # nothing yet
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click(expect=lambda: True)
    heal.locator('save', '#save', role='button', poll_ms=0)    # reads the fingerprint
    assert heal.heals[0].source == 'shadow' and heal.store.shadow_hosts('save') == [HOST]
    driver.set_html(V2)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert heal.heals[-1].new == CHAIN and heal.heals[-1].source == 'fingerprint'
    assert driver.actions[-1][0].get('id') == 'save-2'


# --- a store a 0.9.0 process wrote -----------------------------------------------------------

def _as_0_9_0_writes_it(tmp_path, earlier=()):
    """The store as a 0.9.0 process writes it back: it keeps only the fields it knows, so
    every shadowHosts is gone, the key's and each candidate's. The selectors in earlier last
    matched in a run before (timestamps have a one second resolution)."""
    data = _store(tmp_path)
    for entry in data['selectors'].values():
        entry.pop('shadowHosts', None)
        for c in entry['candidates']:
            c.pop('shadowHosts', None)
            if c['xpath'] in earlier:
                c['lastSucceededAt'] = '2026-10-01T00:00:00+00:00'
    (tmp_path / 'o.json').write_text(json.dumps(data), encoding='utf-8')


def test_on_selenium_a_followed_key_0_9_0_wrote_back_still_heals_inside_its_shadow_root(
        make_shadow_driver, tmp_path):
    driver = make_shadow_driver(V1)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()     # followed in
    heal.confirm_all(True)
    _open(make_shadow_driver(V1), tmp_path).locator('save', '#save', role='button', poll_ms=0)
    _as_0_9_0_writes_it(tmp_path)
    assert 'shadowHosts' not in json.dumps(_store(tmp_path))
    # Selenium refuses the chain as a Playwright selector, and #save misses on the page.
    driver.set_html(V2)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert driver.actions[-1][0].get('id') == 'save-2'      # not the look-alike on the page
    assert heal.heals[-1].new == CHAIN and heal.heals[-1].source == 'fingerprint'
    row = [c for c in _rows(tmp_path) if c['xpath'] == CHAIN][0]
    assert row['shadowHosts'] == [HOST]


def test_on_playwright_a_heal_0_9_0_wrote_back_still_heals_inside_its_shadow_root(
        make_shadow_page, tmp_path):
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)    # CHAIN
    _as_0_9_0_writes_it(tmp_path, earlier=['#save'])
    page_.set_html(page(LOOKALIKE + shadow(SAVE_V2.replace('"save"', '"save-3"'))))
    heal = _open(page_, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert _clicked(page_).get('id') == 'save-2'
    assert page_.dom.owner[_clicked(page_)] is not page_.dom.document
    assert heal.heals[-1].new.startswith(HOST + ' >> ')


@pytest.mark.parametrize('row', [
    {'xpath': HOST + ' >> #save', 'source': 'code'},                 # your code passes it
    {'xpath': HOST + ' >> #save', 'source': 'heal', 'shadowHosts': []},   # on the page now
    {'xpath': HOST + ' >> #save', 'source': 'heal', 'frameXPath': 'iframe[id="f"]'},
    {'xpath': 'div >> #save', 'source': 'heal'},                     # not a host mendwright builds
    {'xpath': HOST + ' >> //button', 'source': 'heal'},              # XPath runs in no shadow root
    {'xpath': HOST + ' >> text=Save', 'source': 'heal'},
    {'xpath': "//button[@id='save']", 'source': 'heal'},
], ids=['code', 'page', 'iframe', 'div-host', 'xpath', 'engine', 'plain'])
def test_only_a_heal_mendwright_saved_inside_a_shadow_root_names_its_hosts(row):
    from mendwright.shadow import chain_hosts
    assert chain_hosts(row) == ()
    assert chain_hosts({'xpath': CHAIN, 'source': 'heal'}) == (HOST,)
    assert chain_hosts({'xpath': HOST + ' >> settings-panel >> .panel > button',
                        'source': 'heal'}) == (HOST, 'settings-panel')


# --- HealCoordinator used on its own ------------------------------------------------------------

def test_a_coordinator_used_on_its_own_never_heals_a_shadow_key_on_the_page(
        make_shadow_page, tmp_path, stub_provider):
    # The guide's low-level API: resolve_with_heal() over resolve(), with no facade to scope it.
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    store = load_store(None, str(tmp_path / 'o.json'))
    driver = mendwright.wrap(page_)
    provider = stub_provider("//button[@data-testid='save']")     # would find the look-alike
    journal = Journal(str(tmp_path / 'journal.jsonl'))
    coordinator = mendwright.HealCoordinator(
        store, provider=provider, driver=driver, html_provider=driver.outer_html,
        inventory_provider=driver.inventory, guard=lambda: True, fingerprint=True,
        journal=journal)
    with pytest.raises(AttentionRequired) as info:
        mendwright.resolve_with_heal(
            lambda: mendwright.resolve(store, page_, 'save', timeout_ms=0, poll_ms=0),
            coordinator, 'save', store.meta('save'), {'url': page_.url})
    message = str(info.value)
    assert "'save' was last found inside the shadow root of " + HOST in message
    assert 'mendwright.open() heals it inside that shadow root' in message
    assert coordinator.heals == [] and provider.bundles == [] and page_.actions == []
    assert [c['source'] for c in _rows(tmp_path)] == ['code']
    events = [json.loads(line) for line in
              (tmp_path / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [e['code'] for e in events if e['event'] == 'heal.declined'] == ['shadow']


def test_a_coordinator_scoped_to_the_shadow_root_still_heals_there(make_shadow_page, tmp_path,
                                                                  stub_provider):
    from mendwright.shadow import driver_shadowed
    page_ = _recorded(make_shadow_page, tmp_path)
    page_.set_html(V2)
    store = load_store(None, str(tmp_path / 'o.json'))
    driver = mendwright.wrap(page_)
    coordinator = mendwright.HealCoordinator(
        store, provider=stub_provider(), driver=driver, html_provider=driver.outer_html,
        inventory_provider=driver.inventory, guard=lambda: True, fingerprint=True)
    with coordinator.scoped(driver_shadowed(driver, [HOST]), None, shadow_hosts=[HOST]):
        applied = coordinator.try_heal('save', store.meta('save'), ['#save'], {'url': page_.url})
    assert applied == CHAIN


# --- the follow: a key's own selector that matches only inside a shadow root -----------------

def test_selenium_follows_a_css_selector_into_the_one_shadow_root_it_matches_in(
        make_shadow_driver, tmp_path, stub_provider):
    driver = make_shadow_driver(V1)
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(driver, tmp_path, provider=provider)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert driver.actions[-1][0].get('id') == 'save'
    assert heal.heals == [mendwright.HealEvent('save', ('#save',), HOST + ' >> #save', None,
                                               'shadow')]
    assert provider.bundles == []
    row = [c for c in _rows(tmp_path) if c['source'] == 'heal'][0]
    assert row['shadowHosts'] == [HOST]


def test_playwright_follows_an_xpath_into_the_shadow_root_through_a_css_form(make_shadow_page,
                                                                            tmp_path):
    page_ = make_shadow_page(V1)
    heal = _open(page_, tmp_path)
    heal.locator('save', "//button[@id='save']", role='button', poll_ms=0).click()
    assert heal.heals[-1].source == 'shadow' and heal.heals[-1].new == CHAIN
    assert _clicked(page_).get('id') == 'save'


@pytest.mark.parametrize('html, why', [
    (page(shadow(SAVE_V1) + shadow(SAVE_V1, host='<other-app>')),
     'it matches in more than one shadow root'),
    (page(shadow(SAVE_V1 + SAVE_V1)), 'it matches 2 elements there'),
    (page(shadow(SAVE_V1, host='<div>') + '<div></div>'),
     'nothing tells one of the shadow hosts on the way apart'),
], ids=['two-roots', 'two-elements', 'untold-host'])
def test_the_follow_refuses_what_it_cannot_tell_apart(make_shadow_driver, tmp_path,
                                                      stub_provider, html, why):
    driver = make_shadow_driver(html)
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(driver, tmp_path, provider=provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert "'save' is inside a shadow root: #save matches nothing on the page" in str(info.value)
    assert why in str(info.value) and 'did not ask a model' in str(info.value)
    assert provider.bundles == [] and driver.actions == [] and heal.heals == []


@pytest.mark.parametrize('kwargs, why', [
    ({'dry_run': True}, 'dry-run mode is on'),
    ({'destructive_keys': {'save'}}, "'save' is marked as a destructive control"),
    ({'guard': lambda: False}, 'a transaction guard is currently blocking heals'),
], ids=['dry-run', 'destructive', 'guard'])
def test_the_follow_keeps_the_rules_of_a_heal(make_shadow_driver, tmp_path, kwargs, why):
    driver = make_shadow_driver(V1)
    heal = _open(driver, tmp_path, **kwargs)
    with pytest.raises(AttentionRequired, match=why):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert driver.actions == [] and heal.heals == []


def test_the_follow_never_runs_a_quarantined_lookup_again(make_shadow_driver, tmp_path):
    driver = make_shadow_driver(V1)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)
    heal.confirm_all(False)
    with pytest.raises(AttentionRequired, match='that lookup is quarantined'):
        _open(driver, tmp_path).locator('save', '#save', role='button', poll_ms=0)


def test_the_follow_checks_the_live_element(make_shadow_driver, tmp_path):
    driver = make_shadow_driver(page(shadow(
        '<button id="save" type="button" data-invisible>Save</button>')))
    heal = _open(driver, tmp_path)
    with pytest.raises(AttentionRequired, match="its element there failed mendwright's checks"):
        heal.locator('save', '#save', role='button', poll_ms=0)


# The page's own submit button, and a newsletter component whose shadow root holds another
# one. 'send' is the page's button.
SEND = '<form><input name="email"><button type="submit" id="send">Send</button></form>'
NEWSLETTER = shadow('<button type="submit" id="subscribe">Subscribe</button>',
                    host='<news-letter>')
SUBMIT = 'button[type="submit"]'


def _sent(target):
    return target.actions[-1][0].get('id') if target.actions else None


def test_playwright_never_follows_a_css_selector_that_matches_on_the_page(make_shadow_page,
                                                                         tmp_path):
    # Playwright's CSS pierces: with the component added, the selector matches 2 elements,
    # one on the page. It is healed on the page, where 'send' was found, as 0.9.0 heals it.
    page_ = make_shadow_page(page(SEND))
    _open(page_, tmp_path).locator('send', SUBMIT, role='button', poll_ms=0)
    page_.set_html(page(SEND + NEWSLETTER))
    heal = _open(page_, tmp_path)
    heal.locator('send', SUBMIT, role='button', poll_ms=0).click()
    assert _sent(page_) == 'send'
    assert [(h.new, h.source) for h in heal.heals] == [("//button[@id='send']", 'fingerprint')]


def test_selenium_never_follows_a_selector_that_matches_several_elements_on_the_page(
        make_shadow_driver, tmp_path, stub_provider):
    driver = make_shadow_driver(page(SEND + '<button type="submit" id="reset">Reset</button>'
                                     + NEWSLETTER))
    provider = stub_provider(None)
    heal = _open(driver, tmp_path, provider=provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('send', SUBMIT, role='button', poll_ms=0)
    assert 'inside a shadow root' not in str(info.value)
    assert driver.actions == [] and heal.heals == []


def test_selenium_never_follows_a_key_another_of_whose_selectors_matches_on_the_page(
        make_shadow_driver, tmp_path):
    # .send matches two buttons on the page, and #send nothing there but one element in the
    # component's shadow root: the key's element may be one of the two on the page.
    driver = make_shadow_driver(page(
        '<button type="submit" id="send-1" class="send">Send</button>'
        '<button type="submit" id="send-2" class="send">Send</button>'
        + shadow('<button type="submit" id="send">Join</button>', host='<news-letter>')))
    heal = _open(driver, tmp_path)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('send', ['.send', '#send'], role='button', poll_ms=0)
    assert 'inside a shadow root' not in str(info.value)
    assert driver.actions == [] and heal.heals == []


def test_a_key_last_found_on_the_page_is_not_followed_into_a_shadow_root(make_shadow_driver,
                                                                        tmp_path):
    # The page's button is no longer a submit button, and the component's is the only one:
    # the selector matches nothing on the page and one element in the shadow root. 'send'
    # heals on the page, where it was last found.
    driver = make_shadow_driver(page(SEND))
    _open(driver, tmp_path).locator('send', SUBMIT, role='button', poll_ms=0)
    driver.set_html(page(SEND.replace('type="submit"', 'type="button"') + NEWSLETTER))
    heal = _open(driver, tmp_path)
    heal.locator('send', SUBMIT, role='button', poll_ms=0).click()
    assert _sent(driver) == 'send'
    assert [h.source for h in heal.heals] == ['fingerprint']


def test_a_key_last_found_in_one_shadow_root_is_not_followed_into_another(make_shadow_driver,
                                                                         tmp_path):
    driver = make_shadow_driver(V1)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0)     # followed into my-app's root
    assert [c['shadowHosts'] for c in _rows(tmp_path) if c['source'] == 'heal'] == [[HOST]]
    driver.set_html(page(shadow(SAVE_V1, host='<other-app>')))
    heal = _open(driver, tmp_path)
    with pytest.raises(AttentionRequired, match='cannot enter that shadow root'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert driver.actions == [] and heal.heals == []


# --- async Playwright -----------------------------------------------------------------------------

class AsyncShadowLocator:
    def __init__(self, loc):
        self._loc = loc

    def locator(self, selector):
        return AsyncShadowLocator(self._loc.locator(selector))

    def nth(self, index):
        return AsyncShadowLocator(self._loc.nth(index))

    async def count(self):
        return self._loc.count()

    async def is_visible(self):
        return self._loc.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        return None

    async def evaluate_all(self, js, arg=None):
        return self._loc.evaluate_all(js, arg)

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)

        async def run(*args, **kwargs):
            return action(*args, **kwargs)
        return run


class AsyncShadowPage:
    def __init__(self, sync):
        self.sync = sync
        self.frames = []

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector):
        return AsyncShadowLocator(self.sync.locator(selector))

    def frame_locator(self, selector):
        return self

    async def content(self):
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        return self.sync.evaluate(js, arg)


def test_the_async_healer_heals_inside_the_shadow_root_and_refuses_one_it_cannot_enter(
        make_shadow_page, tmp_path):
    sync = make_shadow_page(V1)

    async def main(html, store_dir):
        sync.set_html(html)
        heal = await mendwright.aio.open(AsyncShadowPage(sync), provider=None, timeout_ms=0,
                                         store_path=str(store_dir / 'o.json'),
                                         journal=NullJournal())
        await heal.locator('save', '#save', role='button', poll_ms=0).click()
        return heal

    asyncio.run(main(V1, tmp_path))
    heal = asyncio.run(main(V2, tmp_path))
    assert heal.heals[-1].new == CHAIN and _clicked(sync).get('id') == 'save-2'

    other = tmp_path / 'b'
    asyncio.run(main(V1, other))
    with pytest.raises(AttentionRequired, match='cannot enter that shadow root'):
        asyncio.run(main(page(LOOKALIKE), other))


def test_the_async_healer_follows_into_a_shadow_root(make_shadow_page, tmp_path):
    sync = make_shadow_page(V1)

    async def main():
        heal = await mendwright.aio.open(AsyncShadowPage(sync), provider=None, timeout_ms=0,
                                         store_path=str(tmp_path / 'o.json'),
                                         journal=NullJournal())
        await heal.locator('save', "//button[@id='save']", role='button', poll_ms=0).click()
        return heal

    heal = asyncio.run(main())
    assert heal.heals[-1].source == 'shadow' and _clicked(sync).get('id') == 'save'


def test_the_async_healer_never_follows_a_css_selector_that_matches_on_the_page(
        make_shadow_page, tmp_path):
    sync = make_shadow_page(page(SEND))

    async def main():
        heal = await mendwright.aio.open(AsyncShadowPage(sync), provider=None, timeout_ms=0,
                                         store_path=str(tmp_path / 'o.json'),
                                         journal=NullJournal())
        await heal.locator('send', SUBMIT, role='button', poll_ms=0).click()
        return heal

    asyncio.run(main())
    sync.set_html(page(SEND + NEWSLETTER))
    heal = asyncio.run(main())
    assert [h.source for h in heal.heals] == ['fingerprint'] and _sent(sync) == 'send'
