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
from dataclasses import replace
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, InvalidSelector
from mendwright.drivers.base import VISIBLE_MATCH_JS
from mendwright.drivers.playwright_sync import playwright_selector
from mendwright.flow import (UNROUTED_STEP, flow_change_message, handled_message,
                             is_interception_error, loop_message, page_path,
                             rejected_selector_message, run_cancelled_message,
                             run_failed_message, run_plain_message, several_message,
                             skip_message, stuck_message, unhandled_message)
from mendwright.interruptions import (COOKIE_BANNER_KEY, InterruptionEvent, cookie_preset,
                                      precise_selectors)

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

    def locator(self, selector):
        return AsyncFlowLocator(self._loc.locator(selector))

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


# --- an interruption that needs a form: run= ---------------------------------------------------

RELOGIN = ('<div id="relogin" data-overlay="" role="dialog" aria-label="Session expired">'
           '<p>Your session expired. Sign in again.</p><input id="user" name="user">'
           '<input id="password" name="password" type="password">'
           '<button id="signin" type="button">Sign in</button></div>')


def _relogin(side, calls, fail=None):
    """The run= function for RELOGIN, plain on the sync side and async on the other: it
    fills in the popup through the page it gets and signs in, which closes it. calls
    collects the page it got, once per call; fail, when given, is raised before it signs
    in."""
    if side.kind == 'sync':
        def relogin(page):
            calls.append(page)
            page.fill('#user', 'ada')
            page.fill('#password', 'not-a-real-password')
            if fail is not None:
                raise fail
            page.click('#signin')
        return relogin

    async def relogin(page):
        calls.append(page)
        await page.fill('#user', 'ada')
        await page.fill('#password', 'not-a-real-password')
        if fail is not None:
            raise fail
        await page.click('#signin')
    return relogin


def test_a_relogin_popup_is_filled_in_by_its_run_function_before_the_action(side, tmp_path,
                                                                            caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(RELOGIN + SIGNUP)
    provider = stub_provider('//never')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    calls = []
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls))

    side.step('email', '#email', 'fill', 'ada@example.com')

    # Once: its own actions on the popup look for no interruption, so they never run it again.
    assert len(calls) == 1 and calls[0] is heal.page
    assert page.by_id('relogin') is None
    assert page.by_id('email').get('value') == 'ada@example.com'
    assert [(s, v) for s, v, _ in page.actions] == [
        ('css=#user', 'fill'), ('css=#password', 'fill'), ('css=#signin', 'click'),
        ('css=#email', 'fill')]
    assert ("mendwright: handled interruption 'relogin' before 'email' with relogin()"
            in _lines(caplog, logging.WARNING))
    assert heal.handled_interruptions == [InterruptionEvent('relogin', 'email')]
    assert [(e['event'], e['key'], e['before'], e['action'], e['function'], e['selector'])
            for e in _events(tmp_path)] == [
        ('flow.interrupt', 'relogin', 'email', 'run', 'relogin', '#relogin')]
    assert provider.bundles == []


def test_a_relogin_popup_that_hides_the_page_is_handled_on_a_miss(side, tmp_path, stub_provider):
    page = _page(RELOGIN.replace('id="signin"', 'id="signin" data-reveal="form"')
                 + SIGNUP.replace('<form>', '<form id="form" style="display:none">'))
    provider = stub_provider("//input[@name='email']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    calls = []
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls))

    side.step('email', '#email', 'fill', 'ada@example.com', role='input')

    assert len(calls) == 1 and page.by_id('email').get('value') == 'ada@example.com'
    assert provider.bundles == [] and heal.heals == []


def test_a_popup_its_run_function_leaves_on_the_page_stops_the_step_after_three(side, tmp_path,
                                                                               stub_provider):
    sticky = RELOGIN.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _page(sticky + SIGNUP.replace('<form>', '<form style="display:none">'))
    provider = stub_provider('//input')
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    calls = []
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls))

    with pytest.raises(AttentionRequired) as caught:
        side.step('email', '#email', 'fill', 'x', role='input')

    assert str(caught.value) == loop_message('email', 'relogin', 3, 'relogin')
    assert str(caught.value).endswith(
        "Check that relogin(), the run= function registered with heal.interrupt('relogin', "
        "...), really closes it.")
    assert len(calls) == 3 and page.clicks('css=#signin') == 3
    assert len(heal.handled_interruptions) == 3
    assert provider.bundles == []


def test_a_popup_that_stays_over_the_form_ends_in_the_browser_s_own_error(side, tmp_path):
    sticky = RELOGIN.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _page(sticky + SIGNUP)
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    calls = []
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls))

    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'x')

    # Before the action, and once more after it was intercepted, then never again.
    assert len(calls) == 2
    assert [a for a in page.actions if a[0] == 'css=#email'] == [('css=#email', 'fill', ('x',))] * 2
    assert page.by_id('email').get('value') is None


