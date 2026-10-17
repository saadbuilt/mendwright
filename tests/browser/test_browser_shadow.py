"""Open shadow roots on real browsers (see mendwright.shadow), on every engine: a key whose
element is inside a shadow root heals there and never onto a look-alike outside its host,
also inside nested shadow roots; a shadow root that cannot be entered is refused; a selector
saved inside a shadow root matches only there; and Selenium follows a CSS selector into the
one shadow root it matches in. The shadow roots are made by the page's own script with
attachShadow(), as a web component makes them.

On Playwright the first lookup matches through Playwright's CSS, which pierces open shadow
roots; on Selenium, whose CSS does not, it is followed into the shadow root (a heal with
source 'shadow'). Either way the element's hosts are recorded, and the redesign heals there.
"""

import json

import pytest

import mendwright
from mendwright import AttentionRequired
from mw_engines import FAST_MS, POLL_MS, PLAYWRIGHT, page, saved_entry

pytestmark = pytest.mark.browser

HOST = 'my-app[id="root"]'


def mounted(roots, body=''):
    """A page whose script gives hosts their shadow roots. roots is a list of (host CSS, html,
    mode), each host looked for in the shadow root made before it (the page for the first),
    or in the page when its CSS starts with '/'."""
    script = ['let scope = document, root = null;']
    for host, html, mode in roots:
        where = 'document' if host.startswith('/') else 'scope'
        script.append('root = ' + where + '.querySelector(' + json.dumps(host.lstrip('/'))
                      + ').attachShadow({mode: ' + json.dumps(mode) + '});'
                      ' root.innerHTML = ' + json.dumps(html) + '; scope = root;')
    # In a function: set_content() keeps the window, and a second top-level let would throw.
    return page(body + '<script>(() => {' + ''.join(script) + '})();</script>')


def button(ident, place, testid='save'):
    return ('<button type="button" id="' + ident + '" data-testid="' + testid + '" '
            'onclick="document.body.dataset.clicked = \'' + place + '\'">Save</button>')


# Outside the host, on the page, a look-alike: the same test id and text.
LOOKALIKE = button('other', 'outside')


def app_page(inner, mode='open', extra=''):
    return mounted([('my-app', inner, mode)],
                   body=LOOKALIKE + '<my-app id="root"></my-app>' + extra)


def clicked(tab):
    return tab.js('document.body.dataset.clicked || null')


def _open(tab, store_file, **kwargs):
    kwargs.setdefault('provider', None)
    return mendwright.open(tab.target, store_path=store_file, timeout_ms=FAST_MS, **kwargs)


def _first_run(tab, store_file, v1, selector='#save'):
    """The key matches on v1, is confirmed, and is looked up once more, so its fingerprint and
    hosts are saved (a heal made in a run is fingerprinted once it is confirmed)."""
    tab.load(v1)
    heal = _open(tab, store_file)
    heal.locator('save', selector, role='button', poll_ms=POLL_MS).click(expect=lambda: True)
    heal.locator('save', selector, role='button', poll_ms=POLL_MS)
    return heal


def test_a_key_inside_a_shadow_root_records_its_host(tab, store_file):
    heal = _first_run(tab, store_file, app_page(button('save', 'inside')))
    assert clicked(tab) == 'inside'
    assert heal.store.shadow_hosts('save') == [HOST]
    if tab.family == PLAYWRIGHT:
        assert heal.heals == []          # Playwright's CSS reached it
    else:
        assert [h.source for h in heal.heals] == ['shadow']
        assert heal.heals[0].new == HOST + ' >> #save'


def test_a_redesign_heals_inside_the_shadow_root_and_not_on_the_look_alike(tab, store_file):
    _first_run(tab, store_file, app_page(button('save', 'inside')))
    tab.load(app_page(button('save-2', 'inside')))
    heal = _open(tab, store_file)
    heal.locator('save', '#save', role='button', poll_ms=POLL_MS).click()
    assert clicked(tab) == 'inside'
    assert heal.heals[-1].source == 'fingerprint'
    assert heal.heals[-1].new == HOST + ' >> [data-testid="save"]'
    row = [c for c in saved_entry(store_file, 'save')['candidates']
           if c['xpath'] == heal.heals[-1].new][0]
    assert row['shadowHosts'] == [HOST]

    # The next run finds it there at once.
    tab.js('delete document.body.dataset.clicked')
    rerun = _open(tab, store_file)
    rerun.locator('save', '#save', role='button', poll_ms=POLL_MS).click()
    assert clicked(tab) == 'inside' and rerun.heals == []


def test_a_heal_inside_nested_shadow_roots_stays_in_the_inner_one(tab, store_file):
    def nested(ident):
        # A look-alike in the outer shadow root, and the button in the inner one.
        return mounted([('my-app', LOOKALIKE.replace("'outside'", "'outer'")
                         + '<settings-panel></settings-panel>', 'open'),
                        ('settings-panel', button(ident, 'inner'), 'open')],
                       body=LOOKALIKE + '<my-app id="root"></my-app>')
    heal = _first_run(tab, store_file, nested('save'))
    assert heal.store.shadow_hosts('save') == [HOST, 'settings-panel']
    tab.load(nested('save-2'))
    heal = _open(tab, store_file)
    heal.locator('save', '#save', role='button', poll_ms=POLL_MS).click()
    assert clicked(tab) == 'inner'
    assert heal.heals[-1].new == HOST + ' >> settings-panel >> [data-testid="save"]'


