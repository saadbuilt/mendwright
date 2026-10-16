"""heal.page.get_by_*() with mendwright.open(page, heal_get_by=True) on real browsers.

Each test runs on every Playwright engine (see conftest.py): Chromium, Firefox and WebKit,
patchright and camoufox; the get_by_* calls are Playwright's, so Selenium is deselected.
A redesign is a second page loaded at the same address. The stub provider would propose
the right element, and every test that heals or declines a call checks that it was never
asked: mendwright heals a call from its fingerprint only. No network, no model.
"""

import importlib
import re
import statistics
import time

import pytest

import mendwright
import mendwright.aio
from mendwright import getby
from mendwright.getby import GetByNotFound
from mw_engines import FAST_MS, journal_events, page, saved_entry

pytestmark = [pytest.mark.browser, pytest.mark.engine_family('playwright')]

_DONE = ('<p>Done: <span id="done"></span></p><script>function done(what) { '
         "document.getElementById('done').textContent += what + ' '; }</script>")
# A one-pixel image, so an <img> has a size without the network.
_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='


def _open(tab, provider, store_file, **kwargs):
    kwargs.setdefault('heal_get_by', True)
    kwargs.setdefault('timeout_ms', FAST_MS)
    return mendwright.open(tab.target, provider=provider, store_path=store_file, **kwargs)


def _page(body, title='get_by'):
    return page('<main>' + body + '</main>' + _DONE, title)


def _rejected(store_file):
    return [e.get('reason') for e in journal_events(store_file) if e['event'] == 'heal.rejected']


# Each method, before and after a redesign that changed the words and kept an id or a test
# id: (the call, the page before, the page after, the action, what the action did).
_REDESIGNS = {
    'get_by_role': (
        ('get_by_role', ('button',), {'name': 'Sign in'}),
        '<button id="login" type="button" onclick="done(\'login\')">Sign in</button>'
        '<button id="help" type="button">Help</button>',
        '<button id="login" type="button" onclick="done(\'login\')">Log in</button>'
        '<button id="help" type="button">Help</button>',
        'click', lambda tab: tab.text('#done') == 'login '),
    'get_by_text': (
        ('get_by_text', ('Free shipping',), {}),
        '<span id="ship" onclick="done(\'ship\')">Free shipping</span><p>On orders over $50</p>',
        '<span id="ship" onclick="done(\'ship\')">Shipping included</span><p>On orders over $50</p>',
        'click', lambda tab: tab.text('#done') == 'ship '),
    'get_by_label': (
        ('get_by_label', ('Email',), {}),
        '<label for="email">Email</label><input id="email" name="email" type="email">',
        '<label for="email">Where we send receipts</label><input id="email" name="email" '
        'type="email">',
        'fill', lambda tab: tab.value('#email') == 'ada@example.com'),
    'get_by_placeholder': (
        ('get_by_placeholder', ('you@example.com',), {}),
        '<input id="email" name="email" type="email" placeholder="you@example.com">',
        '<input id="email" name="email" type="email" placeholder="name@company.com">',
        'fill', lambda tab: tab.value('#email') == 'ada@example.com'),
    'get_by_alt_text': (
        ('get_by_alt_text', ('Logo',), {}),
        '<img id="logo" alt="Logo" src="' + _PIXEL + '" width="40" height="40" '
        'onclick="done(\'logo\')">',
        '<img id="logo" alt="Acme home" src="' + _PIXEL + '" width="40" height="40" '
        'onclick="done(\'logo\')">',
        'click', lambda tab: tab.text('#done') == 'logo '),
    'get_by_title': (
        ('get_by_title', ('Close',), {}),
        '<button id="close" type="button" title="Close" onclick="done(\'close\')">x</button>',
        '<button id="close" type="button" title="Dismiss" onclick="done(\'close\')">x</button>',
        'click', lambda tab: tab.text('#done') == 'close '),
    'get_by_test_id': (
        ('get_by_test_id', ('login',), {}),
        '<button id="login" data-testid="login" type="button" onclick="done(\'login\')">'
        'Sign in</button>',
        '<button id="login" data-testid="sign-in" type="button" onclick="done(\'login\')">'
        'Sign in</button>',
        'click', lambda tab: tab.text('#done') == 'login '),
}


def _act(locator, action):
    if action == 'fill':
        return locator.fill('ada@example.com')
    return locator.click()