def test_what_a_run_function_raises_is_raised_as_it_is(side, tmp_path, caplog, stub_provider):
    page = _page(RELOGIN + SIGNUP)
    heal = side.open(page, provider=stub_provider('//input'), store_path=_store(tmp_path))
    calls, error = [], ValueError('wrong password')
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls, fail=error))

    with pytest.raises(ValueError) as caught:
        side.step('email', '#email', 'fill', 'ada@example.com')

    assert caught.value is error and len(calls) == 1
    assert page.by_id('email').get('value') is None       # the step was not tried
    assert heal.handled_interruptions == []
    assert run_failed_message('relogin', 'email', 'relogin', error) == (
        "relogin(), the run= function of interruption 'relogin', raised an error while "
        "mendwright handled it before 'email' (ValueError: wrong password). The error is "
        "raised as it is.")
    assert ('mendwright: ' + run_failed_message('relogin', 'email', 'relogin', error)
            in _lines(caplog, logging.WARNING))
    assert [(e['key'], e['before'], e['action'], e['function'], e['outcome'], e['error'])
            for e in _events(tmp_path)] == [
        ('relogin', 'email', 'run', 'relogin', 'failed', 'ValueError')]


def test_an_attention_required_from_a_run_function_is_not_taken_for_a_miss(side, tmp_path,
                                                                           stub_provider):
    page = _page(RELOGIN + SIGNUP.replace('<form>', '<form style="display:none">'))
    provider = stub_provider("//input[@name='email']")
    heal = side.open(page, provider=provider, store_path=_store(tmp_path))
    error = AttentionRequired('mendwright: the sign-in form changed')
    heal.interrupt('relogin', '#relogin', run=_relogin(side, [], fail=error))

    with pytest.raises(AttentionRequired) as caught:
        side.step('email', '#email', 'fill', 'x', role='input')

    assert caught.value is error
    assert provider.bundles == []                                # 'email' was not healed
    assert [e['event'] for e in _events(tmp_path)] == ['flow.interrupt']   # nor skipped


@pytest.mark.parametrize('kind', SIDES)
def test_interrupt_run_rejects_arguments_that_can_never_work(kind, tmp_path):
    side = Side(kind)
    try:
        heal = side.open(_page(SIGNUP), provider=None, store_path=_store(tmp_path))

        def relogin(page):
            pass

        async def arelogin(page):
            pass

        with pytest.raises(TypeError) as caught:
            heal.interrupt('relogin', '#relogin', action='fill', args=('ada',), run=relogin)
        assert str(caught.value) == (
            "mendwright: heal.interrupt('relogin', ...) got run= and action='fill' and "
            "args=('ada',). run= does the whole interruption itself, so it takes no action= "
            "or args=: drop them, or drop run=.")
        with pytest.raises(TypeError, match=r"got run= and action='click'\. run= does"):
            heal.interrupt('relogin', '#relogin', action='click', run=relogin)
        with pytest.raises(TypeError, match=r"got run= and args='Escape'\. run= does"):
            heal.interrupt('relogin', '#relogin', args='Escape', run=relogin)
        with pytest.raises(TypeError, match='takes the function that handles the interruption'):
            heal.interrupt('relogin', '#relogin', run='relogin')
        with pytest.raises(TypeError, match=r"interrupt\('relogin', None\) removes an interruption"):
            heal.interrupt('relogin', None, run=relogin)
        with pytest.raises(InvalidSelector):
            heal.interrupt('relogin', '//div[', run=relogin)
        core = heal if kind == 'sync' else heal._core
        if kind == 'sync':
            with pytest.raises(TypeError, match='got an async function, which the healer of '
                                                r'mendwright\.open\(\) cannot await'):
                heal.interrupt('relogin', '#relogin', run=arelogin)
            assert core._interruptions == {}
        else:
            heal.interrupt('relogin', '#relogin', run=arelogin)     # awaited when handled
            assert core._interruptions['relogin'].action == 'run'
    finally:
        side.close()


def test_async_run_takes_a_plain_function_only_when_it_returns_an_awaitable(tmp_path):
    side = Side('async')
    try:
        page = _page(RELOGIN + SIGNUP)
        heal = side.open(page, provider=None, store_path=_store(tmp_path))
        heal.interrupt('relogin', '#relogin', run=lambda page: None)
        with pytest.raises(TypeError) as caught:
            side.step('email', '#email', 'fill', 'x')
        assert str(caught.value) == run_plain_message('relogin', '<lambda>', None)
        assert str(caught.value).startswith(
            "mendwright: <lambda>(), the run= function of interruption 'relogin', returned "
            "None, not an awaitable.")
        assert [e.get('outcome') for e in _events(tmp_path)] == ['failed']

        heal.interrupt('relogin', '#relogin', run=lambda page: page.click('#signin'))
        side.step('email', '#email', 'fill', 'x')
        assert page.by_id('relogin') is None and page.by_id('email').get('value') == 'x'
    finally:
        side.close()


def test_a_run_function_s_own_actions_handle_another_interruption(side, tmp_path,
                                                                   stub_provider):
    page = _page(RELOGIN + SIGNUP)
    heal = side.open(page, provider=stub_provider('//never'), store_path=_store(tmp_path))
    calls = []
    heal.interrupt('cookies', '#cookie-accept')
    heal.interrupt('relogin', '#relogin', run=_relogin(side, calls))
    page.during_action.append(_show(BANNER))    # it opens over the form as it is filled in

    side.step('email', '#email', 'fill', 'ada@example.com')

    # The banner was handled before the function's own field was tried again, and the
    # function was not called again: on the async side, without the step's lock.
    assert len(calls) == 1
    assert page.by_id('banner') is None and page.by_id('relogin') is None
    assert page.by_id('email').get('value') == 'ada@example.com'
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookies', '#user'), ('relogin', 'email')]


