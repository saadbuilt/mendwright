"""The acceptance matrix of list heals (heal.locator_all()) for two kinds of page change that
look alike to a list fingerprint, collected from the 0.8.0 reviews and their repro scripts:

- MUST REFUSE: the list was emptied while another list on the page now holds its items (a
  cart whose items all moved to Saved for later, an Active list whose tasks were all
  completed). Nothing may be healed or saved, no element of the other list returned,
  AttentionRequired raised, a model is never shown the other list or allowed to pick it,
  and a fresh open() on the same store raises again.
- MUST HEAL: the list is still there after a redesign (its item class renamed in place, its
  container renamed or moved), next to a look-alike that is not it. It heals onto the right
  elements from the list fingerprint, with no model call.

Offline: conftest's lxml-backed FakePage (make_page), and WrongPicker, a stub model that takes the other
list whenever it is shown it. One test per case, with the case as its id."""

import asyncio
import json
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired

from test_lists import _Page

ITEMS = [('Blue mug', 'blue-mug', '$12.00'), ('Red mug', 'red-mug', '$13.00'),
         ('Tea pot', 'tea-pot', '$29.00')]
TASKS = ['Buy milk', 'Call the bank', 'Book flights', 'Pay rent']
# Both possibilities, as every refusal of an emptied list names them.
EMPTY_OR_BROKEN = 'the list is empty at this point, or its selector broke'


class WrongPicker:
    """A model that takes the other list whenever it is shown it: it answers with the first
    group whose xpath reaches one of the elements ``other`` matches on the page now, and with
    nothing otherwise. No network."""

    def __init__(self, page, other):
        self.page, self.other = page, other
        self.bundles, self.picked, self.outcomes = [], [], []
        self.last_error = None

    def propose(self, bundle):
        self.bundles.append(bundle)
        doc = lxml.html.fromstring(self.page.content())
        theirs = set(doc.xpath(self.other))
        for group in bundle.get('groups') or []:
            if theirs & set(doc.xpath(group['xpath'])):
                self.picked.append(group['xpath'])
                return {'xpath': group['xpath'], 'fallbacks': [], 'reasoning': 'stub'}
        return None

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, bool(success)))


def _open(page, root, provider=None):
    return mendwright.open(page, provider=provider, store_path=str(root / 'o.json'),
                           timeout_ms=0)


def _journal(root):
    path = root / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _saved_heals(root, key):
    path = root / 'o.json'
    if not path.exists():
        return []
    entry = json.loads(path.read_text(encoding='utf-8'))['selectors'].get(key) or {}
    return [c['xpath'] for c in entry.get('candidates') or [] if c.get('source') == 'heal']


# --- the pages ---------------------------------------------------------------------------------

def _rows(cls, items, qty=True):
    return ''.join("<div class='" + cls + "'><a href='/p/" + slug + "'>" + name + '</a>'
                   "<span class='price'>" + price + '</span>'
                   + ("<input class='qty' value='1'>" if qty else '')
                   + "<button class='delete'>Delete</button></div>" for name, slug, price in items)


def cart(full, cart_cls='cart', saved_cls='saved', row='cart-item', gone=False, layout=None,
         wrap=False, headings=True, cart_attrs=None):
    """The cart of the review (cart2.py, cart_variants.py): no id or test id, the cart's rows,
    then those saved for later. Emptied (full=False), every item is saved for later. gone
    replaces the cart's <section> with an empty state, wrap puts the rows in a <div> with no
    class, layout is the class of the <main>, and cart_attrs replaces the section's class."""
    inside = _rows(row, ITEMS if full else []) or '<p>Your cart is empty.</p>'
    if wrap:
        inside = '<div>' + inside + '</div>'
    attrs = cart_attrs or "class='" + cart_cls + "'"
    box = ("<div class='empty-state'>Your cart is empty.</div>" if gone and not full
           else '<section ' + attrs + '>' + inside + '</section>')
    main = "<main class='" + layout + "'>" if layout else '<main>'
    return ('<html><body>' + main + ('<h1>Shopping cart</h1>' if headings else '') + box
            + ('<h2>Saved for later</h2>' if headings else '') + "<section class='" + saved_cls
            + "'>" + _rows('saved-item', [] if full else ITEMS, False)
            + '</section></main></body></html>')


