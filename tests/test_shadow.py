"""mendwright.shadow and css.css_xpath(): the host selectors, the chain a heal inside an open
shadow root is saved as, and the driver for the inside of one. No browser: the store and the
resolver are in tests/test_shadow_store.py, client.apply() in tests/test_shadow_apply.py, the
heals on the fakes of tests/conftest.py in tests/test_shadow_heal.py, and real browsers in
tests/browser/test_browser_shadow.py."""

import lxml.html
import pytest

from mendwright import shadow
from mendwright.css import css_selectors, css_xpath
from mendwright.fingerprint import FINGERPRINT_JS, HOST_ATTRS, from_raw, raw_of_element
from mendwright.candidates import TEST_ATTRS
from mendwright.selectors import InvalidSelector
from mendwright.shadow import ShadowHost

HOST = 'my-app[id="root"]'


# --- host selectors -----------------------------------------------------------------------------

def facts(tag='my-app', counts=None, **attrs):
    return {'tag': tag, 'attrs': attrs, 'counts': dict({'tag': 1}, **(counts or {}))}


def test_a_host_is_told_apart_by_its_test_id_then_id_then_name_then_its_custom_tag():
    assert shadow.host_selector(facts(**{'data-testid': 'app', 'id': 'root'},
                                      counts={'data-testid': 1, 'id': 1})) == 'my-app[data-testid="app"]'
    assert shadow.host_selector(facts(id='root', name='n', counts={'id': 1, 'name': 1})) == HOST
    assert shadow.host_selector(facts(name='n', counts={'name': 1})) == 'my-app[name="n"]'
    assert shadow.host_selector(facts()) == 'my-app'


def test_a_host_value_that_others_share_or_that_looks_generated_is_not_used():
    # Another element of its tag there has the same id: the next option is tried.
    assert shadow.host_selector(facts(id='root', counts={'id': 2})) == 'my-app'
    assert shadow.host_selector(facts(id='mui-generated-8842', counts={'id': 1})) == 'my-app'
    assert shadow.host_selector(facts(id='a', counts={'id': 1, 'tag': 2})) == 'my-app[id="a"]'


def test_a_bare_tag_is_used_only_for_a_custom_element_that_is_alone_there():
    assert shadow.host_selector(facts(tag='div')) is None
    assert shadow.host_selector(facts(counts={'tag': 2})) is None
    assert shadow.host_selector(facts(tag='Weird.Tag')) is None
    assert shadow.host_selector('nonsense') is None
    assert shadow.host_selector(facts(tag='div', id='x', counts={'id': 1})) == 'div[id="x"]'


def test_a_host_value_is_quoted_as_css():
    assert shadow.host_selector(facts(id='a"b\\c', counts={'id': 1})) == 'my-app[id="a\\"b\\\\c"]'


def test_the_host_attributes_are_the_test_ids_then_id_and_name():
    assert HOST_ATTRS == TEST_ATTRS + ('id', 'name')


def test_the_fingerprint_read_gives_the_hosts_and_from_raw_leaves_them_out():
    el = lxml.html.fromstring('<p><button id="b">Go</button></p>').find('.//button')
    raw = dict(raw_of_element(el), hosts=[facts(id='root', counts={'id': 1})])
    assert from_raw(raw) == from_raw(raw_of_element(el))
    assert shadow.with_hosts(from_raw(raw), raw)['shadowHosts'] == [HOST]
    assert shadow.with_hosts(from_raw(raw_of_element(el)), raw_of_element(el)) == from_raw(
        raw_of_element(el))
    assert "'value'" not in FINGERPRINT_JS and '.value' not in FINGERPRINT_JS
    assert 'hosts: hostsOf(el)' in FINGERPRINT_JS


def test_take_hosts_splits_a_read_into_the_fingerprint_and_where_it_was():
    fp = {'v': 1, 'tag': 'button', 'shadowHosts': [HOST]}
    assert shadow.take_hosts(fp) == ({'v': 1, 'tag': 'button'}, [HOST])
    assert shadow.take_hosts({'v': 1, 'tag': 'b', 'shadowHosts': []}) == ({'v': 1, 'tag': 'b'}, [])
    assert shadow.take_hosts({'v': 1, 'tag': 'b'})[1] is shadow.MISSING
    assert shadow.take_hosts(None) == (None, shadow.MISSING)


def test_a_malformed_saved_value_never_reads_as_the_page():
    assert shadow.saved_hosts(None) == shadow.saved_hosts([]) == shadow.saved_hosts('') == ()
    assert shadow.saved_hosts([HOST, None]) == (HOST, None)
    assert shadow.saved_hosts('my-app') == (None,)
    assert shadow.saved_hosts([HOST, 3, '  ']) == (HOST, None, None)


# --- the text a heal is saved with, and what runs ---------------------------------------------

def test_a_heal_inside_a_shadow_root_is_saved_as_the_playwright_chain():
    assert shadow.shadow_text([HOST, 'inner-box'], 'css=#save') == HOST + ' >> inner-box >> #save'


def test_split_text_keeps_quoted_and_bracketed_chains_whole():
    assert shadow.split_text('a[aria-label="x >> y"] >> #b') == ['a[aria-label="x >> y"]', '#b']
    assert shadow.split_text("my-app >> [title='a >> b']") == ['my-app', "[title='a >> b']"]
    assert shadow.split_text('#a') == ['#a']


