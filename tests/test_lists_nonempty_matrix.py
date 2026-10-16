"""heal.locator_all(..., nonempty=True) over the acceptance matrix of test_lists_emptied.py: 53
emptied lists and 33 redesigns. Whatever mendwright finds, heal.locator_all() gives only what
the key's own selectors match: every case raises, returns no element, saves no selector a
lookup tries, asks no model, and raises again on a fresh open() of the same store.

The outcome table is a baseline, not a promise about other sites: it may change only with
the table updated in the same change and the reason in its message. What must hold whatever
changes: no redesign is proposed onto the other list, at least 21 redesigns are proposed,
and every emptied list that is proposed says that its heading or container is not the same.

Without nonempty=True, a store with list heals in it changes nothing a lookup gives, raises,
logs or journals (see also test_lists_emptied.py, which passes as it was in 0.8.0).

Offline: conftest's FakePage, WrongPicker (a stub model that takes the other list), no
browser."""

import asyncio
import json
import logging
from pathlib import Path

import lxml.html
import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired
from mendwright.coordinator import _list_missing_message

from test_lists import _Page
from test_lists_emptied import REDESIGN, REFUSE, WrongPicker, tasks, todomvc

# The outcome of every case with nonempty=True: 'proposed', or the refusal's code (see the
# guide's table; L11 every group is another list, L15 a low score, L18c no identity, L20 a
# state word).
REFUSE_PROPOSED = {'E3-notifications-marked-read', 'E4-no-results-recommends-the-same-products',
                   'E5b-last-page-empty-no-heading', 'E6-bag-drawer-gone-items-in-a-modal',
                   'E7-cart-moved-to-a-new-wishlist'}
REFUSE_STATE = {'todomvc-every-task-completed', 'todo-class-swapped-for-done',
                'E1-cart-heading-changes-saved-shown-once-it-has-items',
                'E1c-cart-section-replaced-by-an-empty-state',
                'E2-active-section-only-while-it-has-tasks', 'E2b-sections-of-their-own-classes',
                'E10-no-headings-saved-shown-once-it-has-items'}
REDESIGN_REFUSED = {'under-a-new-heading-old-one-stays', 'R1-list-gains-an-aria-label',
                    'R1b-wrapped-in-a-labelled-section', 'R2-a-filter-sidebar-added',
                    'R3-subheading-dropped', 'R3b-heading-in-a-toolbar',
                    'R4-grid-wrapped-in-a-bare-div', 'R4b-styled-components-wrapper',
                    'R5-renamed-in-place-footer-alike', 'R5b-bootstrap-row-no-heading',
                    'R6-bootstrap-grid-moved-into-a-row'}


def _refuse_outcome(case):
    if case in REFUSE_PROPOSED:
        return 'proposed'
    if case in REFUSE_STATE:
        return 'L20'
    return 'L18c' if case == 'E8-one-tab-panel-reused-for-archive' else 'L11'


def _redesign_outcome(case):
    if case in REDESIGN_REFUSED:
        return 'L11'
    return 'L15' if case == 'R7-grid-moved-into-a-tab-panel' else 'proposed'


# (message part, code) in the order they are told apart.
_CODES = (
    ('Accept it with: mendwright lists accept', 'proposed'),
    ('You accepted a list heal for it on', 'L23'),
    ('but each group of ', 'L11'),
    ('names a state the recorded list', 'L20'),
    ("failed mendwright's list checks", 'L21'),
    ('look like placeholders', 'L12'),
    ('that repeat the same structure', 'L13'),
    ('elements is visible on the live page', 'L14'),
    ('two groups score alike', 'L16'),
    ('not all shaped like the recorded items', 'L17'),
    (') holds only ', 'L18a'),
    ('only with every recorded item at once', 'L18b'),
    ('with the items it recorded, so a list whose items change', 'L18c'),
    ('no selector without positions', 'L19'),
    (' needed', 'L15'),
)


def outcome_of(message):
    """The outcome a message of a lookup with nonempty=True says (see _CODES)."""
    return next((code for part, code in _CODES if part in message), 'other')


def cases(table, outcome):
    """pytest params (case, before, after, key, wanted, other, outcome) for a matrix."""
    out = []
    for param in table:
        values = param.values
        if len(values) == 4:                   # REFUSE: before, after, key, other
            before, after, key, other = values
            wanted = None
        else:
            before, after, key, wanted, other = values
        out.append(pytest.param(param.id, before, after, key, wanted, other, outcome(param.id),
                                id=param.id))
    return out


