"""Steps in any order, offline: heal.step() and heal.run_steps() (mendwright.steps).

Every flow runs twice, on mendwright.open() and on mendwright.aio.open(), through one small
adapter, so the two make the same decisions. The page is Shop: an lxml checkout with the
sync Playwright shape that really changes when it is acted on. Each page is HTML at a URL,
a click on an element with data-go="<url>" goes to that page, fill() sets the value, and an
element with data-overlay covers the page until a click inside it closes it (data-closes
names the element it removes, data-reveal the one it shows). A step's function is written
once as a list of calls and run as a plain function on the sync side and an async one on
the async side. No browser, network or model.
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, InvalidSelector
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import SeleniumDriver
from mendwright.fingerprint import FINGERPRINT_JS
from mendwright.flow import InterruptionLoop
from mendwright.steps import (WAITING_STEP, Step, again_message, cap_message, changed_message,
                              dropped_message, landmark_targets, order_message, plain_message,
                              ran_message, scope_message, unknown_message, url_matches,
                              url_pattern)
from mendwright.store import load_store

SIDES = ('sync', 'async')
SITE = 'https://shop.test/checkout/'


# --- the shop ------------------------------------------------------------------------------

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


class ShopLocator:
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
        if js == FINGERPRINT_JS or len(els) != 1:
            return None
        return {'tag': els[0].tag, 'html': lxml.html.tostring(els[0], encoding='unicode',
                                                              with_tail=False)}

    def get_attribute(self, name, **kwargs):
        els = self._els()
        return els[0].get(name) if els else None

    def __getattr__(self, verb):
        if verb not in ('click', 'fill', 'check', 'press'):
            raise AttributeError(verb)

        def action(*args, **kwargs):
            return self._page.act(self._selector, verb, args)
        return action


class Shop:
    """A checkout: pages {path: html}, served at SITE + path. goto() opens one."""

    def __init__(self, pages, start):
        self.pages = dict(pages)
        self.actions = []        # (selector, verb, args)
        self.visits = []         # every path opened, in order
        self.filled = {}         # field id -> the value filled in, on any page
        self.goto(start)

    def goto(self, path):
        self.url = SITE + path
        self.visits.append(path)
        self.doc = lxml.html.document_fromstring('<html><body>' + self.pages[path]
                                                 + '</body></html>')

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

    def act(self, selector, verb, args):
        self.actions.append((selector, verb, args))
        els = self.query(selector)
        if len(els) != 1:
            raise RuntimeError('Locator.' + verb + ': ' + selector + ' matched ' + str(len(els)))
        el = els[0]
        for cover in self.doc.xpath('//*[@data-overlay]'):
            if not _hidden(cover) and _overlay_of(el) is not cover:
                raise RuntimeError('Locator.' + verb + ': Timeout 50ms exceeded.\nCall log:\n'
                                   '  - <div id="' + (cover.get('id') or '') + '"> intercepts '
                                   'pointer events')
        if verb == 'fill':
            el.set('value', args[0])
            self.filled[el.get('id')] = args[0]
        elif verb == 'click':
            if el.get('data-reveal'):
                self.by_id(el.get('data-reveal')).attrib.pop('style', None)
            if el.get('data-closes'):
                self.by_id(el.get('data-closes')).drop_tree()
            if el.get('data-go'):
                self.goto(el.get('data-go'))

    def locator(self, selector):
        return ShopLocator(self, selector)

    def frame_locator(self, selector):
        return self

    def content(self):
        return lxml.html.tostring(self.doc, encoding='unicode')

    def evaluate(self, js, arg=None):
        return []


class SlowShop(Shop):
    """A Shop whose pages load 30 ms after the click that goes to them, as on a site that
    answers slowly: until then the page that was clicked on stays, with the landmark of the
    step that just ran on it."""

    DELAY = 0.03

    def __init__(self, pages, start):
        self._pending = None
        super().__init__(pages, start)

    @property
    def url(self):
        self._land()
        return self._url

    @url.setter
    def url(self, value):
        self._url = value

    def goto(self, path):
        if getattr(self, 'doc', None) is None:      # the first page opens at once
            super().goto(path)
        else:
            self._pending = (path, time.monotonic() + self.DELAY)

    def _land(self):
        if self._pending is not None and time.monotonic() >= self._pending[1]:
            path, self._pending = self._pending[0], None
            super().goto(path)

    def query(self, selector):
        self._land()
        return super().query(selector)


class AsyncShopLocator:
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

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)

        async def run(*args, **kwargs):
            return action(*args, **kwargs)
        return run


class AsyncShop:
    """The async Playwright shape over a Shop."""

    def __init__(self, shop):
        self.sync = shop

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector):
        return AsyncShopLocator(self.sync.locator(selector))

    def frame_locator(self, selector):
        return self

    async def content(self):
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        return []


# --- the checkout's pages -----------------------------------------------------------------------

SHIPPING = "//h1[.='Shipping']"
PAYMENT = "//h1[.='Payment']"
REVIEW = "//button[.='Place order']"


def shipping_page(go, field='ship-name'):
    return ("<h1>Shipping</h1><label for='" + field + "'>Full name</label><input id='" + field
            + "' name='" + field + "'><button id='next' type='button' data-go='" + go
            + "'>Next</button>")


def payment_page(go):
    return ("<h1>Payment</h1><label for='card-number'>Card number</label>"
            "<input id='card-number' name='card'><button id='next' type='button' data-go='"
            + go + "'>Next</button>")


def review_page(go='done'):
    return "<h1>Review</h1><button id='place' type='button' data-go='" + go + "'>Place order</button>"


DONE = '<h1>Thank you</h1><p>Your order is placed.</p>'


def in_order(*names, **pages):
    """A shop whose steps come in the order of names, at /checkout/a, /checkout/b, ...
    (the addresses say where a step is in the flow, not which step it is)."""
    paths = [chr(ord('a') + i) for i in range(len(names))] + ['done']
    out = {'done': DONE}
    for i, name in enumerate(names):
        go = paths[i + 1]
        if name == 'shipping':
            out[paths[i]] = shipping_page(go, pages.get('field', 'ship-name'))
        elif name == 'payment':
            out[paths[i]] = payment_page(paths[i] if pages.get('declined') else go)
        elif name == 'review':
            out[paths[i]] = review_page(go)
        else:
            out[paths[i]] = pages[name].replace('{go}', go)
    return Shop(out, 'a')


# --- one adapter for both healers -----------------------------------------------------------

class Side:
    """Runs a flow on mendwright.open() or mendwright.aio.open(). The async side keeps one
    event loop in a thread for the whole test, as a real async suite does."""

    def __init__(self, kind):
        self.kind = kind
        self.heal = None
        self._loop = None

    def call(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(30)

    def close(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def open(self, page, **kwargs):
        kwargs.setdefault('fingerprint', False)
        kwargs.setdefault('timeout_ms', 60)
        if self.kind == 'sync':
            self.heal = mendwright.open(page, **kwargs)
        else:
            async def opened():
                return await mendwright.aio.open(AsyncShop(page), **kwargs)
            self.heal = self.call(opened())
        self.heal.coordinator.log = None   # the 'mendwright' logger, which caplog reads
        return self.heal

    @property
    def core(self):
        return self.heal if self.kind == 'sync' else self.heal._core

    def does(self, *calls, record=None):
        """A step function that makes these calls on the page it gets, such as
        ('fill', '#ship-name', 'Ada'). record, when given, collects the page it got."""
        if self.kind == 'sync':
            def fn(page):
                if record is not None:
                    record.append(page)
                for verb, *args in calls:
                    getattr(page, verb)(*args)
            return fn

        async def afn(page):
            if record is not None:
                record.append(page)
            for verb, *args in calls:
                await getattr(page, verb)(*args)
        return afn

    def run(self, **kwargs):
        kwargs.setdefault('poll_ms', 5)
        if self.kind == 'sync':
            return self.heal.run_steps(**kwargs)
        return self.call(self.heal.run_steps(**kwargs))

    def locate(self, key, selector=None, **kwargs):
        kwargs.setdefault('timeout_ms', 0)
        kwargs.setdefault('poll_ms', 0)
        proxy = self.heal.locator(key, selector, **kwargs)
        if self.kind == 'sync':
            return proxy

        async def resolved():
            return await proxy
        return self.call(resolved())


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


def _steps_journal(tmp_path):
    return [{k: v for k, v in e.items() if k not in ('ts', 'event', 'seconds')}
            for e in _journal(tmp_path) if e['event'] == 'flow.step']


def _lines(caplog, level=None):
    return [r.getMessage() for r in caplog.records
            if r.name == 'mendwright' and (level is None or r.levelno == level)]


def _checkout(side, *, review_last=True, upsell=None):
    """Register the approved example's three steps (and an optional upsell)."""
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada'), ('click', '#next')))
    heal.step('payment', PAYMENT,
              side.does(('fill', '#card-number', '4242 4242 4242 4242'), ('click', '#next')))
    if upsell is not None:
        heal.step('upsell', "//h1[.='Add insurance?']", side.does(('click', '#no-thanks')),
                  optional=True)
    heal.step('review', REVIEW, side.does(('click', '#place')), last=review_last)


