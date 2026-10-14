"""heal.page and heal.driver: an existing script heals by changing one line.

Offline: conftest's FakePage for sync Playwright, an async wrapper over it, and a small
lxml-backed WebDriver for Selenium. The stub provider stands in for the model.
"""

import asyncio
import re

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.proxies import (AsyncHealingPage, HealingDriver, HealingElement, HealingLocator,
                                HealingPage, selenium_selector, unwrap)

V1 = ("<form><input id='email' name='email'><label><input type='checkbox' id='terms'> I agree"
      "</label><button id='go' type='button' title='Go now'>Go</button>"
      "<select id='color'><option value='r'>Red</option></select></form>")
V2 = ("<form><input id='mail' name='email'><label><input type='checkbox' id='terms-v2'> I agree"
      "</label><button id='go' type='button' title='Go now'>Go</button></form>")
HEALED = "//input[@type='checkbox']"


class Page:
    """FakePage with Playwright's page-level is_visible(), which conftest's lacks."""

    def __new__(cls, make_page, html):
        page = make_page(html)
        page.visible_calls = []

        def is_visible(selector, **kwargs):
            page.visible_calls.append(selector)
            return page.locator('css=' + selector).is_visible()
        page.is_visible = is_visible
        return page


def _open(page, tmp_path, provider=None, **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'),
                           fingerprint=False, **kwargs)


# --- sync Playwright --------------------------------------------------------------------------------

def test_heal_page_stands_in_for_the_page(make_page, tmp_path):
    page = make_page(V1)
    heal = _open(page, tmp_path)
    proxy = heal.page
    assert type(proxy) is HealingPage and heal.page is proxy
    assert isinstance(proxy, type(page))          # isinstance checks for the page still pass
    assert proxy.native is page and unwrap(proxy) is page
    assert proxy.url == page.url and proxy.content() == page.content()
    proxy.url = 'https://example.test/other'      # attributes are set on the real page
    assert page.url == 'https://example.test/other'
    assert isinstance(heal.driver, PlaywrightSyncDriver)   # as before on Playwright
    again = mendwright.open(proxy, store_path=str(tmp_path / 'b.json'), fingerprint=False)
    assert again._page is page                    # open() unwraps a proxy


def test_page_locator_acts_through_heal_locator(make_page, tmp_path, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(HEALED))
    heal.page.locator('#terms').check()
    assert page.actions[-1][:2] == ('css=#terms', 'check')
    assert [c['xpath'] for c in heal.store.candidates('#terms')] == ['#terms']
    assert heal.store.meta('#terms')['expectedRole'] == 'checkbox'

    page.set_html(V2)                             # the redesign
    loc = heal.page.locator('#terms')
    assert type(loc) is HealingLocator
    loc.check(timeout=50)
    assert page.actions[-1][:2] == ('xpath=' + HEALED, 'check')
    assert heal.heals[-1].key == '#terms' and heal.heals[-1].new == HEALED


def test_the_page_shortcuts_heal_and_pass_their_arguments(make_page, tmp_path, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(None))
    p = heal.page
    p.fill('#email', 'ada@example.com', strict=True, timeout=100)
    p.click('#go', button='right')
    p.dblclick('#go')
    p.check('#terms')
    p.uncheck('#terms')
    p.hover('#go')
    p.press('#email', 'Enter')
    p.type('#email', 'x')
    p.select_option('#color', 'r')
    assert [(a[0], a[1], a[2], a[3]) for a in page.actions] == [
        ('css=#email', 'fill', ('ada@example.com',), {'timeout': 100}),   # strict= dropped
        ('css=#go', 'click', (), {'button': 'right'}),
        ('css=#go', 'dblclick', (), {}),
        ('css=#terms', 'check', (), {}),
        ('css=#terms', 'uncheck', (), {}),
        ('css=#go', 'hover', (), {}),
        ('css=#email', 'press', ('Enter',), {}),
        ('css=#email', 'type', ('x',), {}),
        ('css=#color', 'select_option', ('r',), {}),
    ]
    assert p.get_attribute('#go', 'title') == 'Go now'
    assert p.inner_text('#go') == 'Go'
    assert p.locator('#go').get_attribute('title') == 'Go now'


