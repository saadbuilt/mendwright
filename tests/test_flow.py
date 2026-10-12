"""Flow changes, offline: interruptions (heal.interrupt), optional steps, the page guard
and the store's record of where a key was found.

Every scenario runs twice, on mendwright.open() and on mendwright.aio.open(), through the
same small adapter, so the two make the same decisions. The page is FlowPage: an lxml page
with the sync Playwright shape that really changes when it is acted on. An element with
data-overlay covers the page (actions outside it fail the way Playwright words it), a
click inside the overlay closes it unless it has data-sticky, and a click on an element
with data-reveal="<id>" shows the element with that id. No browser, network or model.
"""

import asyncio
import json
import logging
import threading
import time

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, InvalidSelector
from mendwright.flow import (flow_change_message, is_interception_error, loop_message,
                             page_path, skip_message)
from mendwright.interruptions import COOKIE_BANNER_KEY, InterruptionEvent

SIDES = ('sync', 'async')


# --- the page --------------------------------------------------------------------------------

def _hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def _overlay_of(el):
    while el is not None:
        if el.get('data-overlay') is not None:
            return el
        el = el.getparent()
    return None


class ElementClickInterceptedException(Exception):
    """Named like Selenium's, which mendwright recognises by name."""


class FlowLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def _els(self):
        return self._page.query(self._selector)

    def count(self):
        return len(self._els())

    def is_visible(self, **kwargs):
        els = self._els()
        if len(els) > 1:
            raise RuntimeError('strict mode violation: ' + self._selector)
        return len(els) == 1 and not _hidden(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def evaluate_all(self, js):
        els = self._els()
        if len(els) != 1:
            return None
        return {'tag': els[0].tag, 'html': lxml.html.tostring(els[0], encoding='unicode',
                                                              with_tail=False)}

    def get_attribute(self, name, **kwargs):
        els = self._els()
        return els[0].get(name) if els else None

    def is_checked(self, **kwargs):
        return self._els()[0].get('checked') is not None

    def __getattr__(self, verb):
        if verb not in ('click', 'fill', 'check', 'press'):
            raise AttributeError(verb)

        def action(*args, **kwargs):
            return self._page.act(self._selector, verb, args)
        return action


class FlowPage:
    """A sync-Playwright-shaped page over lxml whose actions change the document."""

    def __init__(self, html, url='https://shop.example/signup', intercept='playwright'):
        self.url = url
        self.intercept = intercept
        self.actions = []           # (selector, verb, args), every attempt
        self.during_action = []     # callables run once each, inside the next actions
        self.fail_next = None       # an exception the next action raises after it ran
        self.set_html(html)

    def set_html(self, html):
        self.doc = lxml.html.document_fromstring(html)

    def query(self, selector):
        if selector.startswith('xpath='):
            try:
                found = self.doc.xpath(selector[len('xpath='):])
            except Exception:
                return []
            return [e for e in found if isinstance(getattr(e, 'tag', None), str)]
        body = selector[len('css='):] if selector.startswith('css=') else selector
        if body.startswith('#'):
            return self.doc.xpath('//*[@id=$i]', i=body[1:])
        return []

    def by_id(self, ident):
        found = self.doc.xpath('//*[@id=$i]', i=ident)
        return found[0] if found else None

    def overlay(self):
        for el in self.doc.xpath('//*[@data-overlay]'):
            if not _hidden(el):
                return el
        return None

    def act(self, selector, verb, args):
        self.actions.append((selector, verb, args))
        if self.during_action:
            self.during_action.pop(0)(self)
        els = self.query(selector)
        if len(els) != 1:
            raise RuntimeError('Locator.' + verb + ': ' + selector + ' matched ' + str(len(els)))
        el = els[0]
        cover = self.overlay()
        if cover is not None and _overlay_of(el) is not cover:
            if self.intercept == 'selenium':
                raise ElementClickInterceptedException('element click intercepted: Other element '
                                                       'would receive the click')
            raise RuntimeError('Locator.' + verb + ': Timeout 50ms exceeded.\nCall log:\n  - <div id="'
                               + (cover.get('id') or '') + '"> intercepts pointer events')
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if verb == 'fill':
            el.set('value', args[0])
        elif verb == 'check':
            el.set('checked', '')
        elif verb == 'click':
            reveal = el.get('data-reveal')
            if reveal is not None:
                target = self.by_id(reveal)
                target.attrib.pop('style', None)
            mine = _overlay_of(el)
            if mine is not None and mine.get('data-sticky') is None:
                mine.drop_tree()

    def locator(self, selector):
        return FlowLocator(self, selector)

    def frame_locator(self, selector):
        return self

    def content(self):
        return lxml.html.tostring(self.doc, encoding='unicode')

    def evaluate(self, js, arg=None):
        return []

    def clicks(self, selector):
        return sum(1 for s, verb, _ in self.actions if s == selector and verb == 'click')


class AsyncFlowLocator:
    def __init__(self, loc):
        self._loc = loc

    async def count(self):
        return self._loc.count()

    async def is_visible(self, **kwargs):
        return self._loc.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate_all(self, js):
        return self._loc.evaluate_all(js)

    async def get_attribute(self, name, **kwargs):
        return self._loc.get_attribute(name)

    async def is_checked(self, **kwargs):
        return self._loc.is_checked()

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)

        async def run(*args, **kwargs):
            return action(*args, **kwargs)
        return run