SAVED_ROWS = "//div[@class='saved-item']"


def todo(active, done, same_lists=False, classes=('todos', 'done')):
    """The to-do app of the review: no id or test id, the active tasks, then the completed
    ones, in a <ul> of each of classes. With same_lists, both lists are <ul class='tasks'>,
    each in a <section> of its own."""
    def items(cls, tasks):
        return ''.join("<li class='" + cls + "'><input type='checkbox'><label>" + t
                       + '</label><button>Remove</button></li>' for t in tasks)
    if same_lists:
        return ("<html><body><div class='app'><section class='pending'><h2>Active</h2>"
                "<ul class='tasks'>" + items('todo', active) + "</ul></section>"
                "<section class='completed'><h2>Completed</h2><ul class='tasks'>"
                + items('done-item', done) + '</ul></section></div></body></html>')
    return ("<html><body><div class='app'><h2>Active</h2><ul class='" + classes[0] + "'>"
            + items('todo', active) + "</ul><h2>Completed</h2><ul class='" + classes[1] + "'>"
            + items('done-item', done) + '</ul></div></body></html>')


DONE_TASKS = "//li[@class='done-item']"
EMPTY_TASKS = todo([], TASKS, same_lists=True)
COLLAPSED = EMPTY_TASKS.replace("<ul class='tasks'></ul>", "<ul class='tasks tasks--collapsed'></ul>")
WRAPPED = (COLLAPSED.replace("<ul class='tasks tasks--collapsed'></ul>",
                             "<div class='wrap'><ul class='tasks tasks--collapsed'></ul></div>")
           .replace("<ul class='tasks'><li", "<div class='wrap'><ul class='tasks'><li")
           .replace('</li></ul>', '</li></ul></div>'))
RENAMED = (COLLAPSED.replace("<ul class='tasks tasks--collapsed'>", "<ul class='tasks tasks--empty'>")
           .replace("class='pending'", "class='todo-active'")
           .replace("class='completed'", "class='todo-done'"))


def _li(cls, items):
    return ''.join("<li class='" + cls + "'><a href='/p/" + s + "'>" + n + "</a><span class='price'>"
                   + p + "</span><button class='delete'>Delete</button></li>" for n, s, p in items)


def react(cart_items, saved_items, item_cls=('cart-item', 'saved-item')):
    """edge_lists.py page1: the cart's <ul class='items'> is rendered only while it has items,
    as {items.length ? <ul class="items">...</ul> : <p>Empty</p>} does; div.cart stays."""
    inside = ("<ul class='items'>" + _li(item_cls[0], cart_items) + '</ul>') if cart_items else (
        "<p class='empty'>Your cart is empty.</p>")
    saved = ("<ul class='items'>" + _li(item_cls[1], saved_items) + '</ul>') if saved_items else ''
    return ("<html><body><main><h1>Cart</h1><div class='cart'>" + inside
            + "</div><h2>Saved for later</h2><div class='saved'>" + saved + '</div></main></body></html>')


FORK = ('Fork', 'fork', '$1')


def react_section(cart_items, saved_items):
    """Like react(), but all of Saved for later, its heading included, is rendered only while
    it has items: when the list is recorded, nothing of it is on the page."""
    inside = ("<ul class='items'>" + _li('cart-item', cart_items) + '</ul>') if cart_items else (
        "<p class='empty'>Your cart is empty.</p>")
    saved = ("<h2>Saved for later</h2><div class='saved'><ul class='items'>"
             + _li('saved-item', saved_items) + '</ul></div>') if saved_items else ''
    return ("<html><body><main><h1>Cart</h1><div class='cart'>" + inside + '</div>' + saved
            + '</main></body></html>')