UPSELL = ("<h1>Add insurance?</h1><button id='no-thanks' type='button' data-go='{go}'>"
          "No thanks</button>")


# --- the flow -------------------------------------------------------------------------------------

def test_steps_in_the_registered_order(side, tmp_path, caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    shop = in_order('shipping', 'payment', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    assert side.run() == ['shipping', 'payment', 'review']

    assert shop.visits == ['a', 'b', 'c', 'done']
    assert [(s, v) for s, v, _ in shop.actions] == [
        ('css=#ship-name', 'fill'), ('css=#next', 'click'), ('css=#card-number', 'fill'),
        ('css=#next', 'click'), ('css=#place', 'click')]
    assert _steps_journal(tmp_path) == [
        {'key': 'shipping', 'position': 1, 'registered': 1, 'page': '/checkout/a', 'outcome': 'ran'},
        {'key': 'payment', 'position': 2, 'registered': 2, 'page': '/checkout/b', 'outcome': 'ran'},
        {'key': 'review', 'position': 3, 'registered': 3, 'page': '/checkout/c', 'outcome': 'ran'},
    ]
    assert all(isinstance(e['seconds'], float) for e in _journal(tmp_path)
               if e['event'] == 'flow.step')
    infos = _lines(caplog, logging.INFO)
    assert [line.split(' (')[0] for line in infos if line.startswith('mendwright: ran step')] == [
        "mendwright: ran step 'shipping' on /checkout/a",
        "mendwright: ran step 'payment' on /checkout/b",
        "mendwright: ran step 'review' on /checkout/c"]
    assert _lines(caplog, logging.WARNING) == []


def test_the_site_asks_for_the_card_before_the_address(side, tmp_path, caplog, stub_provider):
    shop = in_order('payment', 'shipping', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    assert side.run() == ['payment', 'shipping', 'review']

    assert [(s, v, a) for s, v, a in shop.actions if v == 'fill'] == [
        ('css=#card-number', 'fill', ('4242 4242 4242 4242',)),
        ('css=#ship-name', 'fill', ('Ada',))]
    assert _lines(caplog, logging.WARNING) == [
        "mendwright: ran 'payment' before 'shipping': the site changed the order of the steps"]
    assert [(e['key'], e['position'], e['registered']) for e in _steps_journal(tmp_path)] == [
        ('payment', 1, 2), ('shipping', 2, 1), ('review', 3, 3)]


def test_a_one_page_checkout_runs_the_steps_in_the_order_registered(side, tmp_path, caplog,
                                                                   stub_provider):
    one = (shipping_page('x').replace("<button id='next' type='button' data-go='x'>Next</button>", '')
           + payment_page('x').replace("<button id='next' type='button' data-go='x'>Next</button>", '')
           + review_page())
    shop = Shop({'one': one, 'done': DONE}, 'one')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    # The last step is registered first: it still waits until no other step shows.
    heal.step('review', REVIEW, side.does(('click', '#place')), last=True)
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada')))
    heal.step('payment', PAYMENT, side.does(('fill', '#card-number', '4242')))

    assert side.run() == ['shipping', 'payment', 'review']

    assert [v for _, v, _ in shop.actions] == ['fill', 'fill', 'click']
    assert shop.visits == ['one', 'done']
    assert _lines(caplog, logging.WARNING) == []      # the last step's place is not an order


def test_two_steps_on_one_page_then_the_rest(side, tmp_path, stub_provider):
    both = (shipping_page('x').replace("<button id='next' type='button' data-go='x'>Next</button>", '')
            + payment_page('review'))
    shop = Shop({'both': both, 'review': review_page(), 'done': DONE}, 'both')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada')))
    heal.step('payment', PAYMENT, side.does(('fill', '#card-number', '4242'), ('click', '#next')))
    heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    assert side.run() == ['shipping', 'payment', 'review']
    assert shop.visits == ['both', 'review', 'done']


def test_steps_that_showed_together_are_looked_at_again_after_each(side, tmp_path,
                                                                 stub_provider):
    # The upsell shows with shipping, and shipping's Next goes to the review page: the
    # upsell is gone by then, so its function must not run there.
    both = shipping_page('review') + UPSELL.replace('{go}', 'review')
    shop = Shop({'both': both, 'review': review_page(), 'done': DONE}, 'both')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada'), ('click', '#next')))
    heal.step('upsell', "//h1[.='Add insurance?']", side.does(('click', '#no-thanks')),
              optional=True)
    heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    assert side.run() == ['shipping', 'review']
    assert shop.visits == ['both', 'review', 'done']


def test_a_step_that_shows_a_moment_later_is_waited_for(side, tmp_path, stub_provider):
    # Each page loads 30 ms after its click: until then the step that ran is still the only
    # one on the page, which is not yet a step that came back.
    shop = SlowShop(in_order('shipping', 'payment', 'review').pages, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    assert side.run(timeout_ms=2000) == ['shipping', 'payment', 'review']
    assert [v for _, v, _ in shop.actions] == ['fill', 'click', 'fill', 'click', 'click']
    assert shop.filled == {'ship-name': 'Ada', 'card-number': '4242 4242 4242 4242'}


def test_run_steps_waits_as_long_as_the_healer_by_default(side, tmp_path, stub_provider):
    side.open(Shop({'a': '<h1>Nothing</h1>'}, 'a'), provider=stub_provider(None),
              store_path=_store(tmp_path), timeout_ms=70)
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)
    with pytest.raises(AttentionRequired, match='within 70 ms'):
        side.run()


def test_a_step_that_comes_back_stops_the_flow(side, tmp_path, stub_provider):
    shop = in_order('shipping', 'payment', 'review', declined=True)
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=40)

    assert str(caught.value) == again_message('payment', '/checkout/b', 40, ['shipping', 'payment'])
    assert str(caught.value).startswith("mendwright: 'payment' is on the page again after it ran")
    assert 'its submit probably failed' in str(caught.value)
    assert sum(1 for _, v, _ in shop.actions if v == 'fill') == 2     # the card once, never twice
    assert [e['key'] for e in _steps_journal(tmp_path)] == ['shipping', 'payment']


def test_a_page_with_a_step_nobody_registered_stops_the_flow(side, tmp_path, stub_provider):
    gift = ("<h1>Gift wrap</h1><button id='wrap' type='button' data-go='{go}'>Wrap it</button>")
    shop = in_order('shipping', 'gift', 'payment', 'review', gift=gift)
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=30)

    waiting = list(side.core._step_book.values())[1:]
    assert str(caught.value) == unknown_message('/checkout/b', waiting, ['shipping'], 30)
    message = str(caught.value)
    assert message.startswith('mendwright: no step showed on /checkout/b within 30 ms.')
    assert ("looked for 'payment' (//h1[.='Payment']) and 'review' "
            "(//button[.='Place order'])") in message
    assert "Steps that ran: 'shipping'." in message
    assert 'add it with heal.step()' in message


def test_a_new_page_that_shows_part_of_a_step_that_ran_is_named_as_a_new_step(side, tmp_path,
                                                                             stub_provider):
    # The site added gift options on a page that keeps the Shipping heading: shipping's
    # submit worked, so the error must not send the reader after a failed submit.
    gift = ("<h1>Shipping</h1><p>Ada, 1 Main St</p><h2>Gift options</h2>"
            "<button id='wrap' type='button' data-go='{go}'>Wrap it</button>")
    shop = in_order('shipping', 'gift', 'payment', 'review', gift=gift)
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=30)

    message = str(caught.value)
    assert message == changed_message('shipping', '/checkout/a', '/checkout/b', 30, ['shipping'])
    assert message.startswith("mendwright: 'shipping' shows on /checkout/b, and it ran on "
                              '/checkout/a, so the page changed after it ran')
    assert 'add it with heal.step()' in message
    assert 'its submit probably failed' not in message
    assert shop.visits == ['a', 'b']