MATRIX = cases(REFUSE, _refuse_outcome) + cases(REDESIGN, _redesign_outcome)


def _open(page, root, provider=None, **kwargs):
    kwargs.setdefault('timeout_ms', 0)
    return mendwright.open(page, provider=provider, store_path=str(root / 'o.json'), **kwargs)


def _journal(root):
    path = root / 'journal.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _rows(root, key):
    path = root / 'o.json'
    if not path.exists():
        return []
    entry = json.loads(path.read_text(encoding='utf-8'))['selectors'].get(key) or {}
    return entry.get('candidates') or []


def _look(make_page, root, before, after, key, other, provider):
    """Record the list on before with nonempty=True, then look it up on after: it must raise,
    with no heal and no model asked. Returns (heal, page, model, message)."""
    page = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
    model = WrongPicker(page, other) if provider else None
    heal = _open(page, root, model)
    assert len(heal.locator_all(key, key, nonempty=True)) >= 3
    page.set_html(after)
    with pytest.raises(AttentionRequired) as info:
        heal.locator_all(key, key, nonempty=True)
    assert heal.heals == [] and heal.pending() == {}
    return heal, page, model, str(info.value)


@pytest.mark.parametrize('provider', [None, 'wrong-picker'])
@pytest.mark.parametrize('case, before, after, key, wanted, other, outcome', MATRIX)
def test_locator_all_gives_only_what_its_own_selectors_match(tmp_path, make_page, provider, case,
                                                             before, after, key, wanted, other,
                                                             outcome):
    doc = lxml.html.fromstring(after)
    target = other if other and doc.xpath(other) else wanted
    heal, page, model, message = _look(make_page, tmp_path, before, after, key, target, provider)
    assert outcome_of(message) == outcome, message
    assert not [e for e in _journal(tmp_path) if e['event'] == 'heal.applied']
    assert [c['xpath'] for c in _rows(tmp_path, key) if c.get('status', 'active') == 'active'] == [
        key]                                             # no active row but the code's
    if model is not None:
        assert model.bundles == []                       # no model is asked
    proposed = [c for c in _rows(tmp_path, key) if c.get('status') == 'proposed']
    if outcome == 'proposed':
        assert len(proposed) == 1 and heal.coordinator.held[-1]['xpath'] == proposed[0]['xpath']
        found = set(doc.xpath(proposed[0]['xpath']))
        if wanted:                                       # a redesign: exactly the list
            assert found == set(doc.xpath(wanted))
        else:                                            # an emptied list: it says so
            assert 'heading' in proposed[0]['proposal']['differs']
            assert 'The heading is not the same' in message
    else:
        assert proposed == [] and heal.coordinator.held == []
    # The next run, on the same store, raises again: nothing it found is used.
    again = make_page(after, url='https://shop.test/x', fingerprints=True, css=True)
    with pytest.raises(AttentionRequired) as info:
        _open(again, tmp_path, model).locator_all(key, key, nonempty=True)
    assert outcome_of(str(info.value)) == outcome


def test_the_outcome_table_holds_what_must_hold():
    redesigns = [p.values[-1] for p in MATRIX if p.values[4] is not None]
    emptied = [p.values[-1] for p in MATRIX if p.values[4] is None]
    assert (len(emptied), len(redesigns)) == (53, 33)
    assert redesigns.count('proposed') >= 21
    assert emptied.count('proposed') == 5 and emptied.count('L20') == 7
    assert emptied.count('L11') == 40 and emptied.count('L18c') == 1
    assert redesigns.count('L11') == 11 and redesigns.count('L15') == 1


ASYNC_CASES = [p for p in MATRIX if p.values[4] is None][:10] + [
    p for p in MATRIX if p.values[4] is not None][:10]


@pytest.mark.parametrize('case, before, after, key, wanted, other, outcome', ASYNC_CASES)
def test_async_locator_all_gives_only_what_its_own_selectors_match(tmp_path, make_page, case,
                                                                   before, after, key, wanted,
                                                                   other, outcome):
    async def main():
        sync = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
        model = WrongPicker(sync, other or wanted)
        heal = await mendwright.aio.open(_Page(sync), provider=model,
                                         store_path=str(tmp_path / 'o.json'), timeout_ms=0)
        assert len(await heal.locator_all(key, key, nonempty=True)) >= 3
        sync.set_html(after)
        with pytest.raises(AttentionRequired) as info:
            await heal.locator_all(key, key, nonempty=True)
        return heal, model, str(info.value)
    heal, model, message = asyncio.run(main())
    assert outcome_of(message) == outcome, message
    assert heal.heals == [] and model.bundles == []
    assert [c['xpath'] for c in _rows(tmp_path, key) if c.get('status', 'active') == 'active'] == [
        key]


