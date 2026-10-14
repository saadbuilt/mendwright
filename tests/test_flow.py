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
import gc
import json
import logging
import threading
import time

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, InvalidSelector
from mendwright.drivers.playwright_sync import playwright_selector
from mendwright.flow import (UNROUTED_STEP, flow_change_message, is_interception_error,
                             loop_message, page_path, skip_message, stuck_message,
                             unhandled_message)
from mendwright.interruptions import COOKIE_BANNER_KEY, InterruptionEvent, cookie_preset

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


# What Playwright raises when a click that was intercepted once went through on a retry,
# and the navigation it started did not commit before the timeout.
CLICK_DONE_LOG = '\n'.join([
    'Locator.click: Timeout 30000ms exceeded.',
    'Call log:',
    '  - waiting for locator("#next")',
    '  - attempting click action',
    '    - <div id="toast"> intercepts pointer events',
    '  - retrying click action',
    '    - performing click action',
    '    - click action done',
    '    - waiting for scheduled navigations to finish'])


def test_an_action_that_reached_the_page_is_not_retried_after_an_interception(side, tmp_path):
    page = _page(SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    proxy = side.locate('next', '#next')

    # The click went through (it placed an order, say), then timed out waiting for the next
    # page, while a banner opened. The call log still holds the earlier interception.
    def banner_and_timeout(p):
        p.doc.body.insert(0, lxml.html.fragment_fromstring(BANNER.replace('data-overlay=""', '')))
        p.fail_next = TimeoutError(CLICK_DONE_LOG)
    page.during_action.append(banner_and_timeout)
    with pytest.raises(TimeoutError, match='click action done'):
        side.act(proxy, 'click')
    assert page.clicks('css=#next') == 1
    assert page.clicks('css=#cookie-accept') == 0
    assert heal.handled_interruptions == []


def test_an_interruption_whose_selector_broke_is_named_when_it_blocks_a_step(side, tmp_path,
                                                                             caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(BANNER.replace('id="cookie-accept"', 'id="accept-all"') + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '#cookie-accept')
    assert side.locate('cookies', '#cookie-accept').present is False   # skipped, at INFO
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'ada@example.com')
    assert page.clicks('css=#accept-all') == 0
    assert [a for a in page.actions if a[1] == 'fill'] == [('css=#email', 'fill', ('ada@example.com',))]
    assert ("mendwright: the action on 'email' failed because something covered its element or "
            "it could not be reached, and none of the interruptions mendwright looks for "
            "(heal.interrupt(): 'cookies') matched a visible element, so it did not try the "
            "action again. If the element in the way is one of those registered with "
            "heal.interrupt(), its selector no longer matches: update it. Otherwise register the "
            "button that closes the element in the way with heal.interrupt().") in _lines(
                caplog, logging.WARNING)


def test_a_step_covered_by_something_the_cookies_preset_does_not_know(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    spinner = '<div id="spinner" data-overlay=""><p>Loading your account</p></div>'
    page = _page(spinner + SIGNUP)
    side.open(page, provider=None, store_path=_store(tmp_path), cookies='reject')
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'ada@example.com')
    assert [a for a in page.actions if a[1] == 'fill'] == [('css=#email', 'fill', ('ada@example.com',))]
    assert ("mendwright: the action on 'email' failed because something covered its element or "
            "it could not be reached, and none of the interruptions mendwright looks for (the "
            "cookies= preset) matched a visible element, so it did not try the action again. If "
            "the element in the way is a cookie banner the preset does not know, or anything "
            "else, register the button that closes it with heal.interrupt().") in _lines(
                caplog, logging.WARNING)


def test_an_element_that_is_not_visible_is_not_said_to_be_covered(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path), cookies='accept')
    heal.interrupt('newsletter', '#newsletter-close')
    proxy = side.locate('email', '#email')
    page.fail_next = RuntimeError('Locator.fill: Timeout 30000ms exceeded.\nCall log:\n'
                                  '  - waiting for element to be visible, enabled and editable\n'
                                  '  - element is not visible')
    with pytest.raises(RuntimeError, match='element is not visible'):
        side.act(proxy, 'fill', 'ada@example.com')
    warning = [line for line in _lines(caplog, logging.WARNING) if 'none of the interruptions' in line]
    assert warning == [
        "mendwright: the action on 'email' failed because something covered its element or it "
        "could not be reached, and none of the interruptions mendwright looks for "
        "(heal.interrupt(): 'newsletter'; the cookies= preset) matched a visible element, so it "
        "did not try the action again. If the element in the way is one of those registered "
        "with heal.interrupt(), its selector no longer matches: update it. Otherwise register "
        "the button that closes the element in the way with heal.interrupt(), for example for a "
        "cookie banner the cookies= preset does not know."]
    assert 'covered ' + repr('email') not in ' '.join(_lines(caplog))


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


