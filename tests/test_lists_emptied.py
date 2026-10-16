"""The acceptance matrix of list lookups (heal.locator_all()) whose selectors match nothing any
more, collected from the 0.8.0 reviews, their repro scripts and the verifier's cases. Two kinds
of page change look alike there:

- EMPTIED: the list was emptied while another list on the page now holds its items (a cart
  whose items all moved to Saved for later, an Active list whose tasks were all completed,
  notifications marked read, a search with no results that recommends the same products).
- REDESIGN: the list is still there, with its items' class, its container or its heading
  renamed, sometimes next to a look-alike that is not it.

No rule on the page's markup tells them apart: two pairs below are the same page but for a
class name and a heading's text. So mendwright heals neither. Each raises AttentionRequired
naming both possibilities, with no word of optional=True, saves nothing, returns no element,
and asks no model (WrongPicker, a stub model that takes the other list whenever it is shown
it, is never asked), and a fresh open() on the same store raises again. A redesign is fixed in
the code, and the list is found again.

Offline: conftest's lxml-backed FakePage (make_page). One test per case, with the case as its id."""

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


def rows(cls, items, tag='div', button='Delete'):
    """The rows of the verifier's pages (matrixcheck/new_cases.py)."""
    return ''.join('<' + tag + " class='" + cls + "'><a href='/p/" + s + "'>" + n + "</a><span "
                   "class='price'>" + p + "</span><button class='delete'>" + button + '</button></'
                   + tag + '>' for n, s, p in items)


def page(body):
    return '<html><body>' + body + '</body></html>'


def todomvc(cls):
    """TodoMVC's list: one <ul class='todo-list'>, where a completed task gets class='completed'."""
    def item(task):
        return ('<li' + (" class='" + cls + "'" if cls else '') + "><div class='view'><input "
                "class='toggle' type='checkbox'><label>" + task + "</label><button class='destroy'>"
                '</button></div></li>')
    return page("<section class='todoapp'><h1>todos</h1><ul class='todo-list'>"
                + ''.join(item(t) for t in TASKS) + '</ul></section>')


def tasks(heading, cls):
    """One section of tasks, rendered only while it has some (the verifier's E2)."""
    return page("<div class='app'><section class='group'><h2>" + heading + "</h2><ul class='tasks'>"
                + rows(cls, ITEMS, 'li', 'Remove') + '</ul></section></div>')