def test_an_optional_step_that_sometimes_shows(side, tmp_path, caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    shop = in_order('shipping', 'payment', 'upsell', 'review', upsell=UPSELL)
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side, upsell=True)
    assert side.run() == ['shipping', 'payment', 'upsell', 'review']

    caplog.clear()
    shop = in_order('shipping', 'payment', 'review')
    other = Side(side.kind)
    try:
        other.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
        _checkout(other, upsell=True)
        assert other.run() == ['shipping', 'payment', 'review']
    finally:
        other.close()
    assert _lines(caplog, logging.WARNING) == []
    assert ("mendwright: step 'upsell' did not show, and it is optional (optional=True)"
            in _lines(caplog, logging.INFO))


def test_a_step_the_site_dropped_is_named_once_the_flow_ends(side, tmp_path, caplog,
                                                             stub_provider):
    shop = in_order('payment', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side)

    assert side.run() == ['payment', 'review']

    assert _lines(caplog, logging.WARNING) == ['mendwright: ' + dropped_message('shipping', 'review')]
    assert _steps_journal(tmp_path)[-1] == {'key': 'shipping', 'registered': 1,
                                            'outcome': 'missing'}


def test_without_a_last_step_the_flow_ends_when_every_required_step_ran(side, tmp_path,
                                                                        stub_provider):
    shop = in_order('shipping', 'payment', 'upsell', upsell=UPSELL)
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('click', '#next')))
    heal.step('payment', PAYMENT, side.does(('click', '#next')))
    heal.step('upsell', "//h1[.='Add insurance?']", side.does(('click', '#no-thanks')),
              optional=True)
    assert side.run() == ['shipping', 'payment']       # the upsell after them is not waited for
    assert shop.visits == ['a', 'b', 'c']