# --- Playwright's overlay handler (page.add_locator_handler) ------------------------------------

class HandlerLocator(FlowLocator):
    """A FlowLocator with what mendwright builds an overlay locator from: or_(), then
    locator('visible=true') and .first. overlay_visible() is Playwright's check before an
    action (isVisibleInternal(selector, {strict: true}) in the main frame): a locator that
    matches several elements and is not narrowed to one fails with a strict mode violation."""

    def __init__(self, page, selector, others=(), narrowed=''):
        super().__init__(page, selector)
        self.selectors = (selector,) + tuple(others)
        self.narrowed = narrowed

    def _with(self, selectors, narrowed):
        return HandlerLocator(self._page, selectors[0], selectors[1:], narrowed)

    def or_(self, other):
        return self._with(self.selectors + other.selectors, self.narrowed)

    def locator(self, selector):
        return self._with(self.selectors, self.narrowed + ' >> ' + selector)

    @property
    def first(self):
        return self._with(self.selectors, self.narrowed + ' >> nth=0')

    def overlay_visible(self):
        found = [el for s in self.selectors for el in self._page.query(s)]
        if self.narrowed == ' >> visible=true >> nth=0':
            return any(not _hidden(el) for el in found)
        if len(found) > 1:
            raise RuntimeError('strict mode violation: resolved to ' + str(len(found)) + ' elements')
        return bool(found) and not _hidden(found[0])

    def scroll_into_view_if_needed(self, **kwargs):
        self._page.checkpoint()          # Playwright scrolls through its action checks too


class HandlerPage(FlowPage):
    """A FlowPage with Playwright 1.44's add_locator_handler and remove_locator_handler.
    Before each action it does what Playwright does: it runs the handler of each
    registered locator that is visible, never while a handler runs, then goes on with the
    action (no_wait_after=True). during_action callables run before that check, as a
    banner that opens while an action waits."""

    def __init__(self, html, **kwargs):
        super().__init__(html, **kwargs)
        self.handlers = []          # [locator, handler, options]
        self.removed = []
        self.handler_runs = 0
        self._running = False

    def locator(self, selector):
        return HandlerLocator(self, selector)

    def add_locator_handler(self, locator, handler, *, no_wait_after=None, times=None):
        self.handlers.append([locator, handler, {'no_wait_after': no_wait_after}])

    def remove_locator_handler(self, locator):
        self.removed.append(locator)
        self.handlers = [h for h in self.handlers if h[0] is not locator]

    def checkpoint(self):
        if self._running:
            return
        for locator, handler, _ in list(self.handlers):
            if locator.overlay_visible():
                self._running = True
                self.handler_runs += 1
                try:
                    handler(locator)
                finally:
                    self._running = False

    def act(self, selector, verb, args):
        if self.during_action:
            self.during_action.pop(0)(self)
        self.checkpoint()
        later, self.during_action = self.during_action, []
        try:
            return super().act(selector, verb, args)
        finally:
            self.during_action = later + self.during_action


class OldHandlerPage(HandlerPage):
    """Playwright 1.42 and 1.43: no no_wait_after (it never waited), no way to remove."""

    remove_locator_handler = None

    def add_locator_handler(self, locator, handler):
        self.handlers.append([locator, handler, {}])


CONSENT = ('<div id="consent" class="cookie-consent" data-overlay="" role="dialog">'
           '<p>We use cookies.</p><button id="consent-reject" type="button">Reject all</button>'
           '<button id="consent-close" type="button" aria-label="Close">×</button></div>')


def _handler_page(body, cls=HandlerPage):
    return cls('<html><body>' + body + '</body></html>')


def _open_sync(page, tmp_path, **kwargs):
    heal = mendwright.open(page, provider=None, store_path=_store(tmp_path), **kwargs)
    heal.coordinator.log = None
    return heal


def _show(html):
    return lambda p: p.doc.body.insert(0, lxml.html.fragment_fromstring(html))