def mail(tab, cls, button):
    """One lazily rendered tab panel, reused for the Inbox and the Archive (the verifier's E8)."""
    return page("<div class='mail'><div role='tablist'><button id='t-in' role='tab'>Inbox</button>"
                "<button id='t-ar' role='tab'>Archive</button></div><div class='panel' "
                "role='tabpanel' aria-labelledby='" + tab + "'><ul class='msgs'>"
                + rows(cls, ITEMS, 'li', button) + '</ul></div></div>')

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
    pytest.param(todomvc(''), todomvc('completed'), '.todo-list li:not(.completed)',
                 "//li[@class='completed']", id='todomvc-every-task-completed'),
    pytest.param(todomvc('todo'), todomvc('done'), 'li.todo', "//li[@class='done']",
                 id='todo-class-swapped-for-done'),
    # The verifier's (matrixcheck/new_cases.py): the other list, its heading included, is
    # rendered only once it has items, and the list's own heading changes or goes away.
    pytest.param(page("<main><h1>Shopping Cart</h1><section class='cart'>" + rows('cart-item', ITEMS)
                      + '</section></main>'),
                 page("<main><h1>Your cart is empty</h1><section class='cart'><p>Nothing here.</p>"
                      "</section><h2>Saved for later (3 items)</h2><section class='saved'>"
                      + rows('saved-item', ITEMS) + '</section></main>'),
                 '.cart-item', SAVED_ROWS, id='E1-cart-heading-changes-saved-shown-once-it-has-items'),
    pytest.param(page("<main><h1>Shopping Cart</h1><section class='cart'>" + rows('cart-item', ITEMS)
                      + "</section><h2>Saved for later (0 items)</h2><section class='saved'><p>None"
                      '</p></section></main>'),
                 page("<main><h1>Your cart is empty</h1><section class='cart'><p>Nothing here.</p>"
                      "</section><h2>Saved for later (3 items)</h2><section class='saved'>"
                      + rows('saved-item', ITEMS) + '</section></main>'),
                 '.cart-item', SAVED_ROWS, id='E1b-saved-section-there-when-recorded'),
    pytest.param(page("<main><div class='page'><section class='cart'><h2>Cart</h2>"
                      + rows('cart-item', ITEMS) + '</section></div></main>'),
                 page("<main><div class='page'><div class='empty-cart'><h2>Your cart is empty</h2>"
                      "</div><section class='saved'><h2>Saved for later</h2>"
                      + rows('saved-item', ITEMS) + '</section></div></main>'),
                 '.cart-item', SAVED_ROWS, id='E1c-cart-section-replaced-by-an-empty-state'),
    pytest.param(tasks('Active', 'todo'), tasks('Completed', 'done-item'), 'li.todo',
                 "//li[@class='done-item']", id='E2-active-section-only-while-it-has-tasks'),
    pytest.param(tasks('Active', 'todo').replace("class='group'", "class='active-tasks'"),
                 tasks('Completed', 'done-item').replace("class='group'", "class='completed-tasks'"),
                 'li.todo', "//li[@class='done-item']", id='E2b-sections-of-their-own-classes'),
    pytest.param(page("<div class='panel'><h2>Notifications</h2><button>Mark all as read</button>"
                      "<div class='section'><h3>New</h3><ul>"
                      + rows('notif unread', ITEMS, 'li', 'Dismiss') + '</ul></div></div>'),
                 page("<div class='panel'><h2>Notifications</h2><button>Mark all as read</button>"
                      "<div class='section'><h3>Earlier</h3><ul>"
                      + rows('notif read', ITEMS, 'li', 'Dismiss') + '</ul></div></div>'),
                 '.notif.unread', "//li[@class='notif read']", id='E3-notifications-marked-read'),
    pytest.param(page("<main><h1>3 results for mug</h1><ul class='results'>"
                      + rows('result', ITEMS, 'li', 'Add') + '</ul></main>'),
                 page("<main><h1>No results for mugz</h1><h2>Popular right now</h2><ul class='recs'>"
                      + rows('rec', ITEMS, 'li', 'Add') + '</ul></main>'),
                 '.result', "//li[@class='rec']", id='E4-no-results-recommends-the-same-products'),
    pytest.param(page("<main><h1>Products</h1><div class='grid'>" + rows('card', ITEMS)
                      + '</div><nav>1 2</nav></main>'),
                 page("<main><h1>Products</h1><p>No more products.</p><nav>1 2</nav><h2>Recently "
                      "viewed</h2><div class='strip'>" + rows('mini', ITEMS) + '</div></main>'),
                 '.card', "//div[@class='mini']", id='E5-last-page-empty-recently-viewed'),
    pytest.param(page("<main><div class='grid'>" + rows('card', ITEMS) + '</div><nav>1 2</nav></main>'),
                 page("<main><p>No more products.</p><nav>1 2</nav><h2>Recently viewed</h2>"
                      "<div class='strip'>" + rows('mini', ITEMS) + '</div></main>'),
                 '.card', "//div[@class='mini']", id='E5b-last-page-empty-no-heading'),
    pytest.param(page("<header class='top'>Shop</header><aside class='drawer'><h2>Your bag</h2><ul "
                      "class='lines'>" + rows('line', ITEMS, 'li', 'Remove') + '</ul></aside><main>'
                      '<h1>Home</h1></main>'),
                 page("<header class='top'>Shop</header><main><h1>Home</h1></main><div class='modal'>"
                      "<h2>Order summary</h2><ul class='summary'>"
                      + rows('summary-line', ITEMS, 'li', 'Remove') + '</ul></div>'),
                 '.line', "//li[@class='summary-line']", id='E6-bag-drawer-gone-items-in-a-modal'),
    pytest.param(page("<header class='top'>Shop</header><aside class='drawer'><h2>Your bag</h2><ul "
                      "class='lines'>" + rows('line', ITEMS, 'li', 'Remove') + '</ul></aside><main>'
                      '<h1>Home</h1></main>'),
                 page("<header class='top'>Shop</header><aside class='drawer'><h2>Your bag</h2><p>"
                      "Empty</p></aside><main><h1>Home</h1></main><div class='modal'><h2>Order "
                      "summary</h2><ul class='summary'>" + rows('summary-line', ITEMS, 'li', 'Remove')
                      + '</ul></div>'),
                 '.line', "//li[@class='summary-line']", id='E6b-bag-drawer-stays-empty'),
    pytest.param(page("<div class='layout'><main><h1>Cart (3)</h1><ul class='cart'>"
                      + rows('cart-line', ITEMS, 'li') + '</ul></main></div>'),
                 page("<div class='layout'><main><h1>Cart is empty</h1></main><aside class='wishlist'>"
                      "<h2>Wishlist</h2><ul class='wish'>" + rows('wish-line', ITEMS, 'li')
                      + '</ul></aside></div>'),
                 '.cart-line', "//li[@class='wish-line']", id='E7-cart-moved-to-a-new-wishlist'),
    pytest.param(mail('t-in', 'inbox-row', 'Archive'), mail('t-ar', 'archive-row', 'Delete'),
                 '.inbox-row', "//li[@class='archive-row']", id='E8-one-tab-panel-reused-for-archive'),
    pytest.param(page("<div class='board'><section class='col'><h2>To do</h2>"
                      + rows('card todo', ITEMS) + "</section><section class='col'><h2>Done</h2>"
                      '</section></div>'),
                 page("<div class='board'><section class='col'><h2>To do</h2></section><section "
                      "class='col'><h2>Done</h2>" + rows('card done', ITEMS) + '</section></div>'),
                 '.card.todo', "//div[@class='card done']", id='E9-kanban-cards-all-done'),
    pytest.param(page("<main><section class='cart'>" + rows('cart-item', ITEMS) + '</section></main>'),
                 page("<main><section class='cart'><p>Empty</p></section><section class='saved'>"
                      + rows('saved-item', ITEMS) + '</section></main>'),
                 '.cart-item', SAVED_ROWS, id='E10-no-headings-saved-shown-once-it-has-items'),
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