def bare(cart_items, saved_items, empty_ul=True):
    """edge_lists.py page2: the cart's container is a bare <ul> in section.cart, no heading."""
    inside = '<ul>' + _li('cart-item', cart_items) + '</ul>' if (cart_items or empty_ul) else (
        '<p>Your cart is empty.</p>')
    return ("<html><body><main><section class='cart'>" + inside + "</section>"
            "<section class='saved'><ul>" + _li('saved-item', saved_items)
            + '</ul></section></main></body></html>')


def plain(cart_cls, cart_items, saved_items):
    """edge_lists.py page3: two <ul>s in <main>, no heading; the cart's class changes when it
    empties."""
    inside = _li('cart-item', cart_items) or '<p>Your cart is empty.</p>'
    return ("<html><body><main><ul class='" + cart_cls + "'>" + inside + '</ul>'
            "<ul class='saved'>" + _li('saved-item', saved_items) + '</ul></main></body></html>')


SAVED_LI = "//li[@class='saved-item']"

REFUSE = [
    pytest.param(cart(True), cart(False), '.cart-item', SAVED_ROWS, id='cart-class-only'),
    pytest.param(cart(True, cart_attrs="aria-label='Cart'"),
                 cart(False, cart_attrs="aria-label='Cart'"), '.cart-item', SAVED_ROWS,
                 id='cart-aria-label'),
    pytest.param(cart(True, cart_attrs="id='cart'"), cart(False, cart_attrs="id='cart'"),
                 '.cart-item', SAVED_ROWS, id='cart-id'),
    pytest.param(cart(True), cart(False, cart_cls='cart cart--empty'), '.cart-item', SAVED_ROWS,
                 id='cart-adds-cart--empty'),
    pytest.param(cart(True, cart_cls='cart cart--has-items'), cart(False, cart_cls='cart'),
                 '.cart-item', SAVED_ROWS, id='cart-drops-cart--has-items'),
    pytest.param(cart(True, cart_cls='cart cart--full'), cart(False, cart_cls='cart cart--empty'),
                 '.cart-item', SAVED_ROWS, id='cart-swaps-cart--full-for-cart--empty'),
    pytest.param(cart(True, cart_cls='cart has-items'), cart(False, cart_cls='cart'),
                 '.cart-item', SAVED_ROWS, id='cart-drops-has-items'),
    pytest.param(cart(True, cart_cls='cart full'), cart(False, cart_cls='cart empty'),
                 '.cart-item', SAVED_ROWS, id='cart-swaps-full-for-empty'),
    pytest.param(cart(True, cart_cls='cart active-cart'), cart(False, cart_cls='cart'),
                 '.cart-item', SAVED_ROWS, id='cart-drops-active-cart'),
    pytest.param(cart(True, cart_cls='list', saved_cls='list list--saved'),
                 cart(False, cart_cls='list', saved_cls='list list--saved'), '.cart-item',
                 SAVED_ROWS, id='list-and-list--saved'),
    pytest.param(cart(True, cart_cls='list', saved_cls='list list--saved'),
                 cart(False, cart_cls='list list--empty', saved_cls='list list--saved'),
                 '.cart-item', SAVED_ROWS, id='list-adds-list--empty'),
    pytest.param(cart(True, cart_cls='list', saved_cls='list list--saved'),
                 cart(False, cart_cls='list list--empty is-empty', saved_cls='list list--saved'),
                 '.cart-item', SAVED_ROWS, id='list-adds-list--empty-is-empty'),
    pytest.param(cart(True, cart_cls='list list--full', saved_cls='list list--saved'),
                 cart(False, cart_cls='list list--empty', saved_cls='list list--saved'),
                 '.cart-item', SAVED_ROWS, id='list--full-swapped-for-list--empty'),
    pytest.param(cart(True, cart_cls='panel', saved_cls='panel'),
                 cart(False, cart_cls='panel', saved_cls='panel'), '.cart-item', SAVED_ROWS,
                 id='two-section-panel-with-headings'),
    pytest.param(cart(True, cart_cls='panel', saved_cls='panel', headings=False),
                 cart(False, cart_cls='panel', saved_cls='panel', headings=False), '.cart-item',
                 SAVED_ROWS, id='two-section-panel-no-headings'),
    pytest.param(cart(True), cart(False, gone=True), '.cart-item', SAVED_ROWS,
                 id='cart-section-replaced-by-empty-state'),
    pytest.param(cart(True, layout='layout two-col'), cart(False, layout='layout one-col'),
                 '.cart-item', SAVED_ROWS, id='cart-page-layout-class-changes'),
    pytest.param(cart(True, layout='layout two-col'), cart(False), '.cart-item', SAVED_ROWS,
                 id='cart-page-layout-class-dropped'),
    pytest.param(cart(True, wrap=True), cart(False, wrap=True), '.cart-item', SAVED_ROWS,
                 id='cart-rows-in-bare-div'),
    pytest.param(cart(True, wrap=True), cart(False), '.cart-item', SAVED_ROWS,
                 id='cart-bare-div-goes-away'),
    pytest.param(todo(TASKS, []), todo([], TASKS), 'li.todo', DONE_TASKS, id='todo-todos-and-done'),
    pytest.param(todo(TASKS, [], True), todo([], TASKS, True), 'li.todo', DONE_TASKS,
                 id='todo-two-ul-tasks-in-sections'),
    pytest.param(todo(TASKS, [], classes=('todo-list', 'todo-list todo-list--done')),
                 todo([], TASKS, classes=('todo-list', 'todo-list todo-list--done')), 'li.todo',
                 DONE_TASKS, id='todo-bem-todo-list--done'),
    pytest.param(todo(TASKS, [], True), COLLAPSED, 'li.todo', DONE_TASKS,
                 id='todo-emptied-ul-adds-tasks--collapsed'),
    pytest.param(todo(TASKS, [], True), WRAPPED, 'li.todo', DONE_TASKS,
                 id='todo-both-lists-wrapped-in-new-div'),
    pytest.param(todo(TASKS, [], True), RENAMED, 'li.todo', DONE_TASKS,
                 id='todo-both-sections-renamed'),
    pytest.param(react(ITEMS, []), react([], ITEMS), '.cart-item', SAVED_LI,
                 id='react-ul-items-only-while-full'),
    pytest.param(react(ITEMS, ITEMS[:1] + [FORK]), react([], ITEMS + [FORK]), '.cart-item', SAVED_LI,
                 id='react-both-lists-ul-items'),
    pytest.param(react_section(ITEMS, []), react_section([], ITEMS), '.cart-item', SAVED_LI,
                 id='react-saved-section-only-while-it-has-items'),
    pytest.param(react(ITEMS, [], ('item', 'item')), react([], ITEMS, ('item', 'item')),
                 '.cart .item', "//div[@class='saved']//li", id='react-scoped-cart-item'),
    pytest.param(bare(ITEMS, []), bare([], ITEMS), '.cart-item', SAVED_LI,
                 id='bare-ul-in-section-stays'),
    pytest.param(bare(ITEMS, []), bare([], ITEMS, False), '.cart-item', SAVED_LI,
                 id='bare-ul-in-section-goes-away'),
    pytest.param(plain('cart filled', ITEMS, []), plain('cart', [], ITEMS), '.cart-item',
                 SAVED_LI, id='plain-ul-cart-filled-to-cart'),
    pytest.param(plain('cart active', ITEMS, []), plain('cart', [], ITEMS), '.cart-item',
                 SAVED_LI, id='plain-ul-cart-active-to-cart'),
    pytest.param(plain('cart is-active', ITEMS, []), plain('cart is-empty', [], ITEMS),
                 '.cart-item', SAVED_LI, id='plain-ul-is-active-to-is-empty'),
    pytest.param(plain('cart grid', ITEMS, []), plain('cart flex items-center', [], ITEMS),
                 '.cart-item', SAVED_LI, id='plain-ul-utility-classes-swapped'),
]