@pytest.mark.parametrize('method', list(_REDESIGNS))
def test_each_method_heals_after_a_redesign(tab, store_file, stub_provider, method):
    (name, args, kwargs), before, after, action, did = _REDESIGNS[method]
    key = mendwright.get_by_key(name, *args, **kwargs)
    provider = stub_provider("//*[@id='login']")
    tab.load(_page(before))
    heal = _open(tab, provider, store_file)
    _act(getattr(heal.page, name)(*args, **kwargs), action)
    assert saved_entry(store_file, key)['fingerprint']

    tab.load(_page(after))
    if action == 'fill':
        tab.js("document.getElementById('email').value = ''")
    heal = _open(tab, provider, store_file)
    _act(getattr(heal.page, name)(*args, **kwargs), action)
    assert did(tab)
    assert [(h.key, h.source) for h in heal.heals] == [(key, 'fingerprint')]
    assert provider.bundles == []


def _library(tab):
    return importlib.import_module(type(tab.target).__module__.split('.')[0] + '.sync_api')


_TABLE = [
    ('get_by_role', ('button',), {'name': 'Sign in'}),
    ('get_by_role', (), {'role': 'button', 'name': 'Sign in', 'exact': False}),
    ('get_by_role', ('button',), {'name': 'Sign in', 'exact': True}),
    ('get_by_role', ('heading',), {'name': 'Cart', 'level': 2}),
    ('get_by_role', ('button',), {'name': re.compile(r'sign\s?in', re.I)}),
    ('get_by_role', ('navigation',), {}),
    ('get_by_role', ('button',), {'exact': True}),
    ('get_by_text', ('Free shipping',), {}),
    ('get_by_text', ('Say "hi"',), {}),
    ('get_by_label', ('Email',), {'exact': True}),
    ('get_by_placeholder', ('you@example.com',), {}),
    ('get_by_alt_text', ('Logo',), {}),
    ('get_by_title', ('Close',), {}),
    ('get_by_test_id', ('login',), {}),
    ('get_by_text', (re.compile('ship', re.I | re.S),), {'exact': True}),
]


def test_rebuilt_calls_select_the_same_as_the_originals(tab):
    tab.load(_page('<nav><a href="#a">A</a></nav><h2>Cart</h2><button>Sign in</button>'
                   '<button hidden>Hidden</button><p>Free shipping</p>'))
    target = tab.target
    for name, args, kwargs in _TABLE:
        key = mendwright.get_by_key(name, *args, **kwargs)
        rebuilt = getby.build(target, getby.parse(key))
        original = getattr(target, name)(*args, **kwargs)
        assert rebuilt._impl_obj._selector == original._impl_obj._selector, key
    plain = target.get_by_role('button')
    hidden_false = target.get_by_role('button', include_hidden=False)
    assert mendwright.get_by_key('get_by_role', 'button', include_hidden=False) == (
        'get_by_role("button")')
    assert hidden_false.count() == plain.count() == 1


def test_a_heal_without_the_calls_aria_role_is_refused(tab, store_file, stub_provider):
    provider = stub_provider("//button[@id='login']")
    tab.load(_page('<button id="login" type="button">Sign in</button>'
                   '<button id="cancel" type="button">Cancel</button>'))
    heal = _open(tab, provider, store_file)
    heal.page.get_by_role('button', name='Sign in').click()
    # The button is gone; a tab that carries its id and class is not a button to Playwright.
    tab.load(_page('<div role="tablist"><button role="tab" id="login" type="button">Sign in'
                   '</button></div><button id="cancel" type="button">Cancel</button>'))
    heal = _open(tab, provider, store_file)
    with pytest.raises(GetByNotFound) as caught:
        heal.page.get_by_role('button', name='Sign in').click()
    mismatch = "live element role mismatch: Playwright does not give it the ARIA role 'button'"
    assert mismatch in str(caught.value) and _rejected(store_file) == [mismatch]
    key = 'get_by_role("button", name="Sign in")'
    assert [c['xpath'] for c in saved_entry(store_file, key)['candidates']] == [key]
    assert provider.bundles == []


def test_level_on_a_button_is_playwrights_own_error_at_once(tab, store_file, stub_provider):
    tab.load(_page('<button id="login" type="button">Sign in</button>'))
    heal = _open(tab, stub_provider(None), store_file, timeout_ms=10000)
    started = time.monotonic()
    with pytest.raises(_library(tab).Error, match='"level" attribute is only supported') as caught:
        heal.page.get_by_role('button', name='Sign in', level=2).click(timeout=4000)
    assert time.monotonic() - started < 3
    assert not isinstance(caught.value, mendwright.AttentionRequired)
    assert journal_events(store_file) == []