def results_v(item, ul_attrs="class='results'", head='<h2>Results</h2>', wrap=None):
    """The verifier's search results, under h1 Shop and an h2, with a footer list of links."""
    ul = '<ul ' + ul_attrs + '>' + rows(item, ITEMS, 'li', 'Add') + '</ul>'
    if wrap:
        ul = wrap[0] + ul + wrap[1]
    return page('<main><h1>Shop</h1>' + head + ul + "<footer><ul class='links'><li><a href='/help'>"
                "Help</a></li><li><a href='/faq'>FAQ</a></li></ul></footer></main>")


def bootstrap(card_cls, heading):
    """A Bootstrap page: the cards in div.container > div.row, and a footer with the same."""
    return page("<div class='container'>" + heading + "<div class='row'>" + rows(card_cls, ITEMS)
                + "</div></div><footer><div class='container'><div class='row'><div class='col'>About"
                "</div><div class='col'>Help</div></div></div></footer>")


def orders(row_cls, article):
    """A table of orders, moved with its heading into an <article><header> (the verifier's R10)."""
    body = ''.join("<tr class='" + row_cls + "'><td><a href='/o/" + s + "'>" + n + '</a><span>' + p
                   + '</span><button>Delete</button></td></tr>' for n, s, p in ITEMS)
    table = "<table class='orders'><tbody>" + body + '</tbody></table>'
    if article:
        return page("<main><article class='card'><header><h2>Orders</h2></header>" + table
                    + '</article></main>')
    return page('<main><h2>Orders</h2>' + table + '</main>')

