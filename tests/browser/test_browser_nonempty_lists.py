"""heal.locator_all(..., nonempty=True) on real browsers, on every engine (see conftest.py).

The place and items reads a browser makes (LIST_PLACE_JS, ITEMS_JS) must be what lxml computes
from the captured HTML. A cart whose items all moved to Saved for later raises, and a loop that
deletes the cart's rows deletes none of the saved ones, run after run. A cart whose rows were
renamed is proposed and never used: once accepted with mendwright lists accept, the lookup
raises until mendwright fix --apply writes the new selector into the code, and then the rows
are the cart's. An acceptance of the wrong list is quarantined as soon as the code's selector
matches again, so mendwright fix never writes it. Also async Playwright, and how long the reads
take on a large page. No model is asked.
"""

import ast
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, cli, fixback, lists
from mendwright.drivers import wrap
from mendwright.drivers.base import item_rows
from mw_engines import FAST_MS, POLL_MS, SELENIUM, page, saved_candidates

pytestmark = pytest.mark.browser

ITEMS = [('Blue mug', 'blue-mug', '$12.00'), ('Red mug', 'red-mug', '$13.00'),
         ('Tea pot', 'tea-pot', '$29.00')]
SAVED = [('Fork', 'fork', '$1.00'), ('Spoon', 'spoon', '$1.50')]
TASKS = ['Buy milk', 'Call the bank', 'Book flights', 'Pay rent']
ROWS = "//div[contains(concat(' ', normalize-space(@class), ' '), ' cart-row ')]"


def _cls(name):
    return "//*[contains(concat(' ', normalize-space(@class), ' '), ' " + name + " ')]"


def rows(cls, items, tag='div'):
    # The button removes the row, as a real cart's Delete does.
    return ''.join('<' + tag + ' class="' + cls + '"><a href="#' + s + '">' + n + '</a><span '
                   'class="price">' + p + '</span><button type="button" class="delete" '
                   'onclick="this.parentElement.remove()">Delete</button></' + tag + '>'
                   for n, s, p in items)


def cart(cart_items=ITEMS, saved_items=SAVED, row='cart-item'):
    inside = rows(row, cart_items) or '<p>Your cart is empty.</p>'
    return page('<main><h1>Shopping cart</h1><section class="cart">' + inside + '</section>'
                '<h2>Saved for later</h2><section class="saved">' + rows('saved-item', saved_items)
                + '</section></main>', 'cart')


EMPTIED = cart([], SAVED + ITEMS)            # every item moved to Saved for later
RENAMED = cart(row='cart-row')               # a redesign renamed the rows


def notifications(heading, cls):
    return page('<div class="panel"><h2>Notifications</h2><div class="section"><h3>' + heading
                + '</h3><ul>' + rows(cls, ITEMS, 'li') + '</ul></div></div>', 'notifications')


def todomvc(cls):
    def item(task):
        return ('<li class="' + cls + '"><div class="view"><input class="toggle" '
                'type="checkbox"><label>' + task + '</label><button class="destroy"></button>'
                '</div></li>')
    return page('<section class="todoapp"><h1>todos</h1><ul class="todo-list">'
                + ''.join(item(t) for t in TASKS) + '</ul></section>', 'todo')


def _open(tab, store_file):
    return mendwright.open(tab.target, provider=None, store_path=store_file,
                           timeout_ms=FAST_MS)


def _all(heal, selector='.cart-item'):
    return heal.locator_all('cart', selector, nonempty=True, poll_ms=POLL_MS)


def _raised(heal, selector='.cart-item'):
    with pytest.raises(AttentionRequired) as info:
        _all(heal, selector)
    return str(info.value)


def _press(tab, item):
    """Click the Delete button of a row, as a loop that deletes the rows of a list does."""
    if tab.family == SELENIUM:
        item.native.find_element('css selector', 'button').click()
    else:
        item.native.locator('button').click()


def _count(tab, xpath):
    return len(lxml.html.fromstring(wrap(tab.target).outer_html()).xpath(xpath))


def _texts(tab, items):
    if tab.family == SELENIUM:
        return [el.find_element('css selector', 'a').text for el in items]
    return [item.locator('a').inner_text() for item in items]


def _accept(message, store_file, key='cart'):
    """Accept the proposal message names, with mendwright lists accept."""
    found = re.search(r'mendwright lists accept ' + re.escape(key) + r' ([0-9a-f]{8})', message)
    assert found, message
    assert cli.main(['lists', 'accept', key, found.group(1), '--store', store_file]) == 0
    return found.group(1)


