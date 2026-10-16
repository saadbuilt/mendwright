"""The list search mendwright runs for heal.locator_all(..., nonempty=True) (lists.find, restored
from 82f1346), the place and label it records, the state-word refusal, and what a proposal
shows a person (lists.proposal_evidence, proposal_id). Offline: lxml pages, no browser, no
model."""

import lxml.html
import pytest

from mendwright import lists

from test_lists import CARDS, PRODUCTS, TILE, V1, V2, card, grid, shop

NEW_PRODUCTS = [('Lamp', '$40.00'), ('Chair', '$90.00'), ('Desk', '$250.00'), ('Sofa', '$900.00')]


def _lfp(html=V1, xpath=CARDS, place=True):
    return lists.of_elements(lxml.html.fromstring(html).xpath(xpath), place=place)


# --- what is recorded -------------------------------------------------------------------------

def test_a_list_recorded_without_place_is_what_0_8_0_recorded():
    els = lxml.html.fromstring(V1).xpath(CARDS)
    raw = lists.raw_of_elements(els)
    assert set(raw) == {'count', 'depth', 'items', 'container'}
    lfp = lists.of_elements(els)
    assert 'label' not in lfp and 'place' not in lfp and not lists.placed(lfp)


def test_a_list_recorded_with_place_keeps_its_label_and_where_it_was():
    lfp = _lfp()
    assert lists.valid(lfp) and lists.placed(lfp)
    assert lfp['label'] == 'Shop'
    place = lfp['place']
    assert place['cut'] is False and place['chain'] and place['others']
    assert all(len(h) == 8 and int(h, 16) >= 0 for h in place['chain'] + place['others']
               + place['labels'])
    assert lists.from_raw(lists.raw_of_elements(lxml.html.fromstring(V1).xpath(CARDS),
                                                place=True)) == lfp
    # 0.8.0's lists.valid() checks only v, count and items, so it keeps both fields.
    assert lists.valid(dict(lfp, label='x', place={'chain': []}))


def test_the_items_twin_reads_role_type_and_visibility():
    doc = lxml.html.fromstring("<html><body><ul><li role='option'>a</li><li style='display: "
                               "none'>b</li><li><input type='checkbox'></li></ul><div hidden><b>c"
                               "</b></div></body></html>")
    rows = lists.items_of_elements(doc.xpath('//li') + doc.xpath('//b'))
    assert rows == [{'tag': 'li', 'role': 'option', 'type': '', 'visible': True},
                    {'tag': 'li', 'role': '', 'type': '', 'visible': False},
                    {'tag': 'li', 'role': '', 'type': '', 'visible': True},
                    {'tag': 'b', 'role': '', 'type': '', 'visible': False}]
    assert len(lists.items_of_elements(doc.xpath('//li') * 100)) == lists.ITEMS == 200


# --- finding the list ---------------------------------------------------------------------------

def test_a_grid_whose_card_class_changed_is_found():
    match = lists.find(_lfp(), V2, 'div')
    assert match.xpath == TILE and match.css == 'div.product-tile'
    assert match.count == 4 and match.score > 0.9 and match.reason == '' and match.code == ''
    assert (match.held, match.recorded, match.label, match.alike) == (3, 3, 'Shop', [1, 1])
    assert match.container.get('id') == 'grid'


def test_a_list_fingerprint_without_place_finds_nothing():
    match = lists.find(_lfp(place=False), V2, 'div')
    assert match.xpath is None and match.code == 'L3' and match.emptied
    assert lists.find({'v': 1}, V2, 'div').code == 'L2'


def test_new_products_in_the_same_container_are_not_the_list():
    # Where a group sits says nothing about what its items are: none of them is a product
    # the list had.
    match = lists.find(_lfp(), shop(grid('product-tile', NEW_PRODUCTS)), 'div')
    assert match.xpath is None and match.score >= lists.THRESHOLD and match.code == 'L18c'
    assert match.reason == ('the best group (score ' + format(match.score, '.2f') + ') shares '
                            'no id, test id, name, link, label or text with the items that '
                            'were recorded')
    assert match.fragment == ('the best group shares no id, test id, name, link, label or text '
                              "with the items it recorded, so a list whose items change from "
                              "run to run can't be found")


def _form(errors, cls='error'):
    """A signup form: two fields, each with a hint and, with errors, an error."""
    fields = ''
    for name, hint, error in zip(('email', 'password'), HINTS, errors or (None, None), strict=True):
        fields += ("<div class='field'><label for='" + name + "'>" + name.title() + "</label>"
                   "<input id='" + name + "' name='" + name + "'><span class='hint'>" + hint
                   + '</span>' + ("<span class='" + cls + "'>" + error + '</span>' if error else '')
                   + '</div>')
    return "<html><body><form id='signup'>" + fields + '<button>Sign up</button></form></body></html>'