REDESIGN = [
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
    pytest.param(shop('Shop', 'grid', 'card'),
                 shop('Shop', 'grid', 'tile').replace("<div class='grid'>",
                                                      "<h2>Featured</h2><div class='catalog'>"),
                 '.card', "//div[@class='tile']", None, id='under-a-new-heading-old-one-stays'),
    pytest.param(todomvc('todo'), todomvc('task'), 'li.todo', "//li[@class='task']", None,
                 id='todomvc-items-renamed-in-place'),
    pytest.param(tasks('Active', 'todo'), tasks('To do', 'task'), 'li.todo', "//li[@class='task']",
                 None, id='tasks-heading-and-items-renamed'),
    # The verifier's (matrixcheck/new_cases.py).
    pytest.param(results_v('result'), results_v('hit', "class='results' aria-label='Search results'"),
                 '.result', "//li[@class='hit']", '//footer//li', id='R1-list-gains-an-aria-label'),
    pytest.param(results_v('result'),
                 results_v('hit', wrap=("<section aria-label='Search results'>", '</section>')),
                 '.result', "//li[@class='hit']", '//footer//li', id='R1b-wrapped-in-a-labelled-section'),
    pytest.param(results_v('result'),
                 results_v('hit', wrap=("<div class='layout'><aside class='filters'><h3>Filters</h3>"
                                        "<label><input type='checkbox'> In stock</label></aside>"
                                        "<div class='col'>", '</div></div>')),
                 '.result', "//li[@class='hit']", '//footer//li', id='R2-a-filter-sidebar-added'),
    pytest.param(results_v('result'), results_v('hit', head=''), '.result', "//li[@class='hit']",
                 '//footer//li', id='R3-subheading-dropped'),
    pytest.param(results_v('result'),
                 results_v('hit', head="<div class='toolbar'><div class='title'><h2>Results</h2>"
                                       '</div><button>Sort</button></div>'),
                 '.result', "//li[@class='hit']", '//footer//li', id='R3b-heading-in-a-toolbar'),
    pytest.param(page("<main><div><p>Free shipping</p></div><h1>Shop</h1><div class='grid'>"
                      + rows('card', ITEMS) + '</div></main>'),
                 page("<main><div><p>Free shipping</p></div><h1>Shop</h1><div><div class='grid'>"
                      + rows('tile', ITEMS) + '</div></div></main>'),
                 '.card', "//div[@class='tile']", None, id='R4-grid-wrapped-in-a-bare-div'),
    pytest.param(page("<main class='sc-a1b2c3'><div class='sc-x9y8z7'><p>Free shipping</p></div>"
                      "<h1>Shop</h1><div class='grid'>" + rows('card', ITEMS) + '</div></main>'),
                 page("<main class='sc-a1b2c3'><div class='sc-x9y8z7'><p>Free shipping</p></div>"
                      "<h1>Shop</h1><div class='sc-q5w6e7'><div class='grid'>" + rows('tile', ITEMS)
                      + '</div></div></main>'),
                 '.card', "//div[@class='tile']", None, id='R4b-styled-components-wrapper'),
    pytest.param(page("<main><div><ul>" + rows('item', ITEMS, 'li') + '</ul></div></main><footer>'
                      "<div><ul><li><a href='/help'>Help</a></li><li><a href='/faq'>FAQ</a></li>"
                      '</ul></div></footer>'),
                 page("<main><div><ul>" + rows('product', ITEMS, 'li') + '</ul></div></main><footer>'
                      "<div><ul><li><a href='/help'>Help</a></li><li><a href='/faq'>FAQ</a></li>"
                      '</ul></div></footer>'),
                 '.item', "//li[@class='product']", '//footer//li', id='R5-renamed-in-place-footer-alike'),
    pytest.param(bootstrap('product-card', ''), bootstrap('card', ''), '.product-card',
                 "//div[@class='card']", "//div[@class='col']", id='R5b-bootstrap-row-no-heading'),
    pytest.param(bootstrap('product-card', '<h2>Products</h2>'), bootstrap('card', '<h2>Products</h2>'),
                 '.product-card', "//div[@class='card']", "//div[@class='col']",
                 id='R5b-bootstrap-row-with-a-heading'),
    pytest.param(page("<header><div class='container'><div class='row'><div class='col'>Logo</div>"
                      "</div></div></header><main><h1>Shop</h1><div class='product-list'>"
                      + rows('product-card', ITEMS) + '</div></main>'),
                 page("<header><div class='container'><div class='row'><div class='col'>Logo</div>"
                      "</div></div></header><main><h1>Shop</h1><div class='container'><div class='row'>"
                      + rows('card', ITEMS) + '</div></div></main>'),
                 '.product-card', "//div[@class='card']", None, id='R6-bootstrap-grid-moved-into-a-row'),
    pytest.param(page("<main><h1>Products</h1><div class='grid'>" + rows('card', ITEMS) + '</div></main>'),
                 page("<main><h1>Products</h1><div role='tablist'><button id='tab-all' role='tab'>All"
                      "</button><button id='tab-new' role='tab'>New</button></div><div role='tabpanel' "
                      "aria-labelledby='tab-all'><div class='grid'>" + rows('tile', ITEMS)
                      + '</div></div></main>'),
                 '.card', "//div[@class='tile']", None, id='R7-grid-moved-into-a-tab-panel'),
    pytest.param(page("<main><h2>Featured</h2><div class='grid'>" + rows('card', ITEMS) + '</div></main>'),
                 page("<main><h2>Featured products</h2><div class='grid'>" + rows('tile', ITEMS)
                      + '</div></main>'),
                 '.card', "//div[@class='tile']", None, id='R8-heading-renamed'),
    pytest.param(page("<main><h1>Shop</h1><div class='grid'>" + rows('card', ITEMS) + "</div><div "
                      "class='m-only' style='display:none'><div class='mgrid'>" + rows('mcard', ITEMS)
                      + '</div></div></main>'),
                 page("<main><h1>Shop</h1><div class='grid'>" + rows('tile', ITEMS) + "</div><div "
                      "class='m-only' style='display:none'><div class='mgrid'>" + rows('mtile', ITEMS)
                      + '</div></div></main>'),
                 '.card', "//div[@class='tile']", "//div[@class='mtile']", id='R9-hidden-mobile-copy'),
    pytest.param(orders('order', False), orders('order-row', True), '.order',
                 "//tr[@class='order-row']", None, id='R10-into-an-article-with-a-header'),
]