def test_cookies_registers_one_overlay_handler_narrowed_to_one_visible_match(tmp_path):
    page = _handler_page(CONSENT + SIGNUP)
    _open_sync(page, tmp_path, cookies='reject')
    [[locator, _, options]] = page.handlers
    assert options == {'no_wait_after': True}
    assert locator.selectors == tuple(playwright_selector(s)
                                      for s in cookie_preset('reject').selectors)
    assert locator.narrowed == ' >> visible=true >> nth=0'
    # Reject and Close both match: as written, Playwright's strict check would fail.
    with pytest.raises(RuntimeError, match='strict mode violation'):
        HandlerLocator(page, locator.selectors[0], locator.selectors[1:]).overlay_visible()
    assert locator.overlay_visible() is True


def test_an_action_mendwright_does_not_route_has_the_banner_handled_first(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _handler_page(CONSENT + SIGNUP)
    heal = _open_sync(page, tmp_path, cookies='reject')

    page.locator('#email').click()      # as heal.page.get_by_label('Email').click() does

    assert page.by_id('consent') is None
    clicks = [s for s, v, _ in page.actions if v == 'click']
    assert len(clicks) == 2 and clicks[1] == '#email'
    assert [e.get('id') for e in _page(CONSENT).query(clicks[0])] == ['consent-reject']
    assert heal.handled_interruptions == [InterruptionEvent(COOKIE_BANNER_KEY, UNROUTED_STEP)]
    assert ("mendwright: handled interruption 'cookie_banner' before '<page action>'"
            in _lines(caplog, logging.WARNING))
    assert [(e['event'], e['before']) for e in _events(tmp_path)] == [
        ('flow.interrupt', '<page action>')]
    assert page.handler_runs == 1


def test_a_banner_that_opens_while_a_step_acts_counts_against_that_step(tmp_path):
    page = _handler_page(SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-accept')
    proxy = heal.locator('next', '#next', timeout_ms=0, poll_ms=0)
    page.during_action.append(_show(BANNER))
    proxy.click()
    assert page.clicks('css=#next') == 1        # Playwright handed over before the click
    assert page.clicks('css=#cookie-accept') == 1
    assert heal.handled_interruptions == [InterruptionEvent('cookies', 'next')]


def test_mendwright_s_own_click_on_the_banner_leaves_the_handler_out(tmp_path, caplog):
    hidden = SIGNUP.replace('<form>', '<form id="form" style="display: none">')
    page = _handler_page(BANNER.replace('id="cookie-accept"',
                                        'id="cookie-accept" data-reveal="form"') + hidden)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-accept')
    heal.locator('email', '#email', timeout_ms=0, poll_ms=0).fill('ada@example.com')
    assert page.handler_runs == 1               # Playwright called it for that click
    assert page.clicks('css=#cookie-accept') == 1
    assert heal.handled_interruptions == [InterruptionEvent('cookies', 'email')]
    assert not [line for line in _lines(caplog) if 'could not handle' in line]


def test_registering_a_key_again_replaces_its_handler(tmp_path):
    page = _handler_page(SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-accept')
    first = page.handlers[0][0]
    heal.interrupt('cookies', '#cookie-reject')
    assert page.removed == [first]
    assert [h[0].selectors for h in page.handlers] == [('css=#cookie-reject',)]


def test_on_playwright_1_42_an_old_handler_stays_but_does_nothing(tmp_path):
    page = _handler_page(BANNER + SIGNUP, cls=OldHandlerPage)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-close')
    heal.interrupt('cookies', '#cookie-accept')
    assert [options for _, _, options in page.handlers] == [{}, {}]
    old, new = page.handlers[0][1], page.handlers[1][1]
    old(None)
    assert page.by_id('banner') is not None and heal.handled_interruptions == []
    new(None)
    assert page.by_id('banner') is None
    assert heal.handled_interruptions == [InterruptionEvent('cookies', UNROUTED_STEP)]


def test_a_banner_that_stays_is_left_alone_after_three_times_in_a_row(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    sticky = BANNER.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _handler_page(sticky + SIGNUP)
    heal = _open_sync(page, tmp_path, timeout_ms=20)
    heal.interrupt('cookies', '#cookie-accept')

    for _ in range(5):                  # Playwright retries an action that stays covered
        with pytest.raises(RuntimeError, match='intercepts pointer events'):
            page.locator('#next').click()

    assert page.handler_runs == 5
    assert page.clicks('css=#cookie-accept') == 3
    assert [line for line in _lines(caplog, logging.WARNING) if 'times in a row' in line] == [
        'mendwright: ' + stuck_message('cookies', 3)]

    # Once mendwright sees it gone, a banner that comes back is handled again.
    page.by_id('banner').drop_tree()
    heal.locator('email', '#email', timeout_ms=0, poll_ms=0).fill('x')
    page.during_action.append(_show(BANNER))
    page.locator('#next').click()
    assert page.clicks('css=#cookie-accept') == 4 and page.clicks('#next') == 6


def test_the_overlay_handler_never_breaks_the_action(tmp_path, caplog, monkeypatch):
    page = _handler_page(BANNER + SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-accept')

    def broken(*args, **kwargs):
        raise ConnectionError('the browser went away')
    monkeypatch.setattr(heal, '_interrupt', broken)
    with pytest.raises(RuntimeError, match='intercepts pointer events'):   # the action's own
        page.locator('#next').click()
    assert ('mendwright: ' + unhandled_message('cookies', UNROUTED_STEP,
                                               ConnectionError('the browser went away'))
            in _lines(caplog, logging.WARNING))


def test_a_healer_that_is_gone_leaves_the_page_alone(tmp_path):
    page = _handler_page(BANNER + SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('cookies', '#cookie-accept')
    del heal
    gc.collect()
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        page.locator('#next').click()
    assert page.handler_runs == 1 and page.clicks('css=#cookie-accept') == 0


def test_no_overlay_handler_without_add_locator_handler(tmp_path):
    page = _page(BANNER + SIGNUP)       # a Frame, or Playwright before 1.42
    heal = _open_sync(page, tmp_path, cookies='accept')
    heal.interrupt('cookies', '#cookie-accept')
    assert heal._overlays == {}


class AsyncHandlerLocator(AsyncFlowLocator):
    """The async shape of HandlerLocator: each action runs the page's overlay check first."""

    def __init__(self, page, loc):
        super().__init__(loc)
        self._apage = page

    def or_(self, other):
        return AsyncHandlerLocator(self._apage, self._loc.or_(other._loc))

    def locator(self, selector):
        return AsyncHandlerLocator(self._apage, self._loc.locator(selector))

    @property
    def first(self):
        return AsyncHandlerLocator(self._apage, self._loc.first)

    async def scroll_into_view_if_needed(self, **kwargs):
        await self._apage.checkpoint()

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)

        async def run(*args, **kwargs):
            if self._apage.on_action:
                self._apage.on_action.pop(0)()
            await self._apage.checkpoint()
            return action(*args, **kwargs)
        return run


class AsyncHandlerPage(AsyncFlowPage):
    """HandlerPage for the async API: add_locator_handler and remove_locator_handler are
    coroutines, and so is the handler."""

    def __init__(self, page):
        super().__init__(page)
        self.handlers = []
        self.removed = []
        self.on_action = []
        self.handler_threads = []
        self._running = False

    def locator(self, selector):
        return AsyncHandlerLocator(self, HandlerLocator(self.sync, selector))

    async def add_locator_handler(self, locator, handler, *, no_wait_after=None, times=None):
        self.handlers.append([locator, handler, {'no_wait_after': no_wait_after}])

    async def remove_locator_handler(self, locator):
        self.removed.append(locator)
        self.handlers = [h for h in self.handlers if h[0] is not locator]

    async def checkpoint(self):
        if self._running:
            return
        for locator, handler, _ in list(self.handlers):
            if locator._loc.overlay_visible():
                self._running = True
                try:
                    await handler(locator)
                finally:
                    self._running = False


def test_the_async_healer_registers_and_runs_the_overlay_handler(tmp_path):
    sync = _page(SIGNUP)

    async def main():
        page = AsyncHandlerPage(sync)
        heal = await mendwright.aio.open(page, provider=None, store_path=_store(tmp_path),
                                         cookies='accept', timeout_ms=0)
        heal.coordinator.log = None
        assert [options for _, _, options in page.handlers] == [{'no_wait_after': True}]
        heal.interrupt('promo', '#promo-close')
        heal.interrupt('promo', '#promo-x')      # replaces the one before
        while heal._watching:
            await asyncio.sleep(0)
        assert len(page.removed) == 1
        assert [h[0]._loc.selectors for h in page.handlers][1:] == [('css=#promo-x',)]

        threads = []
        run = heal._core._overlay_run
        heal._core._overlay_run = lambda *a: threads.append(threading.get_ident()) or run(*a)
        sync.doc.body.insert(0, lxml.html.fragment_fromstring(ONETRUST))
        await page.locator('#email').fill('ada@example.com')      # not routed
        assert sync.by_id('onetrust-banner-sdk') is None

        sync.doc.body.insert(0, lxml.html.fragment_fromstring(ONETRUST))
        async with heal._lock:                  # a step of this healer is running
            await page.checkpoint()
        assert sync.by_id('onetrust-banner-sdk') is not None

        sync.by_id('onetrust-consent-sdk').drop_tree()
        nxt = await heal.locator('next', '#next', poll_ms=0)
        page.on_action.append(_show_async(sync, ONETRUST))
        await nxt.click()
        return heal, threads

    heal, threads = asyncio.run(main())
    assert len(threads) == 2 and threading.get_ident() not in threads   # in a worker thread
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        (COOKIE_BANNER_KEY, UNROUTED_STEP), (COOKIE_BANNER_KEY, 'next')]
    assert sync.clicks('css=#next') == 1


def _show_async(sync, html):
    return lambda: sync.doc.body.insert(0, lxml.html.fragment_fromstring(html))


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


def test_async_actions_awaited_together_handle_a_banner_once(tmp_path):
    page = _page(BANNER + SIGNUP)
    locked = []   # whether the healer's lock was held each time interruptions were looked for

    async def main():
        heal = await mendwright.aio.open(AsyncFlowPage(page), provider=None,
                                         store_path=_store(tmp_path), timeout_ms=0)
        heal.coordinator.log = None
        heal.interrupt('cookies', '#cookie-accept')
        look = heal._core._interrupt

        def spy(*args, **kwargs):
            locked.append(heal._lock.locked())
            return look(*args, **kwargs)
        heal._core._interrupt = spy
        email = await heal.locator('email', '#email', poll_ms=0)
        nxt = await heal.locator('next', '#next', poll_ms=0)
        await asyncio.gather(email.fill('ada@example.com'), nxt.click())
        return heal

    heal = asyncio.run(main())
    assert locked == [True, True]        # one action looked at a time
    assert page.clicks('css=#cookie-accept') == 1
    assert heal.handled_interruptions == [InterruptionEvent('cookies', 'email')]


# --- where a key was found, and the page guard ---------------------------------------------------

@pytest.mark.parametrize('url, path', [
    ('https://Shop.example/Signup/', '/signup'),
    ('https://shop.example/orders/12345/items?x=1#top', '/orders/*/items'),
    ('https://shop.example/u/0b5a3c1e-7d2f-4c55-9a0e-123456789abc', '/u/*'),
    ('https://shop.example/u/0b5a3c1e7d2f4c559a0e123456789abc/edit', '/u/*/edit'),
    ('file:///C:/tests/pages/signup.html', '/c:/tests/pages/signup.html'),
    # Ids that are not numbers or uuids: a MongoDB ObjectId, a ULID, a long generated token.
    ('https://shop.example/orders/507f1f77bcf86cd799439011', '/orders/*'),
    ('https://shop.example/orders/507F191E810C19729DE860EA/track', '/orders/*/track'),
    ('https://shop.example/o/01ARZ3NDEKTSV4RRFFQ69G5FAV', '/o/*'),
    ('https://shop.example/pay/cs_test_a1b2c3d4e5f6g7h8', '/pay/*'),
    # Page names stay: short ones with digits, and long slugs without two digits.
    ('https://shop.example/v2/2fa/step-3', '/v2/2fa/step-3'),
    ('https://shop.example/account/notification-settings', '/account/notification-settings'),
    # A doubled or trailing slash changes nothing.
    ('https://shop.example//signup//details/', '/signup/details'),
    ('https://shop.example/', None),
    ('https://shop.example//', None),
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


def test_the_guard_reads_a_generated_id_in_the_path_as_the_same_page(side, tmp_path,
                                                                     stub_provider):
    page = _page('<button id="track" type="button">Track package</button>',
                 url='https://shop.example/orders/507f1f77bcf86cd799439011')
    provider = stub_provider("//button[normalize-space()='Track package']")
    side.open(page, provider=provider, store_path=_store(tmp_path))
    side.locate('track', '#track')
    page.set_html('<html><body><button class="t" type="button">Track package</button></body></html>')
    page.url = 'https://shop.example/orders/507f191e810c19729de860ea'   # the next order
    side.locate('track', '#track')
    assert len(provider.bundles) == 1                 # healed, not declined as a flow change
    assert not [e for e in _events(tmp_path) if e['event'] == 'flow.page_guard']


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
    # The interceptor stopped the events of an attempt that was already performing: retried.
    (RuntimeError('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - performing click '
                  'action\n  - <div id="toast"> intercepts pointer events\n  - retrying click '
                  'action'), True),
    # The click reached the page and then timed out waiting for the next page: never again.
    (TimeoutError(CLICK_DONE_LOG), False),
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