def test_the_browser_records_where_a_list_sits_and_its_items_as_lxml_does(tab):
    hidden = cart().replace('<section class="saved">', '<section class="saved" '
                                                         'style="display:none">')
    tab.load(hidden)
    driver = wrap(tab.target)
    live = driver.list_fingerprint('.cart-item', place=True)
    doc = lxml.html.fromstring(driver.outer_html())
    assert live == lists.of_elements(doc.xpath(_cls('cart-item')), place=True)
    assert lists.placed(live) and live['label'] == 'Shopping cart'
    assert not lists.placed(driver.list_fingerprint('.cart-item'))
    for css, xpath in (('.cart-item', _cls('cart-item')), ('.saved-item', _cls('saved-item'))):
        assert driver.list_items(css) == item_rows(lists.items_of_elements(doc.xpath(xpath)))
    assert [r['visible'] for r in driver.list_items('.saved-item')] == [False, False]


def test_an_emptied_cart_is_never_used_and_nothing_saved_for_later_goes(tab, store_file):
    tab.load(cart())
    heal = _open(tab, store_file)
    assert _texts(tab, _all(heal)) == [n for n, _, _ in ITEMS]
    tab.load(EMPTIED)
    for run in (heal, _open(tab, store_file), _open(tab, store_file)):
        deleted = 0
        with pytest.raises(AttentionRequired) as info:
            for row in _all(run):
                _press(tab, row)
                deleted += 1
        assert 'on the page is another list' in str(info.value)
        assert deleted == 0 and _count(tab, _cls('saved-item')) == 5
        assert run.heals == []
    saved = saved_candidates(store_file, 'cart')
    assert [x for x, c in saved.items() if c.get('status', 'active') == 'active'] == ['.cart-item']
    assert not [c for c in saved.values() if c.get('list')]


def test_a_renamed_cart_is_proposed_accepted_and_written_into_the_code(tab, store_file, tmp_path):
    tab.load(cart())
    heal = _open(tab, store_file)
    assert len(_all(heal)) == 3
    tab.load(RENAMED)
    message = _raised(heal)
    assert ROWS + ' (as CSS: div.cart-row)' in message
    _accept(message, store_file)
    again = _open(tab, store_file)
    assert 'You accepted a list heal for it on' in _raised(again)    # not in the code yet
    code = tmp_path / 'code' / 'test_cart.py'
    code.parent.mkdir()
    code.write_text('rows = heal.locator_all("cart", ".cart-item", nonempty=True)\n',
                    encoding='utf-8')
    assert cli.main(['fix', '--apply', '--store', store_file, '--path', str(code.parent)]) == 0
    call = ast.parse(code.read_text(encoding='utf-8')).body[0].value
    assert call.args[0].value == 'cart'                               # the key literal is kept
    selector = call.args[1].value
    assert selector == 'div.cart-row'
    fixed = _open(tab, store_file)
    items = _all(fixed, selector)
    assert _texts(tab, items) == [n for n, _, _ in ITEMS]
    _press(tab, items[0])
    assert _count(tab, _cls('cart-row')) == 2 and _count(tab, _cls('saved-item')) == 2
    data = json.loads(Path(store_file).read_text(encoding='utf-8'))['selectors']['cart']
    assert not [c for c in data['candidates'] if c.get('status') in ('proposed', 'accepted')]


