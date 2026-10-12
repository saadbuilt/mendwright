"""mendwright.candidates: the candidate list, the task text, the answer parser and the XPath
built for a pick. Offline, lxml only."""

import json

import lxml.html
import pytest

from mendwright.candidates import (MAX_CANDIDATES, SYSTEM_PROMPT, VISIBLE, WHY, answer_text,
                                   describe, extract, fingerprint_line, format_task,
                                   parse_answer, robust_xpath, robust_xpaths)
from mendwright.validation import _role_of, validate

PAGE = """<!doctype html><html><head><title>Shop</title></head><body>
<header><a href="/" aria-label="Home">Acme</a>
  <nav aria-label="Main"><a href="/pricing">Pricing</a><a href="/docs">Docs</a></nav>
  <button type="button" aria-label="Open menu"></button></header>
<main>
  <h1>Sign in</h1>
  <form id="login">
    <label for="em">Email address</label><input id="em" name="email" type="email">
    <label>Password <input type="password" name="pw"></label>
    <span id="lbl-r">Remember me</span><input type="checkbox" aria-labelledby="lbl-r">
    <button type="submit" class="btn btn-primary">Log in</button>
    <div role="button" tabindex="0">Use a passkey</div>
  </form>
  <table><tbody>
    <tr><td><span>Maya Okafor</span></td><td><button aria-label="Edit">E</button></td></tr>
    <tr><td><span>Leo Martins</span></td><td><button aria-label="Edit">E</button></td></tr>
  </tbody></table>
  <div style="display:none"><button type="button">Log in</button></div>
</main>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>"""


def bundle(**kw):
    out = {'key': 'login.submit', 'expectedRole': 'button', 'expectedText': [],
           'candidates': ["//button[@id='login-btn']"], 'html': PAGE, 'page': None,
           'lastPage': None}
    out.update(kw)
    return out


def test_extract_lists_every_element_of_the_role_in_document_order():
    cands = extract(bundle())
    assert [c.number for c in cands] == list(range(1, len(cands) + 1))
    assert all(_role_of(c.element) == 'button' for c in cands)
    texts = [c.features.get('aria') or c.features.get('text') for c in cands]
    assert texts == ['Open menu', 'Log in', 'Use a passkey', 'Edit', 'Edit', 'Log in']
    assert cands.total == len(cands) == 6


def test_extract_uses_the_gate_role_for_inputs_checkboxes_and_links():
    assert len(extract(bundle(expectedRole='input'))) == 2
    assert len(extract(bundle(expectedRole='checkbox'))) == 1
    assert len(extract(bundle(expectedRole='link'))) == 4
    assert extract(bundle(expectedRole='combobox')) == []


def test_features_name_the_label_the_row_the_region_and_hidden_copies():
    by_text = {}
    for cand in extract(bundle(expectedRole='input')) + extract(bundle(expectedRole='checkbox')):
        by_text[cand.features.get('label')] = cand.features
    assert by_text['Email address']['tag'] == 'input[email]'
    assert by_text['Email address']['region'] == 'main>form#login'
    assert 'Password' in by_text
    assert 'Remember me' in by_text
    buttons = extract(bundle())
    assert buttons[3].features['in'] == 'item "Maya Okafor"'
    assert buttons[4].features['in'] == 'item "Leo Martins"'
    assert buttons[5].features['hidden'] == 'hidden'
    assert buttons[0].features['region'] == 'header'
    assert buttons[2].features['tag'] == 'div[role=button]'
    assert describe(buttons[1].element)['cls'] == 'btn btn-primary'


def test_a_long_list_keeps_the_candidates_that_share_words_with_the_step():
    links = ''.join('<a href="/c/%d">Category %d</a>' % (i, i) for i in range(80))
    html = '<html><body><nav>' + links + '<a href="/pricing">Plans and pricing</a></nav></body></html>'
    cands = extract({'key': 'nav.pricing', 'expectedRole': 'link',
                     'candidates': ["//a[@id='pricing']"], 'html': html})
    assert len(cands) == MAX_CANDIDATES and cands.total == 81
    assert any(c.features.get('text') == 'Plans and pricing' for c in cands)
    positions = [int(c.element.getroottree().getpath(c.element).split('[')[-1].rstrip(']'))
                 for c in cands if c.element.get('href') != '/pricing']
    assert positions == sorted(positions), 'kept candidates stay in document order'


def test_format_task_shows_the_step_and_numbered_candidates():
    system, user = format_task(bundle(expectedText=['Log in'], page='/login', lastPage='/login'),
                               extract(bundle()))
    assert system == SYSTEM_PROMPT
    assert 'key: login.submit' in user
    assert 'role: button' in user
    assert "old selector: //button[@id='login-btn']" in user
    assert 'expected text: "Log in"' in user
    assert 'page: /login (where it was last found)' in user
    assert 'Candidates (6 buttons)' in user
    assert '\n2. button "Log in" class="btn btn-primary" | in "Sign in" | main>form#login\n' in user