def test_a_name_that_matches_part_of_two_buttons_is_a_strict_mode_violation(tab, store_file,
                                                                          stub_provider):
    tab.load(_page('<button type="button">Save</button><button type="button">Save draft</button>'))
    heal = _open(tab, stub_provider("//button[1]"), store_file, timeout_ms=10000)
    started = time.monotonic()
    with pytest.raises(_library(tab).Error, match='strict mode violation'):
        heal.page.get_by_role('button', name='Save').click(timeout=4000)
    assert time.monotonic() - started < 3
    assert journal_events(store_file) == []
    heal.page.get_by_role('button', name='Save', exact=True).click()
    assert heal.store.candidates('get_by_role("button", name="Save", exact=True)')


_MODAL = ('<div id="consent" role="dialog" aria-modal="true" style="position: fixed; inset: 0; '
          'z-index: 10; background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p>'
          '<button id="accept" type="button" onclick="document.getElementById(\'consent\')'
          ".remove(); document.getElementById('app').removeAttribute('aria-hidden')\">Accept"
          '</button></div>')


def test_a_banner_that_hides_the_page_is_handled_before_the_wait(tab, store_file, stub_provider):
    tab.load(page('<div id="app" aria-hidden="true"><button id="login" type="button" '
                  'onclick="done(\'login\')">Sign in</button></div>' + _MODAL + _DONE, 'banner'))
    heal = _open(tab, stub_provider(None), store_file, timeout_ms=10000)
    heal.interrupt('cookies', '#accept')
    started = time.monotonic()
    heal.page.get_by_role('button', name='Sign in').click()
    assert time.monotonic() - started < 4        # it did not wait the 10 s out first
    assert tab.text('#done') == 'login '
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookies', 'get_by_role("button", name="Sign in")')]


_SHADOW = """<buy-box id="box"></buy-box>
<script>
const root = document.getElementById('box').attachShadow({mode: 'open'});
root.innerHTML = '<button id="buy" type="button">LABEL</button>';
</script>"""


def test_a_renamed_element_in_a_shadow_root_is_refused(tab, store_file, stub_provider):
    provider = stub_provider("//button[@id='buy']")
    tab.load(_page(_SHADOW.replace('LABEL', 'Buy now')))
    heal = _open(tab, provider, store_file)
    heal.page.get_by_role('button', name='Buy now').click()
    tab.load(_page(_SHADOW.replace('LABEL', 'Purchase')))
    heal = _open(tab, provider, store_file)
    with pytest.raises(GetByNotFound, match='was not confident'):
        heal.page.get_by_role('button', name='Buy now').click()
    assert heal.heals == [] and provider.bundles == []


_IFRAME = page('<p>Pay below.</p><iframe name="payment" srcdoc="'
               + page('<button id="pay" type="button" onclick="this.textContent += \'!\'">'
                      'Pay</button>', 'pay').replace('&', '&amp;').replace('"', '&quot;')
               + '"></iframe>', 'checkout')


def test_a_call_inside_an_iframe_is_refused_and_heals_on_the_frame(tab, store_file,
                                                                  stub_provider):
    tab.load(_IFRAME)
    provider = stub_provider("//button[@id='pay']")
    frame = None
    for _ in range(50):
        frame = tab.target.frame(name='payment')
        if frame is not None and frame.locator('#pay').count() == 1:
            break
        time.sleep(0.1)
    heal = _open(tab, provider, store_file)
    with pytest.raises(GetByNotFound) as caught:
        heal.page.get_by_role('button', name='Pay').click()
    assert ("mendwright did not look for it there, because mendwright does not follow a get_by_* "
            "call into an iframe" in str(caught.value))
    assert ("heal = mendwright.open(page.frame(name='payment'), heal_get_by=True)."
            in str(caught.value))

    inner = mendwright.open(frame, provider=provider, store_path=store_file, heal_get_by=True,
                            timeout_ms=FAST_MS)
    inner.page.get_by_role('button', name='Pay').click()
    frame.evaluate("document.getElementById('pay').textContent = 'Confirm'")
    inner.page.get_by_role('button', name='Pay').click()
    assert frame.locator('#pay').text_content() == 'Confirm!'
    assert [h.source for h in inner.heals] == ['fingerprint'] and provider.bundles == []


def _ways():
    from test_browser_flow import _PLAYWRIGHT_WAYS
    return {name: _PLAYWRIGHT_WAYS[name] for name in ('get_by_role', 'get_by_label', 'fill')}