def test_run_target_runs_the_css_inside_the_hosts():
    row = {'xpath': HOST + ' >> #save', 'shadowHosts': [HOST]}
    assert run(row) == ('#save', (ShadowHost(HOST),))
    assert run({'xpath': '//a', 'frameXPath': 'iframe[id="f"]'}) == ('//a', ('iframe[id="f"]',))
    assert run({'xpath': '#a'}) == ('#a', ())


def run(row):
    return shadow.run_target(row)


@pytest.mark.parametrize('row', [
    {'xpath': '#save', 'shadowHosts': [HOST]},                       # not the chain
    {'xpath': 'other-app >> #save', 'shadowHosts': [HOST]},         # another host
    {'xpath': HOST + " >> //button", 'shadowHosts': [HOST]},        # not CSS
    {'xpath': HOST + ' >> #save', 'shadowHosts': [None]},           # a host not saved
    {'xpath': "xpath=//a >> #save", 'shadowHosts': ['xpath=//a']},  # a host not CSS
], ids=['not-a-chain', 'other-host', 'xpath-inside', 'host-none', 'host-xpath'])
def test_a_shadow_row_that_does_not_add_up_cannot_run(row):
    with pytest.raises(InvalidSelector):
        shadow.run_target(row)


def test_a_chain_has_its_frames_first_and_no_iframe_inside_a_shadow_root():
    assert shadow.split_chain(('iframe[id="f"]', ShadowHost(HOST))) == (('iframe[id="f"]',),
                                                                       (HOST,))
    with pytest.raises(InvalidSelector, match='an iframe inside a shadow root'):
        shadow.split_chain((ShadowHost(HOST), 'iframe'))
    with pytest.raises(InvalidSelector):
        shadow.split_chain((ShadowHost(''),))
    with pytest.raises(InvalidSelector, match='only a CSS selector runs inside'):
        shadow.inner_css('//button')


class NoShadowDriver:
    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def handle(self, selector, frame_chain=()):
        return object()


# --- css.css_xpath -------------------------------------------------------------------------------

DOC = """<html><body><form id="f" class="card"><button id="save" class="primary big" type="button"
data-testid="save-btn" name="s" aria-label='Save "it" \\ now'>Save</button>
<button class="primary" type="button">Other</button><input name="q" placeholder="Find"></form>
<button class="primary">Out</button></body></html>"""


def test_css_xpath_matches_what_each_css_form_mendwright_builds_matches():
    doc = lxml.html.fromstring(DOC)
    cssselect = pytest.importorskip('lxml.cssselect')
    for el in doc.iter('button', 'input'):
        for css in css_selectors(el, doc, limit=10):
            xpath = css_xpath(css)
            assert xpath is not None, css
            assert doc.xpath(xpath) == cssselect.CSSSelector(css)(doc) == [el], css


def test_css_xpath_reads_only_the_forms_mendwright_builds():
    assert css_xpath('button.primary') == ("//button[contains(concat(' ', normalize-space(@class),"
                                           " ' '), ' primary ')]")
    assert css_xpath('#a [name="b"]') == "//*[@id='a']//*[@name='b']"
    assert css_xpath('[title="a\\"b"]') == "//*[@title='a\"b']"
    for other in ('button > a', 'a:hover', 'a,b', 'a  b', '', '*', '[x=y]', 3, None):
        assert css_xpath(other) is None, other


# --- the driver for the inside of a shadow root ------------------------------------------------------

class Base:
    def __init__(self):
        self.calls = []

    def count(self, selector, frame_chain=()):
        self.calls.append(('count', selector, frame_chain))
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def handle(self, selector, frame_chain=()):
        return None

    def shadow_html(self, hosts):
        return '<b>x</b>' if list(hosts) == [HOST] else None

    def shadow_inventory(self, hosts, limit=200):
        return [{'tag': 'b'}]


def test_a_shadow_driver_runs_every_call_inside_its_shadow_root():
    base = Base()
    driver = shadow.driver_shadowed(base, (HOST,))
    driver.count('#x')
    assert base.calls == [('count', '#x', (ShadowHost(HOST),))]
    assert driver.outer_html() == '<html><head></head><body><b>x</b></body></html>'
    assert driver.inventory() == [{'tag': 'b'}]
    assert driver.describe is None and driver.fingerprint is None   # the base has neither
    with pytest.raises(InvalidSelector):
        driver.count('#x', ('iframe',))
    assert not hasattr(driver, 'shadow_html')
    assert shadow.driver_shadowed(base, ('other-app',)).inner_html() is None
    assert shadow.driver_shadowed(base, ('other-app',)).outer_html() == ''
    assert shadow.driver_shadowed(base, (None,)) is None
    assert shadow.driver_shadowed(NoShadowDriver(), (HOST,)) is None


def test_the_place_of_a_shadow_root_in_words():
    assert shadow.place_text([HOST]) == 'the shadow root of ' + HOST
    assert shadow.place_text([HOST, 'inner-box']) == 'the shadow root of inner-box inside ' + HOST
    assert shadow.place_text([None]) == 'the shadow root of an element nothing tells apart'


def test_the_shadow_roots_read_is_taken_only_as_a_whole():
    roots, cut = shadow.roots_of({'roots': [{'hosts': [facts(id='root', counts={'id': 1})],
                                             'count': 2}], 'cut': False})
    assert roots == [{'hosts': [HOST], 'count': 2, 'html': None}] and cut is False
    assert shadow.roots_of(None) == ([], True)
    assert shadow.roots_of({'roots': [{'hosts': 'x'}]}) == ([], True)
