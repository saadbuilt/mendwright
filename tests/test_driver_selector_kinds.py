"""Drivers run XPath, CSS and Playwright engine selectors; bad selectors raise
InvalidSelector at once instead of reading as count()=0 and burning the timeout."""

import pytest

from mendwright import AttentionRequired, InvalidSelector, present, resolve, resolve_with_heal
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver, playwright_selector
from mendwright.drivers.selenium import SeleniumDriver, selenium_locator


# --- Playwright fakes -------------------------------------------------------------------

class PwError(Exception):
    """Stands in for playwright._impl._errors.Error."""


class RecLoc:
    def __init__(self, page, chain, selector):
        self.page, self.chain, self.selector = page, chain, selector

    def count(self):
        self.page.counted.append((self.chain, self.selector))
        err = self.page.errors.get(self.selector)
        if err:
            raise err
        return self.page.counts.get(self.selector, 0)

    def is_visible(self):
        return True

    def scroll_into_view_if_needed(self):
        pass


class RecFrame:
    def __init__(self, page, chain):
        self.page, self.chain = page, chain

    def locator(self, selector):
        return RecLoc(self.page, self.chain, selector)

    def frame_locator(self, selector):
        return RecFrame(self.page, self.chain + (selector,))


class RecPage(RecFrame):
    """Playwright-shaped page that records the exact strings mendwright hands it."""

    def __init__(self, counts=None, errors=None):
        super().__init__(self, ())
        self.counts = counts or {}
        self.errors = errors or {}
        self.counted = []
        self.url = 'https://example.test/'

    def content(self):
        return '<html></html>'


@pytest.mark.parametrize('given, sent', [
    ("//button[@id='go']", "xpath=//button[@id='go']"),
    ("xpath=//button[@id='go']", "xpath=//button[@id='go']"),
    ('(//button)[1]', 'xpath=(//button)[1]'),
    ('#go', 'css=#go'),
    ('css=#go', 'css=#go'),
    ('form .primary', 'css=form .primary'),
    ('text=Go', 'text=Go'),
    ("role=button[name='Go']", "role=button[name='Go']"),
    ('data-testid=go', 'data-testid=go'),
    ('//form >> text=Go', '//form >> text=Go'),
])
def test_playwright_selector_mapping(given, sent):
    assert playwright_selector(given) == sent
    page = RecPage(counts={sent: 1})
    assert PlaywrightSyncDriver(page).count(given) == 1
    assert page.counted == [((), sent)]


def test_playwright_frame_chain_takes_any_kind():
    page = RecPage(counts={'css=#in': 1})
    d = PlaywrightSyncDriver(page)
    assert d.count('#in', frame_chain=('#consent-frame', "//iframe[@name='x']")) == 1
    assert page.counted == [(('css=#consent-frame', "xpath=//iframe[@name='x']"), 'css=#in')]


def test_playwright_bad_xpath_raises_before_touching_the_page():
    page = RecPage()
    d = PlaywrightSyncDriver(page)
    for call in (lambda: d.count('//div['), lambda: d.is_visible('//div['),
                 lambda: d.handle('//div['), lambda: d.count('#ok', frame_chain=('//f[',))):
        with pytest.raises(InvalidSelector):
            call()
    assert page.counted == []


def test_playwright_browser_parse_error_becomes_invalid_selector():
    err = PwError('Locator.count: Unexpected token "!" while parsing css selector "#save!!". '
                  'Did you mean to CSS.escape it?\nCall log: ...')
    page = RecPage(errors={'css=#save!!': err})
    with pytest.raises(InvalidSelector) as info:
        PlaywrightSyncDriver(page).count('#save!!')
    assert 'while parsing css selector' in str(info.value)
    assert 'Locator.count' not in str(info.value)

    unknown = PwError('Locator.count: Unknown engine "foo" while parsing selector foo=bar')
    with pytest.raises(InvalidSelector):
        PlaywrightSyncDriver(RecPage(errors={'foo=bar': unknown})).count('foo=bar')


def test_playwright_other_errors_still_read_as_no_match():
    page = RecPage(errors={'css=#x': PwError('Execution context was destroyed')})
    assert PlaywrightSyncDriver(page).count('#x') == 0


def test_playwright_non_string_selector_is_a_type_error():
    with pytest.raises(TypeError) as info:
        PlaywrightSyncDriver(RecPage()).count(object())
    assert "Locator objects can't be stored" in str(info.value)


# --- Selenium fakes ---------------------------------------------------------------------

class InvalidSelectorException(Exception):
    """Same class name as selenium.common.exceptions.InvalidSelectorException."""


class SelDriver:
    def __init__(self, found=None, invalid=()):
        self.found = found or {}
        self.invalid = set(invalid)
        self.queries = []
        self.switch_to = self

    def find_elements(self, by, value):
        self.queries.append((by, value))
        if value in self.invalid:
            raise InvalidSelectorException(
                'Message: invalid selector: An invalid or illegal selector was specified\n')
        return list(self.found.get(value, []))

    def frame(self, el):
        pass

    def default_content(self):
        pass


