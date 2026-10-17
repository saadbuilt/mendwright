"""The drivers with ShadowHost steps in a frame chain (see mendwright.shadow), on the fakes of
tests/conftest.py: a selector inside an open shadow root is CSS, and matches only inside the
one shadow root its hosts lead to, never in the host's light DOM, a nested shadow root or
another host with the same tag."""

import asyncio

import pytest

from mendwright.drivers.playwright_async import AsyncPlaywrightDriver
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.selectors import InvalidSelector
from mendwright.shadow import ShadowHost

HOST = 'my-app[id="root"]'
IN = (ShadowHost(HOST),)


def shadow(inner, host='<my-app id="root">', light='', mode='open'):
    tag = host[1:].split()[0].rstrip('>')
    return (host + '<template shadowrootmode="' + mode + '">' + inner + '</template>' + light
            + '</' + tag + '>')


def page(body):
    return '<html><body>' + body + '</body></html>'


BUTTON = '<button class="go" type="button">Go</button>'
NESTED = page(shadow('<p>no button here</p>' + shadow(BUTTON, host='<inner-box>')))


@pytest.fixture(params=['playwright', 'selenium'])
def drive(request, make_shadow_page, make_shadow_driver):
    """A driver over a fake page or WebDriver showing html, and the fake."""
    def make(html):
        if request.param == 'playwright':
            fake = make_shadow_page(html)
            return PlaywrightSyncDriver(fake), fake
        fake = make_shadow_driver(html)
        return SeleniumDriver(fake), fake
    return make


def test_a_selector_inside_a_shadow_root_matches_there(drive):
    driver, _fake = drive(page(shadow(BUTTON)))
    assert driver.count('.go', IN) == 1
    assert driver.is_visible('.go', IN) is True
    assert driver.describe('.go', IN)['role'] == 'button'
    assert driver.count('.go') in (0, 1)     # Playwright's CSS pierces; Selenium's does not


@pytest.mark.parametrize('html, count', [
    (page(shadow('<p>none</p>', light=BUTTON)), 0),            # only in the host's light DOM
    (page(shadow(BUTTON, light=BUTTON)), 2),                   # and one in the light DOM
    (NESTED, 0),                                               # only in a nested shadow root
    (page(shadow(BUTTON) + shadow(BUTTON)), 0),                # two hosts: none is entered
    (page(shadow(BUTTON, mode='closed')), 0),                  # a closed shadow root
    (page(BUTTON), 0),                                         # no host
], ids=['light-dom-only', 'light-dom-too', 'nested-only', 'two-hosts', 'closed', 'no-host'])
def test_a_match_outside_the_shadow_root_never_counts_as_one(drive, html, count):
    driver, _fake = drive(html)
    # Playwright counts a match in the host's light DOM too, so the selector is not unique
    # (2); Selenium looks inside the shadow root only (1). Neither gives one wrong element.
    got = driver.count('.go', IN)
    assert got == count or (count == 2 and got == 1 and isinstance(driver, SeleniumDriver))
    if got != 1:
        assert driver.describe('.go', IN) is None and driver.fingerprint('.go', IN) is None


def test_the_one_match_outside_is_never_read_or_described(make_shadow_page):
    driver = PlaywrightSyncDriver(make_shadow_page(page(shadow('<p>none</p>', light=BUTTON))))
    assert driver.is_visible('.go', IN) is False
    assert driver.describe('.go', IN) is None
    assert driver.handles('.go', IN) == []
    assert driver.list_items('.go', IN) == []
    with pytest.raises(LookupError):
        driver.act('.go', 'click', frame_chain=IN)


def test_nested_hosts_lead_to_the_inner_shadow_root(drive):
    driver, _fake = drive(NESTED)
    assert driver.count('.go', (ShadowHost(HOST), ShadowHost('inner-box'))) == 1


def test_only_css_runs_inside_a_shadow_root(drive):
    driver, _fake = drive(page(shadow(BUTTON)))
    for selector in ("//button", "text=Go", 'get_by_role("button")'):
        with pytest.raises(InvalidSelector):
            driver.count(selector, IN)
    with pytest.raises(InvalidSelector):
        driver.count('.go', (ShadowHost('//my-app'),))
    with pytest.raises(InvalidSelector, match='an iframe inside a shadow root'):
        driver.count('.go', (ShadowHost(HOST), 'iframe'))