@pytest.mark.parametrize('before, emptied, redesigned, swaps, word, proposed', [
    pytest.param(todomvc('todo'), todomvc('done'), todomvc('task'), [('done', 'task')], 'done',
                 'li.task', id='todo-class-swapped-for-done-or-renamed-to-task'),
    pytest.param(tasks('Active', 'todo'), tasks('Completed', 'done-item'), tasks('To do', 'task'),
                 [('done-item', 'task'), ('Completed', 'To do')], 'done-item', 'li.task',
                 id='active-tasks-all-completed-or-section-redesigned'),
])
def test_an_emptied_list_and_its_redesign_twin_are_told_apart_by_a_state_word(
        tmp_path, make_page, before, emptied, redesigned, swaps, word, proposed):
    _, _, _, message = _look(make_page, tmp_path / 'emptied', before, emptied, 'li.todo',
                             "//li[@class='task']", None)
    assert outcome_of(message) == 'L20' and "is picked by '" + word + "'" in message
    heal, _, _, message = _look(make_page, tmp_path / 'redesign', before, redesigned, 'li.todo',
                                "//li[@class='task']", None)
    assert outcome_of(message) == 'proposed' and '(as CSS: ' + proposed + ')' in message


# --- without nonempty=True, nothing changes -------------------------------------------------------

def _with_list_heals(store, key):
    """Give key a proposed, an accepted and a quarantined list heal, none of them replacing
    key's own selector."""
    for i, status in enumerate(('proposed', 'accepted', 'quarantined')):
        xpath = "//*[@data-list-heal='" + str(i) + "']"
        store.propose_list_heal(key, xpath, proposal={
            'id': '0000000' + str(i), 'at': '2026-11-02T10:14:0' + str(i) + '+00:00', 'count': 3,
            'was': {'selectors': ['.another-page']}, 'now': {}, 'differs': []})
        if status != 'proposed':
            store._find(key, xpath)['status'] = status
    store.set_meta(key, role=store.meta(key)['expectedRole'])
    store._persist_override()


@pytest.mark.parametrize('case, before, after, key, wanted, other, outcome', MATRIX)
def test_a_store_with_list_heals_changes_nothing_without_nonempty(tmp_path, make_page, caplog,
                                                                  case, before, after, key,
                                                                  wanted, other, outcome):
    page = make_page(before, url='https://shop.test/x', fingerprints=True, css=True)
    heal = _open(page, tmp_path)
    assert len(heal.locator_all(key, key, nonempty=True)) >= 3         # a placed fingerprint
    _with_list_heals(heal.store, key)
    count = heal.store.meta(key)['listFingerprint']['count']
    page.set_html(after)
    before_file = (tmp_path / 'o.json').read_bytes()
    events = len(_journal(tmp_path))
    reads = page.items_reads
    with caplog.at_level(logging.INFO, logger='mendwright'):
        for run in (heal, _open(page, tmp_path)):
            with pytest.raises(AttentionRequired) as info:
                run.locator_all(key, key)
            assert str(info.value) == ('No matching element for: ' + key + ' (tried 1 '
                                       'candidate(s)).\n  -> ' + _list_missing_message(key, count))
    assert (tmp_path / 'o.json').read_bytes() == before_file
    assert len(_journal(tmp_path)) == events and page.items_reads == reads
    logged = [r.getMessage() for r in caplog.records if r.name == 'mendwright'
              and not r.getMessage().startswith('mendwright.open()')]
    assert len(logged) == 2 and all(line.startswith(
        "mendwright: did not heal '" + key + "': '" + key + "' matched ") for line in logged)


# --- a store 0.8.0 wrote, and the reports of it ----------------------------------------------------

GOLDEN = Path(__file__).resolve().parent / 'fixtures' / 'lists_080'
MENU = "//a[@class='m']"
GOLDEN_V1 = ("<html><body><main id='content'><h1>Shop</h1><div id='grid' class='grid'>"
             + ''.join("<div class='product-card'><h3>" + n + "</h3><span class='price'>" + p
                       + '</span></div>' for n, p in (('Blue mug', '$12'), ('Red mug', '$13'),
                                                      ('Tea pot', '$29')))
             + "</div><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' "
             "href='/b'>B</a></li></ul></main></body></html>")
