"""Popups mendwright was never told about (open(dismiss=True)), offline.

The page is PopupPage: an lxml page with the sync Playwright shape that answers
mendwright's popup scripts (mendwright.drivers.base.POPUP_*_JS) the way a browser would,
from markup the tests write:

- data-over="<id> <id>" on an element: while it is shown, it is the element on top at the
  middle of the elements with those ids. data-over="*": of every element outside it.
- data-pos="fixed" (or "sticky") and data-area="0.3": its position and the part of the
  window it covers. An element without them is static and covers nothing.
- data-escape: Escape removes it. data-closes="<id>" on a button: a click removes that
  element. data-reopens="<id>" too: a fresh copy of it is put back at once.

A pointer action (click, check, hover, ...) on a covered element fails the way Playwright
words it; fill and press go through, as in Playwright. Every call that would reach the
browser is listed in page.calls. The scenarios run on mendwright.open() and
mendwright.aio.open() through one adapter, and on Selenium through PopupDriver. No browser,
network or model.
"""

import asyncio
import copy
import json
import logging
import threading

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired
from mendwright.dismiss import (MIN_AREA, Probe, classify, control_text, forbidden, is_control,
                                parse_probe, pick, popup_of, summary)
from mendwright.drivers.base import (DESCRIBE_JS, POPUP_CONTROL_JS, POPUP_CONTROLS_JS,
                                     POPUP_PROBE_JS, POPUP_ROOT_JS)
from mendwright.flow import (DISMISS_KEY, InterruptionLoop, dismiss_loop_message,
                             dismissed_message, undismissed_message)
from mendwright.interruptions import InterruptionEvent
from mendwright.proxies import AsyncGuardedLocator, GuardedLocator, unwrap

SIDES = ('sync', 'async')
POINTER = ('click', 'dblclick', 'check', 'uncheck', 'hover', 'tap')


# --- the page --------------------------------------------------------------------------------

def _hidden(el):
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def _holds(a, b):
    """a is b or holds it."""
    while b is not None:
        if b is a:
            return True
        b = b.getparent()
    return False


def _clean(text, limit):
    return ' '.join(str(text or '').split())[:limit]


class Doc:
    """The document, and what the browser's answers to mendwright's popup scripts are."""

    def __init__(self, html):
        self.tree = lxml.html.document_fromstring(html)
        self.calls = []          # every call that would reach the browser
        self.clicked = []        # the id (or text) of every element a click reached
        self.originals = {}      # id -> a copy, for data-reopens

    def by_id(self, ident):
        found = self.tree.xpath('//*[@id=$i]', i=ident)
        return found[0] if found else None

    def attached(self, el):
        while el.getparent() is not None:
            el = el.getparent()
        return el is self.tree

    def shown(self, el):
        return el is not None and self.attached(el) and not _hidden(el)

    def query(self, selector):
        if selector.startswith('xpath='):
            found = self.tree.xpath(selector[len('xpath='):])
            return [e for e in found if isinstance(getattr(e, 'tag', None), str)]
        body = selector[len('css='):] if selector.startswith('css=') else selector
        if body.startswith('#'):
            return self.tree.xpath('//*[@id=$i]', i=body[1:])
        if body.startswith('[id="'):
            return self.tree.xpath('//*[@id=$i]', i=body[5:-2])
        return []

    def hit(self, t):
        """The element on top at the middle of t, or None."""
        if not self.shown(t):
            return None
        top = None
        for el in self.tree.iter():
            over = el.get('data-over') if isinstance(el.tag, str) else None
            if over is None or not self.shown(el):
                continue
            if (over == '*' and not _holds(el, t)) or (t.get('id') or '-') in over.split():
                top = el            # later in the page is on top
        return top

    def covered_by(self, t):
        hit = self.hit(t)
        if hit is None or _holds(t, hit) or _holds(hit, t):
            return None
        return hit

    def probe(self, t, known=None):
        self.calls.append('probe')
        gone = None if known is None else not self.shown(known)
        hit = self.covered_by(t)
        if hit is None:
            return {'covered': False, 'why': 'free', 'gone': gone}
        chain, n = [], hit
        while n is not None and n.tag not in ('body', 'html'):
            role = (n.get('role') or '').strip().lower()
            modal = (n.get('aria-modal') or '').strip().lower() == 'true'
            pos = n.get('data-pos') or 'static'
            inside = _holds(n, t)
            named = not inside and (role in ('dialog', 'alertdialog') or modal
                                    or pos in ('fixed', 'sticky') or n.tag == 'dialog')
            chain.append({'tag': n.tag, 'id': n.get('id') or '', 'cls': n.get('class') or '',
                          'role': role, 'modal': modal,
                          'open': n.tag == 'dialog' and n.get('open') is not None, 'pos': pos,
                          'area': float(n.get('data-area') or 0), 'holds': inside,
                          'text': _clean(' '.join(n.itertext()), 120) if named else ''})
            n = n.getparent()
        return {'covered': True, 'gone': gone, 'chain': chain}

    def root(self, t, expect):
        self.calls.append('root')
        n = self.covered_by(t)
        for _ in range(expect['depth']):
            n = n.getparent() if n is not None else None
        if n is None or n.tag != expect['tag'] or (n.get('id') or '') != expect['id']:
            return None
        return n

    def controls(self, root):
        return [el for el in root.iterdescendants()
                if isinstance(el.tag, str) and self.shown(el)
                and (el.tag in ('button', 'a', 'input') or el.get('role') == 'button')]

    @staticmethod
    def facts(el):
        kind = (el.get('type') or '').strip().lower()
        return {'tag': el.tag, 'type': kind, 'role': (el.get('role') or '').strip().lower(),
                'href': el.get('href') if el.tag == 'a' else None,
                'text': _clean(el.text_content(), 200),
                'value': _clean(el.get('value'), 200) if el.tag == 'input' and kind in (
                    'button', 'submit', 'reset') else '',
                'aria': _clean(el.get('aria-label'), 200), 'title': _clean(el.get('title'), 200),
                'disabled': el.get('disabled') is not None}

    def control(self, root, expect):
        self.calls.append('control')
        found = self.controls(root)
        if expect['index'] >= len(found):
            return None
        el = found[expect['index']]
        f = self.facts(el)
        same = (f['tag'], f['text'], f['aria']) == (expect['tag'], expect['text'], expect['aria'])
        return el if same else None

    def escape(self):
        self.calls.append('escape')
        for el in list(self.tree.iter()):
            if isinstance(el.tag, str) and el.get('data-escape') is not None and self.shown(el):
                el.drop_tree()

    def click(self, el):
        """A click that reached el."""
        self.clicked.append(el.get('id') or _clean(el.text_content(), 40))
        closes = el.get('data-closes')
        if closes:
            target = self.by_id(closes)
            parent, index = target.getparent(), target.getparent().index(target)
            fresh = copy.deepcopy(target)
            target.drop_tree()
            if el.get('data-reopens') == closes:
                parent.insert(index, fresh)

    def act(self, el, verb, args):
        """A Playwright action on el."""
        self.calls.append(verb)
        cover = self.covered_by(el)
        if verb in POINTER and cover is not None:
            raise RuntimeError('Locator.' + verb + ': Timeout 50ms exceeded.\nCall log:\n'
                               '  - <div id="' + (cover.get('id') or '') + '"> intercepts '
                               'pointer events')
        if verb == 'fill':
            el.set('value', args[0])
        elif verb == 'click':
            self.click(el)