def test_reads_and_unlisted_methods_of_a_locator_are_not_healed(make_page, tmp_path,
                                                                 stub_provider):
    page = make_page(V1)
    provider = stub_provider(HEALED)
    heal = _open(page, tmp_path, provider)
    assert heal.page.locator('#nothing').count() == 0       # the real locator, no wait
    assert heal.store.candidates('#nothing') == []
    assert provider.bundles == []


def test_options_and_selectors_mendwright_cannot_store_pass_through(make_page, tmp_path):
    page = make_page(V1)
    real_locator = page.locator
    page.locator = lambda selector, **kwargs: real_locator(selector)   # takes has_text=
    heal = _open(page, tmp_path)
    plain = heal.page.locator('#go', has_text='Go')
    assert type(plain).__name__ == 'FakeLocator'
    assert type(heal.page.locator('//input[')).__name__ == 'FakeLocator'
    clicked = []
    page.click = lambda selector, **kwargs: clicked.append((selector, kwargs))   # its own click
    heal.page.click('//input[', strict=True)
    assert clicked == [('//input[', {'strict': True})]
    assert heal.store.candidates('//input[') == []


def test_is_visible_never_waits_or_heals(make_page, tmp_path, stub_provider):
    page = Page(make_page, V1)
    provider = stub_provider(HEALED)
    heal = _open(page, tmp_path, provider, timeout_ms=5000)
    assert heal.page.is_visible('#go') is True                # not a key yet: the page answers
    assert page.visible_calls == ['#go']
    heal.page.check('#terms')
    page.set_html(V2)
    assert heal.page.is_visible('#terms') is False            # broken, and no heal is tried
    assert provider.bundles == []
    heal.page.check('#terms', timeout=1)                      # an action heals it
    assert heal.page.is_visible('#terms') is True             # now through the saved heal
    assert heal.page.locator('#terms').is_visible() is True
    assert page.visible_calls == ['#go']


def test_is_visible_does_not_scroll_or_write_the_store(make_page, tmp_path, monkeypatch):
    page = Page(make_page, V1)
    heal = _open(page, tmp_path)
    heal.page.check('#terms')
    store_file = tmp_path / 'o.json'
    before = store_file.read_bytes()
    scrolls = []
    locator_type = type(page.locator('#x'))
    monkeypatch.setattr(locator_type, 'scroll_into_view_if_needed',
                        lambda self, **kwargs: scrolls.append(self._selector), raising=False)
    assert heal.page.is_visible('#terms') is True
    assert heal.page.locator('#terms').is_visible() is True
    assert scrolls == [] and store_file.read_bytes() == before


def test_playwright_timeout_zero_means_the_healer_s_own_timeout():
    from mendwright.proxies import _timeout_ms
    assert _timeout_ms({'timeout': 0}) is None        # no limit in Playwright, not "give up now"
    assert _timeout_ms({'timeout': 2500}) == 2500
    assert _timeout_ms({}) is None and _timeout_ms({'timeout': True}) is None


def test_a_step_that_cannot_heal_raises_attention_required(make_page, tmp_path, stub_provider):
    page = make_page(V1)
    heal = _open(page, tmp_path, stub_provider(None))
    heal.page.click('#go')
    page.set_html('<form></form>')
    with pytest.raises(AttentionRequired, match="No matching element for: #go"):
        heal.page.click('#go')


HIDDEN = ("<form><div hidden><input type='hidden' id='csrf' name='csrf' value='tok'></div>"
          "<div id='tpl' style='display:none' title='template'>x</div>"
          "<ul><li class='i'>one</li><li class='i'>two</li></ul>"
          "<button id='go' type='button' title='Go now'>Go</button></form>")


def _raw_selectors(page):
    """Let the fake page run a selector as written ('#id', '//xpath'), as Playwright does
    for page.locator(selector) and the page shortcuts on the real page."""
    query = page.query

    def run(selector):
        if selector.startswith(('//', '(')):
            selector = 'xpath=' + selector
        elif selector.startswith('#'):
            selector = 'css=' + selector
        return query(selector)
    page.query = run
    page.get_attribute = lambda selector, name, **kw: page.locator(selector).get_attribute(name)
    return page