def test_without_a_last_step_a_step_the_site_dropped_stops_the_flow_and_says_so(
        side, tmp_path, stub_provider):
    shop = in_order('shipping', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    _checkout(side, review_last=False)

    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=30)

    waiting = [side.core._step_book['payment']]
    assert str(caught.value) == unknown_message('/checkout/done', waiting,
                                                ['shipping', 'review'], 30, no_last=True)
    assert ('No step has last=True, so run_steps() waits until every step that is not '
            'optional ran, and the site may have dropped one') in str(caught.value)
    assert shop.visits == ['a', 'b', 'done']


def test_an_error_in_a_step_is_raised_as_it_is(side, tmp_path, stub_provider):
    shop = in_order('shipping', 'payment', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    boom = ValueError('the card form is broken')

    if side.kind == 'sync':
        def payment(page):
            raise boom
    else:
        async def payment(page):
            raise boom
    side.heal.step('shipping', SHIPPING, side.does(('click', '#next')))
    side.heal.step('payment', PAYMENT, payment)
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    with pytest.raises(ValueError) as caught:
        side.run()
    assert caught.value is boom
    assert _steps_journal(tmp_path)[-1] == {'key': 'payment', 'position': 2, 'registered': 2,
                                            'page': '/checkout/b', 'outcome': 'failed',
                                            'error': 'ValueError'}
    assert side.core._step_now() is None


def test_a_step_function_gets_heal_page(side, tmp_path, stub_provider):
    shop = in_order('review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    got = []
    side.heal.step('review', REVIEW, side.does(('click', '#place'), record=got), last=True)
    side.run()
    assert got == [side.heal.page]


def test_the_async_healer_takes_a_plain_function_that_returns_an_awaitable(tmp_path,
                                                                           stub_provider):
    side = Side('async')
    try:
        shop = in_order('review')
        heal = side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
        heal.step('review', REVIEW, lambda page: page.click('#place'), last=True)
        assert side.run() == ['review']
        assert shop.visits == ['a', 'done']
        assert shop.actions == [('css=#place', 'click', ())]
    finally:
        side.close()


@pytest.mark.filterwarnings('ignore:coroutine .* was never awaited:RuntimeWarning')
@pytest.mark.parametrize('name', ['review', 'shipping'])
def test_a_plain_function_on_the_async_healer_that_awaits_nothing_raises(tmp_path,
                                                                         stub_provider, name):
    # heal.page's calls are coroutines there: made as statements, they do nothing on the
    # page, so the step must not count as having run, last or not.
    side = Side('async')
    try:
        shop = in_order('shipping', 'review') if name == 'shipping' else in_order('review')
        heal = side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))

        def shipping(page):
            page.fill('#ship-name', 'Ada')
            page.click('#next')

        def review(page):
            page.click('#place')

        heal.step('shipping', SHIPPING, shipping)
        heal.step('review', REVIEW, review, last=True)

        with pytest.raises(TypeError) as caught:
            side.run()
        assert str(caught.value) == plain_message(name, None)
        assert shop.actions == []
        assert _steps_journal(tmp_path) == [{'key': name, 'position': 1,
                                             'registered': 1 if name == 'shipping' else 2,
                                             'page': '/checkout/a', 'outcome': 'failed',
                                             'error': 'TypeError'}]
    finally:
        side.close()