HINTS = ('We never share your email', '8 characters or more')
ERRORS = ('Email is required', 'Password is required')


def test_an_emptied_error_list_is_not_found_in_the_hints_next_to_it():
    lfp = _lfp(_form(ERRORS), "//span[@class='error']")
    match = lists.find(lfp, _form(None), 'span')
    assert match.xpath is None and match.score >= lists.THRESHOLD   # alike, and in the form
    assert 'shares no id, test id, name, link, label or text' in match.reason
    # The same errors under a new class name are the list: their text says so.
    match = lists.find(lfp, _form(ERRORS, cls='field-error'), 'span')
    assert match.xpath == "//span[contains(concat(' ', normalize-space(@class), ' '), ' field-error ')]"
    assert match.best.shape is None                 # two empty shapes say nothing
    assert lists.shape_similarity({}, {}) is None and lists.shape_similarity({'b': 1}, {}) == 0.0


def _results(cls, names, attrs=''):
    cards = ''.join("<div class='" + cls + "'" + attrs + "><img src='/i/x.png' alt='" + n
                    + "'><h3>" + n + "</h3><span class='price'>$9.00</span></div>" for n in names)
    return ("<html><body><main id='search'><h1>Search</h1><div id='results'>" + cards
            + '</div></main></body></html>')


MUGS = ['Blue mug', 'Red mug', 'Green mug', 'Mug set']
POPULAR = ['Tea pot', 'Kettle', 'Coffee grinder', 'French press']


def test_a_search_with_no_results_is_not_found_in_its_recommendations():
    lfp = _lfp(_results('result-card', MUGS), "//div[@class='result-card']")
    match = lists.find(lfp, _results('popular-card', POPULAR), 'div')
    assert match.xpath is None and match.code == 'L18c'
    # One recorded item among them is not enough: the list held three that were recorded.
    match = lists.find(lfp, _results('popular-card', ['Blue mug'] + POPULAR), 'div')
    assert match.xpath is None and match.code == 'L18a' and match.held == 1
    assert match.fragment == ("the best group (5 elements under 'Search') holds only 1 of the 3 "
                              'items it recorded, and 2 are needed, so it may be another list in '
                              'the same place')
    # What every recorded item shares, such as one test id on each card, is not which items.
    tested = " data-testid='card'"
    lfp = _lfp(_results('result-card', MUGS, tested), "//div[@class='result-card']")
    match = lists.find(lfp, _results('popular-card', POPULAR, tested), 'div')
    assert match.xpath is None and match.code == 'L18b'
    assert match.fragment.startswith('the best group shares an id, test id, name, link, label or '
                                     'text only with every recorded item at once')
    assert lists.find(lfp, _results('popular-card', MUGS, tested), 'div').reason == ''


def test_a_long_list_whose_class_changed_is_found():
    # The recorded items are the first, the middle and the last of 450: the last two are
    # far past the first members a group is looked through for.
    many = [('Mug ' + str(i), '$' + str(i) + '.00') for i in range(450)]
    lfp = _lfp(shop(grid('product-card', many)))
    match = lists.find(lfp, shop(grid('product-tile', many)), 'div')
    assert match.xpath == TILE and match.count == 450 and match.reason == ''


def _skeleton():
    return ("<div class='card-skeleton'><img src='/i/blank.png'><h3></h3><span></span>"
            "<button type='button' disabled></button></div>")


def test_the_placeholders_of_a_list_that_is_loading_are_no_list():
    match = lists.find(_lfp(), shop(_skeleton() * 4), 'div')
    assert match.xpath is None and match.code == 'L12'
    assert match.fragment == ('the only groups of div elements look like placeholders (no text '
                              'where the recorded items had text, or marked aria-busy="true"), '
                              'so the list may still be loading')
    busy = shop(grid('product-tile'), grid="id='grid' class='grid' aria-busy='true'")
    assert lists.find(_lfp(), busy, 'div').code == 'L12'
    # Placeholders after the cards, as an infinite scroll loads more, are no rival.
    more = shop(grid('product-tile') + _skeleton() * 4)
    assert lists.find(_lfp(), more, 'div').xpath == TILE