def test_a_read_of_a_hidden_element_or_of_several_goes_to_the_real_page(make_page, tmp_path,
                                                                       stub_provider):
    # Playwright reads a hidden element's attribute or text; mendwright would wait for a
    # visible one and then try to heal, perhaps onto another element.
    page = _raw_selectors(Page(make_page, HIDDEN))
    provider = stub_provider(HEALED)
    heal = _open(page, tmp_path, provider, timeout_ms=5000)
    assert heal.page.locator('#tpl').get_attribute('title') == 'template'
    assert heal.page.get_attribute('#csrf', 'value') == 'tok'
    assert heal.page.locator('//li').inner_text() == 'one'     # the real locator's answer
    assert provider.bundles == []
    assert heal.store.candidates('#tpl') == heal.store.candidates('#csrf') == []
    # A visible match still goes through heal.locator(), which records it.
    assert heal.page.get_attribute('#go', 'title') == 'Go now'
    assert [c['xpath'] for c in heal.store.candidates('#go')] == ['#go']


def test_an_action_on_a_hidden_element_still_heals(make_page, tmp_path, stub_provider):
    # click needs one visible element in Playwright too, so a selector that matches only a
    # hidden leftover of the redesign is healed, as heal.locator() does.
    page = _raw_selectors(Page(make_page, V1))
    provider = stub_provider("//button[@title='Go now']")
    heal = _open(page, tmp_path, provider)
    heal.page.click('#go')
    page.set_html("<form><button id='go' style='display:none'>Go</button>"
                  "<button type='button' title='Go now'>Go</button></form>")
    heal.page.click('#go')
    assert page.actions[-1][:2] == ("xpath=//button[@title='Go now']", 'click')
    assert len(provider.bundles) == 1
    # From now on a read finds the element through the saved heal, not the hidden copy.
    assert heal.page.get_attribute('#go', 'title') == 'Go now'
    assert len(provider.bundles) == 1


# --- async Playwright -------------------------------------------------------------------------------

class AsyncLocator:
    def __init__(self, page, selector, sync):
        self._page, self._sync = page, sync

    async def count(self):
        return self._sync.count()

    async def is_visible(self, **kwargs):
        return self._sync.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate_all(self, js):
        return self._sync.evaluate_all(js)

    def __getattr__(self, verb):
        action = getattr(self._sync, verb)

        async def run(*args, **kwargs):
            return action(*args, **kwargs)
        return run


class AsyncPage:
    def __init__(self, sync):
        self.sync = sync
        self.visible_calls = []

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector, **kwargs):
        return AsyncLocator(self, selector, self.sync.locator(selector))

    def frame_locator(self, selector):
        return self

    async def content(self):
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        return self.sync.evaluate(js, arg)

    async def is_visible(self, selector, **kwargs):
        self.visible_calls.append(selector)
        return self.sync.locator('css=' + selector).is_visible()


def test_async_heal_page(make_page, tmp_path, stub_provider):
    sync = make_page(V1)
    apage = AsyncPage(sync)
    provider = stub_provider(HEALED)

    async def main():
        heal = await mendwright.aio.open(apage, provider=provider, timeout_ms=0,
                                         store_path=str(tmp_path / 'o.json'), fingerprint=False)
        p = heal.page
        assert type(p) is AsyncHealingPage and isinstance(p, AsyncPage) and p.native is apage
        await p.fill('#email', 'ada', strict=True)
        await p.locator('#terms').check()
        visible_before = await p.is_visible('#go')
        sync.set_html(V2)
        hidden = await p.is_visible('#terms')
        await p.check('#terms')                 # heals
        visible_after = await p.locator('#terms').is_visible()
        title = await p.get_attribute('#go', 'title')
        text = await p.locator('#go').inner_text()
        return heal, visible_before, hidden, visible_after, title, text, await p.content()

    heal, before, hidden, after, title, text, content = asyncio.run(main())
    assert (before, hidden, after) == (True, False, True)
    assert (title, text) == ('Go now', 'Go')
    assert 'terms-v2' in content
    assert apage.visible_calls == ['#go']
    assert [a[:2] for a in sync.actions] == [('css=#email', 'fill'), ('css=#terms', 'check'),
                                             ('xpath=' + HEALED, 'check')]
    assert heal.heals[-1].key == '#terms' and len(provider.bundles) == 1