def results(results_cls, section_cls, item_cls):
    """edge_lists.py page4: search results in <ul class=results_cls>, and a sidebar
    <aside><ul class='list'> of Help and FAQ links that never changes. No heading."""
    hits = ''.join("<li class='" + item_cls + "'><a href='/r/" + s + "'>" + n + '</a></li>'
                   for n, s, _ in ITEMS)
    side = "<li><a href='/help'>Help</a></li><li><a href='/faq'>FAQ</a></li>"
    return ("<html><body><main class='page'><section class='" + section_cls + "'><ul class='"
            + results_cls + "'>" + hits + "</ul></section><aside><ul class='list'>" + side
            + '</ul></aside></main></body></html>')


def catalog(after):
    """edge_lists.py page5: div.grid > div.card becomes div.catalog > div.grid.grid--3 >
    div.tile, with a footer div.grid of links on the page throughout. No heading."""
    def cards(cls):
        return ''.join("<div class='" + cls + "'><a href='/p/" + s + "'>" + n + '</a><span>' + p
                       + '</span></div>' for n, s, p in ITEMS)
    footer = "<footer><div class='grid'><a href='/a'>A</a><a href='/b'>B</a></div></footer>"
    if not after:
        return "<html><body><main><div class='grid'>" + cards('card') + '</div>' + footer + (
            '</main></body></html>')
    return ("<html><body><main><div class='catalog'><div class='grid grid--3'>" + cards('tile')
            + '</div></div>' + footer + '</main></body></html>')