def test_registered_interruptions_are_handled_while_waiting(side, tmp_path, caplog,
                                                            stub_provider):
    wall = ("<div id='wall' data-overlay=''><p>We use cookies.</p>"
            "<button id='accept' type='button' data-closes='wall' data-reveal='main'>Accept"
            "</button></div><div id='main' style='display: none'>" + review_page() + '</div>')
    shop = Shop({'a': wall, 'done': DONE}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.interrupt('consent', '#accept')
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    assert side.run() == ['review']

    assert "mendwright: handled interruption 'consent' before '<next step>'" in _lines(
        caplog, logging.WARNING)
    assert [(e['key'], e['before']) for e in _journal(tmp_path)
            if e['event'] == 'flow.interrupt'] == [('consent', WAITING_STEP)]


def test_an_interruption_whose_action_fails_is_tried_once_per_wait(side, tmp_path, caplog,
                                                                   stub_provider):
    # Something covers the promotion's own Close button, so clicking it fails.
    shop = Shop({'a': "<div id='cover' data-overlay=''></div>"
                      "<button id='promo-close' type='button'>Close</button>"}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.interrupt('promo', '#promo-close')
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=60)

    assert str(caught.value).startswith('mendwright: no step showed on /checkout/a')
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("mendwright: could not handle interruption 'promo' before "
                                  "'<next step>'")
    assert shop.actions == [('css=#promo-close', 'click', ())]


def test_an_interruption_whose_action_fails_leaves_the_others_to_be_handled(side, tmp_path,
                                                                           caplog,
                                                                           stub_provider):
    # The promotion's Close button is covered, so its click fails; a consent wall that
    # hides the review step loads a moment later and must still be answered.
    class LateWall(Shop):
        looks = 0

        def query(self, selector):
            if ('Place order' in selector and self.url.endswith('/a')
                    and ('css=#promo-close', 'click', ()) in self.actions):
                self.looks += 1
                if self.looks == 2:
                    self.goto('wall')
            return super().query(selector)

    wall = ("<div id='wall' data-overlay=''><p>We use cookies.</p>"
            "<button id='accept' type='button' data-closes='wall' data-reveal='main'>Accept"
            "</button></div><div id='main' style='display: none'>" + review_page() + '</div>')
    shop = LateWall({'a': "<div id='cover' data-overlay=''></div>"
                          "<button id='promo-close' type='button'>Close</button>",
                     'wall': wall, 'done': DONE}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.interrupt('promo', '#promo-close')
    side.heal.interrupt('consent', '#accept')
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    assert side.run(timeout_ms=1000) == ['review']

    assert shop.actions == [('css=#promo-close', 'click', ()), ('css=#accept', 'click', ()),
                            ('css=#place', 'click', ())]
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert warnings[0].startswith("mendwright: could not handle interruption 'promo' before "
                                  "'<next step>'")
    assert warnings[1] == "mendwright: handled interruption 'consent' before '<next step>'"


def test_an_interruption_that_keeps_coming_back_ends_the_wait(side, tmp_path, stub_provider):
    # The promotion's Close button takes it away only on the 5th click: each wait handles
    # 3 interruptions, as one action does, and then stops instead of clicking forever.
    class Stubborn(Shop):
        closes = 0

        def act(self, selector, verb, args):
            if selector != 'css=#promo-close':
                return super().act(selector, verb, args)
            self.actions.append((selector, verb, args))
            self.closes += 1
            if self.closes >= 5:
                self.by_id('promo').drop_tree()
                self.by_id('main').attrib.pop('style', None)

    shop = Stubborn({'a': "<div id='promo' data-overlay=''><button id='promo-close' "
                          "type='button'>Close</button></div><div id='main' "
                          "style='display: none'>" + review_page() + '</div>',
                     'done': DONE}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.interrupt('promo', '#promo-close')
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)

    with pytest.raises(InterruptionLoop):
        side.run(timeout_ms=50)
    assert shop.closes == 3


def test_a_flow_that_keeps_growing_stops(side, tmp_path, stub_provider):
    shop = Shop({'a': '<h1>Anything</h1>'}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    count = [0]

    def register_another():
        count[0] += 1
        if count[0] < 6:        # so a run_steps() without the cap ends, and fails, here
            heal.step('step' + str(count[0]), '//h1', grow)

    if side.kind == 'sync':
        def grow(page):
            register_another()
    else:
        async def grow(page):
            register_another()

    heal.step('step0', '//h1', grow)
    with pytest.raises(AttentionRequired) as caught:
        side.run()
    assert str(caught.value) == cap_message(3, 1)
    assert count[0] == 3


def test_a_step_found_by_its_url(side, tmp_path, stub_provider):
    shop = Shop({'orders/987/pay': payment_page('orders/987/review'),
                 'orders/987/review': review_page(), 'done': DONE}, 'orders/987/pay')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    heal.step('payment', None, side.does(('click', '#next')), url='/Checkout/Orders/123/PAY/')
    heal.step('review', REVIEW, side.does(('click', '#place')), url='*/review', last=True)
    assert side.run() == ['payment', 'review']


def test_a_landmark_on_the_wrong_url_does_not_show_the_step(side, tmp_path, stub_provider):
    shop = in_order('payment', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.step('payment', PAYMENT, side.does(('click', '#next')), url='/checkout/pay')
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)
    with pytest.raises(AttentionRequired) as caught:
        side.run(timeout_ms=0)
    assert "'payment' (//h1[.='Payment'], url /checkout/pay)" in str(caught.value)


def test_a_step_with_a_url_and_a_landmark_shows_only_with_both(side, tmp_path, stub_provider):
    # url= scopes every step to the checkout and the landmarks tell them apart: payment's
    # url matches the review page, but its landmark is not there.
    shop = in_order('shipping', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada'), ('click', '#next')),
              url='/checkout/*')
    heal.step('payment', PAYMENT, side.does(('fill', '#card-number', '4242'), ('click', '#next')),
              url='/checkout/*')
    heal.step('review', REVIEW, side.does(('click', '#place')), url='/checkout/*', last=True)
    assert side.run() == ['shipping', 'review']


def test_a_later_selector_of_a_landmark_list_shows_the_step(side, tmp_path, stub_provider):
    shop = in_order('payment', 'review')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.step('payment', ['#gone', PAYMENT], side.does(('click', '#next')))
    side.heal.step('review', REVIEW, side.does(('click', '#place')), last=True)
    assert side.run() == ['payment', 'review']


def test_any_visible_match_of_a_landmark_shows_the_step(side, tmp_path, stub_provider):
    page = ("<h1 style='display: none'>Payment</h1><h1>Payment</h1>"
            "<button id='next' type='button' data-go='done'>Next</button>")
    shop = Shop({'a': page, 'done': DONE}, 'a')
    side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))
    side.heal.step('payment', PAYMENT, side.does(('click', '#next')), last=True)
    assert side.run() == ['payment']


# --- the page guard inside a step -----------------------------------------------------------------

def _first_run(tmp_path, stub_provider):
    """The flow in the registered order, which saves where and during which step each key
    was found."""
    first = Side('sync')
    first.open(in_order('shipping', 'payment', 'review'), provider=stub_provider(None),
               store_path=_store(tmp_path))
    _checkout(first)
    first.run()
    return load_store(None, _store(tmp_path))


def test_a_key_saves_the_steps_it_was_found_during(tmp_path, stub_provider):
    store = _first_run(tmp_path, stub_provider)
    assert store.meta('#ship-name')['steps'] == ['shipping']
    assert store.meta('#ship-name')['pages'] == ['/checkout/a']
    assert store.meta('#next')['steps'] == ['payment', 'shipping']   # used in both
    saved = json.loads((tmp_path / 'store' / 'overrides.json').read_text(encoding='utf-8'))
    assert saved['selectors']['#card-number']['steps'] == ['payment']


def test_a_renamed_field_in_a_step_that_moved_heals_there(side, tmp_path, caplog, stub_provider):
    caplog.set_level(logging.INFO, logger='mendwright')
    _first_run(tmp_path, stub_provider)

    # The site now asks for the card first, and the shipping form's field has a new id.
    shop = in_order('payment', 'shipping', 'review', field='ship-full-name')
    provider = stub_provider("//input[@id='ship-full-name']")
    side.open(shop, provider=provider, store_path=_store(tmp_path))
    _checkout(side)

    assert side.run() == ['payment', 'shipping', 'review']

    assert shop.filled == {'card-number': '4242 4242 4242 4242', 'ship-full-name': 'Ada'}
    assert [(h.key, h.new) for h in side.heal.heals] == [
        ('#ship-name', "//input[@id='ship-full-name']")]
    assert len(provider.bundles) == 1
    assert provider.bundles[0]['page'] == '/checkout/b'
    assert provider.bundles[0]['lastPage'] == '/checkout/a'
    assert ("mendwright: the page guard lets '#ship-name' heal on /checkout/b: it was found "
            "during step 'shipping' before (last on /checkout/a), and this is the page "
            "'shipping' showed on") in _lines(caplog, logging.INFO)
    assert not [e for e in _journal(tmp_path) if e['event'] == 'flow.page_guard']


def test_outside_run_steps_the_guard_declines_as_before(side, tmp_path, stub_provider):
    _first_run(tmp_path, stub_provider)
    shop = in_order('payment', 'shipping', 'review', field='ship-full-name')
    shop.goto('b')
    provider = stub_provider("//input[@id='ship-full-name']")
    side.open(shop, provider=provider, store_path=_store(tmp_path))

    with pytest.raises(AttentionRequired, match="'#ship-name' was last found on /checkout/a, "
                                                "and this page is /checkout/b"):
        side.locate('#ship-name', '#ship-name')
    assert provider.bundles == []


def test_a_key_of_another_step_is_declined_on_a_new_page(side, tmp_path, stub_provider):
    _first_run(tmp_path, stub_provider)
    # On the payment step's new page, the script looks for the shipping step's field.
    shop = in_order('review', 'payment')
    provider = stub_provider("//input[@id='card-number']")     # a field that would pass
    side.open(shop, provider=provider, store_path=_store(tmp_path))
    side.heal.step('payment', PAYMENT, side.does(('fill', '#ship-name', 'Ada')))
    shop.goto('b')

    with pytest.raises(AttentionRequired, match="'#ship-name' was last found on /checkout/a"):
        side.run()
    assert provider.bundles == []


def test_a_key_missed_after_the_step_went_to_another_page_is_declined(side, tmp_path,
                                                                     stub_provider):
    # '#ship-name' was found during shipping. This time shipping's function looks for it
    # again after its own Next went to the payment page, which is not shipping's page.
    _first_run(tmp_path, stub_provider)
    shop = in_order('shipping', 'payment', 'review')
    provider = stub_provider("//input[@id='card-number']")     # a field that would pass
    side.open(shop, provider=provider, store_path=_store(tmp_path))
    side.heal.step('shipping', SHIPPING,
                   side.does(('click', '#next'), ('fill', '#ship-name', 'Ada')))

    with pytest.raises(AttentionRequired, match="'#ship-name' was last found on /checkout/a, "
                                                "and this page is /checkout/b"):
        side.run()
    assert provider.bundles == []
    assert shop.filled == {}


def _verify_flow(side):
    heal = side.heal
    heal.step('shipping', SHIPPING, side.does(('fill', '#ship-name', 'Ada'), ('click', '#next'),
                                              ('click', '#use-address')))
    heal.step('payment', PAYMENT,
              side.does(('fill', '#card-number', '4242 4242 4242 4242'), ('click', '#next')))
    heal.step('review', REVIEW, side.does(('click', '#place')), last=True)


def test_a_step_that_no_longer_goes_through_a_page_does_not_heal_onto_the_next_step(
        side, tmp_path, stub_provider):
    # Shipping's Next went to an address check whose Use this address button went on to
    # payment. The site dropped the check: the button's lookup now misses on the payment
    # page, where a heal would press payment's Next with no card.
    verify = ("<h1>Check your address</h1><button id='use-address' type='button' "
              "data-go='b'>Use this address</button>")
    first = Side('sync')
    try:
        first.open(Shop({'a': shipping_page('verify'), 'verify': verify, 'b': payment_page('c'),
                         'c': review_page(), 'done': DONE}, 'a'),
                   provider=stub_provider(None), store_path=_store(tmp_path))
        _verify_flow(first)
        assert first.run() == ['shipping', 'payment', 'review']
    finally:
        first.close()
    meta = load_store(None, _store(tmp_path)).meta('#use-address')
    assert meta['pages'] == ['/checkout/verify']
    assert not meta['steps']        # found after shipping left its page: not shipping's

    shop = in_order('shipping', 'payment', 'review')
    provider = stub_provider("//button[@id='next']")
    side.open(shop, provider=provider, store_path=_store(tmp_path))
    _verify_flow(side)

    with pytest.raises(AttentionRequired, match="'#use-address' was last found on "
                                                "/checkout/verify, and this page is /checkout/b"):
        side.run()
    assert provider.bundles == []
    assert shop.visits == ['a', 'b']
    assert shop.filled == {'ship-name': 'Ada'}


def test_a_key_found_after_the_step_went_to_another_page_is_not_saved_with_it(tmp_path,
                                                                              stub_provider):
    side = Side('sync')
    try:
        side.open(in_order('shipping', 'payment', 'review'), provider=stub_provider(None),
                  store_path=_store(tmp_path))
        _checkout(side)
        # Shipping's function goes on to fill the card on the payment page.
        side.heal.step('shipping', SHIPPING, side.does(
            ('fill', '#ship-name', 'Ada'), ('click', '#next'), ('fill', '#card-number', '4242')))
        assert side.run() == ['shipping', 'payment', 'review']
    finally:
        side.close()
    store = load_store(None, _store(tmp_path))
    assert store.meta('#card-number')['steps'] == ['payment']
    assert store.meta('#ship-name')['steps'] == ['shipping']


def test_a_coroutine_gathered_next_to_run_steps_is_not_part_of_the_step(tmp_path,
                                                                        stub_provider):
    side = Side('async')
    try:
        shop = Shop({'a': review_page() + "<input id='search' name='q'>", 'done': DONE}, 'a')
        heal = side.open(shop, provider=stub_provider(None), store_path=_store(tmp_path))

        async def review(page):
            await asyncio.sleep(0.05)
            await page.click('#place')

        async def search():
            await asyncio.sleep(0.01)                   # while review runs
            await heal.locator('search', '#search', timeout_ms=0)

        async def both():
            return await asyncio.gather(heal.run_steps(poll_ms=5), search())

        heal.step('review', REVIEW, review, last=True)
        ran, _ = side.call(both())
        assert ran == ['review']
    finally:
        side.close()
    store = load_store(None, _store(tmp_path))
    assert store.meta('#place')['steps'] == ['review']
    assert store.meta('search')['pages'] == ['/checkout/a']
    assert not store.meta('search')['steps']


# --- argument checks --------------------------------------------------------------------------------

def _healer(tmp_path, stub_provider):
    return mendwright.open(Shop({'a': review_page()}, 'a'), provider=stub_provider(None),
                           store_path=_store(tmp_path))


def _noop(page):
    pass


@pytest.mark.parametrize('call, error, words', [
    (lambda h: h.step('', REVIEW, _noop), TypeError, 'needs a name'),
    (lambda h: h.step(5, REVIEW, _noop), TypeError, 'needs a name'),
    (lambda h: h.step('review', REVIEW, 'click'), TypeError, 'takes the function'),
    (lambda h: h.step('review', None, _noop), TypeError, 'needs when='),
    (lambda h: h.step('review', [], _noop), ValueError, 'empty list'),
    (lambda h: h.step('review', 5, _noop), TypeError, 'where a selector was expected'),
    (lambda h: h.step('review', '//button[', _noop), InvalidSelector, "'review'"),
    (lambda h: h.step('review', url=5), TypeError, 'takes the path'),
    (lambda h: h.step('review', url='checkout/review'), ValueError, 'starting with / or *'),
    (lambda h: h.step('review', url='https://shop.test/review'), ValueError, 'not a whole address'),
    (lambda h: h.step('review', url='/'), ValueError, 'the site root'),
    (lambda h: h.step('review', REVIEW, _noop, last='yes'), TypeError, "last='yes'"),
    (lambda h: h.step('review', REVIEW, _noop, optional=1), TypeError, 'optional=1'),
    (lambda h: h.step('review', REVIEW, _noop, last=True, optional=True), ValueError,
     'cannot be optional'),
    (lambda h: h.run_steps(), ValueError, 'has no steps to run'),
])
def test_step_rejects_arguments_that_can_never_work(tmp_path, stub_provider, call, error, words):
    heal = _healer(tmp_path, stub_provider)
    with pytest.raises(error, match=words.replace('(', '\\(').replace('[', '\\[').replace('*', '\\*')):
        call(heal)
    assert heal._step_book == {}


def test_the_sync_healer_rejects_an_async_step(tmp_path, stub_provider):
    heal = _healer(tmp_path, stub_provider)

    async def review(page):
        pass

    class Review:
        async def __call__(self, page):
            pass

    for fn in (review, Review()):
        with pytest.raises(TypeError, match='mendwright.aio.open'):
            heal.step('review', REVIEW, fn)
    assert heal._step_book == {}


def test_a_flow_has_one_last_step(tmp_path, stub_provider):
    heal = _healer(tmp_path, stub_provider)
    heal.step('review', REVIEW, _noop, last=True)
    with pytest.raises(ValueError, match="'review' is the last step already"):
        heal.step('done', '//h1', _noop, last=True)
    heal.step('review', REVIEW, _noop, last=True)       # the same step again is fine
    assert list(heal._step_book) == ['review']


def test_run_steps_needs_something_to_wait_for(tmp_path, stub_provider):
    heal = _healer(tmp_path, stub_provider)
    heal.step('upsell', '//h1', _noop, optional=True)
    with pytest.raises(ValueError, match='every step is optional and none has last=True'):
        heal.run_steps()
    with pytest.raises(TypeError, match='timeout_ms= as a number'):
        heal.run_steps(timeout_ms='10')
    with pytest.raises(TypeError, match='poll_ms= as a number'):
        heal.run_steps(poll_ms=None)
    with pytest.raises(ValueError, match='poll_ms= of 0 or more'):
        heal.run_steps(poll_ms=-1)


def test_step_is_a_decorator_and_registering_again_replaces_in_place(tmp_path, stub_provider):
    heal = _healer(tmp_path, stub_provider)

    @heal.step('shipping', SHIPPING)
    def shipping(page):
        return 'shipped'

    assert shipping(None) == 'shipped'                  # the function itself comes back
    assert heal.step('payment', [PAYMENT, '#card-number'], _noop) is _noop
    heal.step('shipping', '#ship-name', _noop)
    book = heal._step_book
    assert list(book) == ['shipping', 'payment']
    assert book['shipping'].when == ('#ship-name',) and book['shipping'].fn is _noop
    assert book['payment'].when == ("//h1[.='Payment']", '#card-number')


def test_a_decorator_with_bad_arguments_raises_before_the_function(tmp_path, stub_provider):
    heal = _healer(tmp_path, stub_provider)
    with pytest.raises(TypeError, match='needs when='):
        heal.step('shipping')


# --- the pieces ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('pattern, normalized', [
    ('/checkout/payment', '/checkout/payment'),
    ('/Checkout//Payment/', '/checkout/payment'),
    ('/orders/123/pay', '/orders/*/pay'),
    ('*/payment*', '*/payment*'),
    ('*payment', '*payment'),
    ('  /checkout/*  ', '/checkout/*'),
])
def test_url_pattern(pattern, normalized):
    assert url_pattern('payment', pattern) == normalized