def test_async_reads_of_a_hidden_element_go_to_the_real_page(make_page, tmp_path,
                                                            stub_provider):
    sync = _raw_selectors(make_page(HIDDEN))
    apage = AsyncPage(sync)
    provider = stub_provider(HEALED)

    async def main():
        heal = await mendwright.aio.open(apage, provider=provider, timeout_ms=5000,
                                         store_path=str(tmp_path / 'o.json'), fingerprint=False)
        title = await heal.page.locator('#tpl').get_attribute('title')
        first = await heal.page.locator('//li').inner_text()
        go = await heal.page.locator('#go').get_attribute('title')
        return heal, title, first, go

    heal, title, first, go = asyncio.run(main())
    assert (title, first, go) == ('template', 'one', 'Go now')
    assert provider.bundles == [] and heal.store.candidates('#tpl') == []
    assert [c['xpath'] for c in heal.store.candidates('#go')] == ['#go']


# --- Selenium ---------------------------------------------------------------------------------------

_ATTR = re.compile(r'^\[(id|name)="((?:[^"\\]|\\.)*)"\]$')


class WebElement:
    def __init__(self, driver, el):
        self._driver, self.el = driver, el

    @property
    def tag_name(self):
        return self.el.tag

    def is_displayed(self):
        el = self.el
        if (el.get('type') or '').lower() == 'hidden':
            return False
        while el is not None:
            if 'display:none' in (el.get('style') or '').replace(' ', '') or el.get('hidden') is not None:
                return False
            el = el.getparent()
        return True

    def is_selected(self):
        return self.el.get('checked') is not None

    def get_attribute(self, name):
        return self.el.get(name)

    def click(self):
        self._driver.clicks.append(self.el.get('id'))


class WebDriver:
    """Enough of a Selenium WebDriver for SeleniumDriver: find_elements by XPath and by
    [id="..."] / [name="..."] / #id CSS, execute_script for describe and scrolling."""

    def __init__(self, html):
        self.doc = lxml.html.fromstring(html)
        self.switch_to = object()
        self.current_url = 'https://example.test/'
        self.clicks = []
        self.direct = []

    def set_html(self, html):
        self.doc = lxml.html.fromstring(html)

    @property
    def page_source(self):
        return lxml.html.tostring(self.doc, encoding='unicode')

    def find_elements(self, by, value):
        if by == 'xpath':
            found = self.doc.xpath(value)
        else:
            m = _ATTR.match(value)
            if m:
                found = self.doc.xpath('//*[@' + m.group(1) + '=$v]',
                                       v=re.sub(r'\\(.)', r'\1', m.group(2)))
            elif value.startswith('#'):
                found = self.doc.xpath('//*[@id=$v]', v=value[1:])
            else:
                found = []
        return [WebElement(self, e) for e in found]

    def find_element(self, by, value):
        self.direct.append((by, value))
        return 'direct'

    def execute_script(self, script, *args):
        if 'outerHTML' in script:
            el = args[0].el
            return {'tag': el.tag, 'html': lxml.html.tostring(el, encoding='unicode')}
        return None


def test_selenium_selectors_for_each_by():
    assert selenium_selector('xpath', '//a') == 'xpath=//a'
    assert selenium_selector('css selector', 'a.b') == 'css=a.b'
    assert selenium_selector('id', ':r0:') == 'css=[id=":r0:"]'
    assert selenium_selector('name', 'a"b\\c') == 'css=[name="a\\"b\\\\c"]'
    assert selenium_selector('link text', 'Docs') is None
    assert selenium_selector('id', '') is None and selenium_selector('id', None) is None


