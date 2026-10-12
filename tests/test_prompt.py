"""Canonical prompt builder + proposal parser (the single source every provider shares)."""

from mendwright.prompt import build_prompt, not_here_reason, parse_proposal


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


def test_the_model_is_told_it_may_say_no_element_fits():
    system = build_prompt({'key': 'continue', 'expectedRole': 'button'})['system']
    assert ('If no element on this page is the one the step means, for example because the '
            'step moved to another page or the element was removed, reply '
            '{"xpath":null,"reason":"..."} instead. Do not pick an element that is only '
            'similar.') in system


def test_the_prompt_names_the_page_and_where_the_key_was_last_found():
    user = build_prompt({'key': 'continue', 'expectedRole': 'button', 'page': '/signup/details',
                         'lastPage': '/signup'})['user']
    assert '\npage: /signup/details\nlast found on page: /signup\n' in user
    plain = build_prompt({'key': 'continue', 'expectedRole': 'button'})['user']
    assert 'page:' not in plain.split('DOM inventory')[0]


def test_a_no_element_reply():
    reply = 'Here: {"xpath": null, "reason": "The Continue button moved to another page."}'
    assert parse_proposal(reply) is None
    assert not_here_reason(reply) == 'The Continue button moved to another page.'
    assert not_here_reason('{"reason": "  gone  "}') == 'gone'
    assert not_here_reason('{"xpath": "", "reason": "gone"}') == 'gone'
    assert not_here_reason('{"xpath": "//a", "reason": "x"}') is None
    assert not_here_reason('{"xpath": null}') is None
    assert not_here_reason('{"xpath": null, "reasoning": "old field"}') is None
    assert not_here_reason('{"xpath": null, "reason": 5}') is None
    assert not_here_reason('no json') is None
    assert not_here_reason('') is None and not_here_reason(None) is None


def test_the_prompt_carries_the_intent_on_one_line():
    user = build_prompt({'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
                         'intent': 'the terms checkbox in the signup form'})['user']
    assert ('expectedText: []\nintent: the terms checkbox in the signup form\ndestructive: false'
            in user)
    for empty in (None, ''):
        plain = build_prompt({'key': 'terms', 'expectedRole': 'checkbox', 'intent': empty})
        assert 'intent:' not in plain['user']