def test_a_wrong_acceptance_is_quarantined_before_mendwright_fix_writes_it(tab, store_file,
                                                                          tmp_path):
    # Every notification was marked read: the read ones are proposed as the list, and a
    # person accepts them. The unread selector matching again quarantines that.
    tab.load(notifications('New', 'notif unread'))
    heal = _open(tab, store_file)
    assert len(heal.locator_all('news', '.unread', nonempty=True, poll_ms=POLL_MS)) == 3
    tab.load(notifications('Earlier', 'notif read'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('news', '.unread', nonempty=True, poll_ms=POLL_MS)
    assert 'The heading is not the same' in str(info.value)
    _accept(str(info.value), store_file, 'news')
    tab.load(notifications('New', 'notif unread'))
    assert len(_open(tab, store_file).locator_all('news', '.unread', poll_ms=POLL_MS)) == 3
    rows = saved_candidates(store_file, 'news')
    assert [c['status'] for c in rows.values() if c.get('list')] == ['quarantined']
    store = json.loads(Path(store_file).read_text(encoding='utf-8'))
    plan = fixback.plan_fixes(store, [])
    assert [f.key for f in plan.fixes] == [] and 'news' not in dict(plan.skipped)


def test_every_task_completed_is_refused_by_its_state_word(tab, store_file):
    tab.load(todomvc('todo'))
    heal = _open(tab, store_file)
    assert len(heal.locator_all('tasks', 'li.todo', nonempty=True, poll_ms=POLL_MS)) == 4
    tab.load(todomvc('completed'))
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all('tasks', 'li.todo', nonempty=True, poll_ms=POLL_MS)
    assert "is picked by 'completed', and 'completed' names a state" in str(info.value)


@pytest.mark.engine_family('playwright')
def test_the_place_and_items_reads_are_fast_on_a_large_page(tab):
    if tab.engine_id != 'pw-chromium':
        pytest.skip('timed in Chromium only')
    filler = ''.join('<div class="f' + str(i % 50) + '"><span>' + str(i) + '</span></div>'
                     for i in range(4900))
    tab.load(page('<main>' + filler + '<section class="cart">'
                  + rows('cart-item', ITEMS * 67) + '</section></main>'))
    loc = tab.page.locator('.cart-item')
    assert tab.page.evaluate('document.querySelectorAll("*").length') >= 10000
    times = []
    for _ in range(5):
        started = time.perf_counter()
        assert loc.evaluate_all(lists.LIST_PLACE_JS)['count'] == 201
        times.append(time.perf_counter() - started)
    assert statistics.median(times) <= 0.150, times
    times = []
    for _ in range(5):
        started = time.perf_counter()
        assert len(loc.evaluate_all(lists.ITEMS_JS)) == 200
        times.append(time.perf_counter() - started)
    assert statistics.median(times) <= 0.020, times


# --- async Playwright ------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def chromium():
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        pytest.skip('playwright is not installed')
    loop = asyncio.new_event_loop()
    state = {}

    async def start():
        from playwright.async_api import async_playwright
        state['pw'] = await async_playwright().start()
        state['browser'] = await state['pw'].chromium.launch()
    try:
        loop.run_until_complete(start())
    except Exception as exc:
        loop.close()
        pytest.skip('chromium could not be launched: ' + str(exc).strip().splitlines()[0])
    yield loop, state['browser']

    async def stop():
        await state['browser'].close()
        await state['pw'].stop()
    loop.run_until_complete(stop())
    loop.close()


def test_async_an_emptied_cart_is_refused_and_a_renamed_one_proposed(chromium, tmp_path):
    loop, browser = chromium
    store_file = str(tmp_path / 'store' / 'overrides.json')

    async def main():
        context = await browser.new_context()
        tab = await context.new_page()
        await tab.set_content(cart())
        heal = await mendwright.aio.open(tab, provider=None, store_path=store_file,
                                         timeout_ms=FAST_MS)
        assert len(await heal.locator_all('cart', '.cart-item', nonempty=True,
                                          poll_ms=POLL_MS)) == 3
        await tab.set_content(EMPTIED)
        with pytest.raises(AttentionRequired) as emptied:
            for row in await heal.locator_all('cart', '.cart-item', nonempty=True,
                                              poll_ms=POLL_MS):
                await row.click()
        saved = await tab.locator('.saved-item').count()
        await tab.set_content(RENAMED)
        with pytest.raises(AttentionRequired) as renamed:
            await heal.locator_all('cart', '.cart-item', nonempty=True, poll_ms=POLL_MS)
        _accept(str(renamed.value), store_file)
        again = await mendwright.aio.open(tab, provider=None, store_path=store_file,
                                          timeout_ms=FAST_MS)
        with pytest.raises(AttentionRequired) as waits:
            await again.locator_all('cart', '.cart-item', nonempty=True, poll_ms=POLL_MS)
        rows = await again.locator_all('cart', 'div.cart-row', nonempty=True, poll_ms=POLL_MS)
        await context.close()
        return heal, str(emptied.value), saved, str(waits.value), len(rows)
    heal, emptied, saved, waits, found = loop.run_until_complete(main())
    assert 'on the page is another list' in emptied and saved == 5
    assert 'You accepted a list heal for it on' in waits and found == 3
    assert heal.heals == []