def test_heal_driver_heals_find_element(tmp_path, stub_provider):
    web = WebDriver(V1)
    heal = _open(web, tmp_path, stub_provider(HEALED))
    driver = heal.driver
    assert type(driver) is HealingDriver and isinstance(driver, WebDriver)
    assert driver.native is web and heal.page is web       # heal.page stays the driver here
    assert driver.find_element('id', 'email').get_attribute('name') == 'email'
    driver.find_element('xpath', "//button[@id='go']").click()
    assert web.clicks == ['go']
    assert driver.find_element('name', 'email').get_attribute('id') == 'email'
    assert driver.find_element('css selector', '#terms').get_attribute('type') == 'checkbox'
    assert heal.store.candidates('id=email')[0]['xpath'] == '[id="email"]'
    assert heal.store.candidates("xpath=//button[@id='go']")[0]['xpath'] == "//button[@id='go']"

    web.set_html(V2)                              # the checkbox lost its id
    healed = driver.find_element('css selector', '#terms')
    assert healed.get_attribute('id') == 'terms-v2'
    assert heal.heals[-1].key == 'css selector=#terms' and heal.heals[-1].new == HEALED


def test_heal_driver_passes_everything_else_through(tmp_path):
    web = WebDriver(V1)
    heal = _open(web, tmp_path)
    driver = heal.driver
    assert driver.find_element('link text', 'Go') == 'direct'
    assert web.direct == [('link text', 'Go')]
    assert len(driver.find_elements('xpath', '//input')) == 2
    assert driver.current_url == 'https://example.test/'
    with pytest.raises(AttentionRequired, match='No matching element for: id=missing'):
        driver.find_element('id', 'missing')


def test_a_missing_element_is_also_selenium_s_no_such_element(tmp_path):
    exceptions = pytest.importorskip('selenium.common.exceptions')
    heal = _open(WebDriver(V1), tmp_path)
    with pytest.raises(exceptions.NoSuchElementException) as info:
        heal.driver.find_element('id', 'missing')
    assert isinstance(info.value, AttentionRequired)
    assert str(info.value).startswith('No matching element for: id=missing')
    assert '-> ' in str(info.value)               # the reason mendwright did not heal


def test_find_element_keeps_selenium_s_first_match_and_hidden_elements(tmp_path, stub_provider):
    # Selenium's find_element returns a hidden element, or the first of several; healing
    # those would change what an unchanged script gets back.
    web = WebDriver(HIDDEN)
    provider = stub_provider(HEALED)
    driver = _open(web, tmp_path, provider, timeout_ms=5000).driver
    assert driver.find_element('xpath', '//li') == 'direct'
    assert driver.find_element('id', 'csrf') == 'direct'
    assert driver.find_element('css selector', '#tpl') == 'direct'
    assert web.direct == [('xpath', '//li'), ('id', 'csrf'), ('css selector', '#tpl')]
    assert provider.bundles == []
    assert driver.find_element('id', 'go').get_attribute('title') == 'Go now'   # healed path
    assert web.direct == [('xpath', '//li'), ('id', 'csrf'), ('css selector', '#tpl')]


# --- Selenium: interruptions on the elements heal.driver finds -------------------------------------

class ElementClickInterceptedException(Exception):
    """Named like Selenium's, which mendwright recognises by name."""


class NoSuchElementException(Exception):
    pass


