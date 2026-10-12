"""heal.page (Playwright) and heal.driver (Selenium) on real browsers: an existing script
heals by changing the one line that makes the page or driver. The stub provider stands
in for the model; the fingerprint tier is off so the model's heal is what is checked.
"""

import importlib

import pytest

import mendwright
from mw_engines import FAST_MS, SIGNUP_V1, SIGNUP_V2, STALE, V2_CHECKBOX, HEALED

pytestmark = pytest.mark.browser


def _open(tab, provider, store_file):
    return mendwright.open(tab.target, provider=provider, store_path=store_file,
                           timeout_ms=FAST_MS, fingerprint=False)


@pytest.mark.engine_family('playwright')
def test_heal_page_runs_an_unchanged_script(tab, store_file, stub_provider):
    provider = stub_provider(HEALED)
    page = _open(tab, provider, store_file).page       # the one changed line

    def script():
        page.fill('#email', 'ada@example.com')
        page.locator(STALE).check()
        return page.locator(STALE).is_checked()

    page.set_content(SIGNUP_V1)            # passes through to the real page
    assert script() is True
    assert tab.value('#email') == 'ada@example.com'
    assert provider.bundles == []

    page.set_content(SIGNUP_V2)            # the redesign: the checkbox lost its id
    assert page.is_visible(STALE) is False     # answers at once, never heals
    assert provider.bundles == []
    assert script() is True                # the same script, healed
    assert tab.checked(V2_CHECKBOX) is True
    assert len(provider.bundles) == 1
    assert page.title() == 'signup v2'
    assert page.is_visible(STALE) is True      # through the saved heal


@pytest.mark.engine_family('playwright')
def test_playwright_expect_takes_the_healing_page(tab, store_file, stub_provider):
    # expect() from the page's own library: patchright pages are not playwright's.
    library = type(tab.target).__module__.split('.')[0]
    expect = importlib.import_module(library + '.sync_api').expect
    page = _open(tab, stub_provider(None), store_file).page
    page.set_content(SIGNUP_V1)
    expect(page).to_have_title('signup v1')
    page.check(STALE)
    expect(page.locator(STALE)).to_be_checked()
    expect(page.locator('#email')).to_be_visible()


@pytest.mark.engine_family('selenium')
def test_heal_driver_runs_an_unchanged_script(tab, store_file, stub_provider):
    from selenium.webdriver.common.by import By
    provider = stub_provider(HEALED)
    driver = _open(tab, provider, store_file).driver   # the one changed line

    def script():
        driver.find_element(By.ID, 'email').send_keys('ada@example.com')
        box = driver.find_element(By.XPATH, STALE)
        box.click()
        return box.is_selected()

    tab.load(SIGNUP_V1)
    assert script() is True
    assert provider.bundles == []
    tab.load(SIGNUP_V2)
    assert script() is True
    assert tab.checked(V2_CHECKBOX) is True
    assert len(provider.bundles) == 1
    assert len(driver.find_elements(By.CSS_SELECTOR, 'input')) == 3   # passes through