GOLDEN_V2 = GOLDEN_V1.replace('product-card', 'product-tile')
GOLDEN_CARDS = "//div[@class='product-card']"
GOLDEN_TILE = "//div[@class='product-tile']"


def scenario_080(mw, FakePage, Journal, load_store, root):
    """Default lookups on a store with no 0.9.0 list data, as 0.8.0 ran them: what 0.8.0
    wrote for them is tests/fixtures/lists_080/overrides.json (made by running this with
    0.8.0's code, mendwright.store._now_iso fixed). Returns the store file's path."""
    class Quiet:
        def propose(self, bundle):
            return None

        def report_outcome(self, key, xpath, success):
            pass

    def stamp():
        return '2026-11-02T10:00:00+00:00'
    path = root / 'o.json'
    journal = Journal(root / 'journal.jsonl', clock=stamp)
    page = FakePage(GOLDEN_V1, url='https://shop.example/shop', fingerprints=True)
    heal = mw.open(page, provider=Quiet(), store=load_store(None, str(path)), journal=journal,
                   timeout_ms=0)
    heal.locator_all('cards', GOLDEN_CARDS)
    heal.locator_all('menu', MENU, role='link')
    heal.store.add_candidate('cards', GOLDEN_TILE, list_heal=True)     # a pre-release list heal
    heal.locator_all('cards', GOLDEN_CARDS)
    page.set_html(GOLDEN_V2)
    for _ in range(2):
        try:
            heal.locator_all('cards', GOLDEN_CARDS)
        except mw.AttentionRequired:
            pass
    page.set_html(GOLDEN_V1)
    again = mw.open(page, provider=Quiet(), store=load_store(None, str(path)), journal=journal,
                    timeout_ms=0)
    again.locator_all('cards', GOLDEN_CARDS)
    again.store.quarantine('cards', GOLDEN_TILE)
    again.locator_all('cards', [GOLDEN_CARDS, "//div[@class='card']"])
    again.locator_all('menu', MENU, role='link', optional=True)
    return path


def _keys_in_order(data):
    """A store file's bytes with its keys in name order: the store writes them in the
    order of a set (store.merge), which changes from one process to the next, in 0.8.0 as
    now. Everything inside each key keeps its bytes."""
    parsed = json.loads(data)
    parsed['selectors'] = dict(sorted(parsed['selectors'].items()))
    return json.dumps(parsed, separators=(',', ':')).encode('utf-8')


def test_a_0_8_0_store_is_written_byte_for_byte_as_0_8_0_wrote_it(tmp_path, make_page,
                                                                monkeypatch):
    from mendwright.journal import Journal
    from mendwright.store import load_store
    monkeypatch.setattr('mendwright.store._now_iso', lambda: '2026-11-02T10:00:00+00:00')
    path = scenario_080(mendwright, make_page, Journal, load_store, tmp_path)
    assert _keys_in_order(path.read_bytes()) == _keys_in_order(
        (GOLDEN / 'overrides.json').read_bytes())
    assert not (tmp_path / 'journal.jsonl').exists()             # 0.8.0 journaled nothing


def report_080(cli, write_store, root, fmt):
    """mendwright report of test_cli.py's store (no list heal in it) in format fmt, with the
    store's folder and mendwright's version as placeholders: what 0.8.0 printed for it is in
    tests/fixtures/lists_080 (report.txt, .md, .html and .json)."""
    import contextlib
    import io
    import re
    store = write_store(root / 's')
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(['report', '--store', str(store), '--format', fmt]) == 0
    text = out.getvalue().replace(json.dumps(str(root / 's'))[1:-1], '<FOLDER>')
    text = text.replace(str(root / 's'), '<FOLDER>')
    return re.sub(r'Made by mendwright [^<]*<', 'Made by mendwright <VERSION><', text)


@pytest.mark.parametrize('fmt', ['text', 'md', 'html', 'json'])
def test_report_is_unchanged_for_a_store_without_list_rows(tmp_path, fmt):
    from mendwright import cli
    from test_cli import _write_store
    got = report_080(cli, _write_store, tmp_path, fmt)
    want = (GOLDEN / ('report.' + {'text': 'txt'}.get(fmt, fmt))).read_text(encoding='utf-8')
    if fmt != 'json':
        assert got == want
        return
    got, want = json.loads(got), json.loads(want)
    assert got['journal']['counts'].pop('held') == 0                  # the one new field
    for counts in got['journal']['per_key'].values():
        assert counts.pop('held') == 0
    assert got == want
