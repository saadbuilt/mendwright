"""heal_get_by='model' and get_by twins on real browsers.

Each test runs on every Playwright engine (see conftest.py); Selenium is deselected. The
model is a StubProvider that proposes a fixed XPath: no network, no model. What the tests
check is what the browser decides: Playwright's own ARIA role check of a pick, its
aria_snapshot() name for a twin, and whether a twin matches the healed element and nothing
else. The last test prints what a twin costs, for the guide.
"""

import statistics
import time

import pytest

import mendwright
import mendwright.aio
from mendwright import client, getby
from mendwright.drivers import wrap
from mw_engines import FAST_MS, journal_events, page, saved_entry

pytestmark = [pytest.mark.browser, pytest.mark.engine_family('playwright')]

_DONE = ('<p>Done: <span id="done"></span></p><script>function done(what) { '
         "document.getElementById('done').textContent += what + ' '; }</script>")
KEY = 'get_by_role("button", name="Sign in")'
BEFORE = ('<form id="login-form"><button id="login" type="button" onclick="done(\'login\')">'
          'Sign in</button></form><button id="help" type="button">Help</button>')
# The id and the words changed: nothing strong for the fingerprint to go on.
AFTER = ('<form id="login-form"><button id="enter" type="button" onclick="done(\'enter\')">'
         'Continue</button></form><button id="help" type="button">Help</button>')
TWIN = 'get_by_role("button", name="Continue", exact=True)'


def _page(body):
    return page('<main>' + body + '</main>' + _DONE, 'get_by model')


def _open(tab, provider, store_file, **kwargs):
    kwargs.setdefault('heal_get_by', 'model')
    kwargs.setdefault('timeout_ms', FAST_MS)
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def _only(tab, call, xpath):
    """True when the call matches one element on the page, and it is the one xpath matches."""
    built = getby.build(tab.target, getby.parse(call))
    return built.count() == 1 and built.and_(tab.target.locator('xpath=' + xpath)).count() == 1


def _row(store_file, key, xpath):
    return next(c for c in saved_entry(store_file, key)['candidates'] if c['xpath'] == xpath)


def test_the_model_heals_what_the_fingerprint_cannot_and_a_twin_is_saved(tab, store_file,
                                                                          stub_provider):
    provider = stub_provider("//button[@id='enter']")
    tab.load(_page(BEFORE))
    _open(tab, provider, store_file).page.get_by_role('button', name='Sign in').click()
    tab.load(_page(AFTER))
    heal = _open(tab, provider, store_file)
    heal.page.get_by_role('button', name='Sign in').click()
    assert tab.text('#done') == 'enter '
    assert [(h.key, h.source) for h in heal.heals] == [(KEY, 'model')]
    assert provider.bundles[0]['intent'] == 'the button named "Sign in"'
    assert _row(store_file, KEY, "//button[@id='enter']")['getBy'] == TWIN
    assert _only(tab, TWIN, "//button[@id='enter']")
    applied = [e for e in journal_events(store_file) if e['event'] == 'heal.applied']
    assert applied[-1]['get_by'] == TWIN


def test_with_true_the_model_is_never_asked(tab, store_file, stub_provider):
    provider = stub_provider("//button[@id='enter']")
    tab.load(_page(BEFORE))
    _open(tab, provider, store_file, heal_get_by=True).page.get_by_role(
        'button', name='Sign in').click()
    tab.load(_page(AFTER))
    with pytest.raises(getby.GetByNotFound):
        _open(tab, provider, store_file, heal_get_by=True).page.get_by_role(
            'button', name='Sign in').click()
    assert provider.bundles == []


def test_a_call_that_never_matched_is_looked_for_by_its_role(tab, store_file, stub_provider):
    provider = stub_provider("//button[@id='login']")
    tab.load(_page(BEFORE))
    heal = _open(tab, provider, store_file)
    heal.page.get_by_role('button', name='Sing in').click()          # a typo
    assert tab.text('#done') == 'login '
    assert provider.bundles[0]['expectedRole'] == 'button'
    with pytest.raises(getby.GetByNotFound):
        heal.page.get_by_text('Sing in').click()                        # no kind of element
    assert len(provider.bundles) == 1


def test_a_model_pick_without_the_calls_aria_role_is_refused(tab, store_file, stub_provider):
    tab.load(_page('<div role="tablist"><button role="tab" id="settings" type="button">'
                   'Settings</button></div>'))
    _open(tab, stub_provider(None), store_file).page.get_by_role('tab', name='Settings').click()
    tab.load(_page('<div><button id="prefs" type="button">Preferences</button></div>'))
    provider = stub_provider("//button[@id='prefs']")
    heal = _open(tab, provider, store_file)
    with pytest.raises(getby.GetByNotFound) as caught:
        heal.page.get_by_role('tab', name='Settings').click()
    assert "Playwright does not give it the ARIA role 'tab'" in str(caught.value)
    assert len(provider.bundles) == 2 and heal.heals == []


