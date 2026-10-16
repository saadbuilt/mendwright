"""The acceptance matrix of tests/test_lists_emptied.py on real browsers, on every engine (see
conftest.py), for the cases a real DOM can change. An emptied list whose items another list
holds now (a cart whose items all moved to Saved for later, an Active list whose tasks were all
completed, notifications marked read) is never healed onto that list, so a loop that deletes
the rows of the list deletes none of the other list's. A redesigned list is not healed either:
its lookup raises the same, and the selector fixed in the code finds it. No model is asked.
"""

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright.drivers import wrap
from mw_engines import FAST_MS, POLL_MS, SELENIUM, page, saved_candidates

pytestmark = pytest.mark.browser

ITEMS = [('Blue mug', 'blue-mug', '$12.00'), ('Red mug', 'red-mug', '$13.00'),
         ('Tea pot', 'tea-pot', '$29.00')]
TASKS = ['Buy milk', 'Call the bank', 'Book flights', 'Pay rent']
EMPTY_OR_BROKEN = 'the list is empty at this point, or its selector broke'


def _cls(name):
    return "//*[contains(concat(' ', normalize-space(@class), ' '), ' " + name + " ')]"


def rows(cls, items, tag='div', button='Delete'):
    # The button removes the row, as a real cart's Delete does.
    return ''.join('<' + tag + ' class="' + cls + '"><a href="#' + s + '">' + n + '</a><span '
                   'class="price">' + p + '</span><button type="button" class="delete" '
                   'onclick="this.parentElement.remove()">' + button + '</button></' + tag + '>'
                   for n, s, p in items)


def cart(full, cart_cls='cart', saved_cls='saved', row='cart-item', headings=True):
    inside = rows(row, ITEMS if full else []) or '<p>Your cart is empty.</p>'
    return page('<main>' + ('<h1>Shopping cart</h1>' if headings else '') + '<section class="'
                + cart_cls + '">' + inside + '</section>'
                + ('<h2>Saved for later</h2>' if headings else '') + '<section class="'
                + saved_cls + '">' + rows('saved-item', [] if full else ITEMS) + '</section></main>',
                'cart')


def react(cart_items, saved_items):
    """The cart's <ul class="items"> is rendered only while it has items."""
    inside = ('<ul class="items">' + rows('cart-item', cart_items, 'li') + '</ul>') if cart_items else (
        '<p class="empty">Your cart is empty.</p>')
    saved = ('<ul class="items">' + rows('saved-item', saved_items, 'li') + '</ul>') if saved_items else ''
    return page('<main><h1>Cart</h1><div class="cart">' + inside + '</div><h2>Saved for later</h2>'
                '<div class="saved">' + saved + '</div></main>', 'cart')


def react_section(cart_items, saved_items):
    """All of Saved for later, its heading included, is rendered only while it has items."""
    inside = ('<ul class="items">' + rows('cart-item', cart_items, 'li') + '</ul>') if cart_items else (
        '<p class="empty">Your cart is empty.</p>')
    saved = ('<h2>Saved for later</h2><div class="saved"><ul class="items">'
             + rows('saved-item', saved_items, 'li') + '</ul></div>') if saved_items else ''
    return page('<main><h1>Cart</h1><div class="cart">' + inside + '</div>' + saved + '</main>',
                'cart')


def todo(active, done, row='todo'):
    """Two <ul class="tasks">, each in a section of its own."""
    def items(cls, tasks):
        return ''.join('<li class="' + cls + '"><input type="checkbox"><label>' + t
                       + '</label><button type="button" onclick="this.parentElement.remove()">'
                       'Remove</button></li>' for t in tasks)
    return page('<div class="app"><section class="pending"><h2>Active</h2><ul class="tasks">'
                + items(row, active) + '</ul></section><section class="completed"><h2>Completed'
                '</h2><ul class="tasks">' + items('done-item', done) + '</ul></section></div>',
                'todo')


def plain(cart_cls, cart_items, saved_items):
    """Two <ul>s and no heading; the cart's classes change when it empties."""
    inside = rows('cart-item', cart_items, 'li') or '<p>Your cart is empty.</p>'
    return page('<main><ul class="' + cart_cls + '">' + inside + '</ul><ul class="saved">'
                + rows('saved-item', saved_items, 'li') + '</ul></main>', 'cart')


# The verifier's real-browser cases (matrixcheck/real_new.py): the other list, its heading
# included, is rendered only once it has items, and the list's own heading changes.
E1_BEFORE = page('<main><h1>Shopping Cart</h1><section class="cart">' + rows('cart-item', ITEMS)
                 + '</section></main>', 'cart')