def test_a_step_waits_for_a_run_function_that_runs_in_another_context(tmp_path):
    # Playwright's overlay handler runs a run= function in a greenlet of its own, and when
    # the function takes longer than the action's timeout, the step goes on while it still
    # runs. Another thread stands in for that greenlet here.
    page = _page(RELOGIN + SIGNUP)
    heal = mendwright.open(page, provider=None, store_path=_store(tmp_path))
    heal.coordinator.log = None
    started, release, calls = threading.Event(), threading.Event(), []

    def relogin(p):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        p.fill('#user', 'ada')
        p.click('#signin')

    heal.interrupt('relogin', '#relogin', run=relogin)
    other = threading.Thread(
        target=lambda: heal.locator('next', '#next', timeout_ms=0, poll_ms=0).click())
    other.start()
    assert started.wait(10)
    threading.Timer(0.2, release.set).start()

    heal.locator('email', '#email', timeout_ms=0, poll_ms=0).fill('ada@example.com')
    other.join(10)

    assert len(calls) == 1          # not called a second time while it ran
    assert page.by_id('relogin') is None
    assert page.by_id('email').get('value') == 'ada@example.com'
    done = [s for s, _, _ in page.actions]
    assert done[:2] == ['css=#user', 'css=#signin']     # the fill came after the sign-in
    assert sorted(done[2:]) == ['css=#email', 'css=#next']


def test_async_cancelling_a_step_cancels_its_run_function(tmp_path, caplog):
    sync = _page(RELOGIN + SIGNUP)
    seen = []

    async def relogin(page):
        seen.append('started')
        try:
            await asyncio.sleep(10)         # waits for a sign-in that never comes
        except asyncio.CancelledError:
            seen.append('cancelled')
            raise
        seen.append('finished')

    async def main():
        heal = await mendwright.aio.open(AsyncFlowPage(sync), provider=None,
                                         store_path=_store(tmp_path))
        heal.coordinator.log = None
        heal.interrupt('relogin', '#relogin', run=relogin)

        async def step():
            email = await heal.locator('email', '#email', timeout_ms=0, poll_ms=0)
            await email.fill('x')

        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(step(), 0.5)
        elapsed = time.monotonic() - started
        for _ in range(5):
            await asyncio.sleep(0)
        return elapsed

    assert asyncio.run(main()) < 5
    assert seen == ['started', 'cancelled']
    assert sync.by_id('email').get('value') is None
    assert [(e['outcome'], e['error']) for e in _events(tmp_path)] == [
        ('failed', 'CancelledError')]
    assert run_cancelled_message('relogin', 'email', 'relogin') == (
        "relogin(), the run= function of interruption 'relogin', was cancelled while "
        "mendwright handled it before 'email', because that step was cancelled. 'relogin' "
        "may still be on the page, partly filled in.")
    assert [line for line in _lines(caplog, logging.WARNING) if 'run= function' in line] == [
        'mendwright: ' + run_cancelled_message('relogin', 'email', 'relogin')]


# --- a selector that matches several elements ---------------------------------------------------

class ManyLocator(FlowLocator):
    """A FlowLocator that also narrows to the visible matches, as Playwright's
    locator('visible=true') does, and runs VISIBLE_MATCH_JS on them (see
    PlaywrightSyncDriver.visible_match)."""

    def __init__(self, page, selector, visible=False):
        super().__init__(page, selector)
        self._visible = visible

    def _els(self):
        els = self._page.query(self._selector)
        return [e for e in els if not _hidden(e)] if self._visible else els

    def locator(self, selector):
        assert selector == 'visible=true'
        return ManyLocator(self._page, self._selector, True)

    def evaluate_all(self, js):
        els = self._els()
        if js != VISIBLE_MATCH_JS:
            return super().evaluate_all(js)
        if not els:
            return None
        return {'tag': els[0].tag, 'visible': min(len(els), 2),
                'html': lxml.html.tostring(els[0], encoding='unicode', with_tail=False)}


class ManyPage(FlowPage):
    """A FlowPage that also runs a class selector such as '.accept', so that a CSS selector
    can match several elements."""

    def query(self, selector):
        body = selector[len('css='):] if selector.startswith('css=') else selector
        if body.startswith('.'):
            return self.doc.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), $c)]",
                                  c=' ' + body[1:] + ' ')
        return super().query(selector)

    def locator(self, selector):
        return ManyLocator(self, selector)


# The banner's Accept all button, and a copy for small screens that CSS hides here.
ACCEPT_TWICE = ('<button id="accept-mobile" class="accept" type="button" style="display:none">'
                'Accept all</button><div id="banner" data-overlay="" role="dialog">'
                '<p>We use cookies.</p><button id="accept-desktop" class="accept" type="button">'
                'Accept all</button></div>')


def test_an_interruption_whose_selector_matches_several_elements_is_named_once_per_page(
        side, tmp_path, caplog):
    page = ManyPage('<html><body>' + ACCEPT_TWICE + SIGNUP + '</body></html>')
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '.accept')

    for _ in range(2):      # each action looks twice: before it, and after it was intercepted
        with pytest.raises(RuntimeError, match='intercepts pointer events'):
            side.step('email', '#email', 'fill', 'x')

    message = several_message('interruption', 'cookies', '.accept', 2, 0, ['#accept-desktop'],
                              playwright=True)
    assert message == (
        "interruption 'cookies' was skipped: its selector '.accept' matches 2 elements, and "
        "mendwright acts on a CSS or Playwright selector only when it matches one (an XPath is "
        "narrowed to its first visible match). Use a selector that matches only the element "
        "to act on, such as '#accept-desktop', which matches one visible element on this page.")
    skipped = [line for line in _lines(caplog, logging.WARNING) if 'was skipped' in line]
    assert skipped == ['mendwright: ' + message]
    assert page.clicks('css=#accept-desktop') == 0

    page.url = 'https://shop.example/signup/details'        # another page: named once there
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'x')
    assert len([line for line in _lines(caplog, logging.WARNING) if 'was skipped' in line]) == 2

    heal.interrupt('cookies', '#accept-desktop')             # the selector it suggested
    side.step('email', '#email', 'fill', 'ada@example.com')
    assert page.by_id('banner') is None
    assert page.by_id('email').get('value') == 'ada@example.com'