@pytest.mark.parametrize('pattern, url, expected', [
    ('/checkout/payment', 'https://shop.test/Checkout/Payment/?step=2', True),
    ('/checkout/payment', 'https://shop.test/checkout/payment/card', False),
    ('/checkout/*', 'https://shop.test/checkout/payment/card', True),
    ('/*/payment*', 'file:///C:/site/checkout/payment.html', True),
    ('*/payment*', 'https://shop.test/payment', True),
    ('*/payment*', 'https://shop.test/checkout/payment', True),
    ('*/payment*', 'https://shop.test/repayment', False),
    ('/orders/*/pay', 'https://shop.test/orders/507f1f77bcf86cd799439011/pay', True),
    ('/*', 'about:blank', False),
    ('/*', 'https://shop.test/', False),
])
def test_url_matches(pattern, url, expected):
    assert url_matches(pattern, url) is expected


def test_landmark_targets():
    playwright = PlaywrightSyncDriver(Shop({'a': ''}, 'a'))
    selenium = SeleniumDriver(object())
    assert landmark_targets(playwright, '#a', 0) == []
    assert landmark_targets(playwright, '#a', 1) == ['#a']
    assert landmark_targets(playwright, '//h1', 2) == ['xpath=(//h1)[1]', 'xpath=(//h1)[2]']
    assert landmark_targets(playwright, 'text=Place order', 7) == [
        'text=Place order >> nth=' + str(i) for i in range(5)]
    assert landmark_targets(playwright, '#a', 2) == ['css=#a >> nth=0', 'css=#a >> nth=1']
    assert landmark_targets(selenium, '#a', 3) == ['#a']        # Selenium: the first match