class Handle:
    """A Playwright JSHandle or ElementHandle on one element, or on null."""

    def __init__(self, page, el):
        self._page, self.el = page, el

    def as_element(self):
        return self if self.el is not None else None

    def evaluate(self, js, arg=None):
        assert js == POPUP_CONTROLS_JS
        self._page.doc.calls.append('controls')
        return [self._page.doc.facts(c) for c in self._page.doc.controls(self.el)]

    def evaluate_handle(self, js, arg=None):
        assert js == POPUP_CONTROL_JS
        return Handle(self._page, self._page.doc.control(self.el, arg))

    def click(self, timeout=None):
        assert timeout and timeout > 0           # 0 would mean no limit to Playwright
        self._page.doc.calls.append('click')
        self._page.doc.click(self.el)

    def dispose(self):
        self._page.disposed += 1


class Locator:
    """A Playwright Locator: what find() returns, and the builders that chain from it."""

    def __init__(self, page, find):
        self._page, self._find = page, find

    def _els(self):
        return self._find()

    def count(self):
        return len(self._els())

    def is_visible(self, **kwargs):
        els = self._els()
        return len(els) == 1 and self._page.doc.shown(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def evaluate_all(self, js, arg=None):
        els = self._els()
        if js == POPUP_PROBE_JS:
            known = arg.get('known') if arg else None
            if len(els) != 1:
                return None
            return self._page.doc.probe(els[0], known.el if known is not None else None)
        if js == DESCRIBE_JS and len(els) == 1:
            return {'tag': els[0].tag,
                    'html': lxml.html.tostring(els[0], encoding='unicode', with_tail=False)}
        return None

    def evaluate_handle(self, js, arg=None, timeout=None):
        assert js == POPUP_ROOT_JS
        [el] = self._els()
        return Handle(self._page, self._page.doc.root(el, arg))

    # Builders, for heal.page's guard-only locators.

    def locator(self, selector, **kwargs):
        return Locator(self._page, lambda: [e for p in self._els() for e in p.iterdescendants()
                                            if e in self._page.doc.query(selector)])

    def frame_locator(self, selector):
        return self

    def nth(self, index):
        return Locator(self._page, lambda: self._els()[index:index + 1])

    @property
    def first(self):
        return self.nth(0)

    @property
    def last(self):
        return Locator(self._page, lambda: self._els()[-1:])

    def filter(self, has_text=None, **kwargs):
        return Locator(self._page, lambda: [e for e in self._els()
                                            if has_text in e.text_content()])

    def get_attribute(self, name, **kwargs):
        return self._els()[0].get(name)

    def input_value(self, **kwargs):
        return self._els()[0].get('value') or ''

    def __getattr__(self, verb):
        if verb not in ('click', 'dblclick', 'fill', 'check', 'hover', 'press', 'focus'):
            raise AttributeError(verb)

        def action(*args, **kwargs):
            [el] = self._els()
            return self._page.doc.act(el, verb, args)
        return action


class Keyboard:
    def __init__(self, doc):
        self._doc = doc

    def press(self, key):
        assert key == 'Escape'
        self._doc.escape()


class PopupPage:
    """A sync-Playwright-shaped page over Doc."""

    def __init__(self, html, url='https://shop.example/signup'):
        self.doc = Doc(html)
        self.url = url
        self.keyboard = Keyboard(self.doc)
        self.disposed = 0

    @property
    def calls(self):
        return self.doc.calls

    def locator(self, selector, has_text=None):
        def find():
            found = self.doc.query(selector)
            return [e for e in found if has_text is None or has_text in e.text_content()]
        return Locator(self, find)

    def get_by_role(self, role, name=None):
        return Locator(self, lambda: [e for e in self.doc.tree.iter(role)
                                      if name is None or _clean(e.text_content(), 200) == name])

    def get_by_label(self, text):
        def find():
            return [self.doc.by_id(label.get('for')) for label in self.doc.tree.iter('label')
                    if _clean(label.text_content(), 200) == text]
        return Locator(self, find)

    def frame_locator(self, selector):
        return self

    def content(self):
        return lxml.html.tostring(self.doc.tree, encoding='unicode')

    def evaluate(self, js, arg=None):
        return []


# The async Playwright shape over a PopupPage.

class AsyncHandle:
    def __init__(self, handle):
        self._h = handle

    def as_element(self):
        return self if self._h.as_element() is not None else None

    async def evaluate(self, js, arg=None):
        return self._h.evaluate(js, arg)

    async def evaluate_handle(self, js, arg=None):
        return AsyncHandle(self._h.evaluate_handle(js, arg))

    async def click(self, timeout=None):
        return self._h.click(timeout=timeout)

    async def dispose(self):
        return self._h.dispose()


class AsyncLocator:
    def __init__(self, loc):
        self._loc = loc

    async def count(self):
        return self._loc.count()

    async def is_visible(self, **kwargs):
        return self._loc.is_visible()

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate_all(self, js, arg=None):
        if arg and arg.get('known') is not None:
            arg = {'known': arg['known']._h}
        return self._loc.evaluate_all(js, arg)

    async def evaluate_handle(self, js, arg=None, timeout=None):
        return AsyncHandle(self._loc.evaluate_handle(js, arg, timeout))

    def locator(self, selector, **kwargs):
        return AsyncLocator(self._loc.locator(selector))

    def frame_locator(self, selector):
        return self

    def nth(self, index):
        return AsyncLocator(self._loc.nth(index))

    @property
    def first(self):
        return AsyncLocator(self._loc.first)

    async def input_value(self, **kwargs):
        return self._loc.input_value()

    def __getattr__(self, verb):
        action = getattr(self._loc, verb)

        async def run(*args, **kwargs):
            return action(*args, **kwargs)
        return run


class AsyncKeyboard:
    def __init__(self, keyboard):
        self._k = keyboard

    async def press(self, key):
        self._k.press(key)


class AsyncPopupPage:
    def __init__(self, page):
        self.sync = page
        self.keyboard = AsyncKeyboard(page.keyboard)

    @property
    def url(self):
        return self.sync.url

    def locator(self, selector, **kwargs):
        return AsyncLocator(self.sync.locator(selector, **kwargs))

    def get_by_role(self, role, name=None):
        return AsyncLocator(self.sync.get_by_role(role, name=name))

    def frame_locator(self, selector):
        return self

    async def content(self):
        return self.sync.content()

    async def evaluate(self, js, arg=None):
        return []


# --- one adapter for both healers ----------------------------------------------------------

class Side:
    """Runs the same steps on mendwright.open() or mendwright.aio.open(); the async side
    keeps one event loop in a thread for the whole test."""

    def __init__(self, kind):
        self.kind = kind
        self.heal = None
        self._loop = None

    def run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(30)

    def close(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def open(self, page, **kwargs):
        kwargs.setdefault('provider', None)
        kwargs.setdefault('timeout_ms', 0)
        if self.kind == 'sync':
            self.heal = mendwright.open(page, **kwargs)
        else:
            async def opened():
                return await mendwright.aio.open(AsyncPopupPage(page), **kwargs)
            self.heal = self.run(opened())
        self.heal.coordinator.log = None   # the 'mendwright' logger, which caplog reads
        return self.heal

    def act(self, target, verb, *args, **kwargs):
        result = getattr(target, verb)(*args, **kwargs)
        return result if self.kind == 'sync' else self.run(result)

    def step(self, key, selector, verb, *args, **kwargs):
        proxy = self.heal.locator(key, selector, timeout_ms=0, poll_ms=0)
        return self.act(proxy, verb, *args, **kwargs)


@pytest.fixture(params=SIDES)
def side(request):
    s = Side(request.param)
    yield s
    s.close()


def _store(tmp_path):
    return str(tmp_path / 'store' / 'overrides.json')


def _events(tmp_path):
    path = tmp_path / 'store' / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if json.loads(line)['event'].startswith('flow.')]


def _warnings(caplog):
    """mendwright's WARNING lines, without open()'s one about the missing provider."""
    return [r.getMessage() for r in caplog.records
            if r.name == 'mendwright' and r.levelno == logging.WARNING
            and r.getMessage().startswith('mendwright: ')]


def _page(*parts):
    return PopupPage('<html><body>' + ''.join(parts) + '</body></html>')


FORM = ('<form id="signup"><label for="email">Email</label><input id="email" name="email">'
        '<button id="go" type="button">Go</button></form>')


def modal(buttons, *, ident='nl', cls='nl', escape=False, extra=''):
    """A newsletter modal over the whole page: a dialog with a backdrop on top of the
    form, a text, an email field and the given buttons."""
    return ('<div id="' + ident + '" class="' + cls + '" role="dialog" aria-modal="true" '
            'data-pos="fixed" data-area="1"' + (' data-escape=""' if escape else '') + '>'
            '<div class="backdrop" data-over="email go"></div>'
            '<div class="panel"><p>Get 10% off your first order.</p>'
            '<input id="nl-email" type="email" placeholder="Your email">'
            + buttons + extra + '</div></div>')


SIGN_UP = '<button id="nl-signup" type="button">Sign up</button>'
NO_THANKS = '<button id="nl-no" type="button" data-closes="nl">No thanks</button>'
NL_TEXT = 'Get 10% off your first order. Sign up No thanks'


def _nl(text=NL_TEXT):
    return '<div id="nl" role="dialog" class="nl"> ("' + text + '")'


# --- the rules -------------------------------------------------------------------------------

def _control(text='', tag='button', **more):
    out = {'tag': tag, 'type': 'button' if tag == 'button' else '', 'role': '', 'href': None,
           'text': text, 'value': '', 'aria': '', 'title': '', 'disabled': False}
    out.update(more)
    return out


@pytest.mark.parametrize('control, kind', [
    (_control('Close'), 'close'),
    (_control('Close dialog'), 'close'),
    (_control('×', aria='Close'), 'close'),
    (_control('', aria='Close newsletter'), 'close'),
    (_control('✕'), 'close'),
    (_control('X'), 'close'),
    (_control('✖', title='Close'), 'close'),
    (_control('No thanks'), 'decline'),
    (_control('No, thanks'), 'decline'),
    (_control("No thanks, I'd rather not"), 'decline'),
    (_control("No thanks, I'll pay full price"), None),            # it says pay
    (_control('Not now'), 'decline'),
    (_control('Maybe later'), 'decline'),
    (_control('Remind me later'), 'decline'),
    (_control('Skip for now'), 'decline'),
    (_control('Dismiss'), 'decline'),
    (_control('Reject all'), 'decline'),
    (_control('Decline'), 'decline'),
    (_control("I'm not interested"), 'decline'),
    (_control('Continue without accepting'), 'decline'),
    (_control('', aria='Dismiss this offer'), 'decline'),
    (_control('No thanks', tag='a'), 'decline'),                  # a link that goes nowhere
    (_control('No thanks', tag='a', href='#'), 'decline'),
    (_control('No thanks', tag='a', href='javascript:void(0)'), 'decline'),
    (_control('', tag='input', type='button', value='Not now'), 'decline'),
    (_control('Not now', tag='div', role='button'), 'decline'),
    # Never: a label that also says something it must not do.
    (_control('Subscribe'), None),
    (_control('Yes, sign me up'), None),
    (_control('Sign up'), None),
    (_control('Accept all'), None),
    (_control('No thanks, subscribe me'), None),
    (_control('OK'), None),
    (_control('Continue'), None),
    (_control('No thanks, continue'), None),
    (_control('Continue without saving'), None),
    (_control('Allow location'), None),
    (_control('Install the app'), None),
    (_control('Get 10% off'), None),
    (_control('Try it free'), None),
    (_control('×', aria='Subscribe'), None),
    (_control('X', aria='Share on X'), None),                     # an X that is not close
    (_control('No thanks', title='Delete my account'), None),
    # Not a decline or a close button at all.
    (_control('Close account'), None),
    (_control('Cancel subscription'), None),
    (_control('Learn more'), None),
    (_control('Read our privacy policy'), None),
    # Not a control mendwright clicks.
    (_control('No thanks', tag='a', href='/no-thanks'), None),
    (_control('No thanks', tag='a', href='https://shop.example/'), None),
    (_control('No thanks', tag='a', role='button', href='/no-thanks'), None),
    (_control('No thanks', disabled=True), None),
    (_control('No thanks', tag='input', type='text'), None),
    (_control('No thanks', tag='input', type='checkbox'), None),
    (_control('No thanks', tag='div'), None),
])
def test_which_buttons_close_or_decline(control, kind):
    assert classify(control) == kind


def test_the_words_a_button_is_never_clicked_for():
    assert forbidden(_control('Join now')) == 'join'
    assert forbidden(_control('No thanks', aria='Notify me')) == 'notify'
    assert forbidden(_control('Continue to checkout')) == 'continue'
    assert forbidden(_control('Continue without an account')) is None
    assert forbidden(_control('Not interested')) is None
    assert is_control(_control('', tag='input', type='submit')) is True
    assert is_control(_control('', tag='input', type='image')) is False


def test_pick_takes_the_first_close_and_decline_buttons_in_page_order():
    controls = [_control('Subscribe'), _control('No thanks'), _control('×', aria='Close'),
                _control('Not now'), 'not a dict']
    chosen = pick(controls)
    assert chosen['close'] == (2, controls[2]) and chosen['decline'] == (1, controls[1])
    assert pick([_control('Subscribe'), _control('Yes')]) == {}
    assert control_text(_control('×', aria='Close')) == 'Close'
    assert control_text(_control('No thanks')) == 'No thanks'
    assert control_text(_control('', tag='input', value='Not now')) == 'Not now'


def _el(tag='div', **more):
    out = {'tag': tag, 'id': '', 'cls': '', 'role': '', 'modal': False, 'open': False,
           'pos': 'static', 'area': 0.0, 'holds': False, 'text': ''}
    out.update(more)
    return out


@pytest.mark.parametrize('chain, depth', [
    ([_el(), _el(role='dialog', id='nl')], 1),                       # the nearest dialog
    ([_el(modal=True)], 0),
    ([_el(tag='dialog', open=True)], 0),
    ([_el(role='alertdialog', area=0.01)], 0),                       # any size
    ([_el(pos='fixed', area=0.3)], 0),
    ([_el(pos='fixed', area=0.1), _el(pos='fixed', area=0.5)], 1),   # the outermost
    ([_el(pos='sticky', area=1.0)], 0),
    ([_el(pos='fixed', area=1.0), _el(pos='fixed', area=1.0, holds=True)], 0),
    # Not a popup.
    ([_el(pos='fixed', area=0.08)], None),                           # a header or toolbar
    ([_el(pos='fixed', area=0.5), _el(pos='fixed', area=0.1)], None),
    ([_el(), _el(pos='absolute', area=0.9)], None),                  # not fixed
    ([_el(), _el(role='dialog', holds=True)], None),                 # the target is inside
    ([_el(pos='fixed', area=1.0, holds=True)], None),
])
def test_which_element_is_the_popup(chain, depth):
    popup = popup_of(parse_probe({'covered': True, 'chain': chain}))
    assert (popup.depth if popup else None) == depth


def test_only_a_probe_mendwright_knows_counts():
    assert parse_probe(None) is None and parse_probe([]) is None
    assert parse_probe({'tag': 'input', 'html': '<input>'}) is None   # a describe() answer
    assert parse_probe({'covered': True, 'chain': [{'tag': 'div'}]}) is None
    assert parse_probe({'covered': False, 'gone': True}) == Probe(False, (), True)
    assert popup_of(parse_probe({'covered': False})) is None
    assert MIN_AREA == 0.25


def test_how_a_popup_is_named():
    assert summary(_el(role='dialog', cls='nl  modal big shadow', text='Get 10% off')) == (
        '<div role="dialog" class="nl modal big"> ("Get 10% off")')
    long = _el(id='promo', text='Your card 4111111111111111 ' + 'x' * 80)
    assert summary(long) == ('<div id="promo"> ("Your card [redacted number] '
                             + 'x' * 32 + '…")')
    assert summary(_el(tag='aside', pos='fixed')) == '<aside>'


# --- off by default ---------------------------------------------------------------------------

def test_dismiss_is_off_by_default_and_costs_no_browser_call(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    heal = side.open(page, store_path=_store(tmp_path))

    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('go', '#go', 'click')
    side.step('email', '#email', 'fill', 'ada@example.com')   # Playwright fills under it

    assert [c for c in page.calls if c not in ('click', 'fill')] == []   # no probe, nothing
    assert page.doc.by_id('nl') is not None and page.doc.clicked == []
    assert heal.handled_interruptions == [] and _events(tmp_path) == []
    assert not [w for w in _warnings(caplog) if 'popup' in w]


def test_off_by_default_heal_page_hands_back_the_real_locators(tmp_path):
    page = _page(FORM)
    heal = mendwright.open(page, provider=None, store_path=_store(tmp_path), timeout_ms=0)
    p = heal.page
    for loc in (p.get_by_role('button', name='Go'), p.locator('#go', has_text='Go'),
                p.locator('#signup').locator('#go'), p.locator('#go').first,
                p.locator('#go').last, p.locator('#go').nth(0),
                p.locator('#go').filter(has_text='Go'), p.get_by_label('Email')):
        assert type(loc) is Locator


def test_dismiss_takes_true_or_false(tmp_path):
    with pytest.raises(TypeError, match=r"takes dismiss=True or dismiss=False, got "
                                        r"dismiss='no'\."):
        mendwright.open(_page(FORM), provider=None, store_path=_store(tmp_path), dismiss='no')

    async def main():
        await mendwright.aio.open(AsyncPopupPage(_page(FORM)), provider=None,
                                  store_path=_store(tmp_path), dismiss=1)
    with pytest.raises(TypeError, match=r'mendwright\.aio\.open\(\) takes dismiss=True'):
        asyncio.run(main())


# --- dismissing ------------------------------------------------------------------------------

def test_a_newsletter_modal_is_declined_with_no_thanks_never_sign_up(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    side.step('go', '#go', 'click')

    assert page.doc.by_id('nl') is None
    assert page.doc.clicked == ['nl-no', 'go']                     # never Sign up
    assert page.calls.count('escape') == 1
    assert ('mendwright: ' + dismissed_message('go', _nl(), '“No thanks”', ['Escape'])
            in _warnings(caplog))
    assert ("mendwright: dismissed a popup in the way of 'go': <div id=\"nl\" role=\"dialog\" "
            "class=\"nl\"> (\"Get 10% off your first order. Sign up No thanks\") with “No "
            "thanks”, after Escape did not close it") in _warnings(caplog)
    assert heal.handled_interruptions == [
        InterruptionEvent(DISMISS_KEY, 'go', popup=_nl(), how='“No thanks”')]
    assert [(e['event'], e['key'], e['popup'], e['how'], e['label'], e['tried'])
            for e in _events(tmp_path)] == [
        ('flow.dismiss', 'go', _nl(), 'decline', 'No thanks', ['escape'])]
    assert heal.heals == [] and heal.pending() == {}                # not a heal: strict passes


def test_a_fill_under_the_modal_dismisses_it_first(side, tmp_path):
    # Playwright's fill never checks what is on top, so only the look before it can tell.
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)
    side.step('email', '#email', 'fill', 'ada@example.com')
    assert page.doc.by_id('email').get('value') == 'ada@example.com'
    assert page.doc.by_id('nl') is None
    assert [e.before for e in heal.handled_interruptions] == ['email']


def test_a_modal_that_closes_on_escape_is_dismissed_by_escape_alone(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(FORM, modal(SIGN_UP + NO_THANKS, escape=True))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    side.step('go', '#go', 'click')

    assert page.doc.clicked == ['go']
    assert "mendwright: dismissed a popup in the way of 'go': " + _nl() + ' with Escape' in (
        _warnings(caplog))
    assert heal.handled_interruptions[0].how == 'Escape'
    assert [(e['how'], e.get('label'), e['tried']) for e in _events(tmp_path)] == [
        ('escape', None, [])]


def test_an_icon_close_button_is_clicked_before_a_decline_button(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    close = '<button id="nl-x" type="button" aria-label="Close" data-closes="nl">×</button>'
    page = _page(FORM, modal(SIGN_UP + NO_THANKS + close))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    side.step('go', '#go', 'click')

    assert page.doc.clicked == ['nl-x', 'go']
    assert heal.handled_interruptions[0].how == '“Close”'
    assert [(e['how'], e['label']) for e in _events(tmp_path)] == [('close', 'Close')]


def test_a_close_button_that_does_not_close_it_is_followed_by_no_thanks(side, tmp_path,
                                                                       caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    stuck = '<button id="nl-x" type="button" aria-label="Close">×</button>'
    page = _page(FORM, modal(SIGN_UP + stuck + NO_THANKS))
    side.open(page, store_path=_store(tmp_path), dismiss=True)
    side.step('go', '#go', 'click')
    assert page.doc.clicked == ['nl-x', 'nl-no', 'go']
    text = 'Get 10% off your first order. Sign up × No thanks'
    assert ('mendwright: ' + dismissed_message('go', _nl(text), '“No thanks”',
                                               ['Escape', '“Close”'])
            in _warnings(caplog))
    assert [e['tried'] for e in _events(tmp_path)] == [['escape', 'close']]


def test_a_modal_with_only_buttons_it_must_not_click_is_left_alone(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    yes = '<button id="nl-yes" type="button">Yes, sign me up</button>'
    page = _page(FORM, modal('<button id="nl-sub" type="button">Subscribe</button>' + yes))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)
    heal.interrupt('cookies', '#cookie-accept')        # registered, and not on the page

    with pytest.raises(RuntimeError, match='intercepts pointer events') as caught:
        side.step('go', '#go', 'click')

    assert 'Locator.click' in str(caught.value)                  # the browser's own error
    assert page.doc.by_id('nl') is not None and page.doc.clicked == []
    popup = _nl('Get 10% off your first order. Subscribe Yes, sign me up')
    warnings = _warnings(caplog)
    assert warnings == ['mendwright: ' + undismissed_message('go', popup, ['Escape'])]
    assert warnings[0].startswith(
        "mendwright: could not dismiss a popup in the way of 'go': " + popup + '. Escape did '
        'not close it, and it has no close or decline button that mendwright may click, so '
        "mendwright left it alone: the action goes ahead, and fails with the browser's own "
        'error if the popup blocks it. Register the button that closes it with '
        'heal.interrupt("<name>", "<selector of the button>"). If it needs data first, '
        'register its field before the button: heal.interrupt("<name>_field", "<selector of '
        'the field>", action="fill", args=("<value>",)).')
    assert page.calls.count('escape') == 1 and page.calls.count('click') == 1   # tried once
    assert heal.handled_interruptions == [] and _events(tmp_path) == []


def test_a_link_with_a_real_href_is_never_clicked(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    link = '<a id="nl-no" href="/no-thanks" data-closes="nl">No thanks</a>'
    page = _page(FORM, modal(SIGN_UP + link))
    side.open(page, store_path=_store(tmp_path), dismiss=True)
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('go', '#go', 'click')
    assert page.doc.clicked == [] and page.doc.by_id('nl') is not None
    assert [w for w in _warnings(caplog) if w.startswith('mendwright: could not dismiss')]

    page2 = _page(FORM, modal(SIGN_UP + link.replace('/no-thanks', '#')))
    side.open(page2, store_path=_store(tmp_path), dismiss=True)
    side.step('go', '#go', 'click')
    assert page2.doc.clicked == ['nl-no', 'go']


def test_the_element_inside_the_popup_is_never_a_reason_to_dismiss_it(side, tmp_path):
    tip = '<span id="tip" data-over="nl-email">We never share your email</span>'
    page = _page(FORM, modal(SIGN_UP + NO_THANKS, extra=tip))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    side.step('nl_email', '#nl-email', 'fill', 'ada@example.com')     # the user uses the popup
    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('nl_email', '#nl-email', 'click')                     # covered, in the popup

    assert page.doc.by_id('nl') is not None
    assert 'escape' not in page.calls and page.doc.clicked == []
    assert heal.handled_interruptions == []


def test_a_small_fixed_header_over_the_element_is_not_a_popup(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    header = ('<header id="top" class="sticky-top" data-pos="sticky" data-area="0.08" '
              'data-over="go"><a href="/">Shop</a><button type="button">Close</button></header>')
    page = _page(header, FORM)
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    with pytest.raises(RuntimeError, match='intercepts pointer events'):
        side.step('go', '#go', 'click')

    assert 'escape' not in page.calls and page.doc.clicked == []
    assert heal.handled_interruptions == [] and _warnings(caplog) == []


def test_a_consent_banner_is_declined_and_never_accepted(side, tmp_path):
    banner = ('<div id="consent" class="cmp" role="dialog" aria-label="Privacy" '
              'data-pos="fixed" data-area="0.4" data-over="go email">'
              '<p>We and our partners use cookies.</p>'
              '<button id="accept" type="button" data-closes="consent">Accept all</button>'
              '<button id="settings" type="button">Settings</button>'
              '<button id="reject" type="button" data-closes="consent">Reject all</button></div>')
    page = _page(FORM, banner)
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)   # no cookies=
    side.step('go', '#go', 'click')
    assert page.doc.clicked == ['reject', 'go']
    assert heal.handled_interruptions[0].how == '“Reject all”'


def test_a_popup_that_comes_back_each_time_stops_the_step(side, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    again = NO_THANKS.replace('data-closes="nl"', 'data-closes="nl" data-reopens="nl"')
    page = _page(FORM, modal(SIGN_UP + again))
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)

    with pytest.raises(InterruptionLoop) as caught:
        side.step('go', '#go', 'click')

    assert str(caught.value) == dismiss_loop_message('go', _nl(), 3)
    assert str(caught.value).startswith(
        "mendwright: 3 interruptions and popups were handled for 'go' and a popup is still in "
        'the way: ' + _nl() + '. It comes back each time it is dismissed, so mendwright '
        'stopped instead of dismissing it again.')
    assert isinstance(caught.value, AttentionRequired)
    assert page.doc.clicked == ['nl-no'] * 3
    assert [e.key for e in heal.handled_interruptions] == [DISMISS_KEY] * 3


def test_a_registered_interruption_is_handled_first(side, tmp_path):
    cookies = ('<div id="cc" data-pos="fixed" data-area="0.2" data-over="go">'
               '<button id="cookie-accept" type="button" data-closes="cc">OK</button></div>')
    page = _page(FORM, cookies, modal(SIGN_UP + NO_THANKS))
    page.doc.by_id('nl').find('div').set('data-over', 'email')   # the modal covers email only
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)
    heal.interrupt('cookies', '#cookie-accept')

    side.step('go', '#go', 'click')                  # the cookie banner, then the click
    side.step('email', '#email', 'fill', 'x')        # nothing registered: the popup

    assert page.doc.clicked == ['cookie-accept', 'go', 'nl-no']
    assert [(e.key, e.before) for e in heal.handled_interruptions] == [
        ('cookies', 'go'), (DISMISS_KEY, 'email')]


def test_a_popup_that_opens_during_the_click_is_dismissed_and_the_click_tried_again(
        side, tmp_path):
    page = _page(FORM)
    heal = side.open(page, store_path=_store(tmp_path), dismiss=True)
    proxy = heal.locator('go', '#go', timeout_ms=0, poll_ms=0)
    if side.kind == 'async':
        side.run(proxy._ensure())

    # The look before the click finds nothing; the popup opens as the click goes out.
    act = page.doc.act

    def popup_opens(el, verb, args):
        if 'probe' in page.calls and page.doc.by_id('nl') is None and not page.doc.clicked:
            page.doc.tree.body.append(lxml.html.fragment_fromstring(modal(SIGN_UP + NO_THANKS)))
        return act(el, verb, args)
    page.doc.act = popup_opens

    side.act(proxy, 'click')
    assert page.doc.clicked == ['nl-no', 'go']
    assert page.calls.count('click') == 3              # the intercepted one, No thanks, again
    assert [e.before for e in heal.handled_interruptions] == ['go']


def test_focus_and_set_input_files_are_not_looked_at(tmp_path):
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    heal = mendwright.open(page, provider=None, store_path=_store(tmp_path), timeout_ms=0,
                           dismiss=True)
    heal.locator('email', '#email', timeout_ms=0, poll_ms=0).focus()
    assert 'probe' not in page.calls and page.doc.by_id('nl') is not None


# --- heal.page's guard-only locators -------------------------------------------------------

def test_heal_page_guards_the_locators_it_does_not_heal(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='mendwright')
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    store = tmp_path / 'store' / 'overrides.json'
    heal = mendwright.open(page, provider=None, store_path=str(store), timeout_ms=0,
                           dismiss=True)
    p = heal.page

    go = p.get_by_role('button', name='Go')
    assert type(go) is GuardedLocator and isinstance(go, Locator)   # expect() takes it
    assert unwrap(go) is go.native and type(go.native) is Locator
    assert repr(go) == "<GuardedLocator \"page.get_by_role('button', name='Go')\">"
    go.click()

    assert page.doc.clicked == ['nl-no', 'go']
    assert heal.handled_interruptions == [InterruptionEvent(
        DISMISS_KEY, "page.get_by_role('button', name='Go')", popup=_nl(), how='“No thanks”')]
    assert not store.exists() or json.loads(store.read_text(encoding='utf-8')).get(
        'selectors', {}) == {}                                       # nothing saved

    names = [p.locator('#go', has_text='Go'), p.locator('#signup').locator('#go'),
             p.locator('#go').first, p.locator('#go').last, p.locator('#go').nth(0),
             p.locator('#go').filter(has_text='Go'), p.get_by_label('Email'),
             p.get_by_role('button').first, p.locator('#signup').locator('#go').nth(0)]
    assert [loc.__dict__['_mw_name'] for loc in names] == [
        "page.locator('#go', has_text='Go')", "page.locator('#signup').locator('#go')",
        "page.locator('#go').first", "page.locator('#go').last", "page.locator('#go').nth(0)",
        "page.locator('#go').filter(has_text='Go')", "page.get_by_label('Email')",
        "page.get_by_role('button').first", "page.locator('#signup').locator('#go').nth(0)"]
    assert all(type(loc) is GuardedLocator for loc in names)
    assert p.locator('#go').count() == 1                              # not a locator: as is
    names[1].click()
    assert page.doc.clicked == ['nl-no', 'go', 'go']


def test_the_async_heal_page_guards_them_too(tmp_path):
    sync = _page(FORM, modal(SIGN_UP + NO_THANKS))

    async def main():
        heal = await mendwright.aio.open(AsyncPopupPage(sync), provider=None,
                                         store_path=_store(tmp_path), timeout_ms=0,
                                         dismiss=True)
        heal.coordinator.log = None
        go = heal.page.get_by_role('button', name='Go')
        assert type(go) is AsyncGuardedLocator and isinstance(go, AsyncLocator)
        await go.click()
        first = heal.page.locator('#go').first
        assert type(first) is AsyncGuardedLocator
        await first.click()
        return heal

    heal = asyncio.run(main())
    assert sync.doc.clicked == ['nl-no', 'go', 'go']
    assert [e.before for e in heal.handled_interruptions] == [
        "page.get_by_role('button', name='Go')"]


# --- async: one popup for actions awaited together -----------------------------------------------

def test_async_actions_awaited_together_dismiss_one_popup_once(tmp_path):
    page = _page(FORM, modal(SIGN_UP + NO_THANKS))
    threads = []

    async def main():
        heal = await mendwright.aio.open(AsyncPopupPage(page), provider=None,
                                         store_path=_store(tmp_path), timeout_ms=0, dismiss=True)
        heal.coordinator.log = None
        dismiss = heal._core._dismiss

        def spy(*args):
            threads.append((threading.get_ident(), heal._lock.locked()))
            return dismiss(*args)
        heal._core._dismiss = spy
        email = await heal.locator('email', '#email', poll_ms=0)
        go = await heal.locator('go', '#go', poll_ms=0)
        await asyncio.gather(email.fill('ada@example.com'), go.click())
        return heal

    heal = asyncio.run(main())
    assert page.doc.clicked == ['nl-no', 'go']
    assert len(heal.handled_interruptions) == 1
    assert threads and all(locked and ident != threading.get_ident()
                           for ident, locked in threads)   # in a worker thread, under the lock


# --- Selenium ------------------------------------------------------------------------------------

class ElementClickInterceptedException(Exception):
    """Named like Selenium's, which mendwright recognises by name."""


class StaleElementReferenceException(Exception):
    """Named like Selenium's."""


class SelElement:
    """A WebElement over one element of a Doc. Each call is one round trip."""

    def __init__(self, driver, el):
        self._driver, self.el = driver, el

    def __eq__(self, other):
        return isinstance(other, SelElement) and other.el is self.el

    def __hash__(self):
        return hash(id(self.el))

    @property
    def tag_name(self):
        return self.el.tag

    def is_displayed(self):
        return self._driver.doc.shown(self.el)

    def is_selected(self):
        return False

    def get_attribute(self, name):
        return self.el.get(name)

    def click(self):
        doc = self._driver.doc
        doc.calls.append('click')
        cover = doc.covered_by(self.el)
        if cover is not None:
            raise ElementClickInterceptedException(
                'element click intercepted: Element is not clickable at point (10, 10). '
                'Other element would receive the click: <div id="' + (cover.get('id') or '')
                + '">')
        doc.click(self.el)

    def send_keys(self, *value):
        self._driver.doc.calls.append('send_keys')
        self.el.set('value', (self.el.get('value') or '') + ''.join(value))


class PopupDriver:
    """Enough of a Selenium WebDriver over a Doc: find_elements by XPath and by id, and
    execute_script for describe() and the popup scripts. Every call is in doc.calls."""

    def __init__(self, html):
        self.doc = Doc('<html><body>' + html + '</body></html>')
        self.switch_to = object()
        self.current_url = 'https://shop.example/signup'
        self.scripts = 0

    @property
    def page_source(self):
        return lxml.html.tostring(self.doc.tree, encoding='unicode')

    def find_elements(self, by, value):
        self.doc.calls.append('find_elements')
        found = self.doc.query(('xpath=' if by == 'xpath' else 'css=') + value)
        return [SelElement(self, e) for e in found]

    def find_element(self, by, value):
        return self.find_elements(by, value)[0]

    def _el(self, value):
        return value.el if isinstance(value, SelElement) else None

    def execute_script(self, script, *args):
        self.scripts += 1
        doc = self.doc
        if POPUP_PROBE_JS in script:
            known = args[1] if len(args) > 1 else None
            if known is not None and not doc.attached(known.el):
                raise StaleElementReferenceException('stale element reference')
            return doc.probe(args[0].el, self._el(known))
        if POPUP_ROOT_JS in script:
            el = doc.root(args[0].el, args[1])
            return SelElement(self, el) if el is not None else None
        if POPUP_CONTROLS_JS in script:
            doc.calls.append('controls')
            return [doc.facts(c) for c in doc.controls(args[0].el)]
        if POPUP_CONTROL_JS in script:
            el = doc.control(args[0].el, args[1])
            return SelElement(self, el) if el is not None else None
        if 'outerHTML' in script:
            el = args[0].el
            return {'tag': el.tag, 'html': lxml.html.tostring(el, encoding='unicode')}
        return None


class Chains:
    """ActionChains, for Escape: send_keys() then perform()."""

    def __init__(self, driver):
        self._driver, self._keys = driver, []

    def send_keys(self, *keys):
        self._keys.extend(keys)
        return self

    def perform(self):
        from mendwright.drivers.selenium import _key_value
        assert self._keys == [_key_value('ESCAPE')]
        self._driver.doc.escape()


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr('mendwright.drivers.selenium._action_chains', Chains)


def test_selenium_find_element_and_click_dismisses_the_modal(tmp_path, caplog, chains):
    caplog.set_level(logging.INFO, logger='mendwright')
    web = PopupDriver(FORM + modal(SIGN_UP + NO_THANKS))
    heal = mendwright.open(web, provider=None, store_path=_store(tmp_path), timeout_ms=0,
                           dismiss=True)
    heal.coordinator.log = None
    driver = heal.driver

    driver.find_element('id', 'go').click()

    assert web.doc.clicked == ['nl-no', 'go']
    assert ("mendwright: " + dismissed_message('id=go', _nl(), '“No thanks”', ['Escape'])
            in _warnings(caplog))
    assert [e.before for e in heal.handled_interruptions] == ['id=go']


def test_selenium_elements_of_any_by_dismiss_too_and_a_stale_popup_reads_as_gone(tmp_path,
                                                                                  chains):
    web = PopupDriver(FORM + modal(SIGN_UP + NO_THANKS))
    heal = mendwright.open(web, provider=None, store_path=_store(tmp_path), timeout_ms=0,
                           dismiss=True)
    heal.coordinator.log = None
    heal.driver.find_elements('xpath', "//button[@id='go']")[0].click()
    assert web.doc.clicked == ['nl-no', 'go']     # removed: Selenium refuses the stale root


def test_selenium_with_dismiss_off_makes_no_extra_call(tmp_path, chains):
    web = PopupDriver(FORM + modal(SIGN_UP + NO_THANKS))
    driver = mendwright.open(web, provider=None, store_path=_store(tmp_path), timeout_ms=0).driver
    el = driver.find_elements('xpath', "//button[@id='go']")[0]
    web.doc.calls.clear()
    with pytest.raises(ElementClickInterceptedException):
        el.click()
    assert web.doc.calls == ['click'] and web.scripts == 0


# --- reporting -----------------------------------------------------------------------------------

def test_the_journal_names_the_dismiss_event(tmp_path):
    from mendwright.journal import Journal
    journal = Journal(tmp_path / 'j.jsonl', clock=lambda: 'T')
    journal.dismissed('email', popup='<div>', how='escape', tried=[])
    line = json.loads((tmp_path / 'j.jsonl').read_text(encoding='utf-8'))
    assert line == {'ts': 'T', 'event': 'flow.dismiss', 'key': 'email', 'popup': '<div>',
                    'how': 'escape', 'tried': []}