@pytest.mark.parametrize('v2', ['gone', 'two', 'closed'])
def test_a_shadow_root_that_cannot_be_entered_is_refused(tab, store_file, stub_provider, v2):
    _first_run(tab, store_file, app_page(button('save', 'inside')))
    if v2 == 'gone':
        tab.load(page(LOOKALIKE))
    elif v2 == 'two':
        tab.load(mounted([('/my-app', button('save-2', 'inside'), 'open'),
                          ('/my-app:last-of-type', button('save-3', 'inside'), 'open')],
                         body=LOOKALIKE + '<my-app id="root"></my-app><my-app id="root"></my-app>'))
    else:
        tab.load(app_page(button('save-2', 'inside'), mode='closed'))
    provider = stub_provider("//button[@data-testid='save']")    # would find the look-alike
    heal = _open(tab, store_file, provider=provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=POLL_MS)
    assert "'save' was last found inside the shadow root of " + HOST in str(info.value)
    assert provider.bundles == [] and clicked(tab) is None


def test_a_saved_shadow_heal_matches_nothing_outside_its_shadow_root(tab, store_file):
    _first_run(tab, store_file, app_page(button('save', 'inside')))
    tab.load(app_page(button('save-2', 'inside')))
    heal = _open(tab, store_file)
    heal.locator('save', '#save', role='button', poll_ms=POLL_MS)
    # The button leaves the shadow root for the host's light DOM, shown through a <slot>:
    # Playwright's CSS would reach it through the saved chain, but it is outside the shadow
    # root.
    tab.load(app_page('<p>no button</p><slot></slot>').replace(
        '<my-app id="root"></my-app>', '<my-app id="root">' + button('save-2', 'light') + '</my-app>'))
    assert tab.js("document.querySelector('my-app > button').getClientRects().length") == 1
    assert heal.present('save') is False
    with pytest.raises(AttentionRequired):
        heal.locator('save', '#save', role='button', poll_ms=POLL_MS).click()
    assert clicked(tab) is None


def test_an_element_in_a_closed_shadow_root_is_never_healed(tab, store_file, stub_provider):
    tab.load(app_page(button('save', 'inside'), mode='closed'))
    provider = stub_provider("//button[@data-testid='save']")
    heal = _open(tab, store_file, provider=provider, fingerprint=False)
    try:
        heal.locator('save', '#save', role='button', poll_ms=POLL_MS)
    except AttentionRequired:
        pass
    # patchright's CSS reaches into a closed shadow root; no other engine does. None clicks
    # the look-alike, and none saves a heal inside it.
    assert clicked(tab) is None
    assert not [c for c in saved_entry(store_file, 'save')['candidates']
                if c.get('shadowHosts')]


def test_a_list_inside_a_shadow_root_is_recorded_as_one(tab, store_file):
    if tab.family != PLAYWRIGHT:
        pytest.skip("Selenium's CSS does not reach into a shadow root, so no list is given")
    items = ''.join('<li class="card"><a href="/p/' + n + '">' + n + '</a></li>' for n in 'ABC')
    tab.load(mounted([('my-app', '<ul class="cart">' + items + '</ul>', 'open')],
                     body='<my-app id="root"></my-app>'))
    heal = _open(tab, store_file)
    assert len(heal.locator_all('cart', '.card', role='li', nonempty=True,
                                poll_ms=POLL_MS)) == 3
    assert heal.store.meta('cart')['listFingerprint']['shadow'] is True


def test_the_async_healer_heals_inside_the_shadow_root(engine, tmp_path):
    if engine.family != PLAYWRIGHT:
        pytest.skip('mendwright.aio is for async Playwright')
    store_file = str(tmp_path / 'o.json')

    async def run(page_):
        heal = await mendwright.aio.open(page_, provider=None, store_path=store_file,
                                         timeout_ms=FAST_MS)
        await page_.set_content(app_page(button('save', 'inside')))
        box = await heal.locator('save', '#save', role='button', poll_ms=POLL_MS)
        await box.click(expect=lambda: True)
        await heal.locator('save', '#save', role='button', poll_ms=POLL_MS)
        await page_.set_content(app_page(button('save-2', 'inside')))
        heal = await mendwright.aio.open(page_, provider=None, store_path=store_file,
                                         timeout_ms=FAST_MS)
        await heal.locator('save', '#save', role='button', poll_ms=POLL_MS).click()
        return (heal.heals[-1].new, await page_.evaluate('document.body.dataset.clicked'))

    import mendwright.aio
    assert engine.with_async_page(run) == (HOST + ' >> [data-testid="save"]', 'inside')