def _refused(root, page, provider, key):
    """Look the list up on page, which must raise; returns the message."""
    heal = _open(page, root, provider)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all(key, key)
    assert heal.heals == []
    return str(info.value)


def _not_healed(tmp_path, make_page, before, after, key, other):
    """Record the list on before, then look it up on after with WrongPicker, which takes the
    elements other matches, as the model: it must raise, saving and journaling no heal,
    naming both possibilities and asking no model, and so must a fresh open() on the same
    store. Returns the message."""
    page = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
    model = WrongPicker(page, other)
    heal = _open(page, tmp_path, model)
    assert len(heal.locator_all(key, key)) >= 3              # recorded as a list
    page.set_html(after)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all(key, key)
    message = str(info.value)
    assert heal.heals == []
    assert _saved_heals(tmp_path, key) == []
    assert not [e for e in _journal(tmp_path) if e['event'] == 'heal.applied']
    assert EMPTY_OR_BROKEN in message, message
    assert 'optional=True' not in message
    assert model.bundles == []                             # no model is asked
    # The next run, on the same store, refuses again: nothing was saved to replay.
    again = make_page(after, url='https://shop.test/x', fingerprints=True, css=True)
    assert EMPTY_OR_BROKEN in _refused(tmp_path, again, model, key)
    assert _saved_heals(tmp_path, key) == [] and model.bundles == []
    return message


@pytest.mark.parametrize('before, after, key, other', REFUSE)
def test_an_emptied_list_is_not_healed_onto_another_list(tmp_path, make_page, before, after, key,
                                                         other):
    assert len(lxml.html.fromstring(after).xpath(other)) >= 3   # the other list has the items
    _not_healed(tmp_path, make_page, before, after, key, other)