_WAY_KEYS = {'get_by_role': 'get_by_role("button", name="Continue")',
             'get_by_label': 'get_by_label("I accept the terms")',
             'fill': 'get_by_label("Email")'}


@pytest.mark.parametrize('way', list(_WAY_KEYS))
def test_a_cookie_banner_is_handled_once_before_a_calls_key(tab, store_file, stub_provider, way):
    from test_browser_flow import _checkout, _rejected_once
    tab.load(_checkout('now'))
    heal = _open(tab, stub_provider(None), store_file, cookies='reject')
    act, did = _ways()[way]

    act(heal.page)

    assert _rejected_once(tab, heal)
    assert did(tab)
    assert [e.before for e in heal.handled_interruptions] == [_WAY_KEYS[way]]


def test_a_popup_over_a_calls_element_is_dismissed(tab, store_file, stub_provider):
    from test_browser_dismiss import NO_THANKS, SIGN_UP, _log, _modal
    from test_browser_dismiss import _page as popup_page
    tab.load(popup_page(_modal([SIGN_UP, NO_THANKS])))
    heal = _open(tab, stub_provider(None), store_file, dismiss=True)
    heal.page.get_by_role('button', name='Go').click(timeout=5000)
    assert tab.text('#done') == 'go ' and _log(tab) == ['key:Escape', 'no-thanks']
    assert [e.before for e in heal.handled_interruptions] == ['get_by_role("button", name="Go")']
    assert heal.store.candidates('get_by_role("button", name="Go")')


# --- async ---------------------------------------------------------------------------------

def test_async_a_renamed_button_heals_and_two_matches_pass_through(engine, tmp_path,
                                                                    stub_provider):
    provider = stub_provider("//button[@id='login']")
    before = _page('<button id="login" type="button" onclick="done(\'login\')">Sign in</button>')
    after = before.replace('>Sign in<', '>Log in<')
    twins = _page('<button type="button">Save</button><button type="button">Save draft</button>')

    async def run(tab):
        store = str(tmp_path / 'o.json')
        await tab.set_content(before)
        heal = await mendwright.aio.open(tab, provider=provider, store_path=store,
                                         heal_get_by=True, timeout_ms=FAST_MS)
        await heal.page.get_by_role('button', name='Sign in').click()
        await tab.set_content(after)
        await heal.page.get_by_role('button', name='Sign in').click()
        done = await tab.text_content('#done')
        await tab.set_content(twins)
        error = missed = None
        try:
            await heal.page.get_by_role('button', name='Save').click(timeout=3000)
        except Exception as exc:
            error = exc
        await tab.set_content(after.replace('id="login"', 'id="signin"'))
        try:
            await heal.page.get_by_role('button', name='Sign in').click(timeout=3000)
        except Exception as exc:
            missed = exc
        return heal, done, error, missed

    heal, done, error, missed = engine.with_async_page(run)
    assert done == 'login '            # the healed click, on the page loaded again
    assert [(h.source, h.new) for h in heal.heals] == [('fingerprint', "//button[@id='login']")]
    assert 'strict mode violation' in str(error) and not isinstance(error, GetByNotFound)
    assert isinstance(missed, GetByNotFound)
    assert provider.bundles == []


# --- what it costs -----------------------------------------------------------------------

_BIG = _page(''.join('<div class="row"><span>Item ' + str(i) + '</span><a href="#i' + str(i)
                     + '">Open</a></div>' for i in range(680))
             + '<button id="go" type="button" onclick="done(\'go\')">Continue</button>')


def test_the_cost_of_a_healed_call_on_a_large_page(tab, store_file, stub_provider):
    tab.load(_BIG)
    assert tab.js('document.getElementsByTagName("*").length') >= 2000
    heal = _open(tab, stub_provider(None), store_file, timeout_ms=5000)
    raw = tab.target.get_by_role('button', name='Continue')
    healed = heal.page.get_by_role('button', name='Continue')
    healed.click()                     # the first lookup reads the role and the fingerprint

    def median(click):
        times = []
        for _ in range(50):
            started = time.perf_counter()
            click()
            times.append(time.perf_counter() - started)
        return statistics.median(times) * 1000

    plain, routed = median(raw.click), median(healed.click)
    print('\n' + tab.engine_id + ': raw get_by_role().click() ' + format(plain, '.1f')
          + ' ms, with heal_get_by=True ' + format(routed, '.1f') + ' ms, '
          + format(routed - plain, '+.1f') + ' ms')
    assert routed < plain + 1000       # a loose bound: the number is for the guide, not CI