def test_the_fingerprint_read_gives_the_hosts(drive):
    driver, _fake = drive(page(shadow(shadow(BUTTON, host='<inner-box>'))))
    fp = driver.fingerprint('.go', (ShadowHost(HOST), ShadowHost('inner-box')), hosts=True)
    assert fp['tag'] == 'button' and fp['shadowHosts'] == [HOST, 'inner-box']
    driver, _fake = drive(page(BUTTON))
    assert driver.fingerprint('.go', hosts=True)['shadowHosts'] == []
    assert 'shadowHosts' not in driver.fingerprint('.go')      # asked for, only


def test_the_shadow_root_reads(drive):
    driver, _fake = drive(page(BUTTON + shadow(BUTTON + shadow(BUTTON, host='<inner-box>'))))
    assert driver.shadow_html([HOST]) == BUTTON + '<inner-box></inner-box>'
    assert driver.shadow_html(['nope-app']) is None
    assert [r['tag'] for r in driver.shadow_inventory([HOST])] == ['button']
    roots, cut = driver.shadow_matches('.go')
    assert [(r['hosts'], r['count']) for r in roots] == [([HOST], 1), ([HOST, 'inner-box'], 1)]
    assert cut is False
    roots, _cut = driver.shadow_matches("//button")
    assert [r['html'].count('<button') for r in roots] == [1, 1]
    assert driver.shadow_matches('text=Go') == ([], False)


def test_selenium_hands_back_the_web_element_inside_the_shadow_root(make_shadow_driver):
    fake = make_shadow_driver(page(shadow(BUTTON)))
    driver = SeleniumDriver(fake)
    el = driver.handle('.go', IN)
    assert el.el.get('class') == 'go' and driver._shadow == ()
    driver.act('.go', 'click', frame_chain=IN)
    assert fake.actions[-1][1] == 'click' and driver._shadow == ()


def test_selenium_leaves_the_shadow_root_after_an_error(make_shadow_driver):
    fake = make_shadow_driver(page(shadow(BUTTON)))
    driver = SeleniumDriver(fake)

    def boom():
        raise RuntimeError('boom')
    with pytest.raises(RuntimeError):
        driver._run(IN, boom)
    assert driver._shadow == ()
    assert driver.count('.go') == 0                  # on the page again


def test_selenium_says_when_the_browser_rejects_the_css_inside(make_shadow_driver):
    fake = make_shadow_driver(page(shadow(BUTTON)))

    def rejects(script, *args):
        raise RuntimeError("Message: javascript error: '[' is not a valid selector.")
    fake.execute_script = rejects
    with pytest.raises(InvalidSelector, match='the browser rejected'):
        SeleniumDriver(fake).count('[', IN)


class AsyncLocator:
    def __init__(self, loc):
        self._loc = loc

    def locator(self, selector):
        return AsyncLocator(self._loc.locator(selector))

    async def count(self):
        return self._loc.count()

    async def evaluate_all(self, js, arg=None):
        return self._loc.evaluate_all(js, arg)


class AsyncPage:
    def __init__(self, sync):
        self.sync = sync

    def locator(self, selector):
        return AsyncLocator(self.sync.locator(selector))

    async def evaluate(self, js, arg=None):
        return self.sync.evaluate(js, arg)


def test_the_async_driver_counts_only_inside_the_shadow_root(make_shadow_page):
    async def main(html):
        driver = AsyncPlaywrightDriver(AsyncPage(make_shadow_page(html)))
        return (await driver.count('.go', IN), await driver.shadow_html([HOST]),
                (await driver.fingerprint('.go', IN, hosts=True) or {}).get('shadowHosts'))

    assert asyncio.run(main(page(shadow(BUTTON)))) == (1, BUTTON, [HOST])
    assert asyncio.run(main(page(shadow('<p>x</p>', light=BUTTON)))) == (0, '<p>x</p>', None)