@pytest.mark.parametrize('before, after, key, wanted, other', REDESIGN)
def test_a_redesigned_list_is_not_healed_either_and_its_new_selector_finds_it(
        tmp_path, make_page, before, after, key, wanted, other):
    # other is a look-alike on the page, which the stub model would take; else it takes the list.
    doc = lxml.html.fromstring(after)
    _not_healed(tmp_path, make_page, before, after, key,
                other if other and doc.xpath(other) else wanted)
    # Fixed in the code, the list is found again.
    page = make_page(after, url='https://shop.test/x', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    got = heal.locator_all(key, wanted)
    want = [' '.join(el.text_content().split()) for el in doc.xpath(wanted)]
    assert len(want) >= 3 and [' '.join(item.inner_text().split()) for item in got] == want
    assert heal.heals == []


@pytest.mark.parametrize('before, emptied, redesigned, swaps', [
    pytest.param(todomvc('todo'), todomvc('done'), todomvc('task'), [('done', 'task')],
                 id='todo-class-swapped-for-done-or-renamed-to-task'),
    pytest.param(tasks('Active', 'todo'), tasks('Completed', 'done-item'), tasks('To do', 'task'),
                 [('done-item', 'task'), ('Completed', 'To do')],
                 id='active-tasks-all-completed-or-section-redesigned'),
])
def test_an_emptied_list_and_a_redesign_can_be_the_same_page(tmp_path, make_page, before, emptied,
                                                            redesigned, swaps):
    # The page after the list was emptied is the page after a redesign but for one class name,
    # and a heading's text: only what the words mean tells them apart, so a rule on the markup
    # that heals the redesign heals the emptied list too. mendwright heals neither.
    for old, new in swaps:
        emptied = emptied.replace(old, new)
    assert emptied == redesigned
    _not_healed(tmp_path, make_page, before, redesigned, 'li.todo', "//li[@class='task']")


def test_a_list_heal_an_earlier_build_saved_is_never_used(tmp_path, make_page):
    # A build of 0.8.0 before its release healed the emptied cart onto the items saved for
    # later and saved it, marked "list": true. A list lookup uses no heal, so it raises.
    saved = "//div[contains(concat(' ', normalize-space(@class), ' '), ' saved-item ')]"
    page = make_page(cart(True), url='https://shop.test/cart', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all('.cart-item', '.cart-item')) == 3
    heal.store.add_candidate('.cart-item', saved, list_heal=True)
    heal.store.record_success('.cart-item', saved)
    assert _saved_heals(tmp_path, '.cart-item') == [saved]
    page.set_html(cart(False))
    assert len(lxml.html.fromstring(cart(False)).xpath(saved)) == 3
    for run in (heal, _open(page, tmp_path)):
        with pytest.raises(AttentionRequired, match=EMPTY_OR_BROKEN):
            run.locator_all('.cart-item', '.cart-item')
        assert run.heals == []


def test_async_an_emptied_cart_is_not_healed_onto_the_items_saved_for_later(tmp_path, make_page):
    async def main():
        sync = make_page(cart(True), url='https://shop.test/cart', fingerprints=True, css=True)
        model = WrongPicker(sync, SAVED_ROWS)
        heal = await mendwright.aio.open(_Page(sync), provider=model,
                                         store_path=str(tmp_path / 'o.json'), timeout_ms=0)
        assert len(await heal.locator_all('.cart-item', '.cart-item')) == 3
        sync.set_html(cart(False))
        with pytest.raises(AttentionRequired) as emptied:
            await heal.locator_all('.cart-item', '.cart-item')
        sync.set_html(cart(True, row='cart-row'))           # a redesign is not healed either
        with pytest.raises(AttentionRequired) as redesigned:
            await heal.locator_all('.cart-item', '.cart-item')
        rows = await heal.locator_all('.cart-item', '.cart-row')   # fixed in the code
        return heal, model, emptied, redesigned, [await r.inner_text() for r in rows]
    heal, model, emptied, redesigned, texts = asyncio.run(main())
    assert EMPTY_OR_BROKEN in str(emptied.value) and EMPTY_OR_BROKEN in str(redesigned.value)
    assert model.bundles == [] and heal.heals == []
    assert texts == ['Blue mug$12.00Delete', 'Red mug$13.00Delete', 'Tea pot$29.00Delete']


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