class AsyncFlowPage:
    """The async Playwright shape over a FlowPage."""

    def __init__(self, page):
        self.sync = page

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector):
        return AsyncFlowLocator(self.sync.locator(selector))

    def frame_locator(self, selector):
        return self

    async def content(self):
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        return []


# --- one adapter for both healers ----------------------------------------------------------

class Side:
    """Runs the same steps on mendwright.open() or mendwright.aio.open(). The async side
    keeps one event loop in a thread for the whole test, as a real async suite does."""

    def __init__(self, kind):
        self.kind = kind
        self.heal = None
        self._loop = None

    def _call(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(30)

    def close(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def open(self, page, **kwargs):
        if self.kind == 'sync':
            self.heal = mendwright.open(page, **kwargs)
        else:
            async def opened():
                return await mendwright.aio.open(AsyncFlowPage(page), **kwargs)
            self.heal = self._call(opened())
        self.heal.coordinator.log = None   # the 'mendwright' logger, which caplog reads
        return self.heal

    def locate(self, key, selector=None, **kwargs):
        """The resolved proxy (an AttentionRequired raises here on both sides)."""
        kwargs.setdefault('timeout_ms', 0)
        kwargs.setdefault('poll_ms', 0)
        proxy = self.heal.locator(key, selector, **kwargs)
        if self.kind == 'sync':
            return proxy

        async def resolved():
            return await proxy
        return self._call(resolved())

    def act(self, proxy, verb, *args, **kwargs):
        result = getattr(proxy, verb)(*args, **kwargs)
        return result if self.kind == 'sync' else self._call(result)

    def step(self, key, selector, verb, *args, **kwargs):
        return self.act(self.locate(key, selector, **kwargs), verb, *args)


@pytest.fixture(params=SIDES)
def side(request):
    s = Side(request.param)
    yield s
    s.close()


def _store(tmp_path):
    return str(tmp_path / 'store' / 'overrides.json')


def _journal(tmp_path):
    path = tmp_path / 'store' / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _events(tmp_path, prefix='flow.'):
    return [e for e in _journal(tmp_path) if e['event'].startswith(prefix)]


def _lines(caplog, level=None):
    return [r.getMessage() for r in caplog.records
            if r.name == 'mendwright' and (level is None or r.levelno == level)]


BANNER = ('<div id="banner" data-overlay="" role="dialog" aria-label="Cookie consent">'
          '<p>We use cookies.</p><button id="cookie-accept" type="button">Accept all</button>'
          '<button id="cookie-reject" type="button">Reject all</button></div>')

SIGNUP = ('<form><label for="email">Email</label><input id="email" name="email">'
          '<button id="next" type="button">Next</button></form>')


def _page(body, **kwargs):
    return FlowPage('<html><body>' + body + '</body></html>', **kwargs)


# --- interruptions ---------------------------------------------------------------------------

def test_a_banner_that_covers_the_form_is_handled_before_the_action(side, tmp_path, caplog,
                                                                     stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(BANNER + SIGNUP)
    provider = stub_provider('//never')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')

    side.step('email', '#email', 'fill', 'ada@example.com')

    assert page.by_id('email').get('value') == 'ada@example.com'
    assert page.by_id('banner') is None
    assert [(s, v) for s, v, _ in page.actions] == [('css=#cookie-accept', 'click'),
                                                     ('css=#email', 'fill')]
    assert "mendwright: handled interruption 'cookies' before 'email'" in _lines(caplog, logging.WARNING)
    assert heal.handled_interruptions == [InterruptionEvent('cookies', 'email')]
    events = _events(tmp_path)
    assert [(e['event'], e['key'], e['before'], e['action'], e['selector']) for e in events] == [
        ('flow.interrupt', 'cookies', 'email', 'click', '#cookie-accept')]
    assert provider.bundles == []


def test_a_banner_that_hides_the_element_is_handled_on_a_miss_before_any_heal(side, tmp_path,
                                                                              stub_provider):
    hidden = SIGNUP.replace('<form>', '<form id="form" style="display: none">')
    page = _page(BANNER.replace('id="cookie-accept"', 'id="cookie-accept" data-reveal="form"')
                 + hidden)
    provider = stub_provider("//input[@name='email']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')

    side.step('email', '#email', 'fill', 'ada@example.com', role='input')

    assert page.by_id('email').get('value') == 'ada@example.com'
    assert provider.bundles == []                    # no model call
    assert [e['event'] for e in _journal(tmp_path)] == ['flow.interrupt']
    assert heal.heals == []


def test_the_old_explicit_step_is_skipped_once_the_banner_was_handled(side, tmp_path, caplog,
                                                                     stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(BANNER + SIGNUP)
    provider = stub_provider('//button')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    side.step('email', '#email', 'fill', 'ada@example.com')
    side.step('next', '#next', 'click')

    started = time.monotonic()
    proxy = side.locate('cookies', '#cookie-accept', timeout_ms=5000)   # no wait for it
    assert time.monotonic() - started < 2
    assert proxy.present is False
    assert proxy.native is None
    assert side.act(proxy, 'click') is None
    with pytest.raises(AttributeError, match="'cookies' was not on the page"):
        proxy.get_attribute('id')

    assert provider.bundles == []                    # never healed
    assert (skip_message('cookies', 'interruption handled', 'email')
            == "skipped 'cookies': it was already handled as an interruption before 'email', "
               "and it is not on the page now.")
    assert ('mendwright: ' + skip_message('cookies', 'interruption handled', 'email')
            in _lines(caplog, logging.INFO))
    skips = [e for e in _events(tmp_path) if e['event'] == 'flow.skip']
    assert [(e['key'], e['reason']) for e in skips] == [('cookies', 'interruption handled')]


def test_a_banner_that_comes_after_the_steps_is_clicked_by_the_old_step(side, tmp_path,
                                                                        stub_provider):
    hidden_banner = BANNER.replace('<div id="banner"', '<div id="banner" style="display:none"')
    page = _page(hidden_banner + SIGNUP.replace('id="next"', 'id="next" data-reveal="banner"'))
    heal = side.open(page, provider=stub_provider(None), store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')

    side.step('email', '#email', 'fill', 'ada@example.com')
    side.step('next', '#next', 'click')            # the banner shows up now, as before
    proxy = side.locate('cookies', '#cookie-accept')
    assert proxy.present is True
    side.act(proxy, 'click')

    assert page.by_id('banner') is None
    assert page.clicks('css=#cookie-accept') == 1   # once, by the step itself
    assert heal.handled_interruptions == []
    assert _events(tmp_path) == []


def test_a_banner_that_never_appears_skips_the_old_step(side, tmp_path, caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(SIGNUP)
    provider = stub_provider('//button')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')

    side.step('email', '#email', 'fill', 'ada@example.com')
    proxy = side.locate('cookies', '#cookie-accept', role='button')
    assert proxy.present is False and side.act(proxy, 'click') is None
    assert provider.bundles == []
    assert ("mendwright: skipped 'cookies': it is registered with heal.interrupt() and is not "
            "on the page, so there was nothing to do." in _lines(caplog, logging.INFO))
    assert [(e['key'], e['reason']) for e in _events(tmp_path)] == [('cookies', 'interruption absent')]


def test_at_most_three_interruptions_per_step(side, tmp_path, stub_provider):
    sticky = BANNER.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _page(sticky + SIGNUP.replace('<form>', '<form style="display:none">'))
    provider = stub_provider('//input')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')

    with pytest.raises(AttentionRequired) as caught:
        side.step('email', '#email', 'fill', 'x', role='input')

    assert str(caught.value) == loop_message('email', 'cookies', 3)
    assert ("3 interruptions were handled for 'email' and 'cookies' is still on the page, so "
            "mendwright stopped instead of handling it again.") in str(caught.value)
    assert page.clicks('css=#cookie-accept') == 3
    assert provider.bundles == []
    assert len(heal.handled_interruptions) == 3


def test_an_intercepted_action_is_retried_once_after_the_banner_is_handled(side, tmp_path):
    page = _page(SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    proxy = side.locate('next', '#next')

    # The banner opens while the click is on its way: the first click hits it.
    def banner_opens(p):
        p.doc.body.insert(0, lxml.html.fragment_fromstring(BANNER))
    page.during_action.append(banner_opens)
    side.act(proxy, 'click')

    assert page.clicks('css=#next') == 2              # the failed click, and one retry
    assert page.clicks('css=#cookie-accept') == 1
    assert heal.handled_interruptions == [InterruptionEvent('cookies', 'next')]


def test_a_selenium_interception_error_is_recognised(side, tmp_path):
    page = _page(SIGNUP, intercept='selenium')
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    proxy = side.locate('next', '#next')
    page.during_action.append(lambda p: p.doc.body.insert(0, lxml.html.fragment_fromstring(BANNER)))
    side.act(proxy, 'click')
    assert page.clicks('css=#next') == 2


def test_a_failed_retry_is_not_retried_again(side, tmp_path):
    sticky = BANNER.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _page(SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    proxy = side.locate('next', '#next')
    page.during_action.append(lambda p: p.doc.body.insert(0, lxml.html.fragment_fromstring(sticky)))
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.act(proxy, 'click')
    assert page.clicks('css=#next') == 2              # never a third time


def test_an_action_that_failed_otherwise_is_never_run_twice(side, tmp_path, stub_provider):
    page = _page(SIGNUP)
    provider = stub_provider("//button[normalize-space()='Next']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    side.locate('next', '#gone', role='button')       # healed: its canary is pending
    assert heal.pending() == {'next': "//button[normalize-space()='Next']"}
    proxy = side.locate('next', '#gone', role='button')

    # The click may have taken effect, then failed for another reason, while a banner
    # opened: mendwright must not click again.
    def banner_and_failure(p):
        p.doc.body.insert(0, lxml.html.fragment_fromstring(
            BANNER.replace('data-overlay=""', '')))
        p.fail_next = RuntimeError('Locator.click: Target page, context or browser has been closed')
    page.during_action.append(banner_and_failure)
    with pytest.raises(RuntimeError, match='has been closed'):
        side.act(proxy, 'click')
    assert page.clicks("xpath=//button[normalize-space()='Next']") == 1
    assert page.clicks('css=#cookie-accept') == 0
    assert heal.pending() == {}                          # the heal was quarantined as before


def test_an_intercepted_action_with_nothing_registered_is_not_retried(side, tmp_path):
    page = _page(SIGNUP)
    side.open(page, provider=None, store_path=_store(tmp_path))
    proxy = side.locate('next', '#next')
    page.during_action.append(lambda p: p.doc.body.insert(0, lxml.html.fragment_fromstring(BANNER)))
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.act(proxy, 'click')
    assert page.clicks('css=#next') == 1


def test_an_interruption_is_never_healed(side, tmp_path, stub_provider):
    page = _page(SIGNUP)
    provider = stub_provider('//button')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    assert side.locate('cookies', '#renamed-accept', role='button').present is False
    assert provider.bundles == [] and heal.heals == []


def test_an_interruption_with_another_action_and_a_role(side, tmp_path):
    modal = ('<div id="modal" data-overlay=""><a id="close-link" href="#">Close</a>'
             '<button id="close-btn" type="button">Close</button></div>')
    page = _page(modal + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    # The link has the wrong role, so only the button is acted on.
    heal.interrupt('newsletter', ['#close-link', '#close-btn'], role='button')
    heal.interrupt('feedback', '#feedback-dialog', action='press', args=('Escape',))
    side.step('email', '#email', 'fill', 'x')
    assert page.clicks('css=#close-link') == 0 and page.clicks('css=#close-btn') == 1
    assert heal.handled_interruptions == [InterruptionEvent('newsletter', 'email')]

    page.doc.body.insert(0, lxml.html.fragment_fromstring(
        '<div id="feedback-dialog" role="dialog">How are we doing?</div>'))
    side.step('next', '#next', 'click')
    assert ('css=#feedback-dialog', 'press', ('Escape',)) in page.actions


def test_an_interruption_whose_action_fails_is_logged_and_not_counted(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page('<div id="promo" role="dialog"><button id="promo-close">x</button></div>' + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('promo', '#promo-close')
    page.during_action.append(lambda p: setattr(p, 'fail_next', RuntimeError('detached')))
    side.step('email', '#email', 'fill', 'x')
    assert page.by_id('email').get('value') == 'x'
    assert heal.handled_interruptions == []
    assert any(line.startswith("mendwright: could not handle interruption 'promo' before 'email' "
                               "(RuntimeError: detached). Check the selector and action passed "
                               "to heal.interrupt('promo', ...).")
               for line in _lines(caplog, logging.WARNING))


def test_nothing_is_looked_up_when_nothing_is_registered(side, tmp_path):
    page = _page(BANNER.replace('data-overlay=""', '') + SIGNUP)
    side.open(page, provider=None, store_path=_store(tmp_path))
    side.step('email', '#email', 'fill', 'x')
    assert page.clicks('css=#cookie-accept') == 0


@pytest.mark.parametrize('kind', SIDES)
def test_interrupt_rejects_arguments_that_can_never_work(kind, tmp_path):
    side = Side(kind)
    try:
        heal = side.open(_page(SIGNUP), provider=None, store_path=_store(tmp_path))
        with pytest.raises(ValueError, match="got action='wave', which mendwright cannot run"):
            heal.interrupt('cookies', '#accept', action='wave')
        with pytest.raises(TypeError, match="takes args= as a tuple"):
            heal.interrupt('modal', '#x', action='press', args='Escape')
        with pytest.raises(TypeError, match='needs a key'):
            heal.interrupt('', '#x')
        with pytest.raises(InvalidSelector):
            heal.interrupt('cookies', '//button[')
        with pytest.raises(ValueError, match='empty list'):
            heal.interrupt('cookies', [])
        with pytest.raises(ValueError):
            heal.interrupt('cookies', '#x', role='not-a-role!')
    finally:
        side.close()


# --- the cookies= preset -----------------------------------------------------------------------

ONETRUST = """<div id="onetrust-consent-sdk"><div id="onetrust-banner-sdk" class="otFlat" role="region"
 aria-label="Cookie banner" data-overlay=""><div id="onetrust-policy">We use cookies.</div>
<div id="onetrust-button-group-parent"><div id="onetrust-button-group">
<button id="onetrust-pc-btn-handler">Cookie Settings</button>
<button id="onetrust-reject-all-handler">Reject All</button>
<button id="onetrust-accept-btn-handler">Accept All Cookies</button></div></div>
<div id="onetrust-close-btn-container"><button class="onetrust-close-btn-handler banner-close-button
 ot-close-icon" aria-label="Close"></button></div></div></div>"""


@pytest.mark.parametrize('choice, clicked', [('reject', 'onetrust-reject-all-handler'),
                                             ('accept', 'onetrust-accept-btn-handler')])
def test_the_cookies_preset_on_a_onetrust_banner(side, tmp_path, caplog, choice, clicked):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(ONETRUST + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path), cookies=choice)
    side.step('email', '#email', 'fill', 'ada@example.com')
    assert page.by_id('onetrust-banner-sdk') is None
    clicks = [s for s, v, _ in page.actions if v == 'click']
    assert len(clicks) == 1
    assert heal.handled_interruptions == [InterruptionEvent(COOKIE_BANNER_KEY, 'email')]
    assert "mendwright: handled interruption 'cookie_banner' before 'email'" in _lines(caplog)
    event = _events(tmp_path)[0]
    assert event['preset'] == choice and 'selector' not in event
    # The one click went to the button the preset is for.
    target = clicks[0]
    page2 = _page(ONETRUST)
    assert [e.get('id') for e in page2.query(target)] == [clicked]


def test_the_cookies_preset_is_off_by_default_and_checks_its_value(side, tmp_path):
    page = _page(ONETRUST.replace('data-overlay=""', '') + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    side.step('email', '#email', 'fill', 'x')
    assert heal.handled_interruptions == [] and page.by_id('onetrust-banner-sdk') is not None
    entry = 'mendwright.open()' if side.kind == 'sync' else 'mendwright.aio.open()'
    with pytest.raises(ValueError) as caught:
        side.open(page, provider=None, store_path=_store(tmp_path), cookies='yes')
    assert str(caught.value) == (
        entry + ' got cookies=\'yes\'. Use cookies="reject" to close consent banners without '
        'accepting, cookies="accept" to accept them, or leave cookies out to click nothing you '
        'did not ask for.')


# --- optional steps ----------------------------------------------------------------------------

def test_an_optional_step_that_is_not_there(side, tmp_path, caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(SIGNUP)
    provider = stub_provider('//button')
    side.open(page, provider=provider, store_path=_store(tmp_path))
    proxy = side.locate('promo', '#promo-close', optional=True, role='button')
    assert proxy.present is False
    assert side.act(proxy, 'click') is None
    assert page.actions == []
    assert provider.bundles == []                     # optional keys are never healed
    assert ("mendwright: skipped 'promo': it is optional (optional=True) and is not on the page."
            in _lines(caplog, logging.INFO))
    assert [(e['key'], e['reason']) for e in _events(tmp_path)] == [('promo', 'optional')]
    assert 'skipped' in repr(proxy)


def test_an_optional_step_that_is_there(side, tmp_path):
    page = _page(SIGNUP)
    side.open(page, provider=None, store_path=_store(tmp_path))
    proxy = side.locate('next', '#next', optional=True)
    assert proxy.present is True
    side.act(proxy, 'click')
    assert page.clicks('css=#next') == 1


def test_a_normal_proxy_is_present(side, tmp_path):
    side.open(_page(SIGNUP), provider=None, store_path=_store(tmp_path))
    assert side.locate('email', '#email').present is True


def test_an_async_proxy_says_present_only_once_awaited(tmp_path):
    async def main():
        heal = await mendwright.aio.open(AsyncFlowPage(_page(SIGNUP)), provider=None,
                                         store_path=_store(tmp_path))
        proxy = heal.locator('promo', '#promo', optional=True, timeout_ms=0)
        with pytest.raises(RuntimeError, match='Await the proxy first'):
            assert proxy.present is None   # not reached: reading it raises
        await proxy
        return proxy.present
    assert asyncio.run(main()) is False


# --- where a key was found, and the page guard ---------------------------------------------------

@pytest.mark.parametrize('url, path', [
    ('https://Shop.example/Signup/', '/signup'),
    ('https://shop.example/orders/12345/items?x=1#top', '/orders/*/items'),
    ('https://shop.example/u/0b5a3c1e-7d2f-4c55-9a0e-123456789abc', '/u/*'),
    ('https://shop.example/u/0b5a3c1e7d2f4c559a0e123456789abc/edit', '/u/*/edit'),
    ('file:///C:/tests/pages/signup.html', '/c:/tests/pages/signup.html'),
    ('https://shop.example/', None),
    ('https://shop.example', None),
    ('about:blank', None),
    ('data:text/html,<p>x</p>', None),
    ('', None),
    (None, None),
])
def test_page_path(url, path):
    assert page_path(url) == path


def test_the_page_a_key_was_found_on_is_saved(side, tmp_path):
    page = _page(SIGNUP, url='https://Shop.example/orders/42/review?step=2')
    side.open(page, provider=None, store_path=_store(tmp_path))
    side.locate('next', '#next')
    page.url = 'https://shop.example/orders/43/review'
    side.locate('next', '#next')                       # the same normalized page
    page.url = 'https://shop.example/help'
    side.locate('next', '#next')
    saved = json.loads((tmp_path / 'store' / 'overrides.json').read_text(encoding='utf-8'))
    entry = saved['selectors']['next']
    assert entry['lastPage'] == '/help'
    assert entry['pages'] == ['/help', '/orders/*/review']


def test_no_page_is_saved_for_about_blank(side, tmp_path):
    side.open(_page(SIGNUP, url='about:blank'), provider=None, store_path=_store(tmp_path))
    side.locate('next', '#next')
    saved = json.loads((tmp_path / 'store' / 'overrides.json').read_text(encoding='utf-8'))
    assert 'lastPage' not in saved['selectors']['next']


CONTINUE_PAGE = '<form><input id="email"><button id="continue" type="button">Continue</button></form>'
DETAILS_PAGE = ('<form><input id="phone"><button id="help" type="button">Help</button>'
                '<button id="finish" type="button">Finish later</button></form>')


def test_the_page_guard_declines_a_heal_on_another_page(side, tmp_path, caplog, stub_provider):
    page = _page(CONTINUE_PAGE, url='https://app.example/signup')
    provider = stub_provider("//button[@id='help']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    side.locate('continue', '#continue')

    page.set_html('<html><body>' + DETAILS_PAGE + '</body></html>')
    page.url = 'https://app.example/signup/details'
    with pytest.raises(AttentionRequired) as caught:
        side.locate('continue', '#continue')

    message = ("'continue' was last found on /signup, and this page is /signup/details. That "
               "looks like a change in the flow, not a renamed element, so mendwright did not "
               "heal it. If the step moved, update your script. If the URL itself changed in a "
               "redesign, pass heal_across_pages=True to "
               + ('mendwright.open().' if side.kind == 'sync' else 'mendwright.aio.open().'))
    assert str(caught.value).endswith('\n  -> ' + message)
    assert heal.coordinator.explain('continue') == message
    assert provider.bundles == []                     # declined before any model call
    guard = [e for e in _events(tmp_path) if e['event'] == 'flow.page_guard']
    assert [(g['key'], g['last_page'], g['page']) for g in guard] == [
        ('continue', '/signup', '/signup/details')]
    assert not any(e['event'] == 'heal.requested' for e in _journal(tmp_path))


def test_heal_across_pages_turns_the_guard_off(side, tmp_path, stub_provider):
    page = _page(CONTINUE_PAGE, url='https://app.example/signup')
    provider = stub_provider("//button[@id='finish']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path), heal_across_pages=True)
    side.locate('continue', '#continue')
    page.set_html('<html><body>' + DETAILS_PAGE + '</body></html>')
    page.url = 'https://app.example/signup/details'
    side.locate('continue', '#continue')
    assert len(provider.bundles) == 1
    bundle = provider.bundles[0]
    assert (bundle['page'], bundle['lastPage']) == ('/signup/details', '/signup')
    assert heal.heals[0].new == "//button[@id='finish']"


def test_the_guard_allows_a_page_the_key_was_found_on_before(side, tmp_path, stub_provider):
    page = _page(CONTINUE_PAGE, url='https://app.example/signup')
    provider = stub_provider("//button[normalize-space()='Next step']")
    side.open(page, provider=provider, store_path=_store(tmp_path))
    side.locate('continue', '#continue')
    page.url = 'https://app.example/checkout'
    side.locate('continue', '#continue')              # a shared control, found here too
    page.url = 'https://app.example/signup'
    page.set_html('<html><body><form><button type="button">Next step</button></form></body></html>')
    side.locate('continue', '#continue')              # heals on /signup, a page it knows
    assert len(provider.bundles) == 1
    assert provider.bundles[0]['lastPage'] == '/checkout'


def test_the_guard_needs_a_known_page_on_both_sides(side, tmp_path, stub_provider):
    page = _page(CONTINUE_PAGE, url='about:blank')
    provider = stub_provider("//button[@id='help']")
    side.open(page, provider=provider, store_path=_store(tmp_path))
    side.locate('continue', '#continue')
    page.set_html('<html><body>' + DETAILS_PAGE + '</body></html>')
    page.url = 'https://app.example/signup/details'
    side.locate('continue', '#continue')              # no page was saved, so no guard
    assert len(provider.bundles) == 1


def test_the_guard_reads_the_page_saved_by_an_earlier_run(side, tmp_path, stub_provider):
    first = Side('sync')
    first.open(_page(CONTINUE_PAGE, url='https://app.example/signup'), provider=None,
               store_path=_store(tmp_path))
    first.locate('continue', '#continue')

    provider = stub_provider("//button[@id='help']")
    page = _page(DETAILS_PAGE, url='https://app.example/signup/details')
    side.open(page, provider=provider, store_path=_store(tmp_path))
    with pytest.raises(AttentionRequired, match='was last found on /signup'):
        side.locate('continue', '#continue')
    assert provider.bundles == []


def test_a_destructive_key_keeps_its_own_message(side, tmp_path, stub_provider):
    page = _page(CONTINUE_PAGE, url='https://app.example/signup')
    side.open(page, provider=stub_provider(None), store_path=_store(tmp_path))
    side.locate('continue', '#continue', destructive=True)
    page.set_html('<html><body>' + DETAILS_PAGE + '</body></html>')
    page.url = 'https://app.example/signup/details'
    with pytest.raises(AttentionRequired, match='is marked as a destructive control'):
        side.locate('continue', '#continue')


def test_flow_change_message_names_the_entry_point():
    assert flow_change_message('k', '/a', '/b', 'mendwright.aio.open()').endswith(
        'pass heal_across_pages=True to mendwright.aio.open().')


@pytest.mark.parametrize('error, expected', [
    (RuntimeError('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - <div class="x"> '
                  'intercepts pointer events\n  - retrying click action'), True),
    (RuntimeError('Locator.fill: Timeout 30000ms exceeded.\n  - element is not visible'), True),
    (ElementClickInterceptedException('element click intercepted'), True),
    (type('ElementNotInteractableException', (Exception,), {})('not interactable'), True),
    (RuntimeError('Locator.click: Target closed'), False),
    (TimeoutError('timed out'), False),
])
def test_is_interception_error(error, expected):
    assert is_interception_error(error) is expected


def test_flow_events_reach_a_journal_without_the_named_helpers(tmp_path):
    from mendwright.flow import journal_event

    class Plain:
        def __init__(self):
            self.lines = []

        def event(self, name, **fields):
            self.lines.append((name, fields))

    plain = Plain()
    journal_event(plain, 'flow.skip', 'promo', reason='optional')
    assert plain.lines == [('flow.skip', {'key': 'promo', 'reason': 'optional'})]
    journal_event(object(), 'flow.skip', 'promo')       # neither: skipped, no error


def test_the_bundle_carries_the_pages_redacted_like_the_url():
    from mendwright.capture import build_bundle
    bundle = build_bundle('k', {'expectedRole': 'button'}, [], [], '<p></p>',
                          {'url': 'https://shop.example/card-4111111111111111/pay',
                           'page': '/card-4111111111111111/pay', 'lastPage': '/cart'})
    assert bundle['url'] == '[URL]'
    assert bundle['page'] == '/card-[redacted number]/pay'
    assert bundle['lastPage'] == '/cart'
    assert build_bundle('k', {}, [], [], '', {})['page'] is None


def test_the_journal_names_the_flow_events(tmp_path):
    from mendwright.journal import Journal
    journal = Journal(tmp_path / 'j.jsonl', clock=lambda: 'T')
    journal.interrupted('cookies', before='email')
    journal.skipped('promo', reason='optional')
    journal.page_guard('continue', last_page='/a', page='/b')
    lines = [json.loads(x) for x in (tmp_path / 'j.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [line['event'] for line in lines] == ['flow.interrupt', 'flow.skip', 'flow.page_guard']