E1_AFTER = page('<main><h1>Your cart is empty</h1><section class="cart"><p>Nothing here.</p>'
                '</section><h2>Saved for later (3 items)</h2><section class="saved">'
                + rows('saved-item', ITEMS) + '</section></main>', 'cart')


def tasks(heading, cls):
    return page('<div class="app"><section class="group"><h2>' + heading + '</h2><ul class="tasks">'
                + rows(cls, ITEMS, 'li', 'Remove') + '</ul></section></div>', 'todo')


def notifications(heading, cls):
    return page('<div class="panel"><h2>Notifications</h2><button type="button">Mark all as read'
                '</button><div class="section"><h3>' + heading + '</h3><ul>'
                + rows(cls, ITEMS, 'li', 'Dismiss') + '</ul></div></div>', 'notifications')


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


def _press(tab, item):
    """Click the button of a row, as a loop that deletes the rows of a list does."""
    if tab.family == SELENIUM:
        item.native.find_element('css selector', 'button').click()
    else:
        item.native.locator('button').click()


def _count(tab, xpath):
    return len(lxml.html.fromstring(wrap(tab.target).outer_html()).xpath(xpath))


def _not_healed(tab, store_file, before, after, key, other):
    """Record the list on before, then run a loop that deletes its rows on after: the lookup
    raises before the loop deletes anything, nothing of other is touched, no heal is saved
    and no model asked, and the next run raises again. Returns the message."""
    tab.load(before)
    model = WrongPicker(tab, other)
    heal = _open(tab, store_file, model)
    assert len(heal.locator_all('list', key, poll_ms=POLL_MS)) >= 3
    tab.load(after)
    there = _count(tab, other)
    deleted = 0
    with pytest.raises(AttentionRequired) as info:
        for row in heal.locator_all('list', key, poll_ms=POLL_MS):
            _press(tab, row)
            deleted += 1
    message = str(info.value)
    assert EMPTY_OR_BROKEN in message and 'optional=True' not in message
    assert deleted == 0 and _count(tab, other) == there    # nothing of the other list went
    assert heal.heals == [] and model.bundles == []
    assert [x for x, c in saved_candidates(store_file, 'list').items() if c['source'] == 'heal'] == []
    again = _open(tab, store_file, model)
    with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
        again.locator_all('list', key, poll_ms=POLL_MS)
    assert model.bundles == []
    return message


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
    pytest.param(E1_BEFORE, E1_AFTER, _cls('cart-item'), _cls('saved-item'),
                 id='E1-cart-heading-changes-saved-shown-once-it-has-items'),
    pytest.param(tasks('Active', 'todo'), tasks('Completed', 'done-item'), _cls('todo'),
                 _cls('done-item'), id='E2-active-section-only-while-it-has-tasks'),
    pytest.param(notifications('New', 'notif unread'), notifications('Earlier', 'notif read'),
                 _cls('unread'), _cls('read'), id='E3-notifications-marked-read'),
])
def test_an_emptied_list_is_not_healed_onto_another_list(tab, store_file, before, after, key,
                                                         other):
    _not_healed(tab, store_file, before, after, key, other)


@pytest.mark.parametrize('before, after, key, wanted', [
    pytest.param(cart(True), cart(True, row='cart-row'), _cls('cart-item'), _cls('cart-row'),
                 id='cart-rows-renamed-in-place'),
    pytest.param(results('list', 'results', 'result'),
                 results('list list--grid', 'search-results', 'hit'), _cls('result'), _cls('hit'),
                 id='results-gain-modifier-section-renamed-aside-list'),
    pytest.param(catalog(False), catalog(True), _cls('card'), _cls('tile'),
                 id='grid-to-catalog-grid--3-tile-footer-grid'),
])
def test_a_redesigned_list_is_not_healed_either_and_its_new_selector_finds_it(
        tab, store_file, before, after, key, wanted):
    _not_healed(tab, store_file, before, after, key, wanted)
    heal = _open(tab, store_file)
    got = heal.locator_all('list', wanted, poll_ms=POLL_MS)    # fixed in the code
    names = [n for n, _, _ in ITEMS]
    assert [_text(tab, item).split('$')[0].strip() for item in got] == names
    assert heal.heals == []
    if wanted == _cls('hit'):                            # the elements are the results
        if tab.family == SELENIUM:
            got[1].native.find_element('css selector', 'a').click()
        else:
            got[1].native.locator('a').click()
        assert tab.text('#out') == 'Red mug'
