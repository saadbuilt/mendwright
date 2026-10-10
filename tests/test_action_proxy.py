"""ActionProxy: keyword arguments, the full action set, routing through driver.act_on,
which errors quarantine a heal, and the public .native property."""

import pytest

import mendwright
from mendwright.facade import ACTIONS, ActionProxy

V2 = "<form><label>terms<input type='checkbox' id='new-id'></label><a id='l' href='#'>Docs</a></form>"
STALE = "//input[@id='old-id']"
HEALED = "//input[@type='checkbox']"


def _healed_proxy(tmp_path, make_page, stub_provider):
    """A proxy for a key that was just healed, so its canary is pending."""
    page = make_page(V2)
    provider = stub_provider(HEALED)
    healer = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    proxy = healer.locator('terms', STALE, role='checkbox', timeout_ms=0, poll_ms=0)
    assert healer.coordinator.pending == {'terms': HEALED}
    return healer, page, provider, proxy


def _active(healer):
    return [c['xpath'] for c in healer.store.candidates('terms')]


def test_keyword_arguments_reach_the_native_call(tmp_path, make_page, stub_provider):
    healer, page, _, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    proxy.click(timeout=500)
    proxy.fill('x', timeout=5)
    proxy.check(force=True)
    assert [(verb, args, kwargs) for _, verb, args, kwargs in page.actions] == [
        ('click', (), {'timeout': 500}),
        ('fill', ('x',), {'timeout': 5}),
        ('check', (), {'force': True}),
    ]


def test_the_native_return_value_comes_back(tmp_path, make_page, stub_provider):
    _, page, _, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    page.results['select_option'] = ['blue']
    assert proxy.select_option('blue') == ['blue']


@pytest.mark.parametrize('verb', [v for v in ACTIONS if v not in ('send_keys', 'submit')])
def test_every_action_quarantines_a_heal_when_the_browser_fails(verb, tmp_path, make_page,
                                                                stub_provider):
    healer, page, provider, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    page.fail[verb] = RuntimeError('Timeout 500ms exceeded')
    with pytest.raises(RuntimeError):
        getattr(proxy, verb)()
    assert HEALED not in _active(healer)
    assert provider.outcomes == [('terms', HEALED, False)]
    assert healer.coordinator.pending == {}


@pytest.mark.parametrize('error', [TypeError('unexpected keyword'), ValueError('bad key'),
                                   AttributeError('no such verb')])
def test_usage_errors_do_not_quarantine(error, tmp_path, make_page, stub_provider):
    healer, page, provider, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    page.fail['check'] = error
    with pytest.raises(type(error)):
        proxy.check()
    assert HEALED in _active(healer)
    assert provider.outcomes == []
    assert healer.coordinator.pending == {'terms': HEALED}   # the canary is still armed


def test_expect_confirms_and_a_raising_expect_quarantines(tmp_path, make_page, stub_provider):
    healer, _, provider, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    proxy.check(expect=lambda: True)
    assert provider.outcomes == [('terms', HEALED, True)]

    healer, _, provider, proxy = _healed_proxy(tmp_path / 'b', make_page, stub_provider)

    def broken_check():
        raise RuntimeError('page went away')
    proxy.click(expect=broken_check)
    assert provider.outcomes == [('terms', HEALED, False)]
    assert HEALED not in _active(healer)


def test_reads_fall_through_without_touching_the_canary(tmp_path, make_page, stub_provider):
    healer, page, provider, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    assert proxy.get_attribute('type') == 'checkbox'
    assert proxy.inner_text() == ''
    assert page.actions == []
    assert healer.coordinator.pending == {'terms': HEALED}


def test_native_is_public_and_drag_to_unwraps_a_proxy(tmp_path, make_page, stub_provider):
    healer, page, _, proxy = _healed_proxy(tmp_path, make_page, stub_provider)
    link = healer.locator('docs', "//a[@id='l']", role='link', timeout_ms=0, poll_ms=0)
    assert proxy.native is proxy._native
    assert not isinstance(proxy.native, ActionProxy)
    proxy.drag_to(link)
    assert page.actions[-1][2] == (link.native,)
    assert 'terms' in repr(proxy)


class RecordingDriver:
    """A custom driver with act_on, the way the Selenium driver maps Playwright verbs."""

    def __init__(self):
        self.calls = []

    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def scroll_into_view(self, selector, frame_chain=()):
        pass

    def handle(self, selector, frame_chain=()):
        return 'element:' + selector

    def url(self):
        return 'https://example.test/'

    def outer_html(self, scope=None):
        return '<input type="checkbox" id="a">'

    def inventory(self, limit=200):
        return []

    def act_on(self, native, verb, *args, **kwargs):
        if verb == 'fly':
            raise AttributeError('no verb fly')
        self.calls.append((native, verb, args, kwargs))
        return 'done'


def test_actions_go_through_driver_act_on(tmp_path, stub_provider):
    driver = RecordingDriver()
    healer = mendwright.open(driver, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    proxy = healer.locator('terms', "//input[@id='a']", role='checkbox', timeout_ms=0, poll_ms=0)
    assert proxy.fill('hi', timeout=1) == 'done'
    proxy.check()
    assert driver.calls == [
        ("element://input[@id='a']", 'fill', ('hi',), {'timeout': 1}),
        ("element://input[@id='a']", 'check', (), {}),
    ]


def test_a_driver_without_act_on_calls_the_native_method(tmp_path, stub_provider):
    class Native:
        def __init__(self):
            self.clicked = []

        def click(self, **kwargs):
            self.clicked.append(kwargs)

    native = Native()

    class OldDriver(RecordingDriver):
        act_on = None

        def handle(self, selector, frame_chain=()):
            return native

    healer = mendwright.open(OldDriver(), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    proxy = healer.locator('go', "//button", role='button', timeout_ms=0, poll_ms=0)
    proxy.click(timeout=3)
    assert native.clicked == [{'timeout': 3}]
    with pytest.raises(AttributeError):
        proxy.check()                                  # the native has no check()