@pytest.mark.parametrize('call, before, after, healed, twin', [
    (('get_by_label', ('Email',), {}),
     '<label for="email">Email</label><input id="email" name="email" type="email">',
     '<label for="email">Where we send receipts</label><input id="email" name="email" '
     'type="email">', "//input[@id='email']",
     'get_by_label("Where we send receipts", exact=True)'),
    (('get_by_test_id', ('login',), {}),
     '<button data-testid="login" id="login" type="button">Sign in</button>',
     '<button data-testid="sign-in" id="login" type="button">Sign in</button>',
     "//button[@data-testid='sign-in']", 'get_by_test_id("sign-in")'),
    (('get_by_role', ('link',), {'name': 'Docs'}),
     '<nav><a id="docs" href="/docs">Docs</a></nav>',
     '<nav><a id="docs" href="/docs">Documentation <b>v2</b></a></nav>',
     "//a[@id='docs']", 'get_by_role("link", name="Documentation v2", exact=True)'),
], ids=['label', 'test id', 'link'])
def test_a_fingerprint_heal_of_a_call_saves_a_twin_the_browser_agrees_with(
        tab, store_file, stub_provider, call, before, after, healed, twin):
    name, args, kwargs = call
    key = mendwright.get_by_key(name, *args, **kwargs)
    tab.load(_page(before))
    heal = _open(tab, None, store_file, heal_get_by=True)
    getattr(heal.page, name)(*args, **kwargs).inner_text()
    tab.load(_page(after))
    heal = _open(tab, None, store_file, heal_get_by=True)
    getattr(heal.page, name)(*args, **kwargs).inner_text()
    assert [(h.new, h.source) for h in heal.heals] == [(healed, 'fingerprint')]
    assert _row(store_file, key, healed)['getBy'] == twin
    assert _only(tab, twin, healed)


def test_a_twin_that_matches_two_buttons_is_not_saved(tab, store_file):
    tab.load(_page('<form><button id="login" type="button">Sign in</button></form>'))
    heal = _open(tab, None, store_file, heal_get_by=True)
    heal.page.get_by_role('button', name='Sign in').inner_text()
    tab.load(_page('<form><button id="login" type="button">Log in</button></form>'
                   '<aside><button type="button">Log in</button></aside>'))
    heal.page.get_by_role('button', name='Sign in').inner_text()
    assert [h.source for h in heal.heals] == ['fingerprint']
    assert 'getBy' not in _row(store_file, KEY, "//button[@id='login']")


def test_async_the_model_heals_and_a_twin_is_saved(engine, tmp_path, stub_provider):
    provider = stub_provider("//button[@id='enter']")
    store = str(tmp_path / 'o.json')

    async def run(tab):
        await tab.set_content(_page(BEFORE))
        heal = await mendwright.aio.open(tab, provider=provider, store_path=store,
                                         heal_get_by='model', timeout_ms=FAST_MS)
        await heal.page.get_by_role('button', name='Sign in').click()
        await tab.set_content(_page(AFTER))
        await heal.page.get_by_role('button', name='Sign in').click()
        return heal, await tab.text_content('#done')

    heal, done = engine.with_async_page(run)
    assert done == 'enter '
    assert [(h.key, h.source) for h in heal.heals] == [(KEY, 'model')]
    assert _row(store, KEY, "//button[@id='enter']")['getBy'] == TWIN


# --- what a twin costs ---------------------------------------------------------------------

_BIG = _page(''.join('<div class="row"><span>Item ' + str(i) + '</span><a href="#i' + str(i)
                     + '">Open</a></div>' for i in range(680))
             + '<button id="go" type="button" onclick="done(\'go\')">Continue</button>')


def test_the_cost_of_a_twin_on_a_large_page(tab):
    tab.load(_BIG)
    assert tab.js('document.getElementsByTagName("*").length') >= 2000
    import lxml.html
    driver = wrap(tab.target)
    doc = lxml.html.fromstring(tab.target.content())
    xpath = "//button[@id='go']"
    assert client.get_by_twin(KEY, doc, xpath, xpath, driver) == TWIN

    times = []
    for _ in range(20):
        started = time.perf_counter()
        client.get_by_twin(KEY, doc, xpath, xpath, driver)
        times.append(time.perf_counter() - started)
    cost = statistics.median(times) * 1000
    print('\n' + tab.engine_id + ': a get_by twin (aria_snapshot, count, and_().count()) took '
          + format(cost, '.1f') + ' ms at the median of 20')
    assert cost < 5000               # a loose bound: the number is for the guide, not CI
