"""Fitting a long page into the prompt (mendwright.fit): the element the step needs survives
even at the end of a long page, nothing is cut mid-row, and once elements are left out a
positional XPath is refused, because positions in what the model read no longer match."""

import json

import lxml.html

from mendwright import HealCoordinator
from mendwright.client import POSITIONAL, apply
from mendwright.fit import BUDGET, NOTE, fit_bundle, fit_html, fit_inventory, has_position
from mendwright.prompt import build_prompt


def _posts(n):
    return ''.join("<article><h3>Post " + str(i) + "</h3><p>" + 'Lorem ipsum dolor sit amet. ' * 12
                   + "</p><button type='button'>Share</button></article>" for i in range(n))


# A long blog page whose checkout form comes last: without fitting, the prompt's cut at
# 60,000 characters dropped it.
LONG = ("<html><head><title>Shop</title></head><body><main>" + _posts(400) + "</main>"
        "<form id='checkout'><label for='c'>Coupon code</label><input id='c' name='coupon'>"
        "<button type='submit' class='go'>Apply coupon</button></form></body></html>")
META = {'expectedRole': 'button', 'expectedText': ['Apply coupon'], 'destructive': False}


def _bundle(html=LONG, **extra):
    return dict({'key': 'apply_coupon', 'expectedRole': 'button', 'expectedText': ['Apply coupon'],
                 'candidates': ["//button[@id='apply-coupon']"], 'inventory': [], 'html': html},
                **extra)


def test_a_page_that_fits_is_left_alone():
    small = _bundle('<form><button>Apply coupon</button></form>')
    assert fit_bundle(small) == (small, 0)
    assert fit_bundle(small)[0] is small


def test_the_element_at_the_end_of_a_long_page_reaches_the_prompt():
    assert len(LONG) > BUDGET
    fitted, left_out = fit_bundle(_bundle())
    html = fitted['html']
    assert left_out > 0 and len(html) <= BUDGET
    assert html.startswith(NOTE)
    assert 'Apply coupon' in html and 'Coupon code' in html
    assert 'left out here' in html
    # The prompt no longer has to cut anything off.
    assert 'Coupon code' in build_prompt(fitted)['user']
    assert 'Coupon code' not in build_prompt(_bundle())['user']


def test_unrelated_text_goes_first_and_the_structure_stays():
    html, left_out = fit_html(LONG, {'apply', 'coupon'}, 'button')
    doc = lxml.html.fromstring(html[len(NOTE):])
    assert left_out > 0
    assert len(doc.xpath('//article')) == 400          # only what was inside them went
    assert len(doc.xpath('//button')) == 401
    assert doc.xpath("//form[@id='checkout']//input[@name='coupon']")


def test_neighbours_left_out_share_one_marker():
    page = ('<div>' + ''.join('<p>' + 'filler text ' * 30 + '</p>' for _ in range(600))
            + '<button>Apply coupon</button></div>')
    html, left_out = fit_html(page, {'apply', 'coupon'}, 'button')
    assert left_out > 0 and html.count('left out here') == 1


def test_only_long_text_shortened_means_nothing_was_left_out():
    page = '<div>' + ''.join('<p>' + 'x' * 400 + '</p>' for _ in range(200)) + '</div>'
    assert len(page) > BUDGET
    html, left_out = fit_html(page)
    assert left_out == 0 and len(html) <= BUDGET and not html.startswith(NOTE)


def test_html_that_cannot_be_fitted_is_cut_as_before():
    page = '<div>' + '<button>Apply coupon</button>' * 5000 + '</div>'
    html, left_out = fit_html(page, {'apply', 'coupon'}, 'button')
    assert len(html) == BUDGET and left_out > 0


def test_the_inventory_keeps_whole_rows_and_related_ones_first():
    rows = [{'tag': 'a', 'role': 'link', 'text': 'Article link number %d' % i, 'id': 'x' * 250}
            for i in range(400)]
    rows.append({'tag': 'button', 'role': 'button', 'text': 'Apply coupon', 'id': 'go'})
    fitted = fit_inventory(rows, {'apply', 'coupon'}, 'button')
    text = json.dumps(fitted)
    assert len(text) <= BUDGET
    assert fitted[-1]['text'] == 'Apply coupon'
    assert all(row['id'] == 'go' or row['id'].endswith('...') for row in fitted)
    assert fit_inventory(rows[:3]) == rows[:3]


def test_has_position_ignores_quoted_text():
    assert has_position("(//button[@type='submit'])[2]")
    assert has_position('//li[last()]') and has_position('//li[position() > 1]')
    assert not has_position("//a[@title='[2]']") and not has_position("//button[@id='go']")


def test_apply_refuses_a_position_when_the_page_was_fitted():
    store = _Store()
    proposal = {'xpath': "(//button[@type='submit'])[1]", 'fallbacks': []}
    applied, reason = apply(store, 'apply_coupon', proposal, LONG, META, positional=False)
    assert applied is None and reason == POSITIONAL
    applied, _ = apply(store, 'apply_coupon', proposal, LONG, META)
    assert applied == "(//button[@type='submit'])[1]"


class _Store:
    def __init__(self):
        self.added = []

    def candidates(self, key):
        return [{'xpath': "//button[@id='apply-coupon']"}]

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath, source))


class _Provider:
    """Proposes the given xpaths in turn and keeps every bundle it was given."""

    def __init__(self, *xpaths, full=False):
        self.xpaths = list(xpaths)
        self.bundles = []
        if full:
            self.wants_full_html = True

    def propose(self, bundle):
        self.bundles.append(bundle)
        return {'xpath': self.xpaths.pop(0), 'fallbacks': [], 'reasoning': ''}

    def report_outcome(self, *args):
        pass


def _coordinator(provider):
    return HealCoordinator(_Store(), provider=provider, html_provider=lambda: LONG,
                           inventory_provider=lambda: [], guard=lambda: True)


def test_the_coordinator_fits_the_page_and_refuses_a_position_in_it():
    provider = _Provider("(//button[@type='submit'])[1]", "//button[normalize-space()='Apply coupon']")
    coord = _coordinator(provider)
    applied = coord.try_heal('apply_coupon', META, coord.store.candidates('apply_coupon'), {})
    assert applied == "//button[normalize-space()='Apply coupon']"
    first, second = provider.bundles
    assert len(first['html']) <= BUDGET and first['html'].startswith(NOTE)
    assert second['rejectReason'] == POSITIONAL
    assert second['html'] == first['html']


def test_a_provider_that_reads_the_whole_page_gets_it_unchanged():
    provider = _Provider("(//button[@type='submit'])[1]", full=True)
    coord = _coordinator(provider)
    applied = coord.try_heal('apply_coupon', META, coord.store.candidates('apply_coupon'), {})
    assert applied == "(//button[@type='submit'])[1]"
    assert 'Apply coupon' in provider.bundles[0]['html']
    assert not provider.bundles[0]['html'].startswith(NOTE)