def test_a_selector_that_matches_several_hidden_elements_says_none_is_visible(side, tmp_path,
                                                                             caplog):
    hidden = ACCEPT_TWICE.replace('<div id="banner" data-overlay=""',
                                  '<div id="banner" style="display:none"')
    page = ManyPage('<html><body>' + hidden + SIGNUP + '</body></html>')
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '.accept')
    heal.interrupt('fine', "//button[@class='accept']")      # an XPath is narrowed: no WARNING
    side.step('email', '#email', 'fill', 'x')
    assert [line for line in _lines(caplog, logging.WARNING) if 'was skipped' in line] == [
        'mendwright: ' + several_message('interruption', 'cookies', '.accept', 2, 0, [],
                                         visible=False, playwright=True)]
    assert 'None of them is visible now.' in several_message(
        'interruption', 'cookies', '.accept', 2, 0, [], visible=False, playwright=True)


def test_the_cookies_preset_is_never_named_for_matching_several(side, tmp_path, caplog):
    page = ManyPage('<html><body>' + ACCEPT_TWICE + SIGNUP + '</body></html>')
    heal = side.open(page, provider=None, store_path=_store(tmp_path), cookies='reject')
    # Its selectors are mendwright's, not the user's to make precise: give it one that
    # matches two elements.
    core = heal if side.kind == 'sync' else heal._core
    core._interruptions[COOKIE_BANNER_KEY] = replace(core._interruptions[COOKIE_BANNER_KEY],
                                                     selectors=('.accept',), probe=None)
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'x')
    assert not [line for line in _lines(caplog) if 'was skipped' in line]


def test_a_selector_that_matches_several_is_not_named_when_a_later_one_matched(side, tmp_path,
                                                                               caplog):
    page = ManyPage('<html><body>' + ACCEPT_TWICE + SIGNUP + '</body></html>')
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', ['.accept', '#accept-desktop'])

    side.step('email', '#email', 'fill', 'x')

    assert page.clicks('css=#accept-desktop') == 1 and page.by_id('banner') is None
    assert page.by_id('email').get('value') == 'x'
    assert not [line for line in _lines(caplog) if 'was skipped' in line]


def test_a_selector_with_several_visible_matches_suggests_none_of_them(side, tmp_path, caplog):
    # A chat widget's Close button comes first on the page, and the banner's own button
    # matches too: a selector built from the first would close the chat instead.
    chat = '<button id="chat-close" class="close" type="button">Close</button>'
    banner = ('<div id="banner" data-overlay="" role="dialog"><p>We use cookies.</p>'
              '<button id="accept-all" class="close" type="button">Accept all</button></div>')
    page = ManyPage('<html><body>' + chat + banner + SIGNUP + '</body></html>')
    heal = side.open(page, provider=None, store_path=_store(tmp_path))
    heal.interrupt('cookies', '.close')

    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('email', '#email', 'fill', 'x')

    message = several_message('interruption', 'cookies', '.close', 2, 0, [], visible=2,
                              playwright=True)
    assert message == (
        "interruption 'cookies' was skipped: its selector '.close' matches 2 elements, and "
        "mendwright acts on a CSS or Playwright selector only when it matches one (an XPath is "
        "narrowed to its first visible match). More than one of them is visible, so mendwright "
        "cannot tell which one is meant. Use a selector that matches only the element to act "
        "on: its id or test id, its role and name, as in role=button[name=\"Accept all\"], its "
        "whole text, as in text=\"Accept all\", or an XPath of its tag and text, as in "
        "//button[normalize-space()='Accept all'].")
    assert [line for line in _lines(caplog, logging.WARNING) if 'was skipped' in line] == [
        'mendwright: ' + message]
    assert page.clicks('css=#chat-close') == 0
    # Even when found is given, two visible matches name none of them.
    assert '#chat-close' not in several_message('interruption', 'cookies', '.close', 2, 0,
                                                ['#chat-close'], visible=2)


def test_the_guide_quotes_the_messages_the_code_writes():
    guide = (Path(__file__).resolve().parents[1] / 'docs' / 'guide.md').read_text(encoding='utf-8')
    landmark = several_message('landmark', 'payment', '.step-title', 2, 1, [])
    two = several_message('interruption', 'cookies', '.close', 2, 0, [], visible=2)
    loop = loop_message('search', 'session_expired', 3, 'relogin')
    for message in (
            handled_message('session_expired', 'search', 'relogin'),
            run_failed_message('session_expired', 'search', 'relogin',
                               LookupError('no password for ada in the vault')),
            run_cancelled_message('session_expired', 'search', 'relogin'),
            several_message('interruption', 'cookies', '.cookie-accept', 2, 0,
                            ['#accept-all', 'role=button[name="Accept all"]'], playwright=True),
            two[two.index('More than one'):two.index(' Use a selector')],
            landmark[:landmark.index(' Use a landmark')],
            loop[loop.index('Check that'):]):
        assert message in guide, message


