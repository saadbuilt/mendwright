"""The acceptance matrix of tests/test_lists_emptied.py on real browsers, on every engine (see
conftest.py), for the cases a real DOM can change: what the browser records of where a list
is and of its label must be what lxml computes from the captured HTML, an emptied list is
never healed onto the other list that holds its items now (a cart whose items all moved to
Saved for later, an Active list whose tasks were all completed), and a redesigned list next
to a look-alike still heals onto the list, with no model call.
"""

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, lists
from mendwright.drivers import wrap
from mw_engines import FAST_MS, POLL_MS, SELENIUM, page, saved_candidates

pytestmark = pytest.mark.browser

ITEMS = [('Blue mug', 'blue-mug', '$12.00'), ('Red mug', 'red-mug', '$13.00'),
         ('Tea pot', 'tea-pot', '$29.00')]
TASKS = ['Buy milk', 'Call the bank', 'Book flights', 'Pay rent']
EMPTY_OR_BROKEN = 'the list is empty at this point, or its selector broke'


def _cls(name):
    return "//*[contains(concat(' ', normalize-space(@class), ' '), ' " + name + " ')]"


def rows(cls, items):
    # Delete removes the row, as a real cart's button does.
    return ''.join('<div class="' + cls + '"><a href="#' + s + '">' + n + '</a><span class="price">'
                   + p + '</span><button type="button" class="delete" '
                   'onclick="this.parentElement.remove()">Delete</button></div>' for n, s, p in items)


def cart(full, cart_cls='cart', saved_cls='saved', row='cart-item', headings=True):
    inside = rows(row, ITEMS if full else []) or '<p>Your cart is empty.</p>'
    return page('<main>' + ('<h1>Shopping cart</h1>' if headings else '') + '<section class="'
                + cart_cls + '">' + inside + '</section>'
                + ('<h2>Saved for later</h2>' if headings else '') + '<section class="'
                + saved_cls + '">' + rows('saved-item', [] if full else ITEMS) + '</section></main>',
                'cart')


def _li(cls, items):
    return ''.join('<li class="' + cls + '"><a href="#' + s + '">' + n + '</a><span>' + p
                   + '</span></li>' for n, s, p in items)


def react(cart_items, saved_items):
    """The cart's <ul class="items"> is rendered only while it has items."""
    inside = ('<ul class="items">' + _li('cart-item', cart_items) + '</ul>') if cart_items else (
        '<p class="empty">Your cart is empty.</p>')
    saved = ('<ul class="items">' + _li('saved-item', saved_items) + '</ul>') if saved_items else ''
    return page('<main><h1>Cart</h1><div class="cart">' + inside + '</div><h2>Saved for later</h2>'
                '<div class="saved">' + saved + '</div></main>', 'cart')


def react_section(cart_items, saved_items):
    """All of Saved for later, its heading included, is rendered only while it has items."""
    inside = ('<ul class="items">' + _li('cart-item', cart_items) + '</ul>') if cart_items else (
        '<p class="empty">Your cart is empty.</p>')
    saved = ('<h2>Saved for later</h2><div class="saved"><ul class="items">'
             + _li('saved-item', saved_items) + '</ul></div>') if saved_items else ''
    return page('<main><h1>Cart</h1><div class="cart">' + inside + '</div>' + saved + '</main>',
                'cart')


def todo(active, done, row='todo'):
    """Two <ul class="tasks">, each in a section of its own."""
    def items(cls, tasks):
        return ''.join('<li class="' + cls + '"><input type="checkbox"><label>' + t
                       + '</label><button type="button">Remove</button></li>' for t in tasks)
    return page('<div class="app"><section class="pending"><h2>Active</h2><ul class="tasks">'
                + items(row, active) + '</ul></section><section class="completed"><h2>Completed'
                '</h2><ul class="tasks">' + items('done-item', done) + '</ul></section></div>',
                'todo')


def plain(cart_cls, cart_items, saved_items):
    """Two <ul>s and no heading; the cart's classes change when it empties."""
    inside = _li('cart-item', cart_items) or '<p>Your cart is empty.</p>'
    return page('<main><ul class="' + cart_cls + '">' + inside + '</ul><ul class="saved">'
                + _li('saved-item', saved_items) + '</ul></main>', 'cart')


def results(results_cls, section_cls, item_cls):
    hits = ''.join('<li class="' + item_cls + '"><a href="#r-' + s + '" onclick="document.'
                   "getElementById('out').textContent = '" + n + "'\">" + n + '</a></li>'
                   for n, s, _ in ITEMS)
    side = '<li><a href="#help">Help</a></li><li><a href="#faq">FAQ</a></li>'
    return page('<main class="page"><section class="' + section_cls + '"><ul class="' + results_cls
                + '">' + hits + '</ul></section><aside><ul class="list">' + side + '</ul></aside>'
                '<p id="out">idle</p></main>', 'search')


def catalog(after):
    def cards(cls):
        return ''.join('<div class="' + cls + '"><a href="#' + s + '">' + n + '</a><span>' + p
                       + '</span></div>' for n, s, p in ITEMS)
    footer = '<footer><div class="grid"><a href="#a">A</a><a href="#b">B</a></div></footer>'
    if not after:
        return page('<main><div class="grid">' + cards('card') + '</div>' + footer + '</main>',
                    'shop')
    return page('<main><div class="catalog"><div class="grid grid--3">' + cards('tile')
                + '</div></div>' + footer + '</main>', 'shop')