class El:
    def is_displayed(self):
        return True


def test_selenium_locator_mapping():
    assert selenium_locator("//a[@id='x']") == ('xpath', "//a[@id='x']")
    assert selenium_locator("xpath=//a") == ('xpath', '//a')
    assert selenium_locator('#x') == ('css selector', '#x')
    assert selenium_locator('css=#x') == ('css selector', '#x')


def test_selenium_runs_css_and_xpath():
    drv = SelDriver(found={'#go': [El()], "//b": [El(), El()]})
    d = SeleniumDriver(drv)
    assert d.count('#go') == 1
    assert d.count('//b') == 2
    assert drv.queries == [('css selector', '#go'), ('xpath', '//b')]


@pytest.mark.parametrize('selector', ['text=Save', "role=button[name='Save']", '//a >> text=x'])
def test_selenium_rejects_playwright_engine_selectors(selector):
    drv = SelDriver()
    with pytest.raises(InvalidSelector) as info:
        SeleniumDriver(drv).count(selector)
    assert 'Playwright selector' in str(info.value)
    assert 'Selenium cannot run it' in str(info.value)
    assert drv.queries == []          # rejected before any browser call


def test_selenium_browser_invalid_selector_becomes_invalid_selector():
    drv = SelDriver(invalid={'#save!!'})
    with pytest.raises(InvalidSelector) as info:
        SeleniumDriver(drv).count('#save!!')
    assert 'invalid or illegal selector' in str(info.value)
    assert 'Message:' not in str(info.value)


def test_selenium_other_errors_still_read_as_no_match():
    class Boom(SelDriver):
        def find_elements(self, by, value):
            raise RuntimeError('stale')
    assert SeleniumDriver(Boom()).count('#x') == 0
    assert SeleniumDriver(Boom()).is_visible('#x') is False


# --- resolver ---------------------------------------------------------------------------

class Store:
    def __init__(self, selectors, role='button'):
        self._c = [{'xpath': s} for s in selectors]
        self.role = role
        self.succeeded = None

    def candidates(self, key):
        return self._c

    def meta(self, key):
        return {'expectedRole': self.role}

    def record_success(self, key, xpath):
        self.succeeded = xpath


def _no_sleep(log):
    return lambda s: log.append(s)


def test_resolve_raises_invalid_selector_at_once_for_bad_xpath():
    slept = []
    with pytest.raises(InvalidSelector) as info:
        resolve(Store(['//div[']), RecPage(), 'save', timeout_ms=60000, sleep=_no_sleep(slept))
    assert slept == []
    assert "the selector for 'save' is not usable" in str(info.value)


def test_resolve_raises_at_once_when_the_browser_rejects_the_only_candidate():
    slept = []
    page = RecPage(errors={'css=#save!!': PwError('Locator.count: Unexpected token "!" while '
                                                  'parsing css selector "#save!!".')})
    with pytest.raises(InvalidSelector):
        resolve(Store(['#save!!']), page, 'save', timeout_ms=60000, sleep=_no_sleep(slept))
    assert slept == []


def test_resolve_skips_an_invalid_candidate_and_uses_a_good_one():
    page = RecPage(counts={'css=#go': 1})
    store = Store(['//div[', '#go'])
    assert resolve(store, page, 'go', timeout_ms=10, poll_ms=0, sleep=lambda s: None) is not None
    assert store.succeeded == '#go'


def test_resolve_times_out_with_a_note_about_unrunnable_candidates():
    clock = iter([0.0, 0.0, 999.0])
    with pytest.raises(AttentionRequired) as info:
        resolve(Store(['text=Go', '#missing']), SeleniumDriver(SelDriver()), 'go',
                timeout_ms=5, poll_ms=0, monotonic=lambda: next(clock), sleep=lambda s: None)
    msg = str(info.value)
    assert msg.startswith('No matching element for: go (tried 2 candidate(s)).')
    assert '1 of them could not run' in msg and 'Playwright selector' in msg


def test_resolve_runs_css_candidates():
    page = RecPage(counts={'css=#go': 1})
    store = Store(['#go'])
    resolve(store, page, 'go', timeout_ms=10, poll_ms=0, sleep=lambda s: None)
    assert store.succeeded == '#go'


def test_present_raises_only_when_no_candidate_can_run():
    with pytest.raises(InvalidSelector):
        present(Store(['//div[']), RecPage(), 'k')
    assert present(Store(['//div[', '#go']), RecPage(counts={'css=#go': 1}), 'k') is True
    assert present(Store(['//div[', '#nope']), RecPage(), 'k') is False
    assert present(Store([]), RecPage(), 'k') is False


def test_resolve_with_heal_never_asks_the_provider_about_a_typo():
    class Coordinator:
        store = Store(['//div['])
        tried = False

        def try_heal(self, *a, **k):
            Coordinator.tried = True

    coord = Coordinator()
    with pytest.raises(InvalidSelector):
        resolve_with_heal(lambda: resolve(coord.store, RecPage(), 'k', timeout_ms=0),
                          coord, 'k', {'expectedRole': 'button'}, {})
    assert Coordinator.tried is False