def test_precise_selectors():
    button = {'tag': 'button', 'role': 'button', 'text': 'Accept all',
              'outer_html': '<button id="accept-desktop" class="accept" type="button">'
                            'Accept all</button>'}
    assert precise_selectors(button, playwright=True) == [
        '#accept-desktop', 'button#accept-desktop', 'button[type="button"]',
        'role=button[name="Accept all"]', 'text="Accept all"',
        "//button[normalize-space()='Accept all']"]
    assert precise_selectors(button) == [
        '#accept-desktop', 'button#accept-desktop', 'button[type="button"]',
        "//button[normalize-space()='Accept all']"]
    dialog = {'tag': 'div', 'role': 'div', 'text': 'x' * 61,
              'outer_html': '<div role="dialog" aria-label="Session expired">...</div>'}
    assert precise_selectors(dialog, playwright=True) == [
        'div[aria-label="Session expired"]', 'div[role="dialog"]',
        'div[aria-label="Session expired"][role="dialog"]']
    quoted = {'tag': 'a', 'role': 'link', 'text': 'Don\'t "show" again', 'outer_html': ''}
    assert precise_selectors(quoted, playwright=True) == [
        'role=link[name="Don\'t \\"show\\" again"]', 'text="Don\'t \\"show\\" again"',
        '//a[normalize-space()=concat(\'Don\', "\'", \'t "show" again\')]']


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

    def same(self, other):
        """Locator._equals: the same frame and selector."""
        return (self._page, self.selectors, self.narrowed) == (other._page, other.selectors,
                                                                other.narrowed)

    def _parse(self):
        """Raise as Playwright does for a selector the browser cannot parse."""
        for selector in self.selectors:
            if selector in getattr(self._page, 'rejects', ()):
                raise RuntimeError('Locator.count: Unexpected token "" while parsing css '
                                   'selector "' + selector[len('css='):] + '".')

    def count(self):
        self._parse()
        return super().count()

    def overlay_visible(self):
        self._parse()           # Playwright's check re-raises a parse error
        found = [el for s in self.selectors for el in self._page.query(s)]
        if self.narrowed == ' >> visible=true >> nth=0':
            return any(not _hidden(el) for el in found)
        if len(found) > 1:
            raise RuntimeError('strict mode violation: resolved to ' + str(len(found)) + ' elements')
        return bool(found) and not _hidden(found[0])

    def scroll_into_view_if_needed(self, **kwargs):
        self._page.checkpoint()          # Playwright scrolls through its action checks too


class MainFrame:
    parent_frame = None


class ChildFrame:
    parent_frame = MainFrame()


class HandlerPage(FlowPage):
    """A FlowPage with Playwright 1.44's add_locator_handler and remove_locator_handler.
    Before each action it does what Playwright does: it runs the handler of each
    registered locator that is visible, never while a handler runs, then goes on with the
    action (no_wait_after=True). during_action callables run before that check, as a
    banner that opens while an action waits. on() keeps event listeners, which emit()
    calls, and rejects lists the selectors the browser cannot parse."""

    def __init__(self, html, **kwargs):
        super().__init__(html, **kwargs)
        self.handlers = []          # [locator, handler, options]
        self.removed = []
        self.handler_runs = 0
        self.listeners = {}         # event -> [callback]
        self.rejects = ()
        self._running = False

    def locator(self, selector):
        return HandlerLocator(self, selector)

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event, arg):
        for callback in self.listeners.get(event, []):
            callback(arg)

    def add_locator_handler(self, locator, handler, *, no_wait_after=None, times=None):
        self.handlers.append([locator, handler, {'no_wait_after': no_wait_after}])

    def remove_locator_handler(self, locator):
        self.removed.append(locator)
        self.handlers = [h for h in self.handlers if not h[0].same(locator)]

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
    assert [r.selectors for r in page.removed] == [first.selectors]
    assert [h[0].selectors for h in page.handlers] == [('css=#cookie-reject',)]


def test_a_page_keeps_one_handler_per_key_for_all_its_healers(tmp_path):
    page = _handler_page(CONSENT + SIGNUP)
    first, second, third = (_open_sync(page, tmp_path / str(n), cookies='reject')
                            for n in range(3))
    assert len(page.handlers) == 1 and page.removed == []
    page.locator('#email').click()
    assert [len(h.handled_interruptions) for h in (first, second, third)] == [0, 0, 1]

    del third               # the newest healer still alive acts for the handler
    gc.collect()
    page.during_action.append(_show(CONSENT))
    page.locator('#email').click()
    assert [len(h.handled_interruptions) for h in (first, second)] == [0, 1]

    first.interrupt(COOKIE_BANNER_KEY, '#consent-reject')     # other selectors: replaced
    assert [h[0].selectors for h in page.handlers] == [('css=#consent-reject',)]
    assert len(page.removed) == 1


def test_the_handler_stays_out_while_any_healer_of_the_page_clicks_the_banner(tmp_path):
    hidden = SIGNUP.replace('<form>', '<form id="form" style="display: none">')
    page = _handler_page(BANNER.replace('id="cookie-accept"',
                                        'id="cookie-accept" data-reveal="form"') + hidden)
    first, second = _open_sync(page, tmp_path / 'a'), _open_sync(page, tmp_path / 'b')
    for heal in (first, second):
        heal.interrupt('cookies', '#cookie-accept')     # one handler, acting for second
    first.locator('email', '#email', timeout_ms=0, poll_ms=0).fill('ada@example.com')
    assert page.handler_runs == 1 and page.clicks('css=#cookie-accept') == 1
    assert first.handled_interruptions == [InterruptionEvent('cookies', 'email')]
    assert second.handled_interruptions == []


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
    assert heal._overlays.handlers == {}