def test_a_look_alike_list_outside_the_recorded_container_is_no_rival():
    recent = ("<section id='recent'><h2>Recently viewed</h2>" + grid('product-tile', PRODUCTS[:3])
              + '</section>')
    match = lists.find(_lfp(), shop(grid('product-tile'), extra=recent), 'div')
    assert match.xpath == ("//div[@id='grid']/div[contains(concat(' ', normalize-space(@class), "
                           "' '), ' product-tile ')]")
    assert match.css == 'div#grid > div.product-tile'
    recent = "<div id='recent' class='grid'>" + grid('product-tile') + '</div>'
    match = lists.find(_lfp(), shop(grid('product-tile'), extra=recent), 'div')
    assert match.reason == '' and match.count == 4 and match.runner_up == 0.0
    assert match.xpath.startswith("//div[@id='grid']/")


def test_one_element_is_never_taken_for_a_list():
    one = shop(card('product-tile', *PRODUCTS[0]) + "<div class='banner'><p>Sale!</p></div>")
    match = lists.find(_lfp(), one, 'div')
    assert match.xpath is None and match.code == 'L17'
    assert ("The only element like the recorded items is one <div> ('Blue mug$12.00Add to "
            "cart'); mendwright never takes one element for a list") in match.reason
    alone = ("<html><body><main id='content'><section>" + card('product-tile', *PRODUCTS[0])
             + "</section></main></body></html>")
    match = lists.find(_lfp(), alone, 'div')
    assert match.xpath is None and match.code == 'L13'
    assert match.fragment == ("the page has no group of 2 or more div elements that repeat the "
                              "same structure. The only element like the recorded items is one "
                              "<div> ('Blue mug$12.00Add to cart'); mendwright never takes one "
                              "element for a list")


def test_a_hidden_copy_is_neither_the_answer_nor_a_rival():
    page = shop(grid('product-tile'), extra="<div id='m' class='grid'>" + grid('product-tile')
                + '</div>', grid="id='d' class='grid'")
    lfp = _lfp(shop(grid('product-card'), grid="class='grid'"))
    match = lists.find(lfp, page, 'div')
    assert match.code == 'L16' and match.runner is not None
    assert match.fragment.startswith('two groups score alike (')
    assert "for 4 elements under 'Shop', " in match.fragment
    hidden = "//div[@id='m']/div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]"
    match = lists.find(lfp, page, 'div', visible=lambda xpath: xpath != hidden)
    assert match.reason == '' and match.count == 4 and match.xpath != hidden
    assert lists.find(lfp, page, 'div', visible=lambda xpath: False).code == 'L14'
    assert lists.find(lfp, page, 'div', visible=lambda xpath: None).code == 'L16'


def test_items_wrapped_one_level_down_and_links_are_found():
    wrapped = ''.join("<div class='col'>" + card('product-tile', n, p) + '</div>' for n, p in PRODUCTS)
    match = lists.find(_lfp(), shop(wrapped), 'div')
    assert match.xpath == TILE and match.count == 4
    v1 = ("<html><body><ul id='menu'><li><a class='m' href='/a'>A</a></li><li><a class='m' "
          "href='/b'>B</a></li><li><a class='m' href='/c'>C</a></li></ul></body></html>")
    lfp = _lfp(v1, "//a[@class='m']")
    assert lists.item_role(lfp) == 'link'
    match = lists.find(lfp, v1.replace("class='m'", "class='menu-link'"), 'link')
    assert match.xpath == "//a[contains(concat(' ', normalize-space(@class), ' '), ' menu-link ')]"


def test_a_page_that_cannot_be_parsed_finds_nothing():
    assert lists.find(_lfp(), '', 'div').code == 'L10'


# --- the static check of the list gate --------------------------------------------------------

def test_the_static_gate():
    assert lists.validate(TILE, V2, 'div').ok
    assert lists.validate(TILE, V2, 'div').count == 4
    assert lists.validate("//div[@id='grid']/div[2]", V2, 'div').reason == lists.POSITIONAL
    assert lists.validate('/html/body[1]/main/div/div', V2, 'div').reason == (
        'brittle absolute-index path')
    assert lists.validate('//h1', V2, 'h1').reason == ('it matches 1 element, and a list heal '
                                                      'needs 2 or more')
    assert lists.validate(TILE, V2, 'article').reason == 'role mismatch: got div want article'
    assert lists.validate('//div', V2, 'div').reason == 'its elements are inside one another'
    assert lists.validate('//p', V2, 'p').reason == 'no match'
    assert lists.validate('//div[', V2, 'div').reason.startswith('xpath error: ')


# --- state words ------------------------------------------------------------------------------

def test_a_state_word_the_list_did_not_have_is_found_in_any_literal():
    lfp = _lfp()
    saved = "//div[contains(concat(' ', normalize-space(@class), ' '), ' saved-item ')]"
    assert lists.state_word(saved, lfp) == ('saved-item', 'saved')
    assert lists.state_word('//li[@id="done-list"]/li', lfp) == ('done-list', 'done')
    assert lists.state_word(TILE, lfp) is None
    assert lists.state_word("//div[text()='Reader']", lfp) is None      # 'reader' is no state
    assert len(lists.STATE_WORDS) == 20