def shop(heading, grid, card, items=ITEMS):
    cards = ''.join("<div class='" + card + "'><a href='/p/" + s + "'>" + n + '</a><span>' + p
                    + '</span></div>' for n, s, p in items)
    return ("<html><body><main><h1>" + heading + "</h1><div class='" + grid + "'>" + cards
            + '</div></main></body></html>')


RESULTS = results('list', 'results', 'result')
HELP = "//aside//li"

HEAL = [
    pytest.param(cart(True), cart(True, row='cart-row'), '.cart-item', "//div[@class='cart-row']",
                 SAVED_ROWS, id='cart-rows-renamed-in-place'),
    pytest.param(cart(True), cart(True, cart_cls='basket', row='basket-row'), '.cart-item',
                 "//div[@class='basket-row']", SAVED_ROWS, id='cart-rows-and-section-renamed'),
    pytest.param(cart(True), cart(True, cart_cls='cart cart--v2', row='cart-row'), '.cart-item',
                 "//div[@class='cart-row']", SAVED_ROWS, id='cart-section-gains-a-class'),
    pytest.param(cart(True, cart_cls='cart cart--legacy'), cart(True, cart_cls='cart', row='cart-row'),
                 '.cart-item', "//div[@class='cart-row']", SAVED_ROWS,
                 id='cart-section-drops-a-class'),
    pytest.param(cart(True, cart_cls='cart cart--full'),
                 cart(True, cart_cls='cart cart--v2', row='cart-row'), '.cart-item',
                 "//div[@class='cart-row']", SAVED_ROWS, id='cart-section-swaps-a-class'),
    pytest.param(cart(True), cart(True, row='cart-row', layout='layout'), '.cart-item',
                 "//div[@class='cart-row']", SAVED_ROWS, id='cart-page-gains-a-layout-class'),
    pytest.param(cart(True),
                 cart(True, row='cart-row').replace('<main>', "<main><div class='layout'>")
                 .replace('</main>', '</div></main>'), '.cart-item', "//div[@class='cart-row']",
                 SAVED_ROWS, id='cart-moved-into-a-new-layout-div'),
    pytest.param(todo(TASKS, []), todo(TASKS, []).replace("class='todo'", "class='task'"),
                 'li.todo', "//li[@class='task']", None, id='todo-items-renamed-in-place'),
    pytest.param(todo(TASKS, [], True), todo(TASKS, [], True).replace("class='todo'", "class='task'"),
                 'li.todo', "//li[@class='task']", None, id='todo-in-sections-items-renamed'),
    pytest.param(RESULTS, results('list list--grid', 'search-results', 'hit'), '.result',
                 "//li[@class='hit']", HELP, id='results-gain-modifier-section-renamed-aside-list'),
    pytest.param(RESULTS, results('list', 'search-results', 'hit'), '.result', "//li[@class='hit']",
                 HELP, id='results-section-renamed-aside-list'),
    pytest.param(RESULTS, results('list grid', 'results', 'hit'), '.result', "//li[@class='hit']",
                 HELP, id='results-ul-gains-a-class-aside-list'),
    pytest.param(catalog(False), catalog(True), '.card', "//div[@class='tile']", '//footer//a',
                 id='grid-to-catalog-grid--3-tile-footer-grid'),
    pytest.param(shop('Shop', 'grid', 'card'), shop('All products', 'catalog', 'tile'), '.card',
                 "//div[@class='tile']", None, id='heading-renamed-with-the-grid'),
    pytest.param(shop('Cart (3)', 'grid', 'card'),
                 shop('Cart (4)', 'grid', 'row', ITEMS + [FORK]), '.card', "//div[@class='row']",
                 None, id='count-in-heading-changes-rows-renamed'),
]