def test_with_nothing_registered_no_overlay_handler_is_added(tmp_path):
    page = _handler_page(SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.locator('email', '#email', timeout_ms=0, poll_ms=0).fill('ada@example.com')
    page.locator('#next').click()
    assert page.handlers == [] and page.listeners == {} and heal._overlays.handlers == {}


def test_a_banner_left_alone_is_handled_again_once_the_page_navigates(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    sticky = BANNER.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _handler_page(sticky + SIGNUP)
    heal = _open_sync(page, tmp_path, timeout_ms=20)
    heal.interrupt('cookies', '#cookie-accept')
    assert sorted(page.listeners) == ['framenavigated', 'load']

    def stuck_three_times():
        for _ in range(4):
            with pytest.raises(RuntimeError, match='intercepts pointer events'):
                page.locator('#next').click()

    stuck_three_times()
    assert page.clicks('css=#cookie-accept') == 3
    page.emit('framenavigated', ChildFrame())       # an iframe navigated: still left alone
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        page.locator('#next').click()
    assert page.clicks('css=#cookie-accept') == 3

    page.emit('framenavigated', MainFrame())        # goto(), a link, a history change
    stuck_three_times()
    page.emit('load', page)                         # set_content()
    stuck_three_times()
    assert page.clicks('css=#cookie-accept') == 9
    assert [line for line in _lines(caplog, logging.WARNING) if 'times in a row' in line] == [
        'mendwright: ' + stuck_message('cookies', 3)] * 3


def test_a_selector_the_browser_rejects_is_left_out_of_the_handler(tmp_path, caplog):
    page = _handler_page(BANNER + SIGNUP)
    page.rejects = ('css=div.promo[',)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('promo', 'div.promo[')
    assert page.handlers == [] and heal._overlays.handlers == {}
    heal.interrupt('cookies', ['div.promo[', '#cookie-accept'])
    assert [h[0].selectors for h in page.handlers] == [('css=#cookie-accept',)]

    page.locator('#next').click()       # the page's own action: no parse error, banner closed
    assert page.by_id('banner') is None
    error = RuntimeError('Locator.count: Unexpected token "" while parsing css selector '
                         '"div.promo[".')
    warned = [line for line in _lines(caplog, logging.WARNING) if 'rejected' in line]
    assert warned == ['mendwright: ' + rejected_selector_message(key, 'div.promo[', error)
                      for key in ('promo', 'cookies')]


def test_replacing_a_key_keeps_the_handler_of_another_key_with_the_same_selectors(tmp_path):
    # Playwright's remove_locator_handler removes every handler with an equal locator.
    page = _handler_page(BANNER + SIGNUP)
    heal = _open_sync(page, tmp_path)
    heal.interrupt('promo', '#cookie-accept')
    heal.interrupt('promo2', '#cookie-accept')
    heal.interrupt('promo', '#cookie-reject')
    assert page.removed == []
    assert [h[0].selectors for h in page.handlers] == [
        ('css=#cookie-accept',), ('css=#cookie-accept',), ('css=#cookie-reject',)]
    page.locator('#next').click()
    assert page.clicks('css=#cookie-accept') == 1 and page.by_id('banner') is None
    assert heal.handled_interruptions == [InterruptionEvent('promo2', UNROUTED_STEP)]
    heal.interrupt('promo2', None)          # the last one on those selectors: removed
    assert [h[0].selectors for h in page.handlers] == [('css=#cookie-reject',)]


def test_interrupt_with_none_removes_it_and_its_handler_once_no_healer_has_it(tmp_path):
    page = _handler_page(SIGNUP)
    first, second = (_open_sync(page, tmp_path / n, cookies='reject') for n in 'ab')
    assert len(page.handlers) == 1
    second.interrupt(COOKIE_BANNER_KEY, None)
    assert len(page.handlers) == 1 and page.removed == []       # first still has it
    assert COOKIE_BANNER_KEY not in second._interruptions

    page.during_action.append(_show(CONSENT))
    page.locator('#email').click()
    assert [len(h.handled_interruptions) for h in (first, second)] == [1, 0]

    first.interrupt(COOKIE_BANNER_KEY, None)
    first.interrupt('never-registered', None)       # nothing to remove
    assert page.handlers == [] and len(page.removed) == 1
    assert first._interruptions == {} and first._overlays.handlers == {}
    page.during_action.append(_show(CONSENT))
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        page.locator('#email').click()
    assert [len(h.handled_interruptions) for h in (first, second)] == [1, 0]


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
        self.handlers = [h for h in self.handlers if not h[0]._loc.same(locator._loc)]

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


class SlowHandlerPage(AsyncHandlerPage):
    """An AsyncHandlerPage whose add_locator_handler and remove_locator_handler give the
    event loop a turn before they act, as Playwright's round trips do."""

    async def add_locator_handler(self, locator, handler, **options):
        await asyncio.sleep(0)
        await super().add_locator_handler(locator, handler, **options)

    async def remove_locator_handler(self, locator):
        await asyncio.sleep(0)
        await super().remove_locator_handler(locator)


async def _open_async(page, tmp_path, **kwargs):
    heal = await mendwright.aio.open(page, provider=None, store_path=_store(tmp_path),
                                     timeout_ms=0, **kwargs)
    heal.coordinator.log = None
    return heal


async def _registered(heal):
    while heal._watching:
        await asyncio.sleep(0)


def test_async_interrupt_calls_in_a_row_leave_one_handler_per_key(tmp_path):
    async def main():
        page = SlowHandlerPage(_page(SIGNUP))
        heal = await _open_async(page, tmp_path)
        for selector in ('#x', '#x', '#y', '#x'):       # no await in between
            heal.interrupt('promo', selector)
        await _registered(heal)
        selectors = [h[0]._loc.selectors for h in page.handlers]
        heal.interrupt('promo', None)
        await _registered(heal)
        return selectors, page.handlers, heal._core._overlays.handlers

    selectors, left, entries = asyncio.run(main())
    assert selectors == [('css=#x',)]
    assert left == [] and entries == {}


def test_async_interrupt_from_another_thread_changes_the_handler_on_the_page_s_loop(tmp_path):
    async def main():
        page = AsyncHandlerPage(_page(SIGNUP))
        heal = await _open_async(page, tmp_path)
        await (await heal.locator('email', '#email', poll_ms=0)).fill('ada@example.com')
        nothing = list(page.handlers)       # nothing registered: no handler, no cost
        await asyncio.to_thread(heal.interrupt, 'promo', '#promo-close')
        added = [h[0]._loc.selectors for h in page.handlers]
        await asyncio.to_thread(heal.interrupt, 'promo', None)
        return nothing, added, page.handlers

    nothing, added, left = asyncio.run(main())
    assert nothing == [] and added == [('css=#promo-close',)] and left == []


def test_async_a_selector_the_browser_rejects_is_left_out_of_the_handler(tmp_path, caplog):
    sync = _page(BANNER + SIGNUP)

    async def main():
        page = AsyncHandlerPage(sync)
        sync.rejects = ('css=div.promo[',)
        heal = await _open_async(page, tmp_path)
        heal.interrupt('cookies', ['div.promo[', '#cookie-accept'])
        await _registered(heal)
        selectors = [h[0]._loc.selectors for h in page.handlers]
        await page.locator('#next').click()
        return heal, selectors

    heal, selectors = asyncio.run(main())
    assert selectors == [('css=#cookie-accept',)] and sync.by_id('banner') is None
    assert heal.handled_interruptions == [InterruptionEvent('cookies', UNROUTED_STEP)]
    assert [line for line in _lines(caplog, logging.WARNING) if 'rejected' in line] == [
        'mendwright: ' + rejected_selector_message(
            'cookies', 'div.promo[', RuntimeError('Locator.count: Unexpected token "" while '
                                                  'parsing css selector "div.promo[".'))]


def test_the_overlay_handler_runs_a_run_function_and_stays_out_of_its_actions(tmp_path):
    page = _handler_page(RELOGIN + SIGNUP)
    heal = _open_sync(page, tmp_path)
    calls = []

    def relogin(p):
        calls.append(p)
        p.fill('#user', 'ada')
        p.click('#signin')

    heal.interrupt('relogin', '#relogin', run=relogin)
    page.locator('#email').click()      # not routed, as heal.page.get_by_label('Email').click()

    assert len(calls) == 1 and calls[0] is heal.page and page.by_id('relogin') is None
    assert page.handler_runs == 1       # Playwright runs no handler inside a handler
    assert heal.handled_interruptions == [InterruptionEvent('relogin', UNROUTED_STEP)]

    # A step mendwright routes handles the popup itself. Playwright's check before each of
    # the function's own actions finds it there and calls the handler, which stays out.
    email = heal.locator('email', '#email', timeout_ms=0, poll_ms=0)
    page.doc.body.insert(0, lxml.html.fragment_fromstring(RELOGIN))
    email.fill('ada@example.com')
    assert len(calls) == 2 and page.handler_runs > 1
    assert heal.handled_interruptions[1:] == [InterruptionEvent('relogin', 'email')]
    assert page.by_id('email').get('value') == 'ada@example.com'


def test_a_run_function_that_raises_in_the_overlay_handler_is_not_run_before_every_action(
        tmp_path, caplog):
    page = _handler_page(RELOGIN + SIGNUP)
    heal = _open_sync(page, tmp_path)
    calls, error = [], ValueError('wrong password')

    def relogin(p):
        calls.append(p)
        raise error

    heal.interrupt('relogin', '#relogin', run=relogin)
    for selector in ('#email', '#next'):        # not routed: each click fails on the popup
        with pytest.raises(RuntimeError, match='intercepts pointer events'):
            page.locator(selector).click()

    assert len(calls) == 1
    assert [line for line in _lines(caplog, logging.WARNING) if 'run= function' in line] == [
        'mendwright: ' + run_failed_message('relogin', UNROUTED_STEP, 'relogin', error,
                                            overlay=True)]
    with pytest.raises(ValueError, match='wrong password'):
        heal.page.fill('#email', 'x')           # a step mendwright routes raises it
    assert len(calls) == 2


def test_the_overlay_handler_stays_out_of_a_lookup_and_the_step_calls_the_function(tmp_path,
                                                                                  caplog):
    page = _handler_page(RELOGIN + SIGNUP)
    heal = _open_sync(page, tmp_path)
    calls, error = [], ValueError('wrong password')

    def relogin(p):
        calls.append(p)
        raise error

    heal.interrupt('relogin', '#relogin', run=relogin)
    with pytest.raises(ValueError) as caught:
        heal.page.fill('#email', 'x')       # Playwright's check runs as the lookup scrolls

    # The handler stayed out of the lookup, as it stays out of mendwright's own clicks, so
    # the function did not run inside it, where it would get only the scroll's timeout. The
    # step called it before the action, once, and raised what it raised.
    assert page.handler_runs >= 1
    assert caught.value is error and len(calls) == 1
    assert page.by_id('email').get('value') is None
    assert [(e['before'], e['outcome']) for e in _events(tmp_path)] == [('#email', 'failed')]
    assert [line for line in _lines(caplog, logging.WARNING) if 'run= function' in line] == [
        'mendwright: ' + run_failed_message('relogin', '#email', 'relogin', error)]


def test_async_an_action_raises_what_its_run_function_raised_in_the_overlay_handler(tmp_path):
    sync = _page(SIGNUP)
    calls, error = [], ValueError('wrong password')

    async def relogin(p):
        calls.append(p)
        raise error

    async def main():
        page = AsyncHandlerPage(sync)
        heal = await _open_async(page, tmp_path)
        heal.interrupt('relogin', '#relogin', run=relogin)
        await _registered(heal)
        nxt = await heal.locator('next', '#next', poll_ms=0)
        page.on_action.append(_show_async(sync, RELOGIN))     # it opens as the click starts
        with pytest.raises(ValueError) as caught:
            await nxt.click()
        return caught.value

    assert asyncio.run(main()) is error
    assert len(calls) == 1 and sync.clicks('css=#next') == 1


def test_async_the_overlay_handler_awaits_a_run_function_whose_calls_skip_the_lock(tmp_path):
    sync = _page(RELOGIN + SIGNUP)
    calls = []

    async def relogin(p):
        calls.append(threading.get_ident())
        await p.fill('#user', 'ada')
        await p.click('#signin')

    async def main():
        page = AsyncHandlerPage(sync)
        heal = await _open_async(page, tmp_path)
        heal.interrupt('relogin', '#relogin', run=relogin)
        await _registered(heal)
        await page.locator('#email').fill('ada@example.com')     # not routed
        sync.doc.body.insert(0, lxml.html.fragment_fromstring(RELOGIN))
        await (await heal.locator('next', '#next', poll_ms=0)).click()   # routed, under the lock
        return heal, threading.get_ident()

    heal, loop_thread = asyncio.run(main())
    assert calls == [loop_thread] * 2         # awaited on the event loop, never in a thread
    assert sync.by_id('relogin') is None and sync.by_id('email').get('value') == 'ada@example.com'
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('relogin', UNROUTED_STEP), ('relogin', 'next')]
    assert sync.clicks('css=#next') == 1


# A session popup over nothing: an action goes through while it shows.
ASIDE_RELOGIN = RELOGIN.replace(' data-overlay=""', '')


def test_an_action_that_went_through_raises_what_its_run_function_raised_meanwhile(tmp_path):
    page = _handler_page(SIGNUP)
    heal = _open_sync(page, tmp_path)
    calls, error = [], ValueError('wrong password')

    def relogin(p):
        calls.append(p)
        raise error

    heal.interrupt('relogin', '#relogin', run=relogin)
    nxt = heal.locator('next', '#next', timeout_ms=0, poll_ms=0)
    page.during_action.append(_show(ASIDE_RELOGIN))    # it opens as the click starts

    with pytest.raises(ValueError) as caught:
        nxt.click()

    assert caught.value is error and len(calls) == 1
    assert page.clicks('css=#next') == 1               # the click went through all the same
    assert [(e['before'], e['outcome']) for e in _events(tmp_path)] == [('next', 'failed')]


def test_async_an_action_that_went_through_raises_what_its_run_function_raised(tmp_path):
    sync = _page(SIGNUP)
    calls, error = [], ValueError('wrong password')

    async def relogin(p):
        calls.append(p)
        raise error

    async def main():
        page = AsyncHandlerPage(sync)
        heal = await _open_async(page, tmp_path)
        heal.interrupt('relogin', '#relogin', run=relogin)
        await _registered(heal)
        nxt = await heal.locator('next', '#next', poll_ms=0)
        page.on_action.append(_show_async(sync, ASIDE_RELOGIN))
        with pytest.raises(ValueError) as caught:
            await nxt.click()
        return caught.value

    assert asyncio.run(main()) is error
    assert len(calls) == 1 and sync.clicks('css=#next') == 1


def test_a_popup_its_run_function_leaves_is_left_alone_after_three_times_in_a_row(tmp_path,
                                                                                  caplog):
    sticky = RELOGIN.replace('data-overlay=""', 'data-overlay="" data-sticky=""')
    page = _handler_page(sticky + SIGNUP)
    heal = _open_sync(page, tmp_path, timeout_ms=20)
    calls = []

    def relogin(p):
        calls.append(p)
        p.click('#signin')          # the password is wrong: the popup stays

    heal.interrupt('relogin', '#relogin', run=relogin)
    for _ in range(4):              # Playwright retries an action that stays covered
        with pytest.raises(RuntimeError, match='intercepts pointer events'):
            page.locator('#next').click()

    assert len(calls) == 3
    message = stuck_message('relogin', 3, 'relogin')
    assert message.endswith(
        "Check that relogin(), the run= function registered with heal.interrupt('relogin', "
        "...), really closes it.")
    assert [line for line in _lines(caplog, logging.WARNING) if 'times in a row' in line] == [
        'mendwright: ' + message]


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