def test_order_message_names_every_step_that_ran_first():
    assert order_message(['payment'], 'shipping') == (
        "ran 'payment' before 'shipping': the site changed the order of the steps")
    assert order_message(['payment', 'gift'], 'shipping') == (
        "ran 'payment' and 'gift' before 'shipping': the site changed the order of the steps")


def test_the_guide_quotes_the_messages_the_code_writes():
    guide = (Path(__file__).resolve().parents[1] / 'docs' / 'guide.md').read_text(encoding='utf-8')
    waiting = [Step('payment', _noop, ('#card-number',)),
               Step('review', _noop, ('text=Place order',), last=True)]
    for message in (
            unknown_message('/checkout/gift', waiting, ['shipping'], 10000),
            again_message('payment', '/checkout/payment', 10000, ['shipping', 'payment']),
            changed_message('shipping', '/checkout/shipping', '/checkout/gift', 10000,
                            ['shipping']),
            dropped_message('gift', 'review'),
            order_message(['payment'], 'shipping'),
            ran_message('payment', '/checkout/step-a', 0.4),
            scope_message('#ship-name', 'shipping', '/checkout/step-b', '/checkout/step-a')):
        assert message in guide, message


def test_set_last_step_keeps_five_and_writes_only_when_the_set_changes(tmp_path):
    path = _store(tmp_path)
    store = load_store(None, path)
    store.set_code_candidates('next', ['#next'])
    writes = []
    original = store._persist_override
    store._persist_override = lambda: (writes.append(1), original())
    for name in ('a', 'b', 'a'):
        store.set_last_step('next', name)
    assert store.meta('next')['steps'] == ['a', 'b']
    assert len(writes) == 2                                     # 'a' again changed the order only
    for name in 'cdefg':
        store.set_last_step('next', name)
    assert store.meta('next')['steps'] == ['g', 'f', 'e', 'd', 'c']
    store.set_last_step('next', '')
    store.set_last_step('next', None)
    assert load_store(None, path).meta('next')['steps'] == ['g', 'f', 'e', 'd', 'c']