def _refused(root, page, provider, key):
    """Look the list up on page, which must raise; returns the message."""
    heal = _open(page, root, provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all(key, key)
    assert heal.heals == []
    return str(info.value)


@pytest.mark.parametrize('before, after, key, other', REFUSE)
def test_an_emptied_list_is_not_healed_onto_another_list(tmp_path, make_page, before, after, key,
                                                         other):
    for with_model in (False, True):
        root = tmp_path / ('model' if with_model else 'none')
        page = make_page(before, url='https://shop.test/cart', fingerprints=True, css=True)
        model = WrongPicker(page, other) if with_model else None
        heal = _open(page, root, model)
        assert len(heal.locator_all(key, key)) >= 3              # recorded as a list
        page.set_html(after)
        assert len(lxml.html.fromstring(after).xpath(other)) >= 3   # the other list has them
        with pytest.raises(AttentionRequired) as info:
            heal.locator_all(key, key)
        message = str(info.value)
        assert heal.heals == []
        assert _saved_heals(root, key) == []
        assert not [e for e in _journal(root) if e['event'] == 'heal.applied']
        assert EMPTY_OR_BROKEN in message, message
        assert 'optional=True' not in message
        if model is not None:
            assert model.picked == []                          # never shown the other list
        # The next run, on the same store, refuses again: nothing was saved to replay.
        again = make_page(after, url='https://shop.test/cart', fingerprints=True, css=True)
        assert EMPTY_OR_BROKEN in _refused(root, again, model, key)
        assert _saved_heals(root, key) == []


@pytest.mark.parametrize('before, after, key, wanted, wrong', HEAL)
def test_a_redesigned_list_heals_onto_the_list(tmp_path, make_page, before, after, key, wanted,
                                               wrong):
    page = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
    model = WrongPicker(page, wrong) if wrong else None        # would take the look-alike
    heal = _open(page, tmp_path, model)
    assert len(heal.locator_all(key, key)) >= 3
    page.set_html(after)
    got = heal.locator_all(key, key)
    want = [' '.join(el.text_content().split()) for el in lxml.html.fromstring(after).xpath(wanted)]
    assert len(want) >= 3 and [item.inner_text() for item in got] == want
    assert [h.source for h in heal.heals] == ['fingerprint']
    assert model is None or model.bundles == []                 # no model call
    assert len(_saved_heals(tmp_path, key)) == 1


def test_async_an_emptied_cart_is_not_healed_onto_the_items_saved_for_later(tmp_path, make_page):
    async def main():
        sync = make_page(cart(True), url='https://shop.test/cart', fingerprints=True, css=True)
        model = WrongPicker(sync, SAVED_ROWS)
        heal = await mendwright.aio.open(_Page(sync), provider=model,
                                         store_path=str(tmp_path / 'o.json'), timeout_ms=0)
        assert len(await heal.locator_all('.cart-item', '.cart-item')) == 3
        sync.set_html(cart(False))
        with pytest.raises(AttentionRequired) as info:
            await heal.locator_all('.cart-item', '.cart-item')
        sync.set_html(cart(True, row='cart-row'))           # a redesign heals
        rows = await heal.locator_all('.cart-item', '.cart-item')
        return heal, model, str(info.value), [await r.inner_text() for r in rows]
    heal, model, message, texts = asyncio.run(main())
    assert EMPTY_OR_BROKEN in message and model.picked == []
    assert texts == ['Blue mug$12.00Delete', 'Red mug$13.00Delete', 'Tea pot$29.00Delete']
    assert [h.source for h in heal.heals] == ['fingerprint']


def test_a_list_put_under_a_new_heading_while_its_old_one_stays_is_refused(tmp_path, make_page):
    # The fail-closed side of the label test: the old heading is still on the page, so the
    # grid under another heading may be another list, and nothing is healed.
    page = make_page(shop('Shop', 'grid', 'card'), fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('.card', '.card')) == 3
    page.set_html(shop('Shop', 'grid', 'tile').replace("<div class='grid'>",
                                                       "<h2>Featured</h2><div class='catalog'>"))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('.card', '.card')
    assert ("The one most like the list (3 elements) is under 'Featured', and the list was "
            "under 'Shop', which is still on the page. So either the list is empty at this point, "
            "or its selector broke") in str(info.value)
    assert heal.heals == []


def test_the_guide_quotes_the_refusal_of_an_emptied_list(tmp_path, make_page):
    path = Path(__file__).resolve().parents[1] / 'docs' / 'guide.md'
    if not path.is_file():
        pytest.skip('docs/ is not in this copy of the project (the sdist leaves it out)')
    page = make_page(cart(True), url='https://shop.test/cart', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('cart', '.cart-item')) == 3
    page.set_html(cart(False))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('cart', '.cart-item')
    assert str(info.value) in path.read_text(encoding='utf-8')


def test_a_list_fingerprint_saved_without_where_the_list_was_heals_nothing(tmp_path, make_page):
    # One an earlier 0.8.0 build saved can't tell another list from the list: no heal, and no
    # model is shown the groups, until a run in which the list matches records it anew.
    page = make_page(cart(True), url='https://shop.test/cart', fingerprints=True, css=True)
    model = WrongPicker(page, SAVED_ROWS)
    heal = _open(page, tmp_path, model)
    assert len(heal.locator_all('cart', '.cart-item')) == 3
    old = dict(heal.store.meta('cart')['listFingerprint'])
    del old['place'], old['label']
    heal.store.set_meta('cart', list_fingerprint=old)
    page.set_html(cart(True, row='cart-row'))                # even a redesign
    with pytest.raises(AttentionRequired, match='saved by an earlier build, which did not record '
                                                'where the list was or its label'):
        heal.locator_all('cart', '.cart-item')
    assert heal.heals == [] and model.bundles == []
    page.set_html(cart(True))
    again = _open(page, tmp_path, model)                     # the next run, where it matches
    assert len(again.locator_all('cart', '.cart-item')) == 3
    assert 'place' in again.store.meta('cart')['listFingerprint']