def test_a_state_word_the_list_had_is_no_refusal():
    done = ("<html><body><h2>Completed</h2><ul class='tasks'><li class='done'>a</li>"
            "<li class='done'>b</li></ul></body></html>")
    lfp = _lfp(done, "//li[@class='done']")
    xpath = "//li[contains(concat(' ', normalize-space(@class), ' '), ' done-task ')]"
    assert lists.state_word(xpath, lfp) is None                       # its items were 'done'
    assert lists.state_word("//li[@class='completed']", lfp) is None  # its heading said it
    assert lists.state_word("//li[@class='archived']", lfp) == ('archived', 'archived')


# --- the evidence and its id -----------------------------------------------------------------------

def _evidence(after=V2, page='/shop', broken=(CARDS,)):
    lfp = _lfp()
    match = lists.find(lfp, after, 'div')
    return lists.proposal_evidence('cards', lfp, match, match.xpath, match.css, list(broken),
                                   page, '2026-11-02T10:14:03+00:00')


def test_the_evidence_says_where_the_list_was_and_where_the_group_is():
    evidence = _evidence()
    assert evidence == {
        'id': evidence['id'], 'at': '2026-11-02T10:14:03+00:00', 'by': 'fingerprint',
        'page': '/shop', 'count': 4, 'found': 3, 'recorded': 3,
        'score': evidence['score'], 'next': 0.0,
        'was': {'selectors': [CARDS], 'count': 4, 'label': 'Shop',
                'container': '<div id="grid" class="grid">',
                'items': ['Blue mug$12.00Add to cart', 'Red mug$13.00Add to cart']},
        'now': {'label': 'Shop', 'container': '<div id="grid" class="grid">', 'alike': [1, 1],
                'items': ['Blue mug$12.00Add to cart', 'Red mug$13.00Add to cart']},
        'differs': []}
    assert len(evidence['id']) == 8 and int(evidence['id'], 16) >= 0
    assert evidence['id'] == _evidence()['id']                         # a fixed page, a fixed id


def test_the_id_changes_with_what_a_person_sees():
    first = _evidence()
    assert _evidence(page='/other')['id'] != first['id']
    assert _evidence(broken=('.product-card',))['id'] != first['id']
    more = shop(grid('product-tile', PRODUCTS + [('Fork', '$1')]))
    assert _evidence(more)['id'] != first['id']
    later = dict(first, at='2027-01-01T00:00:00+00:00', score=0.1)    # time and scores: no
    assert lists.proposal_id('cards', TILE, 'div.product-tile', later) == first['id']


def test_what_is_not_the_same_is_said():
    renamed = V2.replace('<h1>Shop</h1>', '<h1>Catalog</h1>').replace(
        "id='grid' class='grid'", "id='grid' class='catalog'")
    evidence = _evidence(renamed)
    assert evidence['differs'] == ['heading', 'container']
    assert evidence['now']['label'] == 'Catalog'
    assert evidence['now']['container'] == '<div id="grid" class="catalog">'
    lfp = _lfp(V1.replace('<h1>Shop</h1>', '<h1>Shop (4)</h1>'))
    match = lists.find(lfp, V2.replace('<h1>Shop</h1>', '<h1>SHOP (5)</h1>'), 'div')
    evidence = lists.proposal_evidence('cards', lfp, match, match.xpath, match.css, [CARDS],
                                       '/shop', 'now')
    assert evidence['differs'] == []                             # digits and case left out


@pytest.mark.parametrize('heading', ['Shop\x1b[31m', 'Shop\x9b2J'])
def test_control_characters_from_the_page_are_made_printable(heading):
    # lxml drops some of them from a page; a browser's LIST_PLACE_JS reads them all.
    raw = lists.raw_of_elements(lxml.html.fromstring(V1).xpath(CARDS), place=True)
    lfp = lists.from_raw(dict(raw, label=heading))
    match = lists.find(lfp, V2, 'div')
    evidence = lists.proposal_evidence('cards', lfp, match, match.xpath, match.css, [CARDS],
                                       '/shop', 'now')
    assert evidence['was']['label'].startswith('Shop?')
    assert '\x1b' not in str(evidence) and '\x9b' not in str(evidence)
    assert lists._printable('a\x00b\x7fc\x85d') == 'a?b?c?d'
    assert lists.shown_container({'tag': 'ul', 'id': 'a"b', 'classes': ['x', 'y']}) == (
        '<ul id="a\'b" class="x y">')
    assert lists.shown_container(None) == ''