def test_two_processes_keep_each_other_s_steps(tmp_path):
    path = _store(tmp_path)
    first = load_store(None, path)
    first.set_code_candidates('next', ['#next'])
    second = load_store(None, path)
    first.set_last_step('next', 'shipping')
    second.set_last_step('next', 'payment')
    assert sorted(load_store(None, path).meta('next')['steps']) == ['payment', 'shipping']


# --- Selenium ------------------------------------------------------------------------------------------

class Element:
    def __init__(self, driver, el):
        self._driver, self.el = driver, el

    @property
    def tag_name(self):
        return self.el.tag

    def is_displayed(self):
        return not _hidden(self.el)

    def get_attribute(self, name):
        return self.el.get(name)

    def clear(self):
        self.el.set('value', '')

    def send_keys(self, *value):
        self.el.set('value', (self.el.get('value') or '') + ''.join(value))
        self._driver.shop.filled[self.el.get('id')] = self.el.get('value')

    def click(self):
        self._driver.clicks.append(self.el.get('id'))
        if self.el.get('data-go'):
            self._driver.goto(self.el.get('data-go'))


class WebDriver:
    """Enough of a Selenium WebDriver over a Shop's pages: find_elements by XPath and #id."""

    def __init__(self, shop):
        self.shop = shop
        self.switch_to = object()
        self.clicks = []

    def goto(self, path):
        self.shop.goto(path)

    @property
    def current_url(self):
        return self.shop.url

    @property
    def page_source(self):
        return self.shop.content()

    def find_elements(self, by, value):
        if by == 'xpath':
            found = [e for e in self.shop.doc.xpath(value) if hasattr(e, 'tag')]
        elif value.startswith('#'):
            found = self.shop.doc.xpath('//*[@id=$v]', v=value[1:])
        else:
            found = []
        return [Element(self, e) for e in found]

    def execute_script(self, script, *args):
        return None


def test_selenium_rejects_a_playwright_landmark_when_the_step_is_registered(tmp_path,
                                                                            stub_provider):
    heal = mendwright.open(WebDriver(in_order('review')), provider=stub_provider(None),
                           store_path=_store(tmp_path))
    with pytest.raises(InvalidSelector, match="the selector for 'review' is not usable"):
        heal.step('review', 'text=Place order', _noop, last=True)
    assert heal._step_book == {}


def test_selenium_runs_the_steps_with_heal_driver(tmp_path, caplog, stub_provider):
    shop = in_order('payment', 'shipping', 'review')
    driver = WebDriver(shop)
    heal = mendwright.open(driver, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=60, fingerprint=False)
    got = []

    def shipping(drv):
        got.append(drv)
        drv.find_element('css selector', '#ship-name').send_keys('Ada')
        drv.find_element('css selector', '#next').click()

    def payment(drv):
        drv.find_element('xpath', "//input[@name='card']").send_keys('4242')
        drv.find_element('css selector', '#next').click()

    heal.step('shipping', SHIPPING, shipping)
    heal.step('payment', PAYMENT, payment)
    heal.step('review', "//button[.='Place order']",
              lambda drv: drv.find_element('css selector', '#place').click(), last=True)

    assert heal.run_steps(poll_ms=5) == ['payment', 'shipping', 'review']
    assert got == [heal.driver]
    assert shop.visits == ['a', 'b', 'c', 'done']
    assert shop.filled == {'card-number': '4242', 'ship-name': 'Ada'}
    assert "mendwright: ran 'payment' before 'shipping': the site changed the order of the steps" in (
        _lines(caplog, logging.WARNING))