class WrongPicker:
    """A model that takes the other list whenever it is shown it (see test_lists_emptied.py),
    reading the live page."""

    def __init__(self, tab, other):
        self.tab, self.other = tab, other
        self.bundles, self.picked = [], []
        self.last_error = None

    def propose(self, bundle):
        self.bundles.append(bundle)
        doc = lxml.html.fromstring(wrap(self.tab.target).outer_html())
        theirs = set(doc.xpath(self.other))
        for group in bundle.get('groups') or []:
            if theirs & set(doc.xpath(group['xpath'])):
                self.picked.append(group['xpath'])
                return {'xpath': group['xpath'], 'fallbacks': [], 'reasoning': 'stub'}
        return None

    def report_outcome(self, key, xpath, success):
        pass


def _open(tab, store_file, provider=None):
    return mendwright.open(tab.target, provider=provider, store_path=store_file,
                           timeout_ms=FAST_MS)


def _text(tab, item):
    return item.text if tab.family == SELENIUM else item.inner_text()


def _count(tab, xpath):
    return len(lxml.html.fromstring(wrap(tab.target).outer_html()).xpath(xpath))


@pytest.mark.parametrize('html, xpath', [
    pytest.param(cart(True), _cls('cart-item'), id='cart'),
    pytest.param(cart(False), _cls('saved-item'), id='saved'),
    pytest.param(react(ITEMS, []), _cls('cart-item'), id='react'),
    pytest.param(todo(TASKS, []), _cls('todo'), id='todo'),
    pytest.param(plain('cart grid', ITEMS, []), _cls('cart-item'), id='plain'),
    pytest.param(page('<section aria-labelledby="t"><div><span role="heading">Picks</span></div>'
                      '<template><h2>no</h2></template><noscript><h2>no</h2></noscript>'
                      + rows('pick', ITEMS) + '</section><h3 id="t">Our picks</h3>'
                      '<svg><g><text>x</text></g></svg>'), _cls('pick'), id='labels'),
])
def test_the_browser_records_where_a_list_is_as_lxml_computes_it(tab, html, xpath):
    tab.load(html)
    driver = wrap(tab.target)
    live = driver.list_fingerprint(xpath)
    captured = lists.of_elements(lxml.html.fromstring(driver.outer_html()).xpath(xpath))
    assert live == captured
    assert live['place']['chain'] and 'label' in live


@pytest.mark.parametrize('before, after, key, other', [
    pytest.param(cart(True), cart(False), _cls('cart-item'), _cls('saved-item'), id='cart'),
    pytest.param(cart(True, 'panel', 'panel'), cart(False, 'panel', 'panel'), _cls('cart-item'),
                 _cls('saved-item'), id='two-section-panel'),
    pytest.param(react(ITEMS, []), react([], ITEMS), _cls('cart-item'), _cls('saved-item'),
                 id='react-ul-items-only-while-full'),
    pytest.param(react_section(ITEMS, []), react_section([], ITEMS), _cls('cart-item'),
                 _cls('saved-item'), id='react-saved-section-only-while-it-has-items'),
    pytest.param(todo(TASKS, []), todo([], TASKS), _cls('todo'), _cls('done-item'),
                 id='todo-two-ul-tasks'),
    pytest.param(plain('cart grid', ITEMS, []), plain('cart flex items-center', [], ITEMS),
                 _cls('cart-item'), _cls('saved-item'), id='plain-ul-no-heading'),
])
def test_an_emptied_list_is_not_healed_onto_another_list(tab, store_file, before, after, key,
                                                         other):
    tab.load(before)
    model = WrongPicker(tab, other)
    heal = _open(tab, store_file, model)
    assert len(heal.locator_all('list', key, poll_ms=POLL_MS)) >= 3
    tab.load(after)
    moved = _count(tab, other)
    assert moved >= 3
    with pytest.raises(AttentionRequired) as info:      # so a loop over it deletes nothing
        heal.locator_all('list', key, poll_ms=POLL_MS)
    assert EMPTY_OR_BROKEN in str(info.value) and 'optional=True' not in str(info.value)
    assert heal.heals == [] and model.picked == []
    assert _count(tab, other) == moved                    # nothing of the other list was touched
    assert [x for x, c in saved_candidates(store_file, 'list').items() if c['source'] == 'heal'] == []
    # The next run refuses again: nothing was saved to replay.
    again = _open(tab, store_file, model)
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        again.locator_all('list', key, poll_ms=POLL_MS)


@pytest.mark.parametrize('before, after, key, wanted, wrong', [
    pytest.param(cart(True), cart(True, row='cart-row'), _cls('cart-item'), _cls('cart-row'),
                 _cls('saved-item'), id='cart-rows-renamed-in-place'),
    pytest.param(results('list', 'results', 'result'),
                 results('list list--grid', 'search-results', 'hit'), _cls('result'), _cls('hit'),
                 '//aside//li', id='results-gain-modifier-section-renamed-aside-list'),
    pytest.param(catalog(False), catalog(True), _cls('card'), _cls('tile'), '//footer//a',
                 id='grid-to-catalog-grid--3-tile-footer-grid'),
])
def test_a_redesigned_list_heals_onto_the_list(tab, store_file, before, after, key, wanted, wrong):
    tab.load(before)
    model = WrongPicker(tab, wrong)
    heal = _open(tab, store_file, model)
    assert len(heal.locator_all('list', key, poll_ms=POLL_MS)) == 3
    tab.load(after)
    got = heal.locator_all('list', key, poll_ms=POLL_MS)
    names = [n for n, _, _ in ITEMS]
    assert [_text(tab, item).split('$')[0].strip() for item in got] == names
    assert [h.source for h in heal.heals] == ['fingerprint'] and model.bundles == []
    if wanted == _cls('hit'):                            # the healed elements are the results
        if tab.family == SELENIUM:
            got[1].native.find_element('css selector', 'a').click()
        else:
            got[1].native.locator('a').click()
        assert tab.text('#out') == 'Red mug'