class CoveredElement(WebElement):
    """A WebElement on CoveredDriver: a click outside a visible [data-overlay] element
    fails the way Chrome words it, and a click inside one closes it unless it has
    data-sticky. Every call is listed in driver.calls, one per browser round trip."""

    def _call(self, verb):
        drv = self._driver
        drv.calls.append(verb)
        if drv.during_action:
            drv.during_action.pop(0)(drv)

    def _overlay(self):
        """The [data-overlay] element this element is in, or None."""
        return next((a for a in self.el.iterancestors() if a.get('data-overlay') is not None),
                    None)

    def click(self):
        self._call('click')
        cover = self._driver.overlay()
        if cover is not None and self._overlay() is not cover:
            error = ElementClickInterceptedException(
                'element click intercepted: Element is not clickable at point (10, 10). Other '
                'element would receive the click')
            self._driver.raised.append(error)
            raise error
        self._driver.clicks.append(self.el.get('id'))
        mine = self._overlay()
        if mine is not None and mine.get('data-sticky') is None:
            mine.drop_tree()

    def send_keys(self, *value):
        self._call('send_keys')
        self.el.set('value', (self.el.get('value') or '') + ''.join(value))

    def clear(self):
        self._call('clear')
        self.el.set('value', '')

    def submit(self):
        self._call('submit')
        self._driver.submitted.append(self.el.get('id'))

    def find_element(self, by, value):
        self._driver.calls.append('find_element')
        found = self.el.xpath('.' + value) if by == 'xpath' else []
        if not found:
            raise NoSuchElementException(value)
        return CoveredElement(self._driver, found[0])

    def find_elements(self, by, value):
        self._driver.calls.append('find_elements')
        return [CoveredElement(self._driver, e) for e in self.el.xpath('.' + value)]


class CoveredDriver(WebDriver):
    """The lxml WebDriver with CoveredElements and link text lookups. calls lists every
    call that would reach the browser."""

    def __init__(self, html):
        super().__init__(html)
        self.calls = []
        self.raised = []
        self.submitted = []
        self.during_action = []     # callables run once each, inside the next actions

    def _found(self, by, value):
        if by == 'link text':
            return self.doc.xpath('//a[normalize-space()=$v]', v=value)
        return [e.el for e in WebDriver.find_elements(self, by, value)]

    def find_elements(self, by, value):
        self.calls.append('find_elements')
        return [CoveredElement(self, e) for e in self._found(by, value)]

    def find_element(self, by, value):
        self.calls.append('find_element')
        found = self._found(by, value)
        if not found:
            raise NoSuchElementException(value)
        return CoveredElement(self, found[0])

    def execute_script(self, script, *args):
        self.calls.append('execute_script')
        return super().execute_script(script, *args)

    def overlay(self):
        for el in self.doc.xpath('//*[@data-overlay]'):
            if WebElement(self, el).is_displayed():
                return el
        return None

    def show(self, html):
        self.doc.xpath('//body')[0].append(lxml.html.fragment_fromstring(html))


COOKIES = ("<div id='cc' data-overlay='' class='cookie-consent' role='dialog' "
           "aria-label='Cookie consent'><p>We use cookies.</p>"
           "<button id='cc-reject' type='button'>Reject all</button>"
           "<button id='cc-accept' type='button'>Accept all</button></div>")
CHECKOUT = ("<html><body><form id='f'><input id='email' name='email'>"
            "<a href='#terms' id='terms-link'>Terms</a>"
            "<input type='checkbox' class='opt' id='opt-a'>"
            "<input type='checkbox' class='opt' id='opt-b'>"
            "<button id='go' type='button'>Continue</button></form>{}</body></html>")


def _covered(banner=True):
    return CoveredDriver(CHECKOUT.format(COOKIES if banner else ''))


def _interrupts(heal):
    return [(e.key, e.before) for e in heal.handled_interruptions]


@pytest.mark.parametrize('preset', [False, True])
def test_an_element_from_heal_driver_handles_a_banner_before_it_clicks(tmp_path, preset):
    web = _covered()
    if preset:
        heal = _open(web, tmp_path, cookies='reject')
        key = 'cookie_banner'
    else:
        heal = _open(web, tmp_path)
        heal.interrupt('cookies', '#cc-reject')
        key = 'cookies'
    el = heal.driver.find_element('id', 'email')
    assert type(el) is HealingElement and isinstance(el, CoveredElement)
    el.click()
    assert web.overlay() is None and web.clicks == ['cc-reject', 'email']
    assert _interrupts(heal) == [(key, 'id=email')]