def test_format_task_names_a_page_change_and_says_nothing_without_pages():
    _, moved = format_task(bundle(page='/signup/team', lastPage='/signup'), extract(bundle()))
    assert 'page: /signup/team (it was last found on /signup)' in moved
    _, blank = format_task(bundle(), extract(bundle()))
    assert 'page:' not in blank


def test_format_task_includes_the_saved_fingerprint():
    fp = {'v': 1, 'tag': 'button', 'attrs': {'id': 'login-btn', 'type': 'submit'},
          'classes': ['btn', 'btn-primary'], 'text': 'Sign in',
          'ancestors': [{'tag': 'form', 'id': 'login'}, {'tag': 'main'}]}
    _, user = format_task(bundle(fingerprint=fp), extract(bundle()))
    assert ('it was: button "Sign in" id="login-btn" class="btn btn-primary" | main>form#login'
            ' | within form#login < main') in user
    assert fingerprint_line({}) == ''
    assert fingerprint_line({'tag': 'input', 'attrs': {'type': 'email', 'name': 'email'},
                             'label': 'Email'}).startswith('input[email] label="Email" name="email"')


def test_parse_answer_accepts_a_pick_or_a_null_with_a_reason():
    assert parse_answer('{"pick": 3}') == {'pick': 3, 'why': None}
    assert parse_answer('Sure: {"pick": "4"} done') == {'pick': 4, 'why': None}
    assert parse_answer('{"pick": null, "why": "moved"}') == {'pick': None, 'why': 'moved'}
    assert parse_answer('{"pick": null}') == {'pick': None, 'why': 'removed'}
    for bad in ('', 'no idea', '{"xpath": "//a"}', '{"pick": 0}', '{"pick": -1}',
                '{"pick": true}', '{"pick": "two"}', '{"pick": 1.5}', '[1]', None):
        assert parse_answer(bad) is None, bad


def test_answer_text_is_what_parse_answer_reads():
    assert parse_answer(answer_text(7)) == {'pick': 7, 'why': None}
    for why in WHY:
        assert parse_answer(answer_text(None, why)) == {'pick': None, 'why': why}
    assert json.loads(answer_text(2)) == {'pick': 2}


def test_robust_xpaths_are_unique_pass_the_gate_and_never_bare_positions():
    cands = extract(bundle())
    doc = cands.doc
    for cand in cands[:5]:
        xpaths = robust_xpaths(cand.element, doc)
        assert xpaths, cand.line
        for xp in xpaths:
            hits = doc.xpath(xp)
            assert len(hits) == 1 and hits[0] is cand.element
            assert not xp.startswith('(') and not xp.startswith('/html')
        assert validate(xpaths[0], PAGE, expected_role='button').ok


def test_robust_xpath_prefers_stable_attributes_and_skips_generated_ids():
    doc = lxml.html.document_fromstring(
        '<html><body><input id="email" name="email"><input id=":r3:" name="phone">'
        '<button id="ember123" data-testid="save-btn">Save</button></body></html>')
    email, phone = doc.xpath('//input')
    button = doc.xpath('//button')[0]
    assert robust_xpath(email, doc) == "//input[@id='email']"
    assert robust_xpath(phone, doc) == "//input[@name='phone']"
    assert robust_xpath(button, doc) == "//button[@data-testid='save-btn']"


def test_a_row_action_is_anchored_on_its_row_and_a_hidden_copy_is_left_out():
    cands = extract(bundle())
    leo = cands[4].element
    xp = robust_xpath(leo, cands.doc)
    assert 'Leo Martins' in xp
    doc = lxml.html.document_fromstring(
        '<html><body><nav><a href="/p">Pricing</a></nav>'
        '<div hidden><nav><a href="/p">Pricing</a></nav></div></body></html>')
    visible = doc.xpath('//a')[0]
    xp = robust_xpath(visible, doc)
    assert xp is not None and VISIBLE in xp
    assert doc.xpath(xp) == [visible]


def test_identical_twins_get_no_xpath():
    doc = lxml.html.document_fromstring(
        '<html><body><button>Copy</button><button>Copy</button></body></html>')
    assert robust_xpaths(doc.xpath('//button')[0], doc) == []


def test_redacted_values_are_never_used_in_an_xpath():
    doc = lxml.html.document_fromstring(
        '<html><body><a href="[URL]" title="[redacted]">Docs</a><a href="/x">Help</a>'
        '</body></html>')
    xp = robust_xpath(doc.xpath('//a')[0], doc)
    assert xp is not None and '[URL]' not in xp and '[redacted' not in xp


@pytest.mark.parametrize('html', ['', '<html></html>', 'not html at all'])
def test_an_empty_or_odd_page_gives_no_candidates(html):
    assert extract(bundle(html=html)) == []
