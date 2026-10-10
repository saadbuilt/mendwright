"""Canonical prompt builder + proposal parser (the single source every provider shares)."""

from mendwright.prompt import build_prompt, parse_proposal


def test_build_prompt_includes_exactly_one_and_role_and_candidates():
    p = build_prompt({'key': 'terms', 'expectedRole': 'checkbox',
                      'candidates': ['//old1', '//old2'], 'html': '<form></form>'})
    assert 'EXACTLY ONE' in p['system']
    assert "role is 'checkbox'" in p['system']
    assert 'terms' in p['user'] and '//old1' in p['user'] and '//old2' in p['user']


def test_build_prompt_destructive_requires_exact_text():
    p = build_prompt({'key': 'pay', 'expectedRole': 'button', 'destructive': True,
                      'expectedText': ['Pay', 'Bezahlen']})
    assert 'DESTRUCTIVE' in p['system'] and 'Pay' in p['system'] and 'Bezahlen' in p['system']


def test_build_prompt_carries_reject_reason_for_reprompt():
    p = build_prompt({'key': 'k', 'expectedRole': 'button', 'candidates': [],
                      'rejectedXpath': '//bad', 'rejectReason': 'not unique (2)'})
    assert '//bad' in p['user'] and 'not unique (2)' in p['user'] and 'DIFFERENT' in p['user']


def test_parse_proposal_tolerates_surrounding_text_and_caps_fallbacks():
    out = parse_proposal('here you go:\n{"xpath":"//a","fallbacks":["//b","//c","//d","//e","//f"],'
                         '"reasoning":"because"}\nhope that helps')
    assert out['xpath'] == '//a'
    assert out['fallbacks'] == ['//b', '//c', '//d', '//e']  # capped at 4
    assert out['reasoning'] == 'because'


def test_parse_proposal_rejects_missing_xpath_or_no_json():
    assert parse_proposal('{"reasoning":"no xpath here"}') is None
    assert parse_proposal('not json at all') is None
    assert parse_proposal('') is None