EVERY_WAY = {
    'link text': lambda d: d.find_element('link text', 'Terms').click(),
    'find_elements': lambda d: d.find_elements('xpath', "//input[@class='opt']")[1].click(),
    'child': lambda d: d.find_element('id', 'f').find_element('xpath', '//button').click(),
    'children': lambda d: d.find_element('id', 'f').find_elements('xpath', '//a')[0].click(),
    'send_keys': lambda d: d.find_element('name', 'email').send_keys('ada@example.com'),
    'clear': lambda d: d.find_element('css selector', '#email').clear(),
    'submit': lambda d: d.find_element('xpath', '//form').submit(),
}


@pytest.mark.parametrize('way', sorted(EVERY_WAY))
def test_every_way_to_find_an_element_gives_one_that_handles_the_banner(tmp_path, way):
    web = _covered()
    heal = _open(web, tmp_path)
    heal.interrupt('cookies', '#cc-reject')
    EVERY_WAY[way](heal.driver)
    assert web.overlay() is None
    assert web.clicks[0] == 'cc-reject'
    assert [key for key, _ in _interrupts(heal)] == ['cookies']
    assert web.raised == []


def test_a_banner_that_opens_during_the_click_is_handled_and_the_click_retried(tmp_path):
    web = _covered(banner=False)
    heal = _open(web, tmp_path)
    heal.interrupt('cookies', '#cc-reject')
    el = heal.driver.find_element('link text', 'Terms')
    web.during_action.append(lambda d: d.show(COOKIES))   # after the check before the click
    el.click()
    assert web.clicks == ['cc-reject', 'terms-link']
    assert len(web.raised) == 1                           # the first click, once
    assert _interrupts(heal) == [('cookies', 'link text=Terms')]


def test_an_action_no_interruption_explains_raises_its_own_error(tmp_path):
    web = _covered()                                       # a banner nobody registered
    heal = _open(web, tmp_path)
    heal.interrupt('promo', '#promo-close')
    el = heal.driver.find_element('link text', 'Terms')
    with pytest.raises(ElementClickInterceptedException) as info:
        el.click()
    assert info.value is web.raised[0] and len(web.raised) == 1     # not retried
    assert heal.handled_interruptions == []


def test_only_an_element_heal_locator_found_reports_to_the_heal_canary(tmp_path, monkeypatch):
    web = _covered()                                       # every click outside it fails
    heal = _open(web, tmp_path)
    reported = []
    monkeypatch.setattr(heal.coordinator, 'confirm_pending',
                        lambda key, success: reported.append((key, success)))
    driver = heal.driver
    for el in (driver.find_element('link text', 'Terms'), driver.find_elements('css selector', '#go')[0],
               driver.find_element('id', 'f').find_element('xpath', '//button')):
        with pytest.raises(ElementClickInterceptedException):
            el.click()
    assert reported == []
    with pytest.raises(ElementClickInterceptedException):
        driver.find_element('id', 'go').click()           # resolved by heal.locator()
    assert reported == [('id=go', False)]


def test_with_no_interruption_registered_an_action_costs_no_extra_browser_call(tmp_path):
    web = _covered(banner=False)
    driver = _open(web, tmp_path).driver
    found = [driver.find_element('link text', 'Terms'), driver.find_elements('css selector', '#email')[0],
             driver.find_element('id', 'email'),          # passed through, listed, healed
             driver.find_element('id', 'f').find_element('xpath', '//input')]
    for el in found:
        web.calls.clear()
        el.click()
        el.send_keys('x')
        el.clear()
        el.submit()
        assert web.calls == ['click', 'send_keys', 'clear', 'submit']


def test_a_healing_element_stands_in_for_the_element(tmp_path):
    web = _covered(banner=False)
    driver = _open(web, tmp_path).driver
    el = driver.find_element('id', 'email')
    raw = el.native
    assert isinstance(el, CoveredElement) and unwrap(el) is raw
    assert el == raw and raw == el and not (el != raw) and hash(el) == hash(raw)
    assert el != driver.find_element('id', 'go')
    assert el.get_attribute('name') == 'email' and el.tag_name == 'input'
    assert repr(el).startswith("<HealingElement 'id=email' <")
    el.marker = 1                                          # attributes land on the element
    assert raw.marker == 1
